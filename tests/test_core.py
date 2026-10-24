import asyncio
import json
import unittest
from datetime import date, datetime, time, timedelta

from freetonight import cards, ledger_html
from freetonight.agent import PersonalAgent, budget_bucket
from freetonight.cli import load_demo
from freetonight.negotiation import PlanSession
from freetonight.profile import Profile, parse_allergies, parse_diet, parse_schedule, upcoming_dates
from freetonight.split import split_bill, split_even
from freetonight.timeutil import parse_clock, parse_days, parse_range, subtract
from freetonight.venues import GroupFilters, load_venues


def scripted(answers):
    def make(name):
        async def ask(q):
            return answers.get(q.kind, {}).get(name, q.options[0])
        return ask
    return make


def run_demo(**overrides):
    demo = load_demo()
    answers = overrides.pop("answers", demo["answers"])
    make = scripted(answers)
    agents = [PersonalAgent(Profile.from_dict(f), make(f["name"])) for f in demo["friends"]]
    now = datetime.fromisoformat(demo["today"])
    session = PlanSession(agents, demo["host"], demo["activity"], upcoming_dates(now.date(), demo["when"]),
                          load_venues(), now=now, **overrides)
    return asyncio.run(session.run()), demo


class TimeTests(unittest.TestCase):
    def test_clock_and_ranges(self):
        self.assertEqual(parse_clock("9:30pm"), time(21, 30))
        self.assertEqual(parse_clock("noon"), time(12, 0))
        self.assertEqual(parse_range("2-3:30pm"), (time(14, 0), time(15, 30)))
        self.assertEqual(parse_range("9-10:15"), (time(9, 0), time(10, 15)))
        self.assertEqual(parse_range("11am-1pm"), (time(11, 0), time(13, 0)))

    def test_days(self):
        self.assertEqual(parse_days("MWF"), [0, 2, 4])
        self.assertEqual(parse_days("TR"), [1, 3])
        self.assertEqual(parse_days("Tue/Thu"), [1, 3])
        self.assertEqual(parse_days("weekdays"), [0, 1, 2, 3, 4])

    def test_subtract(self):
        d = datetime(2026, 10, 27)
        w = (d.replace(hour=17), d.replace(hour=23))
        free = subtract(w, [(d.replace(hour=18), d.replace(hour=20))])
        self.assertEqual(free, [(d.replace(hour=17), d.replace(hour=18)), (d.replace(hour=20), d.replace(hour=23))])


class ProfileTests(unittest.TestCase):
    def test_schedule_parsing(self):
        cs = parse_schedule("MWF 9-10:15 CS 201; Tue 6-8pm study group (flex 30)\nThu 17:00-19:30 Work")
        self.assertEqual(len(cs), 3)
        self.assertEqual(cs[0].days, [0, 2, 4])
        self.assertEqual((cs[1].start, cs[1].end, cs[1].flex_minutes), ("18:00", "20:00", 30))
        self.assertEqual(cs[1].label, "study group")

    def test_bad_line_names_the_line(self):
        with self.assertRaises(ValueError) as ctx:
            parse_schedule("whenever I feel like it")
        self.assertIn("whenever", str(ctx.exception))

    def test_food(self):
        self.assertEqual(parse_diet("veggie, halal"), ["vegetarian", "halal"])
        self.assertEqual(parse_allergies("peanuts and shrimp"), ["peanut", "shellfish"])
        with self.assertRaises(ValueError):
            parse_allergies("kryptonite")

    def test_dates(self):
        tue = date(2026, 10, 27)
        self.assertEqual(len(upcoming_dates(tue, "weeknights")), 4)
        self.assertEqual(upcoming_dates(tue, "weekend"), [date(2026, 10, 31), date(2026, 11, 1)])
        self.assertEqual(upcoming_dates(tue, "tomorrow"), [date(2026, 10, 28)])


