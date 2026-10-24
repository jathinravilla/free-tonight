"""A personal agent. It holds one student's private profile and decides what leaves it."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable

from .ledger import Ledger
from .profile import Commitment, Profile
from .timeutil import Interval, fmt_clock, fmt_slot, overlaps, subtract
from .venues import Venue

# The agent asks its own human through this. It gets a question and the allowed answers, returns one answer.
AskOwner = Callable[["OwnerQuestion"], Awaitable[str]]


@dataclass
class OwnerQuestion:
    owner: str
    kind: str  # "relax" or "confirm"
    text: str
    options: list[str]
    details: list[str]


@dataclass
class Plan:
    kind: str
    start: datetime
    end: datetime
    venue: Venue | None
    attendees: list[str]

    @property
    def label(self) -> str:
        where = self.venue.name if self.venue else "TBD"
        return f"{self.kind.title()} at {where}, {fmt_slot(self.start, self.end)}"


def budget_bucket(amount: float) -> int:
    """Share budgets rounded down to $5 steps so the exact number never leaves the agent."""
    return int(math.floor(amount / 5.0) * 5)


class PersonalAgent:
    def __init__(self, profile: Profile, ask_owner: AskOwner):
        self.profile = profile
        self.name = profile.name
        self._ask = ask_owner
        # Commitments the owner agreed to trim, for this planning run only.
        self._released: list[Interval] = []

    # ---- What the agent shares -------------------------------------------------

    def free_windows(self, dates: list[date]) -> list[Interval]:
        free: list[Interval] = []
        for d in dates:
            window = self.profile.day_window(d)
            if not window:
                continue
            busy = [iv for iv, _ in self.profile.busy_on(d)]
            busy = [b for b in busy if not any(r[0] <= b[0] and b[1] <= r[1] for r in self._released)]
            free.extend(subtract(window, self._carve(busy)))
        return free

    def _carve(self, busy: list[Interval]) -> list[Interval]:
        # Remove released parts (the owner said they could leave early or arrive late).
        out: list[Interval] = []
        for b in busy:
            pieces = [b]
            for r in self._released:
                next_pieces: list[Interval] = []
                for p in pieces:
                    next_pieces.extend(subtract(p, [r]))
                pieces = next_pieces
            out.extend(pieces)
        return out

    def share_availability(self, dates: list[date], ledger: Ledger, round_name: str) -> list[Interval]:
        windows = self.free_windows(dates)
        n_busy = sum(len(self.profile.busy_on(d)) for d in dates)
        ledger.add(
            round_name, self.name, "table", "availability",
            f"{self.name}'s agent shared {len(windows)} free windows",
            shared={"free_windows": [fmt_slot(s, e) for s, e in windows]},
            withheld=[f"what fills the gaps ({n_busy} commitments: names, places, labels)",
                      "hangout hour preferences (applied, not shown)"],
        )
        return windows

    def private_filters(self) -> dict:
        p = self.profile
        return {"diet": list(p.diet), "allergy_safe": list(p.allergies),
                "budget_cap": budget_bucket(p.budget), "max_walk_min": p.max_walk_min}

    # ---- Judging proposals ----------------------------------------------------------

    def judge(self, plan: Plan) -> tuple[str, str]:
        """("accept", "") or ("decline", reason category). Reasons are categories, never details."""
        p = self.profile
        if not any(s <= plan.start and plan.end <= e for s, e in self.free_windows([plan.start.date()])):
            return "decline", "time"
        v = plan.venue
        if v:
            if v.price_pp > p.budget:
                return "decline", "budget"
            if v.walk_min > p.max_walk_min:
                return "decline", "distance"
            if any(d not in v.diet for d in p.diet) or any(a not in v.allergy_safe for a in p.allergies):
                return "decline", "food"
        return "accept", ""

    # ---- Asking the human --------------------------------------------------------------

    def blockers(self, start: datetime, end: datetime) -> list[tuple[Interval, Commitment]]:
        return [(iv, c) for iv, c in self.profile.busy_on(start.date()) if overlaps(iv, (start, end))]

    def can_relax_for(self, start: datetime, end: datetime) -> list[tuple[Interval, Commitment, Interval]]:
        """Blocking commitments the owner marked flexible, each with the part to give up.

        Empty if anything blocking is fixed, outside hangout hours, or needs more than its flex minutes.
        """
        window = self.profile.day_window(start.date())
        if not window or start < window[0] or end > window[1]:
            return []
        out = []
        for iv, c in self.blockers(start, end):
            give_up = (max(iv[0], start), min(iv[1], end))
            minutes = (give_up[1] - give_up[0]).total_seconds() / 60
            if c.flex_minutes <= 0 or minutes > c.flex_minutes:
                return []
            out.append((iv, c, give_up))
        return out

    async def ask_relax(self, plan_kind: str, start: datetime, end: datetime, others: int,
                        ledger: Ledger, round_name: str, host: str) -> bool:
        options = self.can_relax_for(start, end)
        ledger.add(round_name, host, self.name, "relax_ask",
                   f"Host asked {self.name}'s agent whether {fmt_slot(start, end)} could work",
                   shared={"slot": fmt_slot(start, end), "would_bring_total_to": others + 1})
        if not options:
            ledger.add(round_name, self.name, host, "relax_answer",
                       f"{self.name}'s agent: no (nothing flexible there)",
                       shared={"answer": "no"}, withheld=["why not"])
            return False
        bits = []
        for iv, c, give in options:
            mins = int((give[1] - give[0]).total_seconds() // 60)
            what = c.label or "your commitment"
            if give[0] > iv[0]:
                bits.append(f"leave {what} {mins} min early")
            else:
                bits.append(f"get to {what} {mins} min late")
        q = OwnerQuestion(
            owner=self.name, kind="relax",
            text=(f"Could you {' and '.join(bits)} on {fmt_slot(start, end).split(',')[0]}? "
                  f"Then {others + 1} friends can do {plan_kind} at {fmt_clock(start)}."),
            options=["yes", "no"],
            details=["Only your yes or no is shared, not what you're skipping."],
        )
        answer = (await self._ask(q)).strip().lower()
        yes = answer == "yes"
        ledger.add(round_name, self.name, self.name, "relax_owner",
                   f"{self.name} answered their agent privately: {answer}", human=True,
                   withheld=["the commitment being trimmed", "how long it is"])
        if yes:
            self._released.extend(give for _, _, give in options)
        ledger.add(round_name, self.name, host, "relax_answer",
                   f"{self.name}'s agent: {'yes, I can make it' if yes else 'no'}",
                   shared={"answer": "yes" if yes else "no"}, withheld=["what changed"])
        return yes

    async def confirm(self, plan: Plan, cost_pp: float, ledger: Ledger, round_name: str, host: str) -> str:
        details = [
            f"Who: {', '.join(plan.attendees)}",
            f"About ${cost_pp:.0f} per person" if cost_pp else "Free",
        ]
        if plan.venue and plan.venue.url:
            details.append(plan.venue.url)
        q = OwnerQuestion(owner=self.name, kind="confirm", text=f"Lock in {plan.label}?",
                          options=["yes", "maybe", "no"], details=details)
        ledger.add(round_name, host, self.name, "confirm_ask", f"Asked {self.name} to confirm",
                   shared={"plan": plan.label})
        answer = (await self._ask(q)).strip().lower()
        if answer not in ("yes", "maybe", "no"):
            answer = "maybe"
        ledger.add(round_name, self.name, host, "confirm_answer", f"{self.name} said {answer}",
                   shared={"answer": answer}, human=True)
        return answer


def end_of(start: datetime, minutes: int) -> datetime:
    return start + timedelta(minutes=minutes)
