"""A student's private profile. It lives inside their personal agent and is never shared whole."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

from .timeutil import DAYS, Interval, at, fmt_clock, parse_clock, parse_days, parse_range

DIET_TAGS = {"vegetarian", "vegan", "halal", "kosher", "gluten-free", "dairy-free"}
ALLERGENS = {"peanut", "tree-nut", "shellfish", "fish", "egg", "soy", "dairy", "gluten", "sesame"}
_ALLERGEN_ALIASES = {
    "peanuts": "peanut", "nut": "tree-nut", "nuts": "tree-nut", "tree nut": "tree-nut", "tree nuts": "tree-nut",
    "eggs": "egg", "milk": "dairy", "lactose": "dairy", "wheat": "gluten", "shrimp": "shellfish",
}


@dataclass
class Commitment:
    days: list[int]
    start: str  # "HH:MM"
    end: str
    label: str = ""
    # Minutes the owner might give up if asked (e.g. leave study group early). 0 = fixed.
    flex_minutes: int = 0
    # Set for a one-off date instead of a weekly repeat.
    on_date: str | None = None

    def intervals_on(self, d: date) -> list[tuple[Interval, "Commitment"]]:
        if self.on_date:
            if d.isoformat() != self.on_date:
                return []
        elif d.weekday() not in self.days:
            return []
        s, e = time.fromisoformat(self.start), time.fromisoformat(self.end)
        return [((at(d, s), at(d, e)), self)]

    def describe(self) -> str:
        when = self.on_date or "/".join(DAYS[i] for i in self.days)
        flex = f" (can trim {self.flex_minutes} min)" if self.flex_minutes else ""
        span = f"{fmt_clock(time.fromisoformat(self.start))}–{fmt_clock(time.fromisoformat(self.end))}"
        return f"{when} {span} {self.label}{flex}".strip()


@dataclass
class Profile:
    name: str
    commitments: list[Commitment] = field(default_factory=list)
    budget: float = 20.0  # most they'll spend per outing, in dollars
    diet: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    max_walk_min: int = 20
    earliest: str = "11:00"
    latest: str = "23:30"
    no_go_days: list[int] = field(default_factory=list)
    user_id: str = ""
    free_blocks: list[str] = field(default_factory=list)  # what they tapped in setup, e.g. "Tue evening"
    calendar_url: str = ""  # private iCal link; read for free/busy only, never shared

    def busy_on(self, d: date) -> list[tuple[Interval, Commitment]]:
        out = []
        for c in self.commitments:
            out.extend(c.intervals_on(d))
        return out

    def day_window(self, d: date) -> Interval | None:
        if d.weekday() in self.no_go_days:
            return None
        return at(d, time.fromisoformat(self.earliest)), at(d, time.fromisoformat(self.latest))

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        known = set(cls.__dataclass_fields__)
        data = {k: v for k, v in data.items() if k in known}
        data["commitments"] = [Commitment(**c) for c in data.get("commitments", [])]
        return cls(**data)

    def summary_lines(self) -> list[str]:
        lines = [c.describe() for c in self.commitments] or ["No commitments yet"]
        prefs = [f"Budget up to ${self.budget:.0f}", f"Walk up to {self.max_walk_min} min"]
        if self.diet:
            prefs.append("Diet: " + ", ".join(self.diet))
        if self.allergies:
            prefs.append("Allergies: " + ", ".join(self.allergies))
        prefs.append(f"Hangout hours {fmt_clock(time.fromisoformat(self.earliest))}–"
                     f"{fmt_clock(time.fromisoformat(self.latest))}")
        if self.no_go_days:
            prefs.append("Never on " + ", ".join(DAYS[i] for i in self.no_go_days))
        return lines + prefs


_LINE = re.compile(
    r"^\s*(?P<days>[A-Za-z/,&+ ]+?)\s+(?P<range>[\d:apm.\s]+(?:-|–|to)\s*[\d:apm.]+)\s*(?P<label>.*?)\s*$",
    re.IGNORECASE,
)
_FLEX = re.compile(r"\((?:flex|flexible|can (?:skip|trim|leave early))\s*(\d+)?\s*(?:min|m)?\)", re.IGNORECASE)


def parse_schedule(text: str) -> list[Commitment]:
    """Parse lines like "MWF 9-10:15 CS 201" or "Tue 6-8pm study group (flex 30)".

    Lines may be separated by newlines or semicolons. Raises ValueError naming the bad line.
    """
    out: list[Commitment] = []
    for raw in re.split(r"[;\n]+", text):
        line = raw.strip(" -•*\t")
        if not line:
            continue
        flex = 0
        fm = _FLEX.search(line)
        if fm:
            flex = int(fm.group(1) or 30)
            line = _FLEX.sub("", line).strip()
        m = _LINE.match(line)
        if not m:
            raise ValueError(f"Couldn't read: {raw.strip()!r}. Try like \"MWF 9-10:15 CS 201\".")
        try:
            days = parse_days(m.group("days"))
            start, end = parse_range(m.group("range"))
        except ValueError as exc:
            raise ValueError(f"Couldn't read: {raw.strip()!r} ({exc}).") from None
        out.append(Commitment(days=days, start=start.strftime("%H:%M"), end=end.strftime("%H:%M"),
                              label=m.group("label").strip(), flex_minutes=flex))
    return out


def parse_schedule_loose(text: str) -> tuple[list[Commitment], list[str]]:
    """Like parse_schedule, but keeps every line it can read and returns the ones it couldn't."""
    out, skipped = [], []
    for raw in re.split(r"[;\n]+", text):
        if not raw.strip(" -•*\t"):
            continue
        try:
            out.extend(parse_schedule(raw))
        except ValueError:
            skipped.append(raw.strip())
    return out, skipped


