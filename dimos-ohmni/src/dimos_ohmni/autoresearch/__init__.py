"""Autoresearch — robot-shaped adaptation of karpathy/autoresearch.

Karpathy's pattern (`program.md`):
    LOOP FOREVER:
        1. propose an experimental change to train.py
        2. run experiment with fixed time budget
        3. read out metric (val_bpb)
        4. decide: keep | discard | crash
        5. log to results.tsv
        6. advance branch (keep) or git-reset (discard)

For a robot, each "microloop" targets one *behavior knob* instead of one
file. The loop swaps in a candidate value, runs the robot for a fixed
window, computes a score, and either commits the value or rolls back.
The journal at ~/.ohmni/research/journal.tsv is the cross-loop ledger.

Microloops are deliberately bounded — small, falsifiable, ~30s budget
each — because the robot can't run thousands of experiments per night
the way an LLM-training rig can. The meta loop schedules which
microloop to invest cycles in based on staleness + last-score-gain.

Public surface:
    Loop          — abstract base class (subclass per microloop)
    LoopResult    — dataclass returned by Loop.run()
    Journal       — append-only TSV at ~/.ohmni/research/journal.tsv
    AutoResearchOrchestrator — Module that runs microloops on schedule
"""

from .loop_base import Loop, LoopResult, LoopStatus
from .journal import Journal, JournalEntry
from .orchestrator import AutoResearchOrchestrator, autoresearch_orchestrator

__all__ = [
    "Loop", "LoopResult", "LoopStatus", "Journal", "JournalEntry",
    "AutoResearchOrchestrator", "autoresearch_orchestrator",
]
