"""Offline checks of the Discord layer: commands register, cards render, and a full /plan run completes
with fake members (no network, no token)."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

try:
    import discord  # noqa: F401
    from freetonight import discord_bot as db
except ImportError:  # discord.py not installed
    db = None

from freetonight.cli import load_demo
from freetonight.profile import Profile


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeChannel:
    id = 42

    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return FakeMessage()


class FakeMember:
    def __init__(self, uid, name):
        self.id, self.display_name, self.mention = uid, name, f"<@{uid}>"


@unittest.skipIf(db is None, "discord.py not installed")
class DiscordBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_commands_register(self):
        async def go():
            bot = db.FreeTonightBot(self.dbpath)
            names = sorted(c.name for c in bot.tree.get_commands())
            await bot.close()
            return names
        self.assertEqual(asyncio.run(go()), ["forget", "help", "plan", "profile", "setup", "split"])

    def test_full_plan_run(self):
        demo = load_demo()
        answers = demo["answers"]

        async def fake_ask(member, fallback, q, default):
            return answers.get(q.kind, {}).get(member.display_name, q.options[0])

        async def go():
            bot = db.FreeTonightBot(self.dbpath)
            members = []
            for i, f in enumerate(demo["friends"], start=1):
                p = Profile.from_dict(dict(f, user_id=str(i)))
                bot.store.put(p)
                members.append(FakeMember(i, f["name"]))
            channel = FakeChannel()
            run = db.PlanRun(bot, channel, members[0], "dinner", "weeknights", None)
            for m in members[1:]:
                run.members[m.id] = m
            run.message = FakeMessage()
            # Join card renders with buttons
            self.assertIn("Dinner", run.embed().title)
            self.assertIsInstance(run.view(), db.JoinView)
            fixed_now = datetime.fromisoformat(demo["today"])
            with mock.patch.object(db, "ask_member", fake_ask), \
                    mock.patch.object(db, "datetime", mock.Mock(now=mock.Mock(return_value=fixed_now))):
                await run.start()
            await bot.close()
            return run, channel

        run, channel = asyncio.run(go())
        self.assertEqual(run.result.status, "locked")
        self.assertEqual(run.result.plan.venue.name, "Halal Bros Grill")
        final = run.message.edits[-1]
        self.assertIn("is on", final["embed"].title)
        self.assertIsInstance(final["view"], db.LockedView)
        stages_seen = [e["embed"].description for e in run.message.edits if e["embed"].description]
        self.assertTrue(any("⏳" in s for s in stages_seen))
        self.assertTrue(any("you're set" in (c or "") for c, _ in channel.sent))

    def test_split_modal_and_poll_embed(self):
        async def go():
            m = db.SplitModal(["Maya", "Jordan"])
            self.assertEqual(m.people.default, "Maya, Jordan")
            bot = db.FreeTonightBot(self.dbpath)
            run = db.PlanRun(bot, FakeChannel(), FakeMember(1, "A"), "coffee", "today", None)
            run.members[2] = FakeMember(2, "B")
            pv = db.PollView(run, ["opt one", "opt two"])
            pv.votes = {1: 1, 2: 1}
            self.assertEqual(pv.winner(), 1)
            self.assertIn("👑", pv.embed(final=1).description)
            await bot.close()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
