"""Places to go, and the filters agents agree on before anyone picks one."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time
from importlib import resources
from pathlib import Path

from .timeutil import DAYS

# Diet and allergy filters only matter where people eat or drink.
FOOD_KINDS = {"dinner", "lunch", "coffee"}


@dataclass
class Venue:
    name: str
    kinds: list[str]
    price_pp: float
    walk_min: int
    diet: list[str] = field(default_factory=list)  # diets with real options on the menu
    allergy_safe: list[str] = field(default_factory=list)  # allergens the kitchen can avoid
    hours: dict[str, list[str]] = field(default_factory=dict)  # "Mon": ["11:00", "22:00"]; "*" = every day
    rating: float = 4.0
    area: str = ""
    url: str = ""

    def open_during(self, start: datetime, end: datetime) -> bool:
        span = self.hours.get(DAYS[start.weekday()]) or self.hours.get("*")
        if not span:
            return False
        opens, closes = (_minutes(time.fromisoformat(x)) for x in span)
        if closes <= opens:  # open past midnight
            closes += 24 * 60
        s = _minutes(start.time())
        e = s + int((end - start).total_seconds() // 60)
        return opens <= s and e <= closes


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class GroupFilters:
    """Merged from every agent. Nobody can tell whose constraint is whose."""
    diet: list[str] = field(default_factory=list)
    allergy_safe: list[str] = field(default_factory=list)
    budget_cap: float = 999.0
    max_walk_min: int = 999

    def rejects(self, v: Venue, *, food: bool = True) -> list[str]:
        why = []
        if v.price_pp > self.budget_cap:
            why.append("budget")
        if v.walk_min > self.max_walk_min:
            why.append("distance")
        if food and any(d not in v.diet for d in self.diet):
            why.append("diet")
        if food and any(a not in v.allergy_safe for a in self.allergy_safe):
            why.append("allergy")
        return why


def load_venues(path: str | Path | None = None) -> list[Venue]:
    if path:
        raw = Path(path).read_text()
    else:
        raw = resources.files("freetonight.data").joinpath("venues.json").read_text()
    return [Venue(**v) for v in json.loads(raw)]


def rank(venues: list[Venue], kind: str, filters: GroupFilters, start: datetime, end: datetime,
         *, ignore: tuple[str, ...] = ()) -> list[tuple[Venue, list[str]]]:
    """Venues of this kind open for the slot, best first, each with the filters it breaks.

    Venues breaking a filter come last; `ignore` lists filters to skip (for showing near misses).
    """
    out = []
    for v in venues:
        if kind not in v.kinds or not v.open_during(start, end):
            continue
        broken = [r for r in filters.rejects(v, food=kind in FOOD_KINDS) if r not in ignore]
        out.append((v, broken))
    # Fewest broken filters, then well rated, then cheap and close.
    out.sort(key=lambda p: (len(p[1]), -p[0].rating * 4 + p[0].price_pp * 0.25 + p[0].walk_min * 0.15))
    return out
