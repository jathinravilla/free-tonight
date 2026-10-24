import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import importlib.util

spec = importlib.util.spec_from_file_location(
    "ft_card", Path(__file__).resolve().parents[1] / "cubbie-skill" / "scripts" / "ft_card.py")
ft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ft)


def run(args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = ft.main(args)
    return code, buf.getvalue()


class CardTests(unittest.TestCase):
    def test_card_hides_event_names(self):
        code, out = run(["make", "--name", "Jordan", "--schedule", "Wed 6:30-9:30pm TA lab section",
                         "--when", "weeknights", "--today", "2026-10-27", "--allergies", "peanut"])
        self.assertEqual(code, 0)
        self.assertNotIn("TA lab", out)
        card = ft.decode(ft.find_codes(out)[0])
        self.assertNotIn("TA lab", json.dumps(card))
        self.assertEqual(card["f"]["x"], ["peanut"])

    def test_match_finds_overlap_and_private_near_miss(self):
        _, j = run(["make", "--name", "Jordan", "--schedule", "Tue 5-7pm soccer", "--when", "tomorrow",
                    "--today", "2026-10-26"])
        _, p = run(["make", "--name", "Priya", "--schedule", "Tue 8:30-10pm seminar", "--when", "tomorrow",
                    "--today", "2026-10-26", "--budget", "12"])
        path = Path(self._tmp()) / "cards.txt"
        path.write_text(j + "\n" + p)
        code, out = run(["match", "--name", "Maya", "--schedule", "Tue 6-7:30pm study group (flex 30)",
                         "--cards", str(path), "--now", "2026-10-26T10:00"])
        self.assertEqual(code, 0)
        res = json.loads(out)
        # Nobody's 90-minute window fits all three, but Maya trimming study group by 30 min would.
        self.assertEqual(res["best"][0]["count"], 2)
        near = res["private_near_misses_for_this_student_only"][0]
        self.assertTrue(near["slot"].startswith("Tue Oct 27, 7pm"))
        self.assertEqual(near["ask_student"], ["study group: 30 min"])
        self.assertEqual(res["pooled_filters"]["price_cap"], 10)

    def test_bad_code(self):
        with self.assertRaises(ValueError):
            ft.decode("hello")

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        return d


if __name__ == "__main__":
    unittest.main()
