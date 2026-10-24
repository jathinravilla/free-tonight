"""The privacy ledger: every message between agents, with what was shared and what stayed private."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Entry:
    round: str
    sender: str
    to: str  # "table" for the shared negotiation channel, or a person's name for a private ask
    kind: str  # availability, filters, proposal, response, relax_ask, relax_answer, confirm_ask, confirm_answer, poll, lock, note
    summary: str
    shared: dict[str, Any] = field(default_factory=dict)
    withheld: list[str] = field(default_factory=list)
    human: bool = False  # a person answered or decided this step
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class Ledger:
    def __init__(self) -> None:
        self.entries: list[Entry] = []

    def add(self, round: str, sender: str, to: str, kind: str, summary: str, *,
            shared: dict[str, Any] | None = None, withheld: list[str] | None = None,
            human: bool = False) -> Entry:
        e = Entry(round, sender, to, kind, summary, shared or {}, withheld or [], human)
        self.entries.append(e)
        return e

    def to_json(self) -> str:
        return json.dumps([asdict(e) for e in self.entries], indent=2, default=str)

    def stats(self) -> dict[str, int]:
        return {
            "messages": len(self.entries),
            "fields_shared": sum(len(e.shared) for e in self.entries),
            "fields_withheld": sum(len(e.withheld) for e in self.entries),
            "human_decisions": sum(1 for e in self.entries if e.human),
        }