# Tap-to-pick setup: the parts of a day someone can say they're usually free.
BLOCKS = {"morning": ("08:00", "12:00"), "afternoon": ("12:00", "17:00"),
          "evening": ("17:00", "21:00"), "late night": ("21:00", "23:59")}


def busy_from_free(free: list[str]) -> list[Commitment]:
    """Turn picks like ["Tue evening", "Sat afternoon"] into busy blocks for everything not picked.

    Nothing picked means "no info", so no busy blocks.
    """
    if not free:
        return []
    picked = {tuple(f.split(" ", 1)) for f in free}
    out = []
    for d, day in enumerate(DAYS):
        run: list[str] | None = None
        for name, (s, e) in BLOCKS.items():
            if (day, name) in picked:
                if run:
                    out.append(Commitment(days=[d], start=run[0], end=run[1], label="busy"))
                    run = None
            else:
                run = [run[0] if run else s, e]
        if run:
            out.append(Commitment(days=[d], start=run[0], end=run[1], label="busy"))
    return out


def parse_list(text: str, known: set[str], aliases: dict[str, str] | None = None) -> list[str]:
    out = []
    for part in re.split(r"[,;/\n]+|\band\b", text.lower()):
        p = part.strip()
        if not p or p in ("none", "no", "n/a", "nothing"):
            continue
        p = (aliases or {}).get(p, p)
        if p not in known:
            raise ValueError(f"Unknown item {part.strip()!r}. Options: {', '.join(sorted(known))}")
        if p not in out:
            out.append(p)
    return out


def parse_diet(text: str) -> list[str]:
    return parse_list(text, DIET_TAGS, {"veg": "vegetarian", "veggie": "vegetarian", "gf": "gluten-free"})


def parse_allergies(text: str) -> list[str]:
    return parse_list(text, ALLERGENS, _ALLERGEN_ALIASES)


def parse_money(text: str) -> float:
    m = re.search(r"\d+(?:\.\d+)?", text or "")
    if not m:
        raise ValueError(f"Couldn't read a dollar amount from {text!r}")
    return float(m.group(0))


def upcoming_dates(start: date, when: str) -> list[date]:
    """Dates for "today", "tomorrow", "weeknights", "this week" (today through Sunday), "weekend", "next 7 days"."""
    w = when.strip().lower()
    if w == "today":
        return [start]
    if w == "tomorrow":
        return [start + timedelta(days=1)]
    if w in ("weekend", "this weekend"):
        days_to_sat = (5 - start.weekday()) % 7
        sat = start + timedelta(days=days_to_sat)
        return [d for d in (sat, sat + timedelta(days=1)) if d >= start]
    if w in ("weeknights", "this week nights", "weekdays"):
        return [start + timedelta(days=i) for i in range(7 - start.weekday()) if (start.weekday() + i) < 5]
    if w in ("this week", "week"):
        return [start + timedelta(days=i) for i in range(7 - start.weekday())]
    if w in ("next 7 days", "7 days"):
        return [start + timedelta(days=i) for i in range(7)]
    try:
        idx = parse_days(w)
    except ValueError:
        raise ValueError(f"Unknown 'when': {when!r}") from None
    return [start + timedelta(days=(i - start.weekday()) % 7) for i in idx]


def clock_ok(text: str) -> str:
    return parse_clock(text).strftime("%H:%M")


def now_local() -> datetime:
    return datetime.now()
