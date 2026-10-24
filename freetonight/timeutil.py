"""Time helpers: weekday names, clock parsing, and interval math.

An interval is a (start, end) pair of naive local datetimes with start < end.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Iterable

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_ALIASES = {
    "m": 0, "mon": 0, "monday": 0,
    "t": 1, "tu": 1, "tue": 1, "tues": 1, "tuesday": 1,
    "w": 2, "wed": 2, "weds": 2, "wednesday": 2,
    "r": 3, "th": 3, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "f": 4, "fri": 4, "friday": 4,
    "sa": 5, "sat": 5, "saturday": 5,
    "su": 6, "sun": 6, "sunday": 6,
}

Interval = tuple[datetime, datetime]


def day_index(token: str) -> int:
    key = token.strip().lower().rstrip(".")
    if key not in _DAY_ALIASES:
        raise ValueError(f"Unknown day: {token!r}")
    return _DAY_ALIASES[key]


def parse_days(token: str) -> list[int]:
    """Parse "Mon", "Mon/Wed", "Mon,Wed,Fri", "MWF", "TR", "weekdays", "daily"."""
    t = token.strip().lower()
    if t in ("daily", "everyday", "every day"):
        return list(range(7))
    if t in ("weekdays", "weekday"):
        return [0, 1, 2, 3, 4]
    if t in ("weekend", "weekends"):
        return [5, 6]
    parts = [p for p in re.split(r"[\s,/&+]+", t) if p]
    if len(parts) > 1:
        return sorted({day_index(p) for p in parts})
    try:
        return [day_index(t)]
    except ValueError:
        pass
    # Compact registrar style: MWF, TR, MTWRF
    if re.fullmatch(r"[mtwrf]+", t):
        return sorted({_DAY_ALIASES[c] for c in t})
    raise ValueError(f"Unknown days: {token!r}")


def parse_clock(token: str) -> time:
    """Parse "9", "9am", "9:30", "21:15", "9:30pm", "noon", "midnight"."""
    t = token.strip().lower().replace(" ", "").replace(".", "")
    if t == "noon":
        return time(12, 0)
    if t == "midnight":
        return time(0, 0)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm|a|p)?", t)
    if not m:
        raise ValueError(f"Unknown time: {token!r}")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if hour == 12:
            hour = 0
        if ampm.startswith("p"):
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Unknown time: {token!r}")
    return time(hour, minute)


def parse_range(token: str) -> tuple[time, time]:
    """Parse "9-10:15", "6pm-8pm", "18:00 to 20:00". "2-4pm" puts both ends in the afternoon."""
    parts = re.split(r"\s*(?:-|–|to)\s*", token.strip(), maxsplit=1)
    if len(parts) != 2:
        raise ValueError(f"Unknown time range: {token!r}")
    left, right = parts
    end = parse_clock(right)
    has_suffix = re.search(r"(am|pm|a|p)\s*$", left.strip().lower())
    start = parse_clock(left)
    if not has_suffix and re.search(r"(pm|p)\s*$", right.strip().lower()):
        shifted = time(start.hour + 12, start.minute) if start.hour < 12 else start
        if shifted < end:
            start = shifted
    if start >= end and end != time(0, 0):
        raise ValueError(f"Range ends before it starts: {token!r}")
    return start, end


def fmt_clock(t: time | datetime) -> str:
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}:{t.minute:02d}{suffix}" if t.minute else f"{hour}{suffix}"


def fmt_slot(start: datetime, end: datetime) -> str:
    return f"{DAYS[start.weekday()]} {start:%b} {start.day}, {fmt_clock(start)}–{fmt_clock(end)}"


def at(d: date, t: time) -> datetime:
    if t == time(0, 0):
        return datetime.combine(d + timedelta(days=1), time(0, 0))
    return datetime.combine(d, t)


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(window: Interval, busy: Iterable[Interval]) -> list[Interval]:
    """Free parts of `window` after removing `busy`."""
    free: list[Interval] = []
    cursor, end = window
    for s, e in merge(busy):
        if e <= cursor or s >= end:
            continue
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return free


def contains(intervals: Iterable[Interval], start: datetime, end: datetime) -> bool:
    return any(s <= start and end <= e for s, e in intervals)


def overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]