class NegotiationTests(unittest.TestCase):
    def test_demo_locks_after_two_private_relaxations(self):
        r, _ = run_demo()
        self.assertEqual(r.status, "locked")
        self.assertEqual(r.plan.venue.name, "Halal Bros Grill")
        self.assertEqual((r.plan.start.weekday(), r.plan.start.hour, r.plan.start.minute), (1, 19, 30))
        self.assertEqual(sorted(r.confirmed), ["Jordan", "Leo", "Maya", "Priya"])
        self.assertEqual(r.maybe, ["Sam"])
        asks = [e for e in r.ledger.entries if e.kind == "relax_owner"]
        self.assertEqual(sorted(e.sender for e in asks), ["Maya", "Sam"])

    def test_nothing_private_reaches_the_table(self):
        r, demo = run_demo()
        shared_text = json.dumps([e.shared for e in r.ledger.entries])
        for f in demo["friends"]:
            for c in f["commitments"]:
                self.assertNotIn(c["label"], shared_text, f"{c['label']} leaked")
        # Exact budgets never leave an agent; only $5 steps do.
        self.assertNotIn("$12", shared_text)
        self.assertIn("$10", shared_text)
        # Filters are pooled, never tied to a person.
        filt = next(e for e in r.ledger.entries if e.kind == "filters")
        self.assertEqual(filt.sender, "all agents")

    def test_declining_relaxation_changes_the_plan(self):
        demo = load_demo()
        answers = dict(demo["answers"], relax={"Maya": "no", "Sam": "no"})
        r, _ = run_demo(answers=answers)
        self.assertEqual(r.status, "locked")
        self.assertLess(len(r.plan.attendees), 5)

    def test_everyone_declines(self):
        r, _ = run_demo(answers={"relax": {}, "confirm": {n: "no" for n in ["Maya", "Jordan", "Priya", "Leo", "Sam"]}})
        self.assertEqual(r.status, "fell_through")

    def test_poll_is_used_for_ties(self):
        seen = {}

        async def poll(options):
            seen["options"] = options
            return len(options) - 1

        demo = load_demo()
        make = scripted({"confirm": {}})
        # Two friends with empty schedules on a weekend: many tied options.
        agents = [PersonalAgent(Profile(name=n, budget=20), make(n)) for n in ("A", "B")]
        now = datetime(2026, 10, 27, 10)
        r = asyncio.run(PlanSession(agents, "A", "dinner", upcoming_dates(now.date(), "weekend"), load_venues(),
                                    now=now, poll=poll).run())
        self.assertGreater(len(seen["options"]), 1)
        self.assertEqual(r.status, "locked")
        self.assertTrue(any(e.kind == "poll" and e.human for e in r.ledger.entries))

    def test_no_overlap(self):
        busy = [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "11:00", "end": "23:30", "label": "x"}]
        make = scripted({})
        agents = [PersonalAgent(Profile.from_dict({"name": "A", "commitments": busy}), make("A")),
                  PersonalAgent(Profile(name="B"), make("B"))]
        now = datetime(2026, 10, 27, 10)
        r = asyncio.run(PlanSession(agents, "A", "dinner", [now.date()], load_venues(), now=now).run())
        self.assertEqual(r.status, "no_overlap")

    def test_food_needs_dont_block_non_food_plans(self):
        # Seen live: a games night with halal/vegetarian/peanut needs found no place and said "at TBD".
        make = scripted({"confirm": {"A": "maybe", "B": "maybe"}})
        agents = [PersonalAgent(Profile(name="A", diet=["halal"], allergies=["peanut"]), make("A")),
                  PersonalAgent(Profile(name="B", diet=["vegetarian"]), make("B"))]
        now = datetime(2026, 10, 24, 15)
        r = asyncio.run(PlanSession(agents, "A", "games", [date(2026, 10, 25)], load_venues(), now=now).run())
        self.assertEqual(r.plan.venue.name, "Union Game Room")
        self.assertEqual(r.filters.diet, [])
        shared = json.dumps([e.shared for e in r.ledger.entries])
        self.assertNotIn("halal", shared)
        self.assertNotIn("TBD", r.plan.label)
        self.assertIn("maybe: A, B", cards.copy_card(r))

    def test_profile_store_is_owner_only(self):
        import os, stat, tempfile
        from freetonight.store import ProfileStore
        path = os.path.join(tempfile.mkdtemp(), "p.db")
        ProfileStore(path).close()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_calendar_feed_reads_busy_times_only(self):
        from freetonight.calendar_feed import busy_from_ics, check_url
        ics = "\r\n".join([
            "BEGIN:VCALENDAR", "BEGIN:VEVENT", "DTSTART:20261005T090000", "DTEND:20261005T101500",
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261211T235959Z", "EXDATE:20261028T090000",
            "SUMMARY:Secret class name", "END:VEVENT",
            "BEGIN:VEVENT", "DTSTART;VALUE=DATE:20261027", "SUMMARY:Birthday", "END:VEVENT",
            "BEGIN:VEVENT", "DTSTART:20261029T180000", "DTEND:20261029T200000", "TRANSP:TRANSPARENT",
            "END:VEVENT", "END:VCALENDAR"])
        week = [date(2026, 10, 26) + timedelta(days=i) for i in range(7)]
        busy = busy_from_ics(ics, week)
        self.assertEqual([(c.on_date, c.start, c.end) for c in busy],
                         [("2026-10-26", "09:00", "10:15"), ("2026-10-30", "09:00", "10:15")])
        self.assertNotIn("Secret", json.dumps([c.label for c in busy]))
        for bad in ("http://calendar.google.com/x", "https://localhost/x", "https://evil.com/calendar.google.com",
                    "file:///etc/passwd"):
            with self.assertRaises(ValueError):
                check_url(bad)
        self.assertTrue(check_url("webcal://p01-caldav.icloud.com/x").startswith("https://"))

    def test_free_picks_become_busy_blocks(self):
        from freetonight.profile import busy_from_free
        busy = busy_from_free(["Tue evening", "Tue late night"])
        tue = [c for c in busy if c.days == [1]]
        self.assertEqual([(c.start, c.end) for c in tue], [("08:00", "17:00")])
        self.assertEqual(len(busy), 7)
        self.assertEqual(busy_from_free([]), [])

    def test_budget_bucket(self):
        self.assertEqual(budget_bucket(12), 10)
        self.assertEqual(budget_bucket(15), 15)

    def test_filters(self):
        f = GroupFilters(diet=["halal"], allergy_safe=["peanut"], budget_cap=10, max_walk_min=12)
        names = {v.name: f.rejects(v) for v in load_venues()}
        self.assertEqual(names["Halal Bros Grill"], [])
        self.assertIn("budget", names["Seoul Kitchen KBBQ"])


class OutputTests(unittest.TestCase):
    def test_split_adds_up(self):
        shares = split_bill({"A": 14.5, "B": 22, "C": 9.99}, tax=4.07, tip_pct=18)
        self.assertAlmostEqual(sum(s.total for s in shares), round(46.49 + 4.07 + 46.49 * 0.18, 2), places=2)
        even = split_even(100, ["A", "B", "C"])
        self.assertEqual([s.total for s in even].count(33.34) + [s.total for s in even].count(33.33), 3)
        self.assertAlmostEqual(sum(s.total for s in even), 100.0)

    def test_cards_and_ledger(self):
        r, _ = run_demo()
        ics = cards.ics_invite(r)
        self.assertIn("DTSTART:20261027T193000", ics)
        self.assertIn("LOCATION:Halal Bros Grill", ics)
        card = cards.copy_card(r)
        self.assertIn("Halal Bros Grill", card)
        html = ledger_html.render(r)
        self.assertIn("Privacy ledger", html)
        self.assertIn("kept private", html)
        self.assertNotIn("Orgo study group", html.split('id="ledger-data"')[0].split("<main>")[1]
                         .replace("kept private", ""))


if __name__ == "__main__":
    unittest.main()
