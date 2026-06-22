"""CalibrationLoop — tune wheel-encoder & safety constants by outcome.

Knobs:
    OHMNI_APOS_PER_M    encoder counts per metre of wheel travel
    OHMNI_WHEELBASE_M   distance between wheels
    SAFETY_IMMINENT_M   imminent-collision distance (read by SafetyGovernor)

Score:
    goal_reach_rate = goals_reached / goals_published over the window.
    Higher is better. If no goals are published, fall back to the
    drive-cmd-throughput as a weak proxy.

The loop reads `/tmp/ohmni_full.log` for ground-truth metrics. Knob
values are written to `~/.ohmni/calibration.env`, which the user
sources before launching `run_ohmni_full.py`. (The running coordinator
doesn't pick up env changes without a restart — so this loop builds a
candidate config that can be reviewed and applied between runs.)

This is intentionally one step short of self-modifying: the loop
proposes new constants, scores them against the *most recent* run's
metrics, and writes the winner to the env file. The human (or the
meta-loop) decides when to bounce the running stack to apply.
"""

from __future__ import annotations

import os
import random
import re
import time
from pathlib import Path
from typing import Any

from dimos.utils.logging_config import setup_logger

from ..loop_base import Loop

logger = setup_logger()

CALIB_ENV = Path.home() / ".ohmni" / "calibration.env"
LOG_PATH = Path("/tmp/ohmni_full.log")


def _read_calib() -> dict[str, str]:
    if not CALIB_ENV.exists():
        return {}
    out: dict[str, str] = {}
    for line in CALIB_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _write_calib(d: dict[str, str]) -> None:
    CALIB_ENV.parent.mkdir(parents=True, exist_ok=True)
    body = "# autoresearch-managed calibration; sourced before run_ohmni_full.py\n"
    body += "\n".join(f"export {k}={v}" for k, v in sorted(d.items())) + "\n"
    CALIB_ENV.write_text(body)


def _read_log_metric(pattern: str) -> int:
    if not LOG_PATH.exists():
        return 0
    try:
        text = LOG_PATH.read_text()
    except OSError:
        return 0
    return len(re.findall(pattern, text))


class CalibrationLoop(Loop):
    name = "calibration"
    budget_s = 30.0  # observe ~30s of run

    KNOBS = {
        "OHMNI_APOS_PER_M": (8000.0, 14000.0, 10860.0),
        "OHMNI_WHEELBASE_M": (0.20, 0.40, 0.30),
        "SAFETY_IMMINENT_M": (0.20, 0.50, 0.30),
    }

    def propose(self) -> dict[str, Any]:
        # Pick the knob with the most stale "last try" — round-robin-ish
        recent = self.journal.recent(n=20, loop=self.name)
        recent_knobs = {e.knob.split("=")[0] for e in recent[-3:]}
        candidates = [k for k in self.KNOBS if k not in recent_knobs]
        if not candidates:
            candidates = list(self.KNOBS.keys())
        knob_name = random.choice(candidates)
        lo, hi, default = self.KNOBS[knob_name]
        # Jitter ±15% from the current best, or default
        current = float(_read_calib().get(knob_name, default))
        new_value = current * random.uniform(0.85, 1.15)
        new_value = max(lo, min(hi, new_value))
        return {
            "knob": f"{knob_name}={new_value:.3f}",
            "knob_name": knob_name,
            "value": new_value,
            "notes": f"jittered from {current:.3f}",
        }

    def apply(self, proposal: dict[str, Any]) -> dict[str, str]:
        prev = _read_calib()
        new = dict(prev)
        new[proposal["knob_name"]] = f"{proposal['value']:.3f}"
        _write_calib(new)
        return prev

    def rollback(self, proposal: dict[str, Any], rollback_info: dict[str, str]) -> None:
        _write_calib(rollback_info)

    def run(self, proposal: dict[str, Any], budget_s: float) -> dict[str, Any]:
        before_published = _read_log_metric(r"Published frontier goal")
        before_reached = _read_log_metric(r"Goal reached")
        before_drives = len(re.findall(
            r"drive cmd #\d+: linear=(?!0mm)",
            LOG_PATH.read_text() if LOG_PATH.exists() else "",
        ))
        time.sleep(budget_s)
        after_published = _read_log_metric(r"Published frontier goal")
        after_reached = _read_log_metric(r"Goal reached")
        after_drives = len(re.findall(
            r"drive cmd #\d+: linear=(?!0mm)",
            LOG_PATH.read_text() if LOG_PATH.exists() else "",
        ))
        return {
            "published": after_published - before_published,
            "reached": after_reached - before_reached,
            "drives": after_drives - before_drives,
            "budget_s": budget_s,
        }

    def score(self, observations: dict[str, Any]) -> float:
        published = max(observations["published"], 1)
        reached = observations["reached"]
        drives = observations["drives"]
        # Primary: reach rate, weighted heavily.
        reach_rate = reached / published
        # Secondary: drive throughput (per second), small bonus.
        throughput = drives / max(observations["budget_s"], 1.0)
        return reach_rate + 0.01 * throughput
