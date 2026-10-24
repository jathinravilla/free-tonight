#!/usr/bin/env python3
"""Free Tonight availability cards for Cubbie. Standard library only.

  make   Turn the student's calendar events into a card with free windows only (no event names).
  match  Combine the student's events with friends' cards: best times, pooled filters, private near misses.

Exit codes: 0 ok, 2 bad input, 3 no overlap.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from freetonight.agent import budget_bucket  # noqa: E402
from freetonight.negotiation import ACTIVITIES  # noqa: E402
from freetonight.profile import ALLERGENS, DIET_TAGS, parse_allergies, parse_diet, parse_schedule, upcoming_dates  # noqa: E402,E501
from freetonight.timeutil import at, contains, fmt_slot, subtract  # noqa: E402

PREFIX = "FT1."
MAX_CARDS = 25
MAX_WINDOWS = 200
MAX_CODE_LEN = 20000


def load_events(path: str | None, schedule: str | None, dates: list[date]) -> list[dict]:
    """Events as [{start, end, title, flex_minutes?}] in local time. From a calendar export or typed schedule."""
    events: list[dict] = []
    if path:
        raw = json.loads(Path(path).read_text())
        for e in raw:
            events.append({"start": datetime.fromisoformat(e["start"]), "end": datetime.fromisoformat(e["end"]),
                           "title": e.get("title", ""), "flex": int(e.get("flex_minutes", 0))})
    if schedule:
        for c in parse_schedule(schedule):
            for d in dates:
                for (s, e), cm in c.intervals_on(d):
                    events.append({"start": s, "end": e, "title": cm.label, "flex": cm.flex_minutes})
    return events


def free_windows(events: list[dict], dates: list[date], earliest: str, latest: str) -> list[tuple[datetime, datetime]]:
    out = []
    for d in dates:
        window = (at(d, time.fromisoformat(earliest)), at(d, time.fromisoformat(latest)))
        out.extend(subtract(window, [(e["start"], e["end"]) for e in events]))
    return out


def encode(card: dict) -> str:
    return PREFIX + base64.urlsafe_b64encode(json.dumps(card, separators=(",", ":")).encode()).decode().rstrip("=")


def decode(token: str) -> dict:
    token = token.strip()
    if not token.startswith(PREFIX):
        raise ValueError("Not a Free Tonight card code (it starts with FT1.)")
    if len(token) > MAX_CODE_LEN:
        raise ValueError("Card code is too long")
    body = token[len(PREFIX):]
    try:
        raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("Card code is damaged; ask for the whole message again") from exc
    return clean_card(raw)


def _as_list(v) -> list:
    return v[:20] if isinstance(v, list) else [v] if isinstance(v, str) else []


def clean_card(raw) -> dict:
    """Cards come from other people: keep only well-formed fields, and treat the name as plain text."""
    if not isinstance(raw, dict) or not isinstance(raw.get("w"), list) or not isinstance(raw.get("f"), dict):
        raise ValueError("Card code is damaged; ask for the whole message again")
    name = "".join(ch for ch in str(raw.get("n", "Friend")) if ch.isprintable())[:40].strip() or "Friend"
    windows = []
    for w in raw["w"][:MAX_WINDOWS]:
        s, e = datetime.fromisoformat(str(w[0])), datetime.fromisoformat(str(w[1]))
        if e > s:
            windows.append([s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes")])
    f = raw["f"]
    return {"v": 1, "n": name, "a": str(raw.get("a", "")),
            "w": windows,
            "f": {"d": sorted({str(d) for d in _as_list(f.get("d"))} & set(DIET_TAGS)),
                  "x": sorted({str(x) for x in _as_list(f.get("x"))} & set(ALLERGENS)),
                  "b": max(0, int(f.get("b", 20))), "k": max(1, min(120, int(f.get("k", 20))))}}


def find_codes(text: str) -> list[str]:
    return [w.strip(".,;:()[]\"'") for w in text.split() if w.startswith(PREFIX)]


def cmd_make(a) -> int:
    dates = upcoming_dates(date.fromisoformat(a.today) if a.today else date.today(), a.when)
    events = load_events(a.events, a.schedule, dates)
    windows = free_windows(events, dates, a.earliest, a.latest)
    card = {
        "v": 1, "n": a.name, "a": a.activity,
        "w": [[s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes")] for s, e in windows],
        "f": {"d": parse_diet(a.diet or ""), "x": parse_allergies(a.allergies or ""),
              "b": budget_bucket(a.budget), "k": a.walk},
    }
    act = ACTIVITIES[a.activity]
    print(f"{act.emoji} {a.name} is up for {a.activity} ({a.when}). Free:")
    for s, e in windows[:8]:
        print(f"- {fmt_slot(s, e)}")
    if len(windows) > 8:
        print(f"- +{len(windows) - 8} more")
    print("Paste this whole message to your Cubbie to plan together:")
    print(encode(card))
    return 0


def cmd_match(a) -> int:
    text = Path(a.cards).read_text() if a.cards else sys.stdin.read()
    codes = find_codes(text)
    if not codes:
        print("No FT1. card codes found. Ask friends to paste their whole card message.", file=sys.stderr)
        return 2
    cards = [decode(c) for c in codes[:MAX_CARDS]]
    if not any(c["w"] for c in cards):
        print("The pasted cards have no free times in them.", file=sys.stderr)
        return 3
    dates_all = sorted({datetime.fromisoformat(w[0]).date() for c in cards for w in c["w"]})
    events = load_events(a.events, a.schedule, dates_all)
    mine = free_windows(events, dates_all, a.earliest, a.latest)
    people = {a.name: mine}
    for c in cards:
        people[c["n"]] = [(datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in c["w"]]
    act = ACTIVITIES[a.activity]
    span = timedelta(minutes=a.minutes or act.minutes)
    now = datetime.fromisoformat(a.now) if a.now else datetime.now()
    slots = []
    for d in dates_all:
        t, last = at(d, act.earliest), at(d, act.latest)
        if last <= t:
            last += timedelta(days=1)
        while t + span <= last:
            if t >= now + timedelta(minutes=30):
                who = [n for n, ivs in people.items() if contains(ivs, t, t + span)]
                blockers = [e for e in events if e["start"] < t + span and t < e["end"]]
                slots.append((t, who, blockers))
            t += timedelta(minutes=15)
    ideal = act.ideal

    def score(s):
        t, who, _ = s
        days_out = (t.date() - dates_all[0]).days
        return (-len(who), abs((t - at(t.date(), ideal)).total_seconds()) / 60 + days_out * 20)

    slots.sort(key=score)
    best, picked = [], []
    for t, who, bl in slots:
        if len(who) < 2:
            continue
        if all(abs((t - p).total_seconds()) >= 3600 for p in picked):
            picked.append(t)
            best.append({"slot": fmt_slot(t, t + span), "start": t.isoformat(timespec="minutes"),
                         "people": who, "count": len(who)})
        if len(best) == 3:
            break
    near = []
    everyone = len(people)
    for t, who, bl in slots:
        if a.name in who or len(who) != everyone - 1:
            continue
        need = [(e, int((min(e["end"], t + span) - max(e["start"], t)).total_seconds() // 60)) for e in bl]
        if need and all(e["flex"] and m <= e["flex"] for e, m in need):
            near.append({"slot": fmt_slot(t, t + span), "start": t.isoformat(timespec="minutes"),
                         "ask_student": [f"{e['title'] or 'a commitment'}: {m} min" for e, m in need]})
        if len(near) == 2:
            break
    pooled = {"diet": sorted({d for c in cards for d in c["f"]["d"]} | set(parse_diet(a.diet or ""))),
              "avoid": sorted({x for c in cards for x in c["f"]["x"]} | set(parse_allergies(a.allergies or ""))),
              "price_cap": min([c["f"]["b"] for c in cards] + [budget_bucket(a.budget)]),
              "max_walk_min": min([c["f"]["k"] for c in cards] + [a.walk])}
    out = {"people": list(people), "best": best, "pooled_filters": pooled,
           "private_near_misses_for_this_student_only": near}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if best else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("make", "match"):
        p = sub.add_parser(name)
        p.add_argument("--name", required=True)
        p.add_argument("--activity", default="dinner", choices=list(ACTIVITIES))
        p.add_argument("--events", help="JSON file of the student's calendar events")
        p.add_argument("--schedule", help='Typed schedule, e.g. "MWF 9-10:15 CS 201; Tue 6-8pm study (flex 30)"')
        p.add_argument("--diet", default="")
        p.add_argument("--allergies", default="")
        p.add_argument("--budget", type=float, default=20)
        p.add_argument("--walk", type=int, default=20)
        p.add_argument("--earliest", default="11:00")
        p.add_argument("--latest", default="23:30")
    sub.choices["make"].add_argument("--when", default="this week")
    sub.choices["make"].add_argument("--today", help="YYYY-MM-DD (default: today)")
    sub.choices["match"].add_argument("--cards", help="File with friends' pasted cards (default: stdin)")
    sub.choices["match"].add_argument("--minutes", type=int)
    sub.choices["match"].add_argument("--now", help="YYYY-MM-DDTHH:MM (default: now)")
    a = ap.parse_args(argv)
    try:
        return cmd_make(a) if a.cmd == "make" else cmd_match(a)
    except (ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        print(f"Bad input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
