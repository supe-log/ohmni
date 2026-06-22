"""Loop primitive — the karpathy-shaped propose / run / score / decide / log unit.

Each microloop subclasses `Loop` and implements four methods:

    propose() -> dict        # which knob & value to try this cycle
    apply(proposal) -> None  # mutate the system to use the candidate
    run(budget_s) -> dict    # collect raw observations during the window
    score(observations) -> float  # higher = better

`Loop.tick()` orchestrates the full propose → apply → run → score → decide
→ log → maybe-rollback cycle. Loops are stateless across ticks except
through the journal — which is the explicit memory shared between loops
and across reboots.

The pattern follows karpathy/autoresearch's `program.md`: keep the
implementation deliberately small. Each microloop is one file. The
runtime budget is configurable per-loop but defaults to 30s — we're
not training networks, we're tuning a robot, so the experiments are
much shorter than 5 minutes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from dimos.utils.logging_config import setup_logger

from .journal import Journal, JournalEntry

logger = setup_logger()


class LoopStatus:
    KEEP = "keep"
    DISCARD = "discard"
    CRASH = "crash"


@dataclass
class LoopResult:
    status: str
    score: float
    delta: float
    knob: str
    notes: str = ""

    def to_entry(self, loop: str) -> JournalEntry:
        return JournalEntry(
            loop=loop,
            knob=self.knob,
            score=self.score,
            delta=self.delta,
            status=self.status,
            notes=self.notes,
        )


class Loop(ABC):
    """Abstract microloop. Subclass and implement the four hooks."""

    name: str = "base"
    budget_s: float = 30.0

    def __init__(self, journal: Journal | None = None) -> None:
        self.journal = journal or Journal()

    # -- subclass hooks --

    @abstractmethod
    def propose(self) -> dict[str, Any]:
        """Return a candidate change. Must include a 'knob' string."""
        ...

    @abstractmethod
    def apply(self, proposal: dict[str, Any]) -> Any:
        """Mutate the system to use the candidate. Return rollback info."""
        ...

    @abstractmethod
    def run(self, proposal: dict[str, Any], budget_s: float) -> dict[str, Any]:
        """Collect observations during the window. Return a dict."""
        ...

    @abstractmethod
    def score(self, observations: dict[str, Any]) -> float:
        """Map observations to a single float (higher is better)."""
        ...

    def rollback(self, proposal: dict[str, Any], rollback_info: Any) -> None:
        """Default: no-op. Override if `apply` writes external state."""
        pass

    # -- baseline tracking --

    def baseline_score(self) -> float:
        """Best 'keep' score for this loop, or 0.0 if no history."""
        best = self.journal.best(self.name)
        return best.score if best else 0.0

    # -- the core tick --

    def tick(self) -> LoopResult:
        """One full propose/apply/run/score/decide cycle."""
        try:
            proposal = self.propose()
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] propose failed: %s", self.name, e)
            return self._record_crash("propose", str(e))

        knob = str(proposal.get("knob", "?"))
        rollback_info: Any = None
        try:
            rollback_info = self.apply(proposal)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] apply failed: %s", self.name, e)
            return self._record_crash(knob, f"apply error: {e}")

        try:
            obs = self.run(proposal, self.budget_s)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] run failed: %s", self.name, e)
            try:
                self.rollback(proposal, rollback_info)
            except Exception:  # noqa: BLE001
                pass
            return self._record_crash(knob, f"run error: {e}")

        try:
            score = float(self.score(obs))
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] score failed: %s", self.name, e)
            try:
                self.rollback(proposal, rollback_info)
            except Exception:  # noqa: BLE001
                pass
            return self._record_crash(knob, f"score error: {e}")

        baseline = self.baseline_score()
        delta = score - baseline
        notes = str(proposal.get("notes", ""))[:200]

        if delta > 0:
            status = LoopStatus.KEEP
            logger.info(
                "[%s] KEEP knob=%s score=%.3f delta=%+.3f", self.name, knob, score, delta,
            )
        else:
            status = LoopStatus.DISCARD
            logger.info(
                "[%s] DISCARD knob=%s score=%.3f delta=%+.3f", self.name, knob, score, delta,
            )
            try:
                self.rollback(proposal, rollback_info)
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] rollback failed: %s", self.name, e)

        result = LoopResult(
            status=status, score=score, delta=delta, knob=knob, notes=notes,
        )
        self.journal.append(result.to_entry(self.name))
        return result

    def _record_crash(self, knob: str, notes: str) -> LoopResult:
        result = LoopResult(
            status=LoopStatus.CRASH, score=0.0, delta=0.0, knob=knob, notes=notes,
        )
        self.journal.append(result.to_entry(self.name))
        return result
