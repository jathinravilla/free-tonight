"""Free Tonight on Discord.

Each member gets a private agent (set up in a pop-up only they see). In the group channel, /plan starts a run:
friends tap "I'm in", the host taps Start, agents negotiate, owners answer privately in DMs, and one plan card
updates in place until the plan is locked.

Run:  DISCORD_TOKEN=... python -m freetonight.discord_bot
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import datetime, time, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from . import calendar_feed, cards, ledger_html
from .agent import OwnerQuestion, PersonalAgent
from .llm import parse_schedule_smart
from .negotiation import ACTIVITIES, PlanResult, PlanSession
from .profile import (BLOCKS, Profile, busy_from_free, clock_ok, parse_allergies,
                      parse_diet, parse_money, upcoming_dates)
from .timeutil import DAYS
from .split import format_split, split_bill, split_even
from .store import ProfileStore
from .timeutil import fmt_clock
from .venues import FOOD_KINDS, Venue, load_venues

log = logging.getLogger("freetonight")

ORANGE = discord.Colour.from_rgb(226, 87, 43)
GREEN = discord.Colour.from_rgb(15, 123, 79)
PURPLE = discord.Colour.from_rgb(122, 75, 224)
GRAY = discord.Colour.from_rgb(107, 104, 98)

OWNER_TIMEOUT = int(os.environ.get("FREETONIGHT_ANSWER_TIMEOUT", "600"))
POLL_SECONDS = int(os.environ.get("FREETONIGHT_POLL_SECONDS", "180"))
JOIN_SECONDS = 30 * 60
MAX_MEMBERS = 25  # each member gets DMs, so cap how many one plan can reach

WHEN_CHOICES = [
    app_commands.Choice(name="tonight", value="today"),
    app_commands.Choice(name="tomorrow", value="tomorrow"),
    app_commands.Choice(name="weeknights", value="weeknights"),
    app_commands.Choice(name="this week", value="this week"),
    app_commands.Choice(name="this weekend", value="weekend"),
]
ACTIVITY_CHOICES = [app_commands.Choice(name=f"{a.emoji} {k}", value=k) for k, a in ACTIVITIES.items()]

STAGES = [
    ("gathering", "Agents share free/busy times"),
    ("matching", "Find times that work"),
    ("negotiating", "Ask about flexible plans, privately"),
    ("placing", "Match places to everyone's filters"),
    ("polling", "Group vote on ties"),
    ("locked", "Locked"),
]


# ---------------------------------------------------------------- profile setup

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# One choice per real-life need; each maps onto the diet tags and allergens venues are matched against.
FOOD_OPTIONS = [
    ("Vegetarian", {"diet": ["vegetarian"]}), ("Vegan", {"diet": ["vegan"]}),
    ("Halal", {"diet": ["halal"]}), ("Kosher", {"diet": ["kosher"]}),
    ("No nuts", {"allergies": ["peanut", "tree-nut"]}), ("No seafood", {"allergies": ["shellfish", "fish"]}),
    ("No gluten", {"allergies": ["gluten"]}), ("No dairy", {"allergies": ["dairy"]}),
    ("No eggs", {"allergies": ["egg"]}), ("No soy or sesame", {"allergies": ["soy", "sesame"]}),
]
BUDGETS = [10, 20, 30, 50]


def _loose(text: str, parse) -> tuple[list[str], list[str]]:
    """Read a comma list, keeping what's known and returning what isn't, instead of failing."""
    ok, unknown = [], []
    for part in re.split(r"[,;/\n]+|\band\b", text or ""):
        if not part.strip():
            continue
        try:
            ok += [x for x in parse(part) if x not in ok]
        except ValueError:
            unknown.append(part.strip())
    return ok, unknown


class SetupPanel(discord.ui.View):
    """Tap-to-pick setup: free times, food, budget, with a calendar link and typing as options."""

    def __init__(self, bot: "FreeTonightBot", existing: Profile | None = None, after=None):
        super().__init__(timeout=900)
        self.bot, self.existing, self.after = bot, existing, after
        blocks = list(existing.free_blocks) if existing else []
        self.days = sorted({DAYS.index(b.split(" ", 1)[0]) for b in blocks})
        self.times = [t for t in BLOCKS if any(b.endswith(" " + t) for b in blocks)]
        have = set(existing.diet + existing.allergies) if existing else set()
        self.food = [label for label, m in FOOD_OPTIONS
                     if have and set(m.get("diet", []) + m.get("allergies", [])) <= have]
        self.budget = existing.budget if existing else 20.0
        self.calendar_url = existing.calendar_url if existing else ""

        days = discord.ui.Select(placeholder="1. Which days are you usually free?", min_values=0, max_values=7,
                                 row=0, options=[discord.SelectOption(label=n, value=str(i), default=i in self.days)
                                                 for i, n in enumerate(DAY_NAMES)])
        days.callback = self._on_days
        times = discord.ui.Select(placeholder="2. What times on those days?", min_values=0, max_values=len(BLOCKS),
                                  row=1, options=[discord.SelectOption(label=t.capitalize(), value=t,
                                                                       description=_block_hours("x " + t),
                                                                       default=t in self.times) for t in BLOCKS])
        times.callback = self._on_times
        food = discord.ui.Select(placeholder="3. Food: anything you need or can't eat (optional)", min_values=0,
                                 max_values=len(FOOD_OPTIONS), row=2, options=[
                                     discord.SelectOption(label=label, value=label, default=label in self.food)
                                     for label, _ in FOOD_OPTIONS])
        food.callback = self._on_food
        for item in (days, times, food):
            self.add_item(item)
        budget = discord.ui.Select(placeholder="4. Most you'd spend per hangout", row=3, options=[
            discord.SelectOption(label=f"Up to ${b}" if b < 50 else "$50 or more", value=str(b),
                                 default=abs(self.budget - b) < 0.01) for b in BUDGETS])
        budget.callback = self._on_budget
        self.add_item(budget)

    @staticmethod
    def embed() -> discord.Embed:
        return discord.Embed(title="🔒 Set up your private agent", colour=PURPLE, description=(
            "Pick your days, then your times, then **Save**. Only you can see this, and only free/busy ever leaves "
            "your agent. Different times on different days? Use ⌨️ Type it instead.\n\n"
            "📅 **Use Google Calendar?** Link it and your busy times fill in by themselves.\n"
            "⌨️ Rather type? Write your week however you like."))

    @property
    def free(self) -> list[str]:
        return [f"{DAYS[d]} {t}" for d in sorted(self.days) for t in BLOCKS if t in self.times]

    async def _on_days(self, interaction: discord.Interaction):
        self.days = [int(v) for v in interaction.data.get("values", [])]
        await interaction.response.defer()

    async def _on_times(self, interaction: discord.Interaction):
        self.times = list(interaction.data.get("values", []))
        await interaction.response.defer()

    async def _on_food(self, interaction: discord.Interaction):
        self.food = list(interaction.data.get("values", []))
        await interaction.response.defer()

    async def _on_budget(self, interaction: discord.Interaction):
        self.budget = float(interaction.data.get("values", ["20"])[0])
        await interaction.response.defer()

    def build(self, user: discord.abc.User) -> Profile:
        base = self.existing or Profile(name=user.display_name)
        if self.days and not self.times:  # picked days but no times: free all day on those days
            self.times = list(BLOCKS)
        if self.free:
            commitments = busy_from_free(self.free)
        elif base.free_blocks:  # they un-picked everything
            commitments = []
        else:
            commitments = list(base.commitments)
        earliest = "08:00" if any(f.endswith("morning") for f in self.free) else base.earliest
        return Profile(name=user.display_name, user_id=str(user.id), commitments=commitments,
                       budget=self.budget, diet=self._food("diet"), allergies=self._food("allergies"),
                       max_walk_min=max(1, min(60, base.max_walk_min)), earliest=earliest, latest=base.latest,
                       no_go_days=list(base.no_go_days), free_blocks=self.free,
                       calendar_url=self.calendar_url)

    def _food(self, kind: str) -> list[str]:
        picked = dict(FOOD_OPTIONS)
        out: list[str] = []
        for label in self.food:
            out += [x for x in picked.get(label, {}).get(kind, []) if x not in out]
        return out

    @discord.ui.button(label="Save", emoji="✅", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button):
        profile = self.build(interaction.user)
        self.bot.store.put(profile)
        self.stop()
        embed = profile_embed(profile, "🔒 Saved privately")
        if not (profile.commitments or profile.calendar_url):
            embed.set_footer(text="Tip: pick your free times or link your calendar, or your agent assumes you're "
                                  "free all week. /setup any time to change it.")
        await interaction.response.edit_message(embed=embed, view=None)
        if self.after:
            await self.after(interaction, profile)

    @discord.ui.button(label="Link Google Calendar", emoji="📅", style=discord.ButtonStyle.primary, row=4)
    async def link(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(CalendarModal(self))

    @discord.ui.button(label="Type it instead", emoji="⌨️", style=discord.ButtonStyle.secondary, row=4)
    async def typed(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SetupModal(self.bot, self.build(interaction.user), after=self.after))


def _block_hours(slot: str) -> str:
    start, end = BLOCKS[slot.split(" ", 1)[1]]
    return f"{fmt_clock(time.fromisoformat(start))}–{'midnight' if end == '23:59' else fmt_clock(time.fromisoformat(end))}"


class CalendarModal(discord.ui.Modal, title="Link your calendar"):
    url = discord.ui.TextInput(label="Secret address in iCal format", max_length=500, required=False,
                               placeholder="https://calendar.google.com/calendar/ical/.../basic.ics")

    def __init__(self, panel: SetupPanel):
        super().__init__()
        self.panel = panel
        if panel.calendar_url:
            self.url.default = panel.calendar_url

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = self.url.value.strip()
        if not text:
            self.panel.calendar_url = ""
            await interaction.followup.send("Calendar unlinked. Press **Save** to keep that.", ephemeral=True)
            return
        try:
            url = calendar_feed.check_url(text)
            today = datetime.now().date()
            busy = await asyncio.to_thread(calendar_feed.busy_from_link, url,
                                           [today + timedelta(days=i) for i in range(7)])
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}\n\n{calendar_feed.HELP}", ephemeral=True)
            return
        self.panel.calendar_url = url
        await interaction.followup.send(
            f"📅 Linked. I see **{len(busy)} busy blocks** in the next 7 days. Your agent re-reads it every time "
            "you plan. Only start and end times are read; event names are never read or stored.\n"
            "Now press **Save** on the setup card.", ephemeral=True)


class SetupModal(discord.ui.Modal, title="Type your week"):
    schedule = discord.ui.TextInput(
        label="When are you busy? Any format", style=discord.TextStyle.paragraph,
        placeholder="MWF 9-10:15 CS 201\nTue 6-8pm study group (flex 30)\nThu 5-7:30pm work shift",
        required=False, max_length=1500)
    budget = discord.ui.TextInput(label="Most you'd spend per hangout ($)", placeholder="20", max_length=10,
                                  required=False)
    food = discord.ui.TextInput(label="Food needs or allergies (optional)", required=False, max_length=150,
                                placeholder="vegetarian, halal, no peanuts...")
    walk = discord.ui.TextInput(label="Max walk in minutes (optional)", placeholder="20", required=False,
                                max_length=3)
    hours = discord.ui.TextInput(label="Hangout hours (optional)", placeholder="11am-11:30pm", required=False,
                                 max_length=30)

    def __init__(self, bot: "FreeTonightBot", existing: Profile | None = None, after=None):
        super().__init__()
        self.bot, self.existing = bot, existing
        self.after = after  # coroutine(interaction, profile) run after a successful save
        if existing:
            if not existing.free_blocks:
                self.schedule.default = "\n".join(
                    f"{'/'.join(DAYS[d] for d in c.days)} {c.start}-{c.end} {c.label}"
                    + (f" (flex {c.flex_minutes})" if c.flex_minutes else "")
                    for c in existing.commitments if not c.on_date)[:1500]
            self.budget.default = f"{existing.budget:g}"
            self.food.default = ", ".join(existing.diet + [f"no {a}" for a in existing.allergies])
            self.walk.default = str(existing.max_walk_min)
            self.hours.default = f"{existing.earliest}-{existing.latest}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Reading a messy schedule can call Claude, which takes longer than Discord's 3-second reply window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        base = self.existing or Profile(name=interaction.user.display_name)
        notes = []
        try:
            if self.schedule.value.strip():
                commitments, how, skipped = await asyncio.to_thread(parse_schedule_smart, self.schedule.value)
                free_blocks: list[str] = []
            else:
                commitments, how, skipped, free_blocks = list(base.commitments), "", [], list(base.free_blocks)
            if skipped:
                notes.append("Couldn't read: " + "; ".join(f"“{s[:60]}”" for s in skipped[:5])
                             + ". Try like `Tue 6-8pm gym`.")
            food = re.sub(r"\b(no|not|allergic to|allergy)\b", "", self.food.value, flags=re.I)
            diet, unknown_d = _loose(food, parse_diet)
            allergies, unknown_a = _loose(food, parse_allergies)
            unknown = [u for u in unknown_d if u in unknown_a]
            if unknown:
                notes.append("Skipped food I don't know: " + ", ".join(unknown[:5]))
            profile = Profile(name=interaction.user.display_name, user_id=str(interaction.user.id),
                              commitments=commitments, free_blocks=free_blocks,
                              budget=parse_money(self.budget.value) if self.budget.value.strip() else base.budget,
                              diet=diet, allergies=allergies, max_walk_min=base.max_walk_min,
                              earliest=base.earliest, latest=base.latest, no_go_days=list(base.no_go_days),
                              calendar_url=base.calendar_url)
            if self.walk.value.strip():
                profile.max_walk_min = int(parse_money(self.walk.value))
                if not 1 <= profile.max_walk_min <= 60:
                    raise ValueError(f"Walk limit should be 1 to 60 minutes, not {profile.max_walk_min}.")
            if self.hours.value.strip():
                a, _, b = self.hours.value.replace("–", "-").replace(" to ", "-").partition("-")
                profile.earliest, profile.latest = clock_ok(a), clock_ok(b)
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}\nNothing was saved.", ephemeral=True,
                                            view=RetrySetupView(self.bot, self))
            return
        self.bot.store.put(profile)
        embed = profile_embed(profile, "🔒 Saved privately")
        if how == "claude":
            notes.append("Claude read your schedule. Check it looks right.")
        if notes:
            embed.set_footer(text="\n".join(notes)[:2000])
        await interaction.followup.send(embed=embed, ephemeral=True)
        if self.after:
            await self.after(interaction, profile)


class RetrySetupView(discord.ui.View):
    def __init__(self, bot: "FreeTonightBot", modal: SetupModal):
        super().__init__(timeout=600)
        self.bot, self.modal = bot, modal

    @discord.ui.button(label="Fix and try again", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, _: discord.ui.Button):
        fresh = SetupModal(self.bot, self.modal.existing, after=self.modal.after)
        for field in ("schedule", "budget", "food", "walk", "hours"):
            getattr(fresh, field).default = getattr(self.modal, field).value
        await interaction.response.send_modal(fresh)


async def send_setup(interaction: discord.Interaction, bot: "FreeTonightBot", after=None) -> None:
    """Show the tap-to-pick setup card, privately."""
    await interaction.response.send_message(embed=SetupPanel.embed(), ephemeral=True,
                                            view=SetupPanel(bot, bot.store.get(str(interaction.user.id)), after))


def _free_summary(blocks: list[str]) -> str:
    by_day: dict[str, list[str]] = {}
    for b in blocks:
        day, part = b.split(" ", 1)
        by_day.setdefault(day, []).append(part)
    return "\n".join(f"{day}: {', '.join(parts)}" for day, parts in by_day.items())


def profile_embed(p: Profile, title: str) -> discord.Embed:
    e = discord.Embed(title=title, colour=PURPLE,
                      description="Only you can see this. Your agent shares free/busy times, pooled filters "
                                  "and yes/no answers, never these details.")
    if p.free_blocks:
        name, sched = "Usually free", _free_summary(p.free_blocks)
    else:
        name, sched = "Busy", "\n".join(c.describe() for c in p.commitments) or "Nothing yet"
    if len(sched) > 1000:  # Discord rejects fields over 1024 characters
        sched = sched[:990].rsplit("\n", 1)[0] + "\n…and more"
    e.add_field(name=name, value=sched, inline=False)
    if p.calendar_url:
        e.add_field(name="Calendar", value="📅 Linked (free/busy only)", inline=False)
    e.add_field(name="Budget", value=f"up to ${p.budget:.0f}")
    e.add_field(name="Walk", value=f"up to {p.max_walk_min} min")
    e.add_field(name="Food", value=", ".join(p.diet + [f"no {a}" for a in p.allergies]) or "anything")
    return e


class ProfileView(discord.ui.View):
    def __init__(self, bot: "FreeTonightBot", profile: Profile):
        super().__init__(timeout=600)
        self.bot, self.profile = bot, profile

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button):
        await send_setup(interaction, self.bot)

    @discord.ui.button(label="Delete my data", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.bot.store.delete(str(interaction.user.id))
        await interaction.response.edit_message(content="🗑️ Deleted. Your agent forgot everything.", embed=None,
                                                view=None)


# ---------------------------------------------------------------- asking an owner privately

class AnswerView(discord.ui.View):
    def __init__(self, owner_id: int, options: list[str], future: asyncio.Future):
        super().__init__(timeout=OWNER_TIMEOUT)
        self.owner_id, self.future = owner_id, future
        styles = {"yes": discord.ButtonStyle.success, "no": discord.ButtonStyle.danger,
                  "maybe": discord.ButtonStyle.secondary}
        labels = {"yes": "Yes", "no": "No", "maybe": "Maybe"}
        for opt in options:
            b = discord.ui.Button(label=labels.get(opt, opt.title()), style=styles.get(opt, discord.ButtonStyle.primary))
            b.callback = self._make(opt)
            self.add_item(b)

    def _make(self, opt: str):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("This question is for someone else.", ephemeral=True)
                return
            if not self.future.done():
                self.future.set_result(opt)
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(content=f"{interaction.message.content}\n\n**You said {opt}.**",
                                                    view=self)
            self.stop()
        return cb


async def ask_member(member: discord.abc.User, fallback: discord.abc.Messageable, q: OwnerQuestion,
                     default: str) -> str:
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    view = AnswerView(member.id, q.options, fut)
    text = f"**Free Tonight** · {q.text}\n" + "\n".join(f"> {d}" for d in q.details)
    try:
        await member.send(text, view=view)
    except (discord.Forbidden, discord.HTTPException):
        # DMs closed: ask in the channel; only this member can press the buttons.
        await fallback.send(f"{member.mention} (your DMs are off, so asking here) · {q.text}", view=view)
    try:
        return await asyncio.wait_for(fut, timeout=OWNER_TIMEOUT)
    except asyncio.TimeoutError:
        return default


# ---------------------------------------------------------------- the plan card

class PlanRun:
    """One /plan in one channel: join phase, negotiation, result."""

    def __init__(self, bot: "FreeTonightBot", channel: discord.abc.Messageable, host: discord.Member,
                 activity: str, when: str, minutes: int | None, place: str | None = None):
        self.bot, self.channel, self.host = bot, channel, host
        self.activity, self.when, self.minutes = activity, when, minutes
        self.place = custom_place(place, activity) if place and place.strip() else None
        self.members: dict[int, discord.abc.User] = {host.id: host}
        self.message: discord.Message | None = None
        self.stage = "joining"
        self.detail = ""
        self.result: PlanResult | None = None
        self.started = False
        self.task: asyncio.Task | None = None
        self.created = datetime.now()

    @property
    def act(self):
        return ACTIVITIES[self.activity]

    def embed(self) -> discord.Embed:
        a = self.act
        when_label = next((c.name for c in WHEN_CHOICES if c.value == self.when), self.when)
        if self.result and self.result.status == "locked":
            return locked_embed(self.result)
        if self.result:
            e = discord.Embed(title=f"{a.emoji} No plan this time", colour=GRAY,
                              description=("Nobody's free times overlap. Try a wider range or a shorter hangout."
                                           if self.result.status == "no_overlap"
                                           else "Fewer than 2 people confirmed."))
            return e
        e = discord.Embed(title=f"{a.emoji} {a.name.title()} {when_label}?", colour=ORANGE)
        names = ", ".join(m.display_name for m in self.members.values())
        if self.place:
            e.add_field(name="Where", value=f"📍 {self.place.name}", inline=False)
        e.add_field(name=f"In ({len(self.members)})", value=names, inline=False)
        if self.stage == "joining":
            e.description = (f"Tap **I'm in**. Your agent shares only when you're free, never why.\n"
                             f"{self.host.display_name} taps **Start** when everyone's in.")
        else:
            order = [s for s, _ in STAGES]
            cur = order.index(self.stage) if self.stage in order else 0
            lines = []
            for i, (key, label) in enumerate(STAGES[:-1]):
                mark = "✅" if i < cur else ("⏳" if i == cur else "▫️")
                lines.append(f"{mark} {label}")
            e.description = "\n".join(lines) + (f"\n\n*{self.detail}*" if self.detail else "")
        e.set_footer(text="Free Tonight · private agents, shared plans")
        return e

    async def refresh(self) -> None:
        if self.message:
            try:
                await self.message.edit(embed=self.embed(), view=self.view())
            except discord.HTTPException:
                log.exception("Couldn't update the plan card")

    def view(self) -> discord.ui.View | None:
        if self.stage == "joining":
            return JoinView(self)
        if self.result and self.result.status == "locked":
            return LockedView(self.bot, self.result)
        return None

    async def progress(self, stage: str, detail: str) -> None:
        self.stage, self.detail = stage, detail
        await self.refresh()

    async def start(self) -> None:
        self.started = True
        self.stage = "gathering"
        await self.refresh()
        agents = []
        now = datetime.now()
        dates = upcoming_dates(now.date(), self.when)
        profiles = {uid: self.bot.store.get(str(uid)) for uid in self.members}
        await self._read_calendars(profiles, dates)
        for uid, member in self.members.items():
            profile = profiles[uid]
            profile.name = member.display_name

            def make_ask(m=member):
                async def ask(q: OwnerQuestion) -> str:
                    return await ask_member(m, self.channel, q, "no" if q.kind == "relax" else "maybe")
                return ask
            agents.append(PersonalAgent(profile, make_ask()))
        session = PlanSession(agents, self.host.display_name, self.activity,
                              dates, self.bot.venues, minutes=self.minutes,
                              now=now, progress=self.progress, poll=self.poll, confirm=False,
                              auto_place=False, place=self.place)
        try:
            self.result = await session.run()
        except Exception:
            log.exception("Planning failed")
            await self.channel.send("Something went wrong while planning. Try /plan again.")
            self.bot.runs.pop(getattr(self.channel, "id", 0), None)
            return
        self.stage = "locked" if self.result.status == "locked" else "done"
        await self.refresh()
        self.bot.runs.pop(getattr(self.channel, "id", 0), None)
        await self.channel.send(self.announcement())

    def announcement(self) -> str:
        """The verdict, posted as soon as the agents finish: does it work out, and who can make it."""
        r = self.result
        if r.status != "locked":
            return ("❌ **It doesn't work out this time.** There's no time when 2 or more of you are free. "
                    "Try `/plan` with *this week* or a shorter hangout.")
        going = [m.mention for m in self.members.values() if m.display_name in r.confirmed]
        v = r.plan.venue
        lines = [f"✅ **It works out!** {r.plan.kind.title()} · {cards.when_line(r.plan.start, r.plan.end)}",
                 f"📍 {v.name} · {v.distance}" if v else "📍 Place not picked yet: tap **Set place** on the card.",
                 f"🙋 Going ({len(going)}): " + " ".join(going)]
        if r.unavailable:
            lines.append(f"🚫 Busy then ({len(r.unavailable)}): " + ", ".join(r.unavailable))
        lines.append("Your agents checked everyone's schedule, so no need to double-check. "
                     "Plans change? Tap **Can't go** on the card.")
        return "\n".join(lines)

    async def _read_calendars(self, profiles: dict, dates) -> None:
        """Add fresh busy times from linked calendars (this plan only; nothing is saved)."""
        async def one(uid, p):
            try:
                p.commitments += await asyncio.to_thread(calendar_feed.busy_from_link, p.calendar_url, dates)
            except Exception as exc:  # a broken link shouldn't stop the plan; use the saved schedule
                log.warning("Calendar read failed for one member: %s", exc)
                member = self.members.get(uid)
                if member:
                    try:
                        await member.send("📅 I couldn't read your linked calendar this time, so I used your saved "
                                          "free times. Run /setup to re-link it.")
                    except discord.HTTPException:
                        pass
        await asyncio.gather(*[one(uid, p) for uid, p in profiles.items() if p and p.calendar_url])

    async def poll(self, options: list[str]) -> int:
        view = PollView(self, options)
        msg = await self.channel.send(embed=view.embed(), view=view)
        view.message = msg
        try:
            await asyncio.wait_for(view.done.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
        winner = view.winner()
        for item in view.children:
            item.disabled = True
        await msg.edit(embed=view.embed(final=winner), view=view)
        return winner


class JoinView(discord.ui.View):
    def __init__(self, run: PlanRun):
        super().__init__(timeout=JOIN_SECONDS)
        self.run = run

    async def on_timeout(self) -> None:
        # Nobody pressed Start: free the channel so /plan works again.
        run = self.run
        if run.started or run.bot.runs.get(getattr(run.channel, "id", 0)) is not run:
            return
        run.bot.runs.pop(getattr(run.channel, "id", 0), None)
        if run.message:
            try:
                await run.message.edit(content="This plan expired before anyone pressed Start. Run /plan again.",
                                       embed=None, view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="I'm in", style=discord.ButtonStyle.success, emoji="🙋")
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.run.started:
            await interaction.response.send_message("Planning already started.", ephemeral=True)
            return
        if interaction.user.id not in self.run.members and len(self.run.members) >= MAX_MEMBERS:
            await interaction.response.send_message(f"This plan is full ({MAX_MEMBERS} people).", ephemeral=True)
            return
        if not self.run.bot.store.get(str(interaction.user.id)):
            async def after(i: discord.Interaction, profile: Profile):
                if self.run.started or len(self.run.members) >= MAX_MEMBERS:
                    await i.followup.send("Saved, but this plan is closed now. Start a new one with /plan.",
                                          ephemeral=True)
                    return
                self.run.members[i.user.id] = i.user
                await self.run.refresh()
            await send_setup(interaction, self.run.bot, after)
            return
        self.run.members[interaction.user.id] = interaction.user
        await interaction.response.edit_message(embed=self.run.embed(), view=self)

    @discord.ui.button(label="Out", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id == self.run.host.id:
            await interaction.response.send_message("You're hosting; use Cancel instead.", ephemeral=True)
            return
        self.run.members.pop(interaction.user.id, None)
        await interaction.response.edit_message(embed=self.run.embed(), view=self)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, emoji="▶️")
    async def go(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.run.host.id:
            await interaction.response.send_message(f"Only {self.run.host.display_name} can start.", ephemeral=True)
            return
        if len(self.run.members) < 2:
            await interaction.response.send_message("Need at least 2 people in.", ephemeral=True)
            return
        if self.run.started:
            await interaction.response.defer()
            return
        await interaction.response.defer()
        self.stop()
        self.run.task = asyncio.create_task(self.run.start())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.run.host.id:
            await interaction.response.send_message("Only the host can cancel.", ephemeral=True)
            return
        self.run.bot.runs.pop(interaction.channel_id, None)
        self.stop()
        await interaction.response.edit_message(content="Plan cancelled.", embed=None, view=None)


class PollView(discord.ui.View):
    def __init__(self, run: PlanRun, options: list[str]):
        super().__init__(timeout=POLL_SECONDS + 30)
        self.run, self.options = run, options
        self.votes: dict[int, int] = {}
        self.done = asyncio.Event()
        self.message: discord.Message | None = None
        for i, _ in enumerate(options):
            b = discord.ui.Button(label=f"Option {i + 1}", style=discord.ButtonStyle.primary)
            b.callback = self._make(i)
            self.add_item(b)

    def counts(self) -> list[int]:
        return [sum(1 for v in self.votes.values() if v == i) for i in range(len(self.options))]

    def winner(self) -> int:
        c = self.counts()
        return c.index(max(c)) if any(c) else 0

    def embed(self, final: int | None = None) -> discord.Embed:
        c = self.counts()
        lines = []
        for i, o in enumerate(self.options):
            bar = "█" * c[i] + "░" * (len(self.run.members) - c[i])
            crown = " 👑" if final == i else ""
            lines.append(f"**{i + 1}.** {o}{crown}\n`{bar}` {c[i]}")
        title = "🗳️ It's a tie. Vote!" if final is None else "🗳️ Vote closed"
        return discord.Embed(title=title, description="\n\n".join(lines), colour=ORANGE)

    def _make(self, i: int):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id not in self.run.members:
                await interaction.response.send_message("Only people in this plan can vote.", ephemeral=True)
                return
            self.votes[interaction.user.id] = i
            await interaction.response.edit_message(embed=self.embed(), view=self)
            if len(self.votes) >= len(self.run.members):
                self.done.set()
        return cb


def locked_embed(r: PlanResult) -> discord.Embed:
    p = r.plan
    a = ACTIVITIES[p.kind]
    v = p.venue
    where = (f"📍 **{v.name}** · {v.distance}" if v
             else "📍 **Place not picked yet.** Tap **Set place** to add one.")
    desc = f"{where}\n🕒 **{cards.when_line(p.start, p.end)}**"
    if len(r.confirmed) + len(r.maybe) < 2:
        title, colour = f"{a.emoji} Fell through", GRAY
        desc += "\nFewer than 2 people can go now. Start a new /plan to try another time."
    elif r.confirmed:
        title, colour = f"{a.emoji} {p.kind.title()} is on", GREEN
    else:
        title, colour = f"{a.emoji} {p.kind.title()} is penciled in", ORANGE
        desc += "\nEveryone said maybe. It's on the card so nobody forgets; tap 📅 if you're going."
    e = discord.Embed(title=title, colour=colour, description=desc)
    if v and not v.custom:
        e.add_field(name="Cost", value=f"about ${v.price_pp:.0f} each" if v.price_pp else "free", inline=True)
    if not v and r.suggestions:
        e.add_field(name="Ideas that fit everyone (just suggestions)", inline=False, value="\n".join(
            f"• {s.name} · {s.distance} · about ${s.price_pp:.0f}" for s in r.suggestions))
    going = ", ".join(r.confirmed) or "—"
    e.add_field(name=f"Going ({len(r.confirmed)})", value=going, inline=False)
    if r.maybe:
        e.add_field(name="Maybe", value=", ".join(r.maybe), inline=True)
    if r.declined:
        e.add_field(name="Can't go", value=", ".join(r.declined), inline=True)
    if r.unavailable:
        e.add_field(name="Busy then", value=", ".join(r.unavailable), inline=True)
    if p.kind in FOOD_KINDS and r.filters and (r.filters.diet or r.filters.allergy_safe):
        bits = [f"{d} options" for d in r.filters.diet] + [f"{x}-safe" for x in r.filters.allergy_safe]
        e.add_field(name="Respected for the group", value=", ".join(bits), inline=False)
    s = r.ledger.stats()
    e.set_footer(text=f"🔒 {s['fields_withheld']} things stayed private · {s['human_decisions']} human decisions")
    return e


class LockedView(discord.ui.View):
    def __init__(self, bot: "FreeTonightBot", result: PlanResult):
        super().__init__(timeout=None)
        self.bot, self.result = bot, result

    @discord.ui.button(label="Add to calendar", emoji="📅", style=discord.ButtonStyle.primary)
    async def cal(self, interaction: discord.Interaction, _: discord.ui.Button):
        data = cards.ics_invite(self.result).encode()
        await interaction.response.send_message("Open this to add it to your calendar.", ephemeral=True,
                                                file=discord.File(io.BytesIO(data), filename="free-tonight.ics"))

    @discord.ui.button(label="Copy card", emoji="📋", style=discord.ButtonStyle.secondary)
    async def copy(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Paste this into iMessage, GroupMe or WhatsApp:\n```\n" + cards.copy_card(self.result).replace("`", "'")
            + "\n```",
            ephemeral=True)

    @discord.ui.button(label="Privacy ledger", emoji="🔒", style=discord.ButtonStyle.secondary)
    async def ledger(self, interaction: discord.Interaction, _: discord.ui.Button):
        s = self.result.ledger.stats()
        html = ledger_html.render(self.result).encode()
        await interaction.response.send_message(
            f"{s['messages']} agent messages · {s['fields_shared']} fields shared · "
            f"{s['fields_withheld']} things kept private · {s['human_decisions']} human decisions.\n"
            "Open the file to see every message.", ephemeral=True,
            file=discord.File(io.BytesIO(html), filename="privacy-ledger.html"))

    @discord.ui.button(label="Split the bill", emoji="💸", style=discord.ButtonStyle.success)
    async def split(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SplitModal(self.result.confirmed + self.result.maybe))

    @discord.ui.button(label="Set place", emoji="📍", style=discord.ButtonStyle.primary, row=1)
    async def set_place(self, interaction: discord.Interaction, _: discord.ui.Button):
        r = self.result
        if interaction.user.display_name not in r.confirmed + r.maybe + r.declined:
            await interaction.response.send_message("Only people in this plan can set the place.", ephemeral=True)
            return
        await interaction.response.send_modal(PlaceModal(self))

    @discord.ui.button(label="Can't go", emoji="✋", style=discord.ButtonStyle.danger, row=1)
    async def cant(self, interaction: discord.Interaction, _: discord.ui.Button):
        r, name = self.result, interaction.user.display_name
        if name in r.confirmed or name in r.maybe:
            r.confirmed = [n for n in r.confirmed if n != name]
            r.maybe = [n for n in r.maybe if n != name]
            r.declined.append(name)
            note = "Got it, you're marked as can't go. Tap again if you can make it after all."
        elif name in r.declined:
            r.declined.remove(name)
            r.confirmed.append(name)
            note = "You're back in."
        else:
            await interaction.response.send_message("You weren't part of this plan.", ephemeral=True)
            return
        r.ledger.add("7 · Changes", name, "table", "update", f"{name} {'dropped out' if name in r.declined else 'is back in'}",
                     shared={"answer": "no" if name in r.declined else "yes"}, human=True)
        await interaction.response.edit_message(embed=locked_embed(r), view=self)
        await interaction.followup.send(note, ephemeral=True)
        if len(r.confirmed) + len(r.maybe) < 2 and name in r.declined:
            await interaction.followup.send(f"Heads up: with {name} out, fewer than 2 people are going. "
                                            "Start a new `/plan` if you still want to hang out.")


def custom_place(name: str, activity: str, minutes: int = 0) -> Venue:
    clean = " ".join(name.replace("`", "'").split())[:100]
    return Venue(name=clean, kinds=[activity], price_pp=0, walk_min=minutes, custom=True)


class PlaceModal(discord.ui.Modal, title="Where are we going?"):
    name = discord.ui.TextInput(label="Place name", placeholder="Chipotle on Main St", max_length=100)
    minutes = discord.ui.TextInput(label="Minutes from campus (optional)", placeholder="10", required=False,
                                   max_length=3)

    def __init__(self, view: "LockedView"):
        super().__init__()
        self.view = view
        v = view.result.plan.venue
        if v:
            self.name.default = v.name
            self.minutes.default = str(v.walk_min) if v.walk_min else ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            mins = int(parse_money(self.minutes.value)) if self.minutes.value.strip() else 0
        except ValueError:
            mins = 0
        r = self.view.result
        r.plan.venue = custom_place(self.name.value, r.plan.kind, max(0, min(mins, 180)))
        r.cost_pp = 0.0
        r.ledger.add("7 · Changes", interaction.user.display_name, "table", "place",
                     f"{interaction.user.display_name} set the place: {r.plan.venue.name}",
                     shared={"place": r.plan.venue.name}, human=True)
        await interaction.response.edit_message(embed=locked_embed(r), view=self.view)
        await interaction.followup.send(f"📍 {interaction.user.display_name} set the place: "
                                        f"**{r.plan.venue.name}** ({r.plan.venue.distance}).")


class SplitModal(discord.ui.Modal, title="Split the bill"):
    total = discord.ui.TextInput(label="Total with tax (even split)", placeholder="86.40", required=False,
                                 max_length=12)
    items = discord.ui.TextInput(label="Or itemized: one 'name amount' per line", style=discord.TextStyle.paragraph,
                                 placeholder="Maya 14.50\nJordan 22", required=False, max_length=600)
    tax = discord.ui.TextInput(label="Tax $ (itemized only)", placeholder="6.20", required=False, max_length=10)
    tip = discord.ui.TextInput(label="Tip %", placeholder="18", required=False, max_length=5)
    people = discord.ui.TextInput(label="People (even split)", required=False, max_length=300)

    def __init__(self, names: list[str] | None = None):
        super().__init__()
        if names:
            self.people.default = ", ".join(names)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            tip = parse_money(self.tip.value) if self.tip.value.strip() else 0.0
            if tip > 100:
                raise ValueError("Tip should be a percent from 0 to 100.")
            if self.items.value.strip():
                items = {}
                for line in self.items.value.strip().splitlines():
                    name, _, amount = line.strip().rpartition(" ")
                    if not name:
                        raise ValueError(f"Couldn't read {line!r}; use 'name amount'.")
                    items[name.strip()] = parse_money(amount)
                tax = parse_money(self.tax.value) if self.tax.value.strip() else 0.0
                shares = split_bill(items, tax=tax, tip_pct=tip)
            else:
                names = [n.strip() for n in self.people.value.split(",") if n.strip()]
                if len(names) > MAX_MEMBERS:
                    raise ValueError(f"Split between at most {MAX_MEMBERS} people.")
                shares = split_even(parse_money(self.total.value), names, tip_pct=tip)
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        e = discord.Embed(title="💸 Who owes what", description=f"```\n{format_split(shares)}\n```", colour=GREEN)
        e.set_footer(text="Tax split by what each person ordered; tip on each subtotal; cents add up exactly."
                     if self.items.value.strip() else "Split evenly; cents add up exactly.")
        await interaction.response.send_message(embed=e)


# ---------------------------------------------------------------- the bot

class FreeTonightBot(commands.Bot):
    def __init__(self, db_path: str = "freetonight.db", venues_path: str | None = None):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        self.store = ProfileStore(db_path)
        self.venues = load_venues(venues_path)
        self.runs: dict[int, PlanRun] = {}
        register_commands(self)

    async def setup_hook(self) -> None:
        guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        try:
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Slash commands ready in server %s", guild_id)
            else:
                await self.tree.sync()
                log.info("Slash commands synced globally (can take up to an hour to appear)")
        except ValueError:
            raise SystemExit(f"DISCORD_GUILD_ID should be your server's number, got {guild_id!r}.") from None
        except discord.Forbidden:
            raise SystemExit(
                "Discord said Missing Access while adding slash commands to server "
                f"{guild_id or '(global)'}.\n"
                "Fix: in the Developer Portal open OAuth2 > URL Generator, tick both 'bot' and "
                "'applications.commands', open the generated URL and add the bot to that server again.\n"
                "Also check DISCORD_GUILD_ID is the ID of the server the bot is in "
                "(right-click the server > Copy Server ID).") from None

    async def close(self) -> None:
        self.store.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Free Tonight is online as %s", self.user)


def register_commands(bot: FreeTonightBot) -> None:
    tree = bot.tree

    @tree.command(name="setup", description="Set up your private agent (only you see this)")
    async def setup_cmd(interaction: discord.Interaction):
        await send_setup(interaction, bot)

    @tree.command(name="profile", description="See or edit what your agent knows (only you see this)")
    async def profile_cmd(interaction: discord.Interaction):
        p = bot.store.get(str(interaction.user.id))
        if not p:
            await interaction.response.send_message("You haven't set up an agent yet. Use /setup.", ephemeral=True)
            return
        await interaction.response.send_message(embed=profile_embed(p, "🔒 Your agent"), view=ProfileView(bot, p),
                                                ephemeral=True)

    @tree.command(name="forget", description="Delete everything your agent knows")
    async def forget_cmd(interaction: discord.Interaction):
        gone = bot.store.delete(str(interaction.user.id))
        await interaction.response.send_message("🗑️ Deleted." if gone else "Nothing to delete.", ephemeral=True)

    @tree.command(name="plan", description="Plan a hangout with this group")
    @app_commands.describe(activity="What are we doing?", when="Which days to look at",
                           minutes="How long (optional; defaults per activity)",
                           place="Where? Type a name, or leave empty and set it on the card later")
    @app_commands.choices(activity=ACTIVITY_CHOICES, when=WHEN_CHOICES)
    async def plan_cmd(interaction: discord.Interaction, activity: app_commands.Choice[str],
                       when: app_commands.Choice[str], minutes: app_commands.Range[int, 30, 300] | None = None,
                       place: app_commands.Range[str, 1, 100] | None = None):
        old = bot.runs.get(interaction.channel_id)
        if old and not old.started and (old.host.id == interaction.user.id
                                        or (datetime.now() - old.created).total_seconds() > JOIN_SECONDS):
            bot.runs.pop(interaction.channel_id, None)
            if old.message:
                try:
                    await old.message.edit(content="Replaced by a newer plan below.", embed=None, view=None)
                except discord.HTTPException:
                    pass
        elif old:
            await interaction.response.send_message(
                f"{old.host.display_name}'s plan is still going in this channel. Join that one, or wait for it "
                "to finish.", ephemeral=True)
            return
        run = PlanRun(bot, interaction.channel, interaction.user, activity.value, when.value, minutes, place)
        if not bot.store.get(str(interaction.user.id)):
            async def after(i: discord.Interaction, profile: Profile):
                bot.runs[interaction.channel_id] = run
                run.message = await interaction.channel.send(embed=run.embed(), view=run.view())
            await send_setup(interaction, bot, after)
            return
        bot.runs[interaction.channel_id] = run
        await interaction.response.send_message(embed=run.embed(), view=run.view())
        run.message = await interaction.original_response()

    @tree.command(name="split", description="Split a bill fairly")
    async def split_cmd(interaction: discord.Interaction):
        await interaction.response.send_modal(SplitModal())

    @tree.command(name="help", description="How Free Tonight works")
    async def help_cmd(interaction: discord.Interaction):
        e = discord.Embed(title="🌙 Free Tonight", colour=ORANGE, description=(
            "Everyone gets a private agent. Agents plan together; you only answer yes or no.\n\n"
            "**/setup** tap your free times and food, or link Google Calendar (only you see it)\n"
            "**/plan** start a hangout in this channel; friends tap I'm in\n"
            "**/split** split a bill to the cent\n"
            "**/profile** see or edit your agent · **/forget** delete it\n\n"
            "Agents share only free/busy windows, pooled filters (nobody knows whose) and yes/no answers. "
            "Each plan has a privacy ledger showing every message."))
        await interaction.response.send_message(embed=e, ephemeral=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN (see README: 'Run the Discord bot').")
    token = token.strip().strip('"').strip("'")
    if token in ("...", "YOUR_BOT_TOKEN") or " " in token:
        raise SystemExit("DISCORD_TOKEN still holds the placeholder. Paste your bot token right after the = sign.")
    bot = FreeTonightBot(os.environ.get("FREETONIGHT_DB", "freetonight.db"), os.environ.get("FREETONIGHT_VENUES"))
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the token. In the Developer Portal open your app → Bot → Reset Token, "
                         "then run: export DISCORD_TOKEN=<the new token>") from None


if __name__ == "__main__":
    main()
