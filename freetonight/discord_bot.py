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
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from . import cards, ledger_html
from .agent import OwnerQuestion, PersonalAgent
from .llm import parse_schedule_smart
from .negotiation import ACTIVITIES, PlanResult, PlanSession
from .profile import Profile, clock_ok, parse_allergies, parse_diet, parse_money, upcoming_dates
from .split import format_split, split_bill, split_even
from .store import ProfileStore
from .timeutil import fmt_clock
from .venues import load_venues

log = logging.getLogger("freetonight")

ORANGE = discord.Colour.from_rgb(226, 87, 43)
GREEN = discord.Colour.from_rgb(15, 123, 79)
PURPLE = discord.Colour.from_rgb(122, 75, 224)
GRAY = discord.Colour.from_rgb(107, 104, 98)

OWNER_TIMEOUT = int(os.environ.get("FREETONIGHT_ANSWER_TIMEOUT", "600"))
POLL_SECONDS = int(os.environ.get("FREETONIGHT_POLL_SECONDS", "180"))
JOIN_SECONDS = 30 * 60

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
    ("confirming", "Everyone confirms in DMs"),
    ("locked", "Locked"),
]


# ---------------------------------------------------------------- profile setup

class SetupModal(discord.ui.Modal, title="Your private agent"):
    schedule = discord.ui.TextInput(
        label="Weekly schedule (only free/busy is shared)", style=discord.TextStyle.paragraph,
        placeholder="MWF 9-10:15 CS 201\nTue 6-8pm study group (flex 30)\nThu 5-7:30pm work shift",
        required=False, max_length=1500)
    budget = discord.ui.TextInput(label="Most you'd spend per hangout ($)", placeholder="15", max_length=10)
    diet = discord.ui.TextInput(label="Diet (optional)", placeholder="vegetarian, halal, vegan, gluten-free...",
                                required=False, max_length=100)
    allergies = discord.ui.TextInput(label="Allergies (optional)", placeholder="peanut, shellfish...",
                                     required=False, max_length=100)
    extras = discord.ui.TextInput(label="Max walk (min) and hangout hours", placeholder="20, 11am-11:30pm",
                                  required=False, max_length=40)

    def __init__(self, bot: "FreeTonightBot", existing: Profile | None = None, after=None):
        super().__init__()
        self.bot = bot
        self.after = after  # coroutine(interaction, profile) run after a successful save
        if existing:
            self.schedule.default = "\n".join(
                f"{'/'.join(['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][d] for d in c.days)} {c.start}-{c.end} "
                f"{c.label}" + (f" (flex {c.flex_minutes})" if c.flex_minutes else "")
                for c in existing.commitments)[:1500]
            self.budget.default = f"{existing.budget:g}"
            self.diet.default = ", ".join(existing.diet)
            self.allergies.default = ", ".join(existing.allergies)
            self.extras.default = f"{existing.max_walk_min}, {existing.earliest}-{existing.latest}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            commitments, how = parse_schedule_smart(self.schedule.value) if self.schedule.value.strip() else ([], "")
            profile = Profile(name=interaction.user.display_name, commitments=commitments,
                              budget=parse_money(self.budget.value), diet=parse_diet(self.diet.value),
                              allergies=parse_allergies(self.allergies.value), user_id=str(interaction.user.id))
            if self.extras.value.strip():
                walk, _, hours = self.extras.value.partition(",")
                if walk.strip():
                    profile.max_walk_min = int(parse_money(walk))
                if hours.strip():
                    a, _, b = hours.strip().partition("-")
                    profile.earliest, profile.latest = clock_ok(a), clock_ok(b)
        except ValueError as exc:
            await interaction.response.send_message(
                f"⚠️ {exc}\nNothing was saved.", ephemeral=True,
                view=RetrySetupView(self.bot, self))
            return
        self.bot.store.put(profile)
        embed = profile_embed(profile, "🔒 Saved privately")
        if how == "claude":
            embed.set_footer(text="Claude read your schedule. Check it looks right.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        if self.after:
            await self.after(interaction, profile)


class RetrySetupView(discord.ui.View):
    def __init__(self, bot: "FreeTonightBot", modal: SetupModal):
        super().__init__(timeout=600)
        self.bot, self.modal = bot, modal

    @discord.ui.button(label="Fix and try again", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, _: discord.ui.Button):
        fresh = SetupModal(self.bot, after=self.modal.after)
        for field in ("schedule", "budget", "diet", "allergies", "extras"):
            getattr(fresh, field).default = getattr(self.modal, field).value
        await interaction.response.send_modal(fresh)


def profile_embed(p: Profile, title: str) -> discord.Embed:
    e = discord.Embed(title=title, colour=PURPLE,
                      description="Only you can see this. Your agent shares free/busy times, pooled filters "
                                  "and yes/no answers, never these details.")
    e.add_field(name="Schedule", value="\n".join(c.describe() for c in p.commitments) or "Nothing yet", inline=False)
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
        await interaction.response.send_modal(SetupModal(self.bot, self.profile))

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
                 activity: str, when: str, minutes: int | None):
        self.bot, self.channel, self.host = bot, channel, host
        self.activity, self.when, self.minutes = activity, when, minutes
        self.members: dict[int, discord.abc.User] = {host.id: host}
        self.message: discord.Message | None = None
        self.stage = "joining"
        self.detail = ""
        self.result: PlanResult | None = None
        self.started = False

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
        for uid, member in self.members.items():
            profile = self.bot.store.get(str(uid))
            profile.name = member.display_name

            def make_ask(m=member):
                async def ask(q: OwnerQuestion) -> str:
                    return await ask_member(m, self.channel, q, "no" if q.kind == "relax" else "maybe")
                return ask
            agents.append(PersonalAgent(profile, make_ask()))
        now = datetime.now()
        session = PlanSession(agents, self.host.display_name, self.activity,
                              upcoming_dates(now.date(), self.when), self.bot.venues, minutes=self.minutes,
                              now=now, progress=self.progress, poll=self.poll)
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
        if self.result.status == "locked":
            await self.channel.send(
                " ".join(m.mention for uid, m in self.members.items()
                         if m.display_name in self.result.confirmed + self.result.maybe)
                + f" you're set: **{self.result.plan.label}**. Calendar invite and bill split are on the card.")

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

    @discord.ui.button(label="I'm in", style=discord.ButtonStyle.success, emoji="🙋")
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.run.started:
            await interaction.response.send_message("Planning already started.", ephemeral=True)
            return
        if not self.run.bot.store.get(str(interaction.user.id)):
            async def after(i: discord.Interaction, profile: Profile):
                self.run.members[i.user.id] = i.user
                await self.run.refresh()
            await interaction.response.send_modal(SetupModal(self.run.bot, after=after))
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
        asyncio.create_task(self.run.start())

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
    e = discord.Embed(title=f"{a.emoji} {p.kind.title()} is on", colour=GREEN,
                      description=f"**{cards.when_line(p.start, p.end)}**")
    if p.venue:
        e.add_field(name="Where", value=f"{p.venue.name}" + (f" · {p.venue.area}" if p.venue.area else "")
                    + f"\n{p.venue.walk_min} min walk", inline=True)
        e.add_field(name="Cost", value=f"about ${p.venue.price_pp:.0f} each" if p.venue.price_pp else "free",
                    inline=True)
    going = ", ".join(r.confirmed) or "—"
    e.add_field(name=f"Going ({len(r.confirmed)})", value=going, inline=False)
    if r.maybe:
        e.add_field(name="Maybe", value=", ".join(r.maybe), inline=True)
    if r.declined:
        e.add_field(name="Can't", value=", ".join(r.declined), inline=True)
    if r.filters and (r.filters.diet or r.filters.allergy_safe):
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
            "Paste this into iMessage, GroupMe or WhatsApp:\n```\n" + cards.copy_card(self.result) + "\n```",
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
                shares = split_even(parse_money(self.total.value), names, tip_pct=tip)
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        e = discord.Embed(title="💸 Who owes what", description=f"```\n{format_split(shares)}\n```", colour=GREEN)
        e.set_footer(text="Tax split by what each person ordered; tip on each subtotal; cents add up exactly.")
        await interaction.response.send_message(embed=e)


