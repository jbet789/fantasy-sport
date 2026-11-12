import unittest
from datetime import datetime
from engine import load_config
from engine import commissioner as C

CFG = load_config()
PLAYERS = {"Ace": ("MLB", "SP"), "Slugger": ("MLB", "Hitter"), "Max": ("F1", "Driver"), "Wing": ("NBA", "Wing")}


class Weeks(unittest.TestCase):
    def test_week_of(self):
        self.assertEqual(C.week_of(datetime(2027, 1, 5, 12, 0)), 1)
        self.assertEqual(C.week_of(datetime(2027, 1, 12, 11, 59)), 1)
        self.assertEqual(C.week_of(datetime(2027, 1, 12, 12, 0)), 2)
        self.assertEqual(C.week_of(datetime(2027, 5, 17, 20, 0)), 19)     # Monday night before week 20

    def test_finality_is_wednesday_noon(self):
        self.assertEqual(C.final_at(1), datetime(2027, 1, 13, 12, 0))
        self.assertFalse(C.is_final(1, datetime(2027, 1, 13, 11, 59)))
        self.assertTrue(C.is_final(1, datetime(2027, 1, 13, 12, 0)))


class LineupWindow(unittest.TestCase):
    def test_open_only_tuesday_3am_to_noon(self):
        self.assertTrue(C.lineup_locked(datetime(2027, 1, 12, 2, 59)))
        self.assertFalse(C.lineup_locked(datetime(2027, 1, 12, 3, 0)))
        self.assertFalse(C.lineup_locked(datetime(2027, 1, 12, 11, 59)))
        self.assertTrue(C.lineup_locked(datetime(2027, 1, 12, 12, 0)))

    def test_locked_the_rest_of_the_week(self):
        for day in range(13, 19):                                  # Wednesday through Monday
            self.assertTrue(C.lineup_locked(datetime(2027, 1, day, 10, 0)), day)

    def test_waiver_run_happens_inside_the_open_window(self):
        from engine.transactions import is_waiver_slot
        run = datetime(2027, 1, 12, 9, 0)
        self.assertTrue(is_waiver_slot(run) and not C.lineup_locked(run))


class Corrections(unittest.TestCase):
    def setUp(self):
        self.book = C.StatBook(CFG, PLAYERS, {("Slugger", 1, "Home runs"): 2})
        self.t = datetime(2027, 1, 12, 20, 0)

    def test_correction_is_logged_with_old_and_new(self):
        e = self.book.correct("Slugger", 1, "Home runs", 3, "Scorer changed a double to a homer", "commish", self.t)
        self.assertEqual((e["old"], e["new"], e["recompute_week"]), (2, 3, 1))
        self.assertEqual(self.book.values[("Slugger", 1, "Home runs")], 3)
        self.assertEqual(len(self.book.log), 1)

    def test_refused_after_week_is_final(self):
        with self.assertRaises(C.WeekFinal):
            self.book.correct("Slugger", 1, "Home runs", 3, "late", "commish", datetime(2027, 1, 13, 12, 0))
        self.assertEqual(self.book.values[("Slugger", 1, "Home runs")], 2)

    def test_validation(self):
        for args in [("Nobody", 1, "Home runs", 1), ("Slugger", 1, "Goals", 1), ("Slugger", 1, "Home runs", -1)]:
            with self.assertRaises(C.BadCorrection):
                self.book.correct(*args, "x", "commish", self.t)
        with self.assertRaises(C.BadCorrection):
            self.book.correct("Slugger", 1, "Home runs", 3, "  ", "commish", self.t)
        self.assertEqual(self.book.log, [])

    def test_negative_allowed_for_plus_minus_and_f1_stat_names_ok(self):
        book = C.StatBook(CFG, {"Skater": ("NHL", "Skater"), "Max": ("F1", "Driver")})
        book.correct("Skater", 1, "Plus/minus", -2, "fix", "commish", self.t)
        book.correct("Max", 1, "Race finish position", 3, "fix", "commish", self.t)

    def test_revert_appends_and_cannot_repeat(self):
        e = self.book.correct("Slugger", 1, "Home runs", 3, "r", "commish", self.t)
        r = self.book.revert(e["id"], "commish", self.t)
        self.assertEqual(self.book.values[("Slugger", 1, "Home runs")], 2)
        self.assertEqual(len(self.book.log), 2)
        self.assertEqual(r["reverts"], e["id"])
        with self.assertRaises(C.BadCorrection):
            self.book.revert(e["id"], "commish", self.t)

    def test_revert_of_new_value_removes_it(self):
        e = self.book.correct("Ace", 1, "Strikeouts", 9, "added", "commish", self.t)
        self.book.revert(e["id"], "commish", self.t)
        self.assertNotIn(("Ace", 1, "Strikeouts"), self.book.values)


