"""WebResearchLoop — periodically pull external knowledge into the brain.

Reads recent `brain.md` entries, picks one that suggests a research
question (heuristics: any line containing "?", "look up", "research",
"how to", or unfamiliar nouns), runs `WebResearcher.web_search` on it,
fetches the top result, summarizes the first ~2000 chars to a
single-line takeaway, and appends it back to brain.md as a [research]
entry.

Score:
    sources_added — how many distinct URLs the cycle injected into the
    brain. 0 on no work, capped at 3 per cycle.

Knob:
    The query is the knob — it's chosen each tick from brain context,
    so each cycle records *what* the loop researched.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from dimos.utils.logging_config import setup_logger

from ..loop_base import Loop

logger = setup_logger()

BRAIN_PATH = Path.home() / ".ohmni" / "brain.md"


_QUESTION_PATTERNS = [
    re.compile(r"\?\s*$"),
    re.compile(r"\b(how to|look up|research|investigate|why does|what is)\b", re.IGNORECASE),
]

_FALLBACK_TOPICS = [
    "RPLidar A2M8 motor PWM",
    "differential drive kinematics calibration",
    "frontier exploration heuristics indoor robot",
    "cp210x usb serial Linux Android driver",
    "voxel grid mapping outdoor vs indoor",
    "Ohmni telepresence robot SDK",
    "ROS-free SLAM minimal stack",
    "battery thresholds for autonomous robot dock-on-low",
]


def _pick_query() -> str:
    if not BRAIN_PATH.exists():
        return random.choice(_FALLBACK_TOPICS)
    try:
        lines = BRAIN_PATH.read_text().splitlines()[-200:]
    except OSError:
        return random.choice(_FALLBACK_TOPICS)
    candidates: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("- 20") and "[boot]" in s:
            continue
        if any(p.search(s) for p in _QUESTION_PATTERNS):
            candidates.append(s)
    if candidates:
        # Bias toward the most recent question
        return candidates[-1]
    return random.choice(_FALLBACK_TOPICS)


def _append_brain(line: str) -> None:
    BRAIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with BRAIN_PATH.open("a") as f:
        f.write(f"- {ts} [research] {line}\n")


class WebResearchLoop(Loop):
    name = "web_research"
    budget_s = 12.0

    def __init__(self, *args, max_results: int = 3, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_results = max_results

    def propose(self) -> dict[str, Any]:
        query = _pick_query()[:120]
        return {"knob": f"query={query[:60]}", "query": query, "notes": ""}

    def apply(self, proposal: dict[str, Any]) -> Any:
        return None  # purely additive; no rollback needed

    def run(self, proposal: dict[str, Any], budget_s: float) -> dict[str, Any]:
        from dimos_ohmni.web_research import WebResearcher
        wr = object.__new__(WebResearcher)  # bypass Module.__init__
        try:
            results = WebResearcher.web_search(wr, proposal["query"], max_results=self.max_results)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e), "sources": 0}

        sources_added = 0
        for r in results[:self.max_results]:
            url = r.get("url", "")
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            if not url:
                continue
            line = f'q="{proposal["query"][:60]}" -> {title[:80]} <{url}> :: {snippet[:160]}'
            _append_brain(line)
            sources_added += 1
            if time.monotonic() - getattr(self, "_t0", time.monotonic()) > budget_s:
                break
        return {"sources": sources_added, "results": len(results)}

    def score(self, observations: dict[str, Any]) -> float:
        return float(observations.get("sources", 0))
