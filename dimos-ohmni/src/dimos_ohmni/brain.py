"""BrainResearcher — self-improvement loop with append-only brain log.

Inspired by karpathy/autoresearch and its platform forks. The robot has a
"brain" file (~/.ohmni/brain.md) that records what it tried, what
happened, and what it learned. A loop:

    1. Read the brain (recent entries + headlines).
    2. Propose a small experiment based on the world state and the brain.
    3. Execute via the agent / planner. (For now: pick a frontier goal
       biased toward unexplored regions, or revisit a tagged location
       to compare what's changed.)
    4. Score: did the action complete? did anything change?
    5. Append a brain entry with timestamp, intent, outcome.

This is intentionally lightweight at start: no LLM required. It tracks
spatial coverage, periodicity, and battery-aware idle hours, and writes
structured markdown the LLM (Phase 2) can later read. When LLM extras
are installed, the proposal step gets richer.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from reactivex.disposable import Disposable

from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs import PoseStamped, Vector3
from dimos.msgs.sensor_msgs import PointCloud2
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

DEFAULT_BRAIN_DIR = Path.home() / ".ohmni"


@dataclass
class BrainConfig(ModuleConfig):
    """Adjustable parameters for the researcher loop."""

    brain_dir: Path = field(default_factory=lambda: DEFAULT_BRAIN_DIR)
    brain_file: str = "brain.md"
    world_file: str = "world.json"
    skills_file: str = "skills.md"

    propose_interval_s: float = 60.0  # how often to propose
    idle_window_s: float = 300.0      # only propose if idle this long
    min_battery_pct: float = 25.0     # don't propose motion below this
    dock_battery_pct: float = 20.0    # below this, route to dock

    grid_cell_m: float = 0.5          # spatial coverage tracker resolution
    coverage_radius_m: float = 8.0    # within-radius cells we track

    revisit_interval_s: float = 86400.0  # 24h since-last-visit triggers revisit


class BrainResearcher(Module):
    """A reflective loop that proposes goals and logs outcomes."""

    default_config = BrainConfig
    config: BrainConfig

    odom: In[PoseStamped]
    pointcloud: In[PointCloud2]
    battery: In[dict]

    goal_request: Out[PoseStamped]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.config.brain_dir.mkdir(parents=True, exist_ok=True)

        self._latest_pose: PoseStamped | None = None
        self._latest_battery: dict = {"level": 100.0, "charging": False}
        self._battery_seen: bool = False
        self._last_motion_ts: float = time.monotonic()
        self._last_pose_xy: tuple[float, float] | None = None
        self._coverage: dict[tuple[int, int], float] = {}  # cell → last visited ts
        self._lock = threading.Lock()
        self._loop_thread: threading.Thread | None = None
        self._running = False

    @rpc
    def start(self) -> None:
        super().start()
        self._disposables.add(Disposable(self.odom.subscribe(self._on_odom)))
        self._disposables.add(
            Disposable(self.pointcloud.subscribe(self._on_pointcloud))
        )
        self._disposables.add(Disposable(self.battery.subscribe(self._on_battery)))

        self._running = True
        self._loop_thread = threading.Thread(
            target=self._brain_loop, daemon=True, name="brain-loop"
        )
        self._loop_thread.start()

        self._append_entry(
            kind="boot",
            intent="brain wake",
            outcome="ready",
            extra={"propose_interval_s": self.config.propose_interval_s},
        )
        logger.info(
            "BrainResearcher up. Brain at %s",
            self.config.brain_dir / self.config.brain_file,
        )

    @rpc
    def stop(self) -> None:
        self._running = False
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=2.0)
        self._append_entry(kind="boot", intent="brain sleep", outcome="ok")
        super().stop()

    # -- input handlers --

    def _on_odom(self, pose: PoseStamped) -> None:
        with self._lock:
            self._latest_pose = pose
            xy = (pose.position.x, pose.position.y)
            cell = self._cell(xy)
            self._coverage[cell] = time.time()
            if self._last_pose_xy is None:
                self._last_pose_xy = xy
            else:
                d = math.hypot(
                    xy[0] - self._last_pose_xy[0],
                    xy[1] - self._last_pose_xy[1],
                )
                if d > 0.05:
                    self._last_motion_ts = time.monotonic()
                self._last_pose_xy = xy

    def _on_pointcloud(self, pc: PointCloud2) -> None:
        # Could later log obstacle counts / clutter density into world.json.
        pass

    def _on_battery(self, status: dict) -> None:
        if not status:
            return
        # Only mark as 'seen' once a non-zero level lands, so we don't
        # trip dock on the OhmniBatteryStatus dataclass default (level=0).
        level = float(status.get("level", 0.0))
        self._latest_battery = status
        if level > 0.0:
            self._battery_seen = True

    # -- core loop --

    def _brain_loop(self) -> None:
        # Stagger initial proposal so other modules finish booting.
        time.sleep(15.0)
        while self._running:
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                logger.warning("brain loop error: %s", e)
            time.sleep(self.config.propose_interval_s)

    def _read_overrides(self) -> dict[str, float]:
        """Pick up live overrides from ~/.ohmni/exploration.json (written
        by ExplorationTuningLoop). Missing file = defaults."""
        path = self.config.brain_dir.parent / "exploration.json"
        # Brain dir is itself ~/.ohmni already, so parent is ~/, not what
        # we want. Use the brain dir directly.
        path = self.config.brain_dir / "exploration.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _tick(self) -> None:
        cfg = self.config
        # Live-tunable overrides from autoresearch
        overrides = self._read_overrides()
        propose_interval_s = float(overrides.get("propose_interval_s", cfg.propose_interval_s))
        revisit_bias = float(overrides.get("revisit_bias", 0.5))
        outward_radius_frac = float(overrides.get("outward_radius_frac", 0.8))
        # Stash for use in _propose
        self._effective_revisit_bias = revisit_bias
        self._effective_outward_radius_frac = outward_radius_frac

        battery = float(self._latest_battery.get("level", 0.0))
        charging = bool(self._latest_battery.get("charging", False))

        # Don't make any battery-gated decisions until a real telemetry
        # reading lands. Telemetry's default OhmniBatteryStatus.level is
        # 0.0, which would falsely trigger dock-on-low-battery before the
        # first poll completes.
        if not self._battery_seen:
            return

        if charging:
            return  # already on the dock; nothing to do

        # Battery low → route to dock instead of exploring
        if battery < cfg.dock_battery_pct:
            self._invoke_dock(reason=f"battery={battery:.1f}%")
            return

        if battery < cfg.min_battery_pct:
            return  # too low to safely explore, but not yet docking; wait
        if self._latest_pose is None:
            return

        proposal = self._propose()
        if proposal is None:
            return
        kind, target, reason = proposal

        # Publish goal
        gx, gy = target
        ts = time.time()
        goal = PoseStamped(
            position=Vector3(float(gx), float(gy), 0.0),
            frame_id="world",
            ts=ts,
        )
        self.goal_request.publish(goal)
        self._append_entry(
            kind=kind,
            intent=f"goto ({gx:.2f}, {gy:.2f})",
            outcome="published",
            extra={"reason": reason, "battery": battery},
        )

    def _propose(self) -> tuple[str, tuple[float, float], str] | None:
        """Pick the next experiment.

        Strategy v0 (no-LLM):
        - If any tracked cell hasn't been visited in revisit_interval_s,
          revisit one of those.
        - Else, pick a randomized point at the edge of the coverage
          radius to push exploration outward.

        Strategy v1 (LLM-assisted, when extras installed): same fallback,
        but propose a freeform "what should I look at?" through the agent
        and use its goal suggestion when available.
        """
        if self._latest_pose is None:
            return None
        import random
        now = time.time()
        # Revisit candidates
        stale: list[tuple[tuple[int, int], float]] = [
            (cell, ts)
            for cell, ts in self._coverage.items()
            if now - ts > self.config.revisit_interval_s
        ]
        bias = float(getattr(self, "_effective_revisit_bias", 0.5))
        radius_frac = float(getattr(self, "_effective_outward_radius_frac", 0.8))
        # Pick revisit with probability `bias` if any are stale
        if stale and random.random() < bias:
            stale.sort(key=lambda x: x[1])
            cell, _ = stale[0]
            x, y = self._cell_center(cell)
            return ("revisit", (x, y), "stale-cell")

        # Outward exploration
        ang = random.uniform(0, 2 * math.pi)
        r = self.config.coverage_radius_m * radius_frac
        x = self._latest_pose.position.x + r * math.cos(ang)
        y = self._latest_pose.position.y + r * math.sin(ang)
        return ("explore", (x, y), "outward-edge")

    # -- helpers --

    def _cell(self, xy: tuple[float, float]) -> tuple[int, int]:
        s = self.config.grid_cell_m
        return (int(round(xy[0] / s)), int(round(xy[1] / s)))

    def _cell_center(self, cell: tuple[int, int]) -> tuple[float, float]:
        s = self.config.grid_cell_m
        return (cell[0] * s, cell[1] * s)

    def _invoke_dock(self, reason: str) -> None:
        """Trigger the autodock routine through the bus' RPC system.

        We don't hard-link to OhmniConnection here to keep this module
        decoupled. The dock RPC is reachable via the connection's
        `dock` skill — agents/skills will see it through the standard
        skill list.
        """
        self._append_entry(
            kind="dock",
            intent="autodock invoked",
            outcome="requested",
            extra={"reason": reason},
        )
        # Best-effort: try to call dock via on-system rpc registry.
        # If unavailable, the appended brain entry still records the intent.
        try:
            dock = getattr(self, "dock", None)
            if callable(dock):
                dock()
        except Exception as e:  # noqa: BLE001
            logger.warning("brain dock invoke failed: %s", e)

    def _append_entry(
        self,
        kind: str,
        intent: str,
        outcome: str,
        extra: dict | None = None,
    ) -> None:
        path = self.config.brain_dir / self.config.brain_file
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"- {ts} [{kind}] {intent} -> {outcome}"
        if extra:
            line += f"  ({json.dumps(extra, separators=(',', ':'))})"
        try:
            with path.open("a") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.warning("brain write failed: %s", e)

    # -- skills --

    @rpc
    @skill
    def remember(self, note: str) -> str:
        """Append a freeform note to the robot's brain log.

        Use this whenever the user shares context worth keeping for later
        sessions ("the kitchen is around the corner past the dock"). The
        note is timestamped and append-only.
        """
        self._append_entry(kind="note", intent=note, outcome="recorded")
        return "noted"

    @rpc
    @skill
    def recall(self, n: int = 20) -> list[str]:
        """Return the last `n` brain entries as plain strings.

        Use this to remind yourself what you tried recently before
        proposing a new experiment. Returns most-recent-last.
        """
        path = self.config.brain_dir / self.config.brain_file
        if not path.exists():
            return []
        try:
            with path.open("r") as f:
                lines = f.readlines()
        except OSError:
            return []
        return [ln.rstrip("\n") for ln in lines[-int(n):]]

    @rpc
    @skill
    def coverage_summary(self) -> dict:
        """Report spatial-coverage stats for the current session.

        Returns: number of distinct cells visited, oldest cell age, and
        the most recent unvisited-direction estimate.
        """
        with self._lock:
            cells = len(self._coverage)
            now = time.time()
            ages = [now - ts for ts in self._coverage.values()]
        return {
            "cells_visited": cells,
            "oldest_cell_age_s": max(ages) if ages else 0,
            "youngest_cell_age_s": min(ages) if ages else 0,
        }


brain_researcher = BrainResearcher.blueprint
