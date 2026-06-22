"""AutoResearchOrchestrator — meta-loop that schedules microloops.

Strategy:
- All microloops are added to a registry with an initial weight.
- On each tick, pick a loop weighted by `weight × (1 + last_delta)`,
  so loops that recently produced positive deltas get more cycles, and
  loops that have been quiet still occasionally get a turn.
- After each microloop's tick, update its weight using a small EMA on
  the score delta. Successful loops gradually accrete budget;
  stagnant ones decay toward a floor.

This module runs as a dimos `Module` so it integrates into ohmni_full.
The orchestrator owns its own thread; microloops sleep within their
budgets and don't compete for the dimos worker pool.

Pause via env: set OHMNI_AUTORESEARCH=0 before starting the stack
(or remove the orchestrator from the blueprint) to skip entirely.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field

from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.utils.logging_config import setup_logger

from .journal import Journal
from .loop_base import Loop
from .microloops import (
    CalibrationLoop,
    ExplorationTuningLoop,
    GitHubResearchLoop,
    SkillProbeLoop,
    WebResearchLoop,
)

logger = setup_logger()


@dataclass
class AutoResearchConfig(ModuleConfig):
    """Knobs for the orchestrator itself."""

    enabled: bool = True
    initial_idle_s: float = 60.0  # wait this long before the first tick
    inter_tick_s: float = 5.0     # min sleep between ticks
    min_weight: float = 0.1
    max_weight: float = 5.0
    ema_alpha: float = 0.3


class AutoResearchOrchestrator(Module):
    """Schedules microloops and accumulates their journal."""

    default_config = AutoResearchConfig
    config: AutoResearchConfig

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self._journal = Journal()
        self._loops: dict[str, Loop] = {}
        self._weights: dict[str, float] = {}
        self._last_score: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._running = False
        self._register_default_loops()

    def _register_default_loops(self) -> None:
        for cls in (
            CalibrationLoop,
            SkillProbeLoop,
            WebResearchLoop,
            GitHubResearchLoop,
            ExplorationTuningLoop,
        ):
            inst = cls(journal=self._journal)
            self._loops[inst.name] = inst
            self._weights[inst.name] = 1.0
            self._last_score[inst.name] = 0.0

    @rpc
    def start(self) -> None:
        super().start()
        if not self.config.enabled or os.environ.get("OHMNI_AUTORESEARCH", "1") == "0":
            logger.info("autoresearch disabled via config or OHMNI_AUTORESEARCH=0")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._meta_loop, daemon=True, name="autoresearch-meta",
        )
        self._thread.start()
        logger.info(
            "autoresearch live. Loops: %s. Journal: %s",
            list(self._loops), self._journal.path,
        )

    @rpc
    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        super().stop()

    # -- core meta loop --

    def _meta_loop(self) -> None:
        time.sleep(self.config.initial_idle_s)
        while self._running:
            try:
                name = self._pick_loop()
                loop = self._loops[name]
                logger.info("autoresearch tick: %s (weight=%.2f)", name, self._weights[name])
                t0 = time.monotonic()
                # Some loops use _t0 as run-budget reference
                setattr(loop, "_t0", t0)
                result = loop.tick()
                self._update_weight(name, result.delta)
                self._last_score[name] = result.score
                logger.info(
                    "autoresearch %s -> %s score=%.3f delta=%+.3f", name, result.status,
                    result.score, result.delta,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("autoresearch tick failed: %s", e)
            time.sleep(self.config.inter_tick_s)

    def _pick_loop(self) -> str:
        # Weighted random — bias toward higher-weight loops
        names = list(self._weights.keys())
        weights = [max(self.config.min_weight, self._weights[n]) for n in names]
        total = sum(weights)
        r = random.uniform(0, total)
        acc = 0.0
        for n, w in zip(names, weights):
            acc += w
            if r <= acc:
                return n
        return names[-1]

    def _update_weight(self, name: str, delta: float) -> None:
        cfg = self.config
        # EMA update: positive delta increases weight, negative decays
        signal = max(-1.0, min(1.0, delta))
        new_weight = (1 - cfg.ema_alpha) * self._weights[name] + cfg.ema_alpha * (1.0 + signal)
        self._weights[name] = max(cfg.min_weight, min(cfg.max_weight, new_weight))

    # -- skills exposed to agents --

    @rpc
    @skill
    def autoresearch_status(self) -> dict:
        """Return the current loop weights and last scores. Use this to
        peek at what the autonomous-research scheduler has been working
        on and which microloops are paying off most."""
        return {
            "loops": list(self._loops.keys()),
            "weights": dict(self._weights),
            "last_scores": dict(self._last_score),
        }

    @rpc
    @skill
    def autoresearch_recent(self, n: int = 20) -> list[dict]:
        """Return the last `n` autoresearch journal entries, newest last.
        Each entry: {ts, loop, knob, score, delta, status, notes}."""
        out: list[dict] = []
        for e in self._journal.recent(n=n):
            out.append({
                "ts": e.ts,
                "loop": e.loop,
                "knob": e.knob,
                "score": e.score,
                "delta": e.delta,
                "status": e.status,
                "notes": e.notes,
            })
        return out

    @rpc
    @skill
    def autoresearch_run_now(self, loop_name: str) -> dict:
        """Force one tick of the named microloop and return the result."""
        if loop_name not in self._loops:
            return {"error": f"unknown loop {loop_name}", "available": list(self._loops)}
        result = self._loops[loop_name].tick()
        return {
            "status": result.status,
            "score": result.score,
            "delta": result.delta,
            "knob": result.knob,
            "notes": result.notes,
        }


autoresearch_orchestrator = AutoResearchOrchestrator.blueprint
