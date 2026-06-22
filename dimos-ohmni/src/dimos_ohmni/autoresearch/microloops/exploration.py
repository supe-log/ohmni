"""ExplorationTuningLoop — measure how productively we're exploring.

Knobs:
    PROPOSE_INTERVAL_S    how often the brain proposes a goal
    REVISIT_BIAS          probability of choosing revisit over outward
    OUTWARD_RADIUS_FRAC   fraction of geofence used as outward target

Score:
    cells_visited_per_minute = (new cells in window) / (window minutes)
    Read directly from `brain.md` `[explore]` entries — the loop
    counts unique grid cells visited in the most recent N minutes.

This loop tunes the brain's *behavior*, not the planner. Knobs are
written to `~/.ohmni/exploration.json`, which BrainResearcher reads on
each tick (lightweight reload — no restart needed).
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any

from dimos.utils.logging_config import setup_logger

from ..loop_base import Loop

logger = setup_logger()

CONFIG_PATH = Path.home() / ".ohmni" / "exploration.json"
BRAIN_PATH = Path.home() / ".ohmni" / "brain.md"

DEFAULTS = {
    "propose_interval_s": 60.0,
    "revisit_bias": 0.5,
    "outward_radius_frac": 0.8,
}

KNOBS = {
    "propose_interval_s": (20.0, 180.0),
    "revisit_bias": (0.1, 0.9),
    "outward_radius_frac": (0.3, 1.0),
}


def _read_config() -> dict[str, float]:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        d = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    out.update({k: float(v) for k, v in d.items() if k in DEFAULTS})
    return out


def _write_config(d: dict[str, float]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(d, indent=2))


_GOTO_RE = re.compile(r"\[explore\]\s*goto\s*\(([-\d.]+),\s*([-\d.]+)\)")


def _count_unique_cells(window_s: float, cell_m: float = 0.5) -> int:
    if not BRAIN_PATH.exists():
        return 0
    try:
        lines = BRAIN_PATH.read_text().splitlines()
    except OSError:
        return 0
    cutoff = time.time() - window_s
    cells: set[tuple[int, int]] = set()
    for line in lines[-2000:]:
        m_ts = re.match(r"-\s+(\S+)\s+", line)
        if not m_ts:
            continue
        try:
            t = time.mktime(time.strptime(m_ts.group(1), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if t < cutoff:
            continue
        m = _GOTO_RE.search(line)
        if not m:
            continue
        x, y = float(m.group(1)), float(m.group(2))
        cells.add((int(round(x / cell_m)), int(round(y / cell_m))))
    return len(cells)


class ExplorationTuningLoop(Loop):
    name = "exploration_tuning"
    budget_s = 90.0

    def propose(self) -> dict[str, Any]:
        cfg = _read_config()
        # Pick a knob to perturb
        knob_name = random.choice(list(KNOBS.keys()))
        lo, hi = KNOBS[knob_name]
        current = cfg[knob_name]
        new_value = current * random.uniform(0.7, 1.3)
        new_value = max(lo, min(hi, new_value))
        return {
            "knob": f"{knob_name}={new_value:.3f}",
            "knob_name": knob_name,
            "value": new_value,
            "prev": current,
            "notes": f"prev={current:.3f}",
        }

    def apply(self, proposal: dict[str, Any]) -> dict[str, float]:
        prev = _read_config()
        new = dict(prev)
        new[proposal["knob_name"]] = proposal["value"]
        _write_config(new)
        return prev

    def rollback(self, proposal: dict[str, Any], rollback_info: dict[str, float]) -> None:
        _write_config(rollback_info)

    def run(self, proposal: dict[str, Any], budget_s: float) -> dict[str, Any]:
        before = _count_unique_cells(window_s=600.0)
        time.sleep(budget_s)
        after = _count_unique_cells(window_s=600.0)
        return {
            "before": before,
            "after": after,
            "new_cells": max(after - before, 0),
            "minutes": budget_s / 60.0,
        }

    def score(self, observations: dict[str, Any]) -> float:
        return observations["new_cells"] / max(observations["minutes"], 1e-6)
