import unittest
from collections import Counter
from datetime import date
from engine import schedule as S

TEAMS = list(range(1, 13))


class Regular(unittest.TestCase):
    def setUp(self):
        self.sch = S.regular_season(TEAMS, seed=7)

    def test_shape(self):
        self.assertEqual(sorted(self.sch), list(range(1, 45)))
        for w, ms in self.sch.items():
            self.assertEqual(len(ms), 6, w)
            self.assertEqual(sorted(t for m in ms for t in m), TEAMS, w)   # everyone plays exactly once a week

    def test_each_pair_meets_four_times(self):
        c = Counter(frozenset(m) for ms in self.sch.values() for m in ms)
        self.assertEqual(len(c), 66)
        self.assertTrue(all(n == 4 for n in c.values()))

    def test_each_block_is_a_full_round_robin(self):
        for b in range(4):
            block = Counter(frozenset(m) for w in range(b * 11 + 1, b * 11 + 12) for m in self.sch[w])
            self.assertEqual(len(block), 66)
            self.assertTrue(all(n == 1 for n in block.values()))

    def test_no_back_to_back_rematch(self):
        for w in range(1, 44):
            a = {frozenset(m) for m in self.sch[w]}
            b = {frozenset(m) for m in self.sch[w + 1]}
            self.assertFalse(a & b, w)

    def test_deterministic_and_seed_sensitive(self):
        self.assertEqual(self.sch, S.regular_season(TEAMS, seed=7))
        self.assertNotEqual(self.sch, S.regular_season(TEAMS, seed=8))


class Cup(unittest.TestCase):
    def test_groups_take_one_team_per_tier(self):
        ranked = list(range(1, 13))
        for seed in range(20):
            groups = S.cup_groups(ranked, seed)
            self.assertEqual(len(groups), 3)
            for g in groups:
                self.assertEqual(sorted((t - 1) // 3 for t in g), [0, 1, 2, 3])
            self.assertEqual(sorted(t for g in groups for t in g), ranked)

    def test_group_stage_is_round_robin_one_game_a_week(self):
        groups = S.cup_groups(list(range(1, 13)), 3)
        sch = S.cup_group_schedule(groups, 3)
        self.assertEqual(sorted(sch), [20, 21, 22])
        for w, ms in sch.items():
            self.assertEqual(sorted(t for m in ms for t in m), list(range(1, 13)))
        pairs = Counter(frozenset(m) for ms in sch.values() for m in ms)
        self.assertEqual(len(pairs), 18)
        self.assertTrue(all(n == 1 for n in pairs.values()))

    def test_table_and_knockout_seeds(self):
        def res(w, l, pw, pl):
            return {"winner": w, "loser": l, "points": {w: pw, l: pl}}
        g1 = S.cup_group_table([1, 2, 3, 4], [res(1, 2, 100, 90), res(3, 4, 80, 70), res(1, 3, 95, 90),
                                              res(2, 4, 85, 60), res(1, 4, 70, 60), res(2, 3, 88, 80)])
        self.assertEqual([r["id"] for r in g1], [1, 2, 3, 4])
        g2 = S.cup_group_table([5, 6, 7, 8], [res(5, 6, 90, 80), res(7, 8, 80, 70), res(5, 7, 90, 80),
                                              res(6, 8, 99, 50), res(5, 8, 70, 60), res(6, 7, 85, 80)])
        g3 = S.cup_group_table([9, 10, 11, 12], [res(9, 10, 100, 99), res(11, 12, 80, 70), res(9, 11, 95, 90),
                                                 res(10, 12, 85, 60), res(9, 12, 70, 60), res(11, 10, 120, 118)])
        seeds = S.cup_knockout_seeds([g1, g2, g3])
        # winners 1 (3-0, 265), 5 (3-0, 250), 9 (3-0, 265); best runner-up by group points: 6 (234), 2 (263), 11 (275)
        self.assertEqual(sorted(seeds), [1, 5, 9, 11])
        self.assertEqual(S.cup_semifinals(seeds), [(seeds[0], seeds[3]), (seeds[1], seeds[2])])


class Playoffs(unittest.TestCase):
    def standings(self):
        # wins descending 1..12, but team 12 has the most points and team 9 the second most among those outside the top 4
        wins = {1: 30, 2: 28, 3: 27, 4: 25, 5: 24, 6: 22, 7: 20, 8: 19, 9: 18, 10: 15, 11: 12, 12: 10}
        cum = {t: 1000 + t for t in wins}
        cum[12], cum[9] = 3000, 2500
        return [{"id": t, "wins": w, "cumulative": cum[t]} for t, w in wins.items()]

    def test_seeds_top4_plus_two_best_by_points(self):
        self.assertEqual(S.playoff_seeds(self.standings()), [1, 2, 3, 4, 12, 9])

    def test_cup_bonus_win_can_change_seeding(self):
        st = self.standings()
        st[3]["wins"] = 22          # team 4 drops to 22 wins
        st[4]["wins"] = 23          # team 5 had 24 -> 23 and now seeds 4th
        self.assertEqual(S.playoff_seeds(st)[:4], [1, 2, 3, 5])

    def test_rounds(self):
        seeds = [1, 2, 3, 4, 12, 9]
        self.assertEqual(S.wildcard_round(seeds), [(3, 9), (4, 12)])
        # seed 6 (team 9) and seed 4 win: seed 1 faces the lowest remaining seed (team 9)
        self.assertEqual(S.semifinal_round(seeds, [9, 4]), [(1, 9), (2, 4)])
        self.assertEqual(S.semifinal_round(seeds, [3, 4]), [(1, 4), (2, 3)])

    def test_aggregate_tie_goes_to_higher_seed(self):
        seeds = [1, 2, 3, 4, 12, 9]
        self.assertEqual(S.advance_aggregate(4, 2, [100, 90], [95, 95], seeds), 2)
        self.assertEqual(S.advance_aggregate(4, 2, [120, 90], [95, 95], seeds), 4)

    def test_single_week_full_tie_goes_to_higher_seed(self):
        seeds = [1, 2, 3, 4, 12, 9]
        self.assertEqual(S.advance_single(3, 9, ("Tie", "Tie"), seeds), 3)
        self.assertEqual(S.advance_single(3, 9, ("B", "Best starter"), seeds), 9)


class Calendar(unittest.TestCase):
    def test_dates_match_rulebook(self):
        self.assertEqual(S.week_start(1), date(2027, 1, 5))
        self.assertEqual(S.week_start(20), date(2027, 5, 18))        # Cup begins about May 18
        self.assertEqual(S.week_end(24), date(2027, 6, 22))          # Cup ends about June 22
        self.assertEqual(S.week_end(36), date(2027, 9, 14))          # week 36 end (no longer the trade deadline)
        self.assertEqual(S.week_end(44), date(2027, 11, 9))          # playoffs begin
        self.assertEqual(S.week_end(49), date(2027, 12, 14))         # champion crowned
        self.assertLess(S.week_end(51), date(2028, 1, 1))            # buffer ends before the new year

    def test_phases(self):
        cal = {r["week"]: r["phase"] for r in S.season_calendar()}
        self.assertEqual(len(cal), 51)
        self.assertIn("group", cal[21])
        self.assertIn("final", cal[24])
        self.assertEqual(cal[45], "Playoffs: wildcard")
        self.assertEqual(cal[51], "Buffer")


if __name__ == "__main__":
    unittest.main()
