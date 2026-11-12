"""Unit tests for rulebook behaviours the workbook sample does not exercise."""
import unittest
from engine import (load_config, native_points, snake_order, can_draft, autopick, decide_winner,
                    score_player, compute_baselines, week_totals, UnknownStat)
from engine.scoring import Baseline
from engine.bonus import bonus_points, bonus_draft_order

CFG = load_config()


class Bonus(unittest.TestCase):
    def test_finish_table(self):
        for place, pts in [(1, 30), (2, 24), (3, 20), (4, 16), (5, 16), (6, 10), (10, 10), (11, 5), (20, 5), (21, 2), (150, 2)]:
            self.assertEqual(bonus_points(CFG, "Placed", place), pts, place)

    def test_zero_statuses(self):
        for s in ("Missed cut", "Withdrew", "Did not start", "Not classified"):
            self.assertEqual(bonus_points(CFG, s, 1), 0)

    def test_ties_share_the_average(self):
        self.assertEqual(bonus_points(CFG, "Placed", 2, 2), 22)      # 24 and 20
        self.assertEqual(bonus_points(CFG, "Placed", 3, 2), 18)      # 20 and 16
        self.assertEqual(bonus_points(CFG, "Placed", 4, 4), 13)      # 16, 16, 10, 10
        self.assertEqual(bonus_points(CFG, "Placed", 5, 8), (16 + 10 * 5 + 5 * 2) / 8)  # places 5 to 12

    def test_draft_order_tiebreaks(self):
        teams = [
            {"name": "A", "wins": 3, "cumulative": 500, "slot": 1},
            {"name": "B", "wins": 3, "cumulative": 400, "slot": 2},   # fewer cumulative points -> before A
            {"name": "C", "wins": 3, "cumulative": 400, "slot": 1},   # tied with B -> lower round-1 slot first
            {"name": "D", "wins": 1, "cumulative": 900, "slot": 9},   # worst record -> first
        ]
        self.assertEqual(bonus_draft_order(teams), ["D", "C", "B", "A"])


class Scoring(unittest.TestCase):
    def test_floor_and_dnp(self):
        b = Baseline(avg=20, sd=5, pool=10, fallback=False)
        self.assertEqual(score_player(CFG, 0, b, True)[1], -10)   # z = -4 -> -40, floored at -10
        self.assertEqual(score_player(CFG, 99, b, False), (0.0, 0.0))

    def test_unknown_stat_raises(self):
        with self.assertRaises(UnknownStat):
            native_points(CFG, "NFL", "QB", {"Touchdowns": 2})

    def test_f1_tables(self):
        self.assertEqual(native_points(CFG, "F1", "Driver", {"Race finish position": 1, "Qualifying position": 1,
                                                             "Fastest lap": 1, "Positions gained": 0}), 40 + 5 + 3)
        self.assertEqual(native_points(CFG, "F1", "Driver", {"Race finish position": 12, "Did not finish": 0}), 3)
        self.assertEqual(native_points(CFG, "F1", "Driver", {"Sprint finish position": 3}), 6)

    def test_small_pool_uses_fallback(self):
        lines = [(f"QB{i}", "NFL", "QB", "Passing TD", i) for i in range(1, 5)] + \
                [(f"QB{i}", "NFL", "QB", "Games played", 1) for i in range(1, 5)]
        base = compute_baselines(CFG, week_totals(CFG, lines))["NFL|QB"]
        self.assertTrue(base.fallback)
        self.assertEqual((base.avg, base.sd), tuple(CFG["baselines_fallback"]["NFL|QB"]))

    def test_min_games_keeps_cameos_out_of_the_baseline(self):
        lines = []
        for i in range(6):
            lines += [(f"H{i}", "MLB", "Hitter", "Home runs", i), (f"H{i}", "MLB", "Hitter", "Games played", 5)]
        lines += [("Cameo", "MLB", "Hitter", "Home runs", 50), ("Cameo", "MLB", "Hitter", "Games played", 1)]
        base = compute_baselines(CFG, week_totals(CFG, lines))["MLB|Hitter"]
        self.assertEqual(base.pool, 6)           # the one-game cameo is excluded
        self.assertAlmostEqual(base.avg, 10.0)   # mean of 0,4,8,12,16,20

    def test_tiebreak_chain(self):
        def t(total, best): return {"weekly_total": total, "best_starter": best}
        self.assertEqual(decide_winner(t(100, 5), t(90, 50), 0, 0), ("A", "Weekly total"))
        self.assertEqual(decide_winner(t(100, 5), t(100, 9), 0, 0), ("B", "Best starter"))
        self.assertEqual(decide_winner(t(0, 0), t(0, 0), 10, 20), ("B", "Cumulative pts"))
        self.assertEqual(decide_winner(t(0, 0), t(0, 0), 10, 10), ("Tie", "Tie"))


class Draft(unittest.TestCase):
    def test_snake_order(self):
        picks = snake_order(list("ABC"), 3)
        self.assertEqual([p[2] for p in picks], list("ABCCBAABC"))
        self.assertEqual(len(snake_order(list(range(12)), 20)), 240)

    def test_f1_cap_blocks_third_driver(self):
        roster = [{"sport": "F1"}, {"sport": "F1"}]
        capped = {**CFG, "max_f1_per_team": 2}                      # the league removed its limit; the rule still works when switched on
        self.assertFalse(can_draft(capped, roster, {"sport": "F1"})[0])
        self.assertTrue(can_draft(capped, roster, {"sport": "NFL"})[0])
        self.assertTrue(can_draft(CFG, roster, {"sport": "F1"})[0])     # as configured: no limit

    def test_autopick_prefers_queue_then_rank_and_skips_illegal(self):
        roster = [{"sport": "F1"}, {"sport": "F1"}]
        pool = [{"id": 1, "sport": "F1"}, {"id": 2, "sport": "NBA"}, {"id": 3, "sport": "NFL"}]
        capped = {**CFG, "max_f1_per_team": 2}
        self.assertEqual(autopick(capped, roster, pool, queue=[1, 3])["id"], 3)   # F1 in queue is illegal
        self.assertEqual(autopick(capped, roster, pool)["id"], 2)                 # best ranked legal player


if __name__ == "__main__":
    unittest.main()
