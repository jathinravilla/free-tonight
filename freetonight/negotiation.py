"""One planning run: agents share availability and filters, negotiate a slot and a place, then confirm.

The host is the agent of whoever typed /plan. It only ever sees what other agents put on the table.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Awaitable, Callable

from .agent import PersonalAgent, Plan
from .ledger import Ledger
from .timeutil import Interval, at, contains, fmt_clock, fmt_slot
from .venues import FOOD_KINDS, GroupFilters, Venue, rank


@dataclass(frozen=True)
class Activity:
    name: str
    minutes: int
    ideal: time
    earliest: time
    latest: time  # latest end; time(0, 0) means midnight
    emoji: str


ACTIVITIES = {
    "dinner": Activity("dinner", 90, time(19, 0), time(17, 0), time(22, 30), "🍜"),
    "lunch": Activity("lunch", 60, time(12, 30), time(11, 0), time(15, 0), "🥪"),
    "coffee": Activity("coffee", 45, time(15, 0), time(8, 0), time(23, 0), "☕"),
    "study": Activity("study", 120, time(18, 0), time(9, 0), time(0, 0), "📚"),
    "movie": Activity("movie", 150, time(20, 0), time(17, 0), time(0, 0), "🎬"),
    "games": Activity("games", 120, time(20, 0), time(12, 0), time(0, 0), "🎮"),
}

Progress = Callable[[str, str], Awaitable[None]]
Poll = Callable[[list[str]], Awaitable[int]]


@dataclass
class Candidate:
    start: datetime
    end: datetime
    attendees: list[str]
    missing: list[str]


@dataclass
class PlanResult:
    status: str  # "locked", "no_overlap", "fell_through"
    plan: Plan | None
    confirmed: list[str] = field(default_factory=list)
    maybe: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    cost_pp: float = 0.0
    filters: GroupFilters | None = None
    near_misses: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ledger: Ledger = field(default_factory=Ledger)


async def _noop_progress(stage: str, detail: str) -> None:
    return None


class PlanSession:
    def __init__(self, agents: list[PersonalAgent], host: str, activity: str, dates: list[date],
                 venues: list[Venue], *, minutes: int | None = None, now: datetime | None = None,
                 progress: Progress | None = None, poll: Poll | None = None, step_minutes: int = 15):
        if activity not in ACTIVITIES:
            raise ValueError(f"Unknown activity {activity!r}. Try: {', '.join(ACTIVITIES)}")
        if len(agents) < 2:
            raise ValueError("Planning needs at least 2 people.")
        self.agents = agents
        self.by_name = {a.name: a for a in agents}
        self.host = host
        self.activity = ACTIVITIES[activity]
        self.minutes = minutes or self.activity.minutes
        self.dates = dates
        self.venues = venues
        self.now = now or datetime.now()
        self.progress = progress or _noop_progress
        self.poll = poll
        self.step = timedelta(minutes=step_minutes)
        self.ledger = Ledger()

    # ---- Round 1 and 2: what goes on the table ---------------------------------------

    def _gather_availability(self) -> dict[str, list[Interval]]:
        return {a.name: a.share_availability(self.dates, self.ledger, "1 · Availability") for a in self.agents}

    def _gather_filters(self) -> GroupFilters:
        merged = GroupFilters()
        food = self.activity.name in FOOD_KINDS
        diet: set[str] = set()
        allergy: set[str] = set()
        for a in self.agents:
            f = a.private_filters()
            if food:  # diet and allergies stay inside each agent unless the plan involves food
                diet.update(f["diet"])
                allergy.update(f["allergy_safe"])
            merged.budget_cap = min(merged.budget_cap, f["budget_cap"])
            merged.max_walk_min = min(merged.max_walk_min, f["max_walk_min"])
        merged.diet, merged.allergy_safe = sorted(diet), sorted(allergy)
        self.ledger.add(
            "2 · Filters", "all agents", "table", "filters",
            f"Agents pooled their filters anonymously ({len(self.agents)} agents)",
            shared={**({"needs_options_for": merged.diet or ["nothing special"],
                       "must_avoid": merged.allergy_safe or ["nothing"]} if food else {}),
                    "price_cap_per_person": f"${merged.budget_cap:.0f}",
                    "max_walk": f"{merged.max_walk_min} min"},
            withheld=["whose filter is whose", "exact budgets (only $5 steps leave each agent)",
                      "why anyone has a filter"] + ([] if food else ["diet and allergies (no food at this plan)"]),
        )
        return merged

    # ---- Round 3: time ----------------------------------------------------------------------------

    def _candidates(self, avail: dict[str, list[Interval]]) -> list[Candidate]:
        act = self.activity
        span = timedelta(minutes=self.minutes)
        out: list[Candidate] = []
        for d in self.dates:
            start, latest_end = at(d, act.earliest), at(d, act.latest)
            if latest_end <= start:
                latest_end += timedelta(days=1)
            earliest_ok = self.now + timedelta(minutes=30)
            while start + span <= latest_end:
                if start >= earliest_ok:
                    end = start + span
                    who = [n for n, ivs in avail.items() if contains(ivs, start, end)]
                    out.append(Candidate(start, end, who, [n for n in avail if n not in who]))
                start += self.step
        ideal_cache: dict[date, datetime] = {}

        def score(c: Candidate):
            d = c.start.date()
            ideal = ideal_cache.setdefault(d, at(d, act.ideal))
            drift = abs((c.start - ideal).total_seconds()) / 60
            days_out = (d - self.dates[0]).days
            return (-len(c.attendees), drift + days_out * 20, c.start)

        out.sort(key=score)
        return out

    async def _negotiate_time(self, cands: list[Candidate]) -> list[Candidate]:
        """Try to bring missing people in on the strongest slots by asking about flexible commitments."""
        if not cands or not cands[0].attendees:
            return cands
        everyone = len(self.agents)
        if len(cands[0].attendees) == everyone:
            return cands
        best_count = len(cands[0].attendees)
        # Each missing agent says privately whether it's worth asking its owner (yes/no only).
        # Rank slots by how many people they could reach, keeping the time preference as the tiebreak.
        pool = [c for c in cands[:200] if len(c.attendees) >= max(2, best_count - 2) and len(c.missing) <= 2]
        reach = {id(c): len(c.attendees) + sum(1 for n in c.missing
                                                if self.by_name[n].can_relax_for(c.start, c.end)) for c in pool}
        pool.sort(key=lambda c: -reach[id(c)])
        shortlist: list[Candidate] = []
        for c in pool:
            if reach[id(c)] <= best_count:
                break
            if all(abs((c.start - s.start).total_seconds()) >= 3600 for s in shortlist):
                shortlist.append(c)
            if len(shortlist) == 3:
                break
        if not shortlist:
            return cands
        await self.progress("negotiating", f"{everyone - best_count} can't make the best time; asking about flexible plans")
        for c in shortlist:
            for name in list(c.missing):
                agent = self.by_name[name]
                if not agent.can_relax_for(c.start, c.end):
                    self.ledger.add("3 · Time", self.host, name, "relax_skip",
                                    f"{name}'s agent can't stretch for {fmt_slot(c.start, c.end)}",
                                    shared={"answer": "no"}, withheld=["why not"])
                    continue
                yes = await agent.ask_relax(self.activity.name, c.start, c.end, len(c.attendees),
                                            self.ledger, "3 · Time", self.host)
                if yes:
                    c.attendees.append(name)
                    c.missing.remove(name)
            if len(c.attendees) == everyone:
                break
        cands.sort(key=lambda c: -len(c.attendees))  # stable: keeps time preference inside each count
        return cands

    # ---- Round 4: place -------------------------------------------------------------------------

    def _pick_venue(self, c: Candidate, filters: GroupFilters) -> tuple[Venue | None, list[str]]:
        ranked = rank(self.venues, self.activity.name, filters, c.start, c.end)
        near = [f"{v.name} (${v.price_pp:.0f}, {v.walk_min} min walk; misses {', '.join(b)})"
                for v, b in ranked if b][:3]
        for v, broken in ranked:
            if broken:
                break
            plan = Plan(self.activity.name, c.start, c.end, v, list(c.attendees))
            verdicts = {n: self.by_name[n].judge(plan) for n in c.attendees}
            declines = {n: why for n, (ok, why) in verdicts.items() if ok == "decline"}
            self.ledger.add("4 · Place", self.host, "table", "proposal",
                            f"Proposed {v.name} for {fmt_slot(c.start, c.end)}",
                            shared={"place": v.name, "price_pp": f"${v.price_pp:.0f}",
                                    "walk": f"{v.walk_min} min", "slot": fmt_slot(c.start, c.end)})
            for n, (ok, why) in verdicts.items():
                self.ledger.add("4 · Place", n, "table", "response",
                                f"{n}'s agent: {ok}" + (f" ({why})" if why else ""),
                                shared={"answer": ok, **({"reason": why} if why else {})},
                                withheld=["the private detail behind it"] if why else [])
            if not declines:
                return v, near
        return None, near

    # ---- Full run ---------------------------------------------------------------------------

    async def run(self) -> PlanResult:
        act = self.activity
        await self.progress("gathering", "Agents are sharing free/busy times")
        avail = self._gather_availability()
        filters = self._gather_filters()
        await self.progress("matching", "Finding times that work")
        cands = self._candidates(avail)
        if not cands or len(cands[0].attendees) < 2:
            self.ledger.add("3 · Time", self.host, "table", "note", "No time works for 2 or more people")
            return PlanResult("no_overlap", None, unavailable=list(self.by_name), filters=filters,
                              notes=["Nobody's free windows overlap for this activity and time range."],
                              ledger=self.ledger)
        top = cands[0]
        self.ledger.add("3 · Time", self.host, "table", "proposal",
                        f"Best time from shared windows: {fmt_slot(top.start, top.end)} "
                        f"({len(top.attendees)}/{len(self.agents)} free)",
                        shared={"slot": fmt_slot(top.start, top.end), "free_count": len(top.attendees)})
        cands = await self._negotiate_time(cands)

        await self.progress("placing", "Matching places to everyone's filters")
        options: list[tuple[Candidate, Venue | None, list[str]]] = []
        best_count = len(cands[0].attendees)
        seen_days: set[tuple[date, int]] = set()
        for c in cands[:40]:
            if len(c.attendees) < best_count:
                break
            key = (c.start.date(), c.start.hour // 2)
            if key in seen_days:
                continue
            seen_days.add(key)
            venue, near = self._pick_venue(c, filters)
            options.append((c, venue, near))
            if len([o for o in options if o[1]]) >= 3:
                break
        with_place = [o for o in options if o[1]]
        chosen = with_place[0] if with_place else options[0]

        if self.poll and len(with_place) > 1:
            labels = [f"{fmt_slot(c.start, c.end)} · {v.name} (${v.price_pp:.0f})" for c, v, _ in with_place]
            await self.progress("polling", "Two or more options tie, so the group gets a vote")
            idx = await self.poll(labels)
            idx = idx if 0 <= idx < len(with_place) else 0
            chosen = with_place[idx]
            self.ledger.add("5 · Vote", "group", "table", "poll", f"Group voted: {labels[idx]}",
                            shared={"options": labels, "winner": labels[idx]}, human=True)

        c, venue, near = chosen
        plan = Plan(act.name, c.start, c.end, venue, list(c.attendees))
        cost = venue.price_pp if venue else 0.0
        await self.progress("confirming", f"Waiting on {len(plan.attendees)} confirmations")
        answers = await asyncio.gather(*[self.by_name[n].confirm(plan, cost, self.ledger, "6 · Confirm", self.host)
                                         for n in plan.attendees])
        confirmed = [n for n, a in zip(plan.attendees, answers) if a == "yes"]
        maybe = [n for n, a in zip(plan.attendees, answers) if a == "maybe"]
        declined = [n for n, a in zip(plan.attendees, answers) if a == "no"]
        plan.attendees = confirmed + maybe
        unavailable = [n for n in self.by_name if n not in c.attendees]
        notes = []
        if not venue:
            notes.append("No place fits every filter at that time, so pick one in chat.")
        if len(confirmed) + len(maybe) < 2:
            self.ledger.add("6 · Confirm", self.host, "table", "lock", "Plan fell through: fewer than 2 said yes")
            return PlanResult("fell_through", plan, confirmed, maybe, declined, unavailable, cost, filters,
                              near, notes, self.ledger)
        self.ledger.add("6 · Confirm", self.host, "table", "lock", f"Locked: {plan.label}",
                        shared={"plan": plan.label, "going": confirmed, "maybe": maybe})
        await self.progress("locked", plan.label)
        return PlanResult("locked", plan, confirmed, maybe, declined, unavailable, cost, filters, near, notes,
                          self.ledger)


def describe_result(r: PlanResult) -> str:
    if r.status == "no_overlap":
        return "No overlap this time. Try a wider range like 'this week' or a shorter activity."
    p = r.plan
    lines = [p.label]
    if r.confirmed:
        lines.append("Going: " + ", ".join(r.confirmed))
    if r.maybe:
        lines.append("Maybe: " + ", ".join(r.maybe))
    if r.declined:
        lines.append("Can't: " + ", ".join(r.declined))
    if r.unavailable:
        lines.append("Busy then: " + ", ".join(r.unavailable))
    lines.append(f"About ${r.cost_pp:.0f} each" if r.cost_pp else "Free")
    return "\n".join(lines + r.notes)


def fmt_time(t: datetime) -> str:
    return fmt_clock(t)
