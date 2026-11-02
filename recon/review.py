"""Reviewer decisions: an append-only log per batch, with stale-decision detection.

A decision is recorded together with the facts it was made on (status,
difference, blocking reason codes). If a later run of the same batch — after a
rules change, a new supplier profile or a code fix — produces different facts,
the decision is reported as stale instead of silently carrying over.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .normalize import fmt_signed
from .reason_codes import blocking

DECISIONS = ["Approve as booked in ERP", "Hold — request credit note / corrected invoice",
             "Correct ERP entry", "Reject / dispute", "Escalate"]


def batch_id(paths: Iterable[Path]) -> str:
    """Content hash of the input files: the same batch re-run finds its decisions again."""
    h = hashlib.sha256()
    for p in paths:
        h.update(Path(p).read_bytes())
    return h.hexdigest()[:12]


@dataclass(frozen=True)
class Decision:
    key: str
    decision: str
    reviewer: str
    note: str
    at: str
    status: str                         # facts the decision was made on ...
    difference: Optional[int]
    blocking: Optional[tuple] = None    # ... None for records from before blocking codes were logged

    def stale_reason(self, r) -> str:
        """'' if the decision still applies to result `r`, else what changed."""
        changes = []
        if self.status != r.status:
            changes.append(f"status was {self.status}, now {r.status}")
        if self.difference != r.difference:
            changes.append(f"difference was {fmt_signed(self.difference)}, now {fmt_signed(r.difference)}")
        if self.blocking is not None:
            now = blocking(r.reasons)
            added = [c for c in now if c not in self.blocking]
            gone = [c for c in self.blocking if c not in now]
            if added:
                changes.append("new: " + ", ".join(added))
            if gone:
                changes.append("no longer: " + ", ".join(gone))
        return "; ".join(changes)


@dataclass(frozen=True)
class DecisionState:
    decision: Decision
    stale: str = ""                     # why it no longer applies; '' = current

    @property
    def current(self) -> bool:
        return not self.stale


class DecisionLog:
    """`<batch dir>/decisions.jsonl`, one decision per line, never rewritten.
    The latest line per document wins; earlier lines stay as the history."""

    def __init__(self, batch_dir: Path):
        self.dir = Path(batch_dir)
        self.path = self.dir / "decisions.jsonl"
        self._legacy = self.dir / "decisions.json"      # earlier format {key: {...}}: read until the first new decision copies it into the log

    def records(self) -> list[Decision]:
        if self.path.exists():
            return [Decision(**_tuples(json.loads(line))) for line in self.path.read_text().splitlines() if line.strip()]
        if self._legacy.exists():
            return [Decision(key=k, decision=v["decision"], reviewer=v["reviewer"], note=v.get("note", ""),
                             at=v["at"], status=v["status"], difference=v.get("difference"))
                    for k, v in json.loads(self._legacy.read_text()).items()]
        return []

    @property
    def version(self) -> int:
        return len(self.records())

    def latest(self) -> dict[str, Decision]:
        out: dict[str, Decision] = {}
        for d in self.records():
            out[d.key] = d
        return out

    def state(self, results) -> dict[str, DecisionState]:
        """Latest decision per document of this run, marked stale where the facts changed."""
        latest = self.latest()
        return {r.key: DecisionState(latest[r.key], latest[r.key].stale_reason(r)) for r in results if r.key in latest}

    def record(self, r, decision: str, reviewer: str, note: str = "", at: Optional[datetime] = None) -> Decision:
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r}")
        if not reviewer.strip():
            raise ValueError("a decision needs the reviewer's name")
        d = Decision(key=r.key, decision=decision, reviewer=reviewer.strip(), note=note,
                     at=(at or datetime.now()).strftime("%Y-%m-%d %H:%M"), status=r.status,
                     difference=r.difference, blocking=tuple(blocking(r.reasons)))
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() and self._legacy.exists():     # carry earlier decisions into the log first
            old = self.records()
            self.path.write_text("".join(json.dumps(asdict(x), ensure_ascii=False) + "\n" for x in old))
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(d), ensure_ascii=False) + "\n")
        return d


def _tuples(d: dict) -> dict:
    if d.get("blocking") is not None:
        d["blocking"] = tuple(d["blocking"])
    return d
