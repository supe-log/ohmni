"""Append-only TSV journal for autoresearch experiments.

Mirrors karpathy/autoresearch's `results.tsv`. Each row is one
experiment, with status `keep`, `discard`, or `crash`.

Columns (tab-separated):
    ts          ISO-8601 UTC timestamp
    loop        loop name (e.g. "calibration", "skill_probe")
    knob        what was changed (e.g. "OHMNI_APOS_PER_M=12000")
    score       float score (higher = better — convert if needed)
    delta       score change vs baseline (positive = improvement)
    status      keep | discard | crash
    notes       short text description (NO TABS)

The journal is the *single* persistent learning artifact. Other state
(`brain.md`, `world.json`, `skills.md`) records *what happened*; the
journal records *what we tried and whether it worked*.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_JOURNAL_DIR = Path.home() / ".ohmni" / "research"


@dataclass
class JournalEntry:
    loop: str
    knob: str
    score: float
    delta: float
    status: str  # 'keep' | 'discard' | 'crash'
    notes: str = ""
    ts: float = field(default_factory=time.time)

    def to_tsv(self) -> str:
        # Strip tabs and newlines from any string field
        clean = lambda s: str(s).replace("\t", " ").replace("\n", " ")
        return "\t".join([
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.ts)),
            clean(self.loop),
            clean(self.knob),
            f"{self.score:.6f}",
            f"{self.delta:+.6f}",
            self.status,
            clean(self.notes),
        ])

    @classmethod
    def from_tsv(cls, line: str) -> "JournalEntry | None":
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 7:
            return None
        try:
            ts_struct = time.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ")
            ts = time.mktime(ts_struct)
        except ValueError:
            ts = time.time()
        try:
            score = float(parts[3])
            delta = float(parts[4])
        except ValueError:
            score, delta = 0.0, 0.0
        return cls(
            loop=parts[1],
            knob=parts[2],
            score=score,
            delta=delta,
            status=parts[5],
            notes=parts[6],
            ts=ts,
        )


_HEADER = "ts\tloop\tknob\tscore\tdelta\tstatus\tnotes"


class Journal:
    """Append-only journal of microloop experiments."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DEFAULT_JOURNAL_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "journal.tsv"
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w") as f:
                f.write(_HEADER + "\n")

    def append(self, entry: JournalEntry) -> None:
        with self._lock, self.path.open("a") as f:
            f.write(entry.to_tsv() + "\n")
            f.flush()

    def recent(self, n: int = 50, loop: str | None = None) -> list[JournalEntry]:
        with self._lock:
            try:
                lines = self.path.read_text().splitlines()
            except OSError:
                return []
        out: list[JournalEntry] = []
        for line in lines[-1:-(n * 5):-1]:  # walk backward, allow some headers
            if line == _HEADER or not line.strip():
                continue
            entry = JournalEntry.from_tsv(line)
            if entry is None:
                continue
            if loop and entry.loop != loop:
                continue
            out.append(entry)
            if len(out) >= n:
                break
        out.reverse()
        return out

    def best(self, loop: str, *, status: str = "keep") -> JournalEntry | None:
        """Highest-scoring kept entry for a given loop."""
        kept = [e for e in self.recent(n=200, loop=loop) if e.status == status]
        if not kept:
            return None
        return max(kept, key=lambda e: e.score)

    def last(self, loop: str) -> JournalEntry | None:
        items = self.recent(n=1, loop=loop)
        return items[-1] if items else None