# ---------------------------------------------------------------- the bot

class FreeTonightBot(commands.Bot):
    def __init__(self, db_path: str = "freetonight.db", venues_path: str | None = None):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.store = ProfileStore(db_path)
        self.venues = load_venues(venues_path)
        self.runs: dict[int, PlanRun] = {}
        register_commands(self)

    async def setup_hook(self) -> None:
        guild_id = os.environ.get("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        self.store.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Free Tonight is online as %s", self.user)


def register_commands(bot: FreeTonightBot) -> None:
    tree = bot.tree

    @tree.command(name="setup", description="Set up your private agent (only you see this)")
    async def setup_cmd(interaction: discord.Interaction):
        await interaction.response.send_modal(SetupModal(bot, bot.store.get(str(interaction.user.id))))

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
                           minutes="How long (optional; defaults per activity)")
    @app_commands.choices(activity=ACTIVITY_CHOICES, when=WHEN_CHOICES)
    async def plan_cmd(interaction: discord.Interaction, activity: app_commands.Choice[str],
                       when: app_commands.Choice[str], minutes: app_commands.Range[int, 30, 300] | None = None):
        if interaction.channel_id in bot.runs:
            await interaction.response.send_message("There's already a plan going in this channel.", ephemeral=True)
            return
        run = PlanRun(bot, interaction.channel, interaction.user, activity.value, when.value, minutes)
        if not bot.store.get(str(interaction.user.id)):
            async def after(i: discord.Interaction, profile: Profile):
                bot.runs[interaction.channel_id] = run
                run.message = await interaction.channel.send(embed=run.embed(), view=run.view())
            await interaction.response.send_modal(SetupModal(bot, after=after))
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
            "**/setup** your schedule, budget and food needs (only you see it)\n"
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
    bot = FreeTonightBot(os.environ.get("FREETONIGHT_DB", "freetonight.db"), os.environ.get("FREETONIGHT_VENUES"))
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
