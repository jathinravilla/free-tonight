"""Command line: run the 5-friend demo, split a bill, or check a schedule parses.

    python -m freetonight demo [--interactive] [--out out]
    python -m freetonight split 86.40 --people Maya,Jordan,Priya --tip 18
    python -m freetonight split --items "Maya=14.5,Jordan=22" --tax 3.1 --tip 20
    python -m freetonight parse "MWF 9-10:15 CS 201; Tue 6-8pm study group (flex 30)"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import cards, ledger_html
from .agent import OwnerQuestion, PersonalAgent
from .negotiation import PlanSession, describe_result
from .profile import Profile, parse_schedule, upcoming_dates
from .split import format_split, split_bill, split_even
from .venues import load_venues

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s): return c("1", s)
def dim(s): return c("2", s)
def green(s): return c("32", s)
def purple(s): return c("35", s)
def orange(s): return c("33", s)


def load_demo(path: str | None = None) -> dict:
    if path:
        return json.loads(Path(path).read_text())
    return json.loads(resources.files("freetonight.data").joinpath("demo_friends.json").read_text())


async def run_demo(args) -> int:
    demo = load_demo(args.scenario)
    now = datetime.fromisoformat(demo["today"])
    scripted = demo.get("answers", {})

    def make_ask(name: str):
        async def ask(q: OwnerQuestion) -> str:
            print()
            print(purple(f"  📱 DM to {name}: ") + q.text)
            for d in q.details:
                print(dim(f"     {d}"))
            if args.interactive:
                while True:
                    ans = input(f"     {name}, answer [{'/'.join(q.options)}]: ").strip().lower()
                    if ans in q.options:
                        return ans
            ans = scripted.get(q.kind, {}).get(name, q.options[0])
            print(purple(f"     {name} taps: {ans}"))
            return ans
        return ask

    agents = [PersonalAgent(Profile.from_dict(f), make_ask(f["name"])) for f in demo["friends"]]
    dates = upcoming_dates(now.date(), demo.get("when", "this week"))

    async def progress(stage: str, detail: str) -> None:
        print()
        print(bold(orange(f"▸ {stage.upper()}")) + "  " + detail)

    async def poll(options: list[str]) -> int:
        print()
        print(bold("  🗳  Group poll"))
        for i, o in enumerate(options, 1):
            print(f"     {i}. {o}")
        if args.interactive:
            while True:
                raw = input("     Winning option number: ").strip()
                if raw.isdigit() and 1 <= int(raw) <= len(options):
                    return int(raw) - 1
        print(dim("     (scripted: option 1 wins 3–2)"))
        return 0

    print(bold("\nFree Tonight") + dim(f" · {len(agents)} friends · {demo['activity']} {demo.get('when')} · "
                                       f"host: {demo['host']}"))
    print(dim("  Each friend's agent keeps their schedule, budget and food needs private."))
    session = PlanSession(agents, demo["host"], demo["activity"], dates, load_venues(args.venues), now=now,
                          progress=progress, poll=poll)
    result = await session.run()

    print()
    print(bold("─" * 60))
    print(bold("📋 PLAN CARD"))
    print(describe_result(result))
    print(bold("─" * 60))
    if result.plan and result.status == "locked":
        print(bold("📎 Copy card for other group chats:\n"))
        print(cards.copy_card(result))
    s = result.ledger.stats()
    print()
    print(green(f"🔒 Privacy: {s['messages']} messages, {s['fields_shared']} fields shared, "
                f"{s['fields_withheld']} things kept private, {s['human_decisions']} human decisions."))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ledger.html").write_text(ledger_html.render(result))
    (out / "ledger.json").write_text(result.ledger.to_json())
    if result.plan and result.status == "locked":
        (out / "plan.ics").write_text(cards.ics_invite(result))
    print(dim(f"\nWrote {out/'ledger.html'} (open it in a browser), {out/'ledger.json'}"
              + (f", {out/'plan.ics'}" if result.status == "locked" else "")))
    return 0 if result.status == "locked" else 1


def run_split(args) -> int:
    try:
        if args.items:
            items = {}
            for part in args.items.split(","):
                name, _, amount = part.partition("=")
                items[name.strip()] = float(amount)
            shares = split_bill(items, tax=args.tax, tip_pct=args.tip, shared=args.shared)
        else:
            if args.total is None or not args.people:
                print("Give a total and --people, or --items.", file=sys.stderr)
                return 2
            shares = split_even(args.total, [p.strip() for p in args.people.split(",")], tip_pct=args.tip)
    except ValueError as exc:
        print(f"Couldn't split: {exc}", file=sys.stderr)
        return 2
    print(format_split(shares))
    return 0


def run_parse(args) -> int:
    try:
        for cm in parse_schedule(args.text):
            print(cm.describe())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="freetonight", description="Private agents that plan hangouts.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="Run the 5-friend planning demo")
    d.add_argument("--interactive", action="store_true", help="Answer each friend's DMs yourself")
    d.add_argument("--out", default="out", help="Where to write ledger.html, ledger.json and plan.ics")
    d.add_argument("--scenario", help="A scenario JSON like freetonight/data/demo_friends.json")
    d.add_argument("--venues", help="A venues JSON like freetonight/data/venues.json")
    s = sub.add_parser("split", help="Split a bill")
    s.add_argument("total", nargs="?", type=float)
    s.add_argument("--people", help="Comma-separated names for an even split")
    s.add_argument("--items", help='Itemized: "Maya=14.5,Jordan=22"')
    s.add_argument("--tax", type=float, default=0.0)
    s.add_argument("--tip", type=float, default=0.0, help="Tip percent")
    s.add_argument("--shared", type=float, default=0.0, help="Shared items split evenly (itemized only)")
    p = sub.add_parser("parse", help="Check that a schedule parses")
    p.add_argument("text")
    args = ap.parse_args(argv)
    if args.cmd == "demo":
        return asyncio.run(run_demo(args))
    if args.cmd == "split":
        return run_split(args)
    return run_parse(args)


if __name__ == "__main__":
    raise SystemExit(main())