class Results(unittest.TestCase):
    def test_valid_with_ties(self):
        entries = {"A": ("Placed", 1), "B": ("Placed", 2), "C": ("Placed", 2), "D": ("Placed", 4), "E": ("Withdrew", None)}
        self.assertEqual(C.validate_results(CFG, entries), [])

    def test_bad_results(self):
        self.assertTrue(C.validate_results(CFG, {"A": ("Placed", 1), "B": ("Placed", 3)}))          # gap
        self.assertTrue(C.validate_results(CFG, {"A": ("Placed", 1), "B": ("Placed", 1), "C": ("Placed", 2)}))  # overlap
        self.assertTrue(C.validate_results(CFG, {"A": ("Placed", 0)}))
        self.assertTrue(C.validate_results(CFG, {"A": ("Banana", None)}))
        self.assertTrue(C.validate_results(CFG, {"A": ("Withdrew", 5)}))
        with self.assertRaises(C.BadResults):
            C.BonusEvent(CFG, "X", datetime(2027, 4, 11, 18), {}).enter_results({"A": ("Placed", 2)})

    def test_event_scores_the_week_it_finishes(self):
        ev = C.BonusEvent(CFG, "Masters", datetime(2027, 4, 11, 19, 0), {"T1": ["A", "D"], "T2": ["B", "C", "E"]})
        self.assertEqual(ev.status(), "Waiting for results")
        ev.enter_results({"A": ("Placed", 1), "B": ("Placed", 2), "C": ("Placed", 2), "D": ("Placed", 4), "E": ("Missed cut", None)})
        self.assertEqual(ev.status(), "Scored")
        self.assertEqual(ev.team_points(), {"T1": 30 + 16, "T2": 22 + 22 + 0})   # 2nd/3rd tie share (24+20)/2
        self.assertEqual(ev.scoring_week, C.week_of(datetime(2027, 4, 11, 19, 0)))

    def test_unlisted_pick_scores_zero(self):
        ev = C.BonusEvent(CFG, "X", datetime(2027, 4, 11, 19), {"T1": ["A", "Ghost"]})
        ev.enter_results({"A": ("Placed", 1)})
        self.assertEqual(ev.team_points(), {"T1": 30})

    def test_held_event_releases_nothing_until_concluded(self):
        ev = C.BonusEvent(CFG, "Iditarod", datetime(2027, 3, 14, 12), {"T1": ["M1"], "T2": ["M2"]}, hold_until_concluded=True)
        ev.enter_results({"M1": ("Placed", 1)})                  # first musher home, others still racing
        self.assertEqual(ev.status(), "Held until the race concludes")
        self.assertEqual(ev.team_points(), {})
        self.assertEqual(C.bonus_by_week([ev]), {})
        ev.enter_results({"M1": ("Placed", 1), "M2": ("Placed", 2)})
        ev.conclude(datetime(2027, 3, 19, 9))
        self.assertEqual(ev.team_points(), {"T1": 30, "T2": 24})
        self.assertEqual(list(C.bonus_by_week([ev])), [C.week_of(datetime(2027, 3, 19, 9))])

    def test_only_held_events_can_be_concluded(self):
        with self.assertRaises(ValueError):
            C.BonusEvent(CFG, "Masters", datetime(2027, 4, 11, 19), {}).conclude(datetime(2027, 4, 12))

    def test_bonus_by_week_sums_events_in_the_same_week(self):
        a = C.BonusEvent(CFG, "A", datetime(2027, 4, 10, 19), {"T1": ["x"]}); a.enter_results({"x": ("Placed", 1)})
        b = C.BonusEvent(CFG, "B", datetime(2027, 4, 11, 19), {"T1": ["y"]}); b.enter_results({"w": ("Placed", 1), "y": ("Placed", 2)})
        w = C.week_of(datetime(2027, 4, 10, 19))
        self.assertEqual(C.bonus_by_week([a, b]), {w: {"T1": 54}})


if __name__ == "__main__":
    unittest.main()
