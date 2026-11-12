"""The engine must reproduce the Excel workbook (cached values) for the same inputs."""
import unittest
from pathlib import Path
from openpyxl import load_workbook
from engine import load_config, week_totals, compute_baselines, score_lineup, decide_winner
from engine.bonus import bonus_points, bonus_draft_order

WB = Path(__file__).with_name("fixtures") / "fantasy_scoring_model.xlsx"


class Parity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.wb = load_workbook(WB, data_only=True)
        sl = cls.wb["StatLines"]
        lines = []
        for r in range(5, sl.max_row + 1):
            p, sp, pos, stat, val = (sl.cell(row=r, column=c).value for c in range(1, 6))
            if p and stat:
                lines.append((p, sp, pos, stat, val))
        cls.week = week_totals(cls.cfg, lines)
        cls.base = compute_baselines(cls.cfg, cls.week)

    def test_every_stat_line_scores_like_the_workbook(self):
        sl = self.wb["StatLines"]
        by_player = {}
        for r in range(5, sl.max_row + 1):
            p, pts = sl.cell(row=r, column=1).value, sl.cell(row=r, column=8).value
            if p:
                by_player[p] = by_player.get(p, 0) + pts
        self.assertGreater(len(by_player), 50)
        for p, expected in by_player.items():
            self.assertAlmostEqual(self.week[p].native, expected, places=6, msg=p)

    def test_player_totals_and_games(self):
        pt = self.wb["PlayerTotals"]
        n = 0
        for r in range(5, pt.max_row + 1):
            p = pt.cell(row=r, column=1).value
            if not p:
                continue
            n += 1
            self.assertAlmostEqual(self.week[p].native, pt.cell(row=r, column=4).value, places=6, msg=p)
            self.assertEqual(self.week[p].games, pt.cell(row=r, column=7).value, msg=p)
        self.assertGreater(n, 50)

    def test_baselines_match_settings(self):
        st = self.wb["Settings"]
        for r in range(17, 34):
            key = f"{st.cell(row=r, column=1).value}|{st.cell(row=r, column=2).value}"
            b = self.base[key]
            self.assertEqual(b.pool, st.cell(row=r, column=8).value, key)
            self.assertAlmostEqual(b.avg, st.cell(row=r, column=11).value, places=6, msg=key)
            self.assertAlmostEqual(b.sd, st.cell(row=r, column=12).value, places=6, msg=key)

    def _lineup(self, first):
        mu = self.wb["Matchup"]
        return [(mu.cell(row=r, column=2).value, mu.cell(row=r, column=3).value,
                 mu.cell(row=r, column=4).value, mu.cell(row=r, column=5).value == "Y")
                for r in range(first, first + 10)]

    def _bonus_by_team(self, week_no):
        bc = self.wb["BonusChallenges"]
        totals = {}
        for r in range(8, 108):
            if bc.cell(row=r, column=1).value and bc.cell(row=r, column=2).value == week_no:
                status, place, tied = (bc.cell(row=r, column=c).value for c in (5, 6, 7))
                team = bc.cell(row=r, column=3).value
                totals[team] = totals.get(team, 0) + bonus_points(self.cfg, status, place, tied or 1)
        return totals

    def test_matchup_sample_week_matches_workbook(self):
        mu, bc = self.wb["Matchup"], self.wb["BonusChallenges"]
        bonus = self._bonus_by_team(bc["B4"].value)
        a = score_lineup(self.cfg, self._lineup(16), self.week, self.base, bonus.get(mu["B5"].value, 0))
        b = score_lineup(self.cfg, self._lineup(30), self.week, self.base, bonus.get(mu["C5"].value, 0))
        for res, col, first in ((a, 2, 16), (b, 3, 30)):
            for i, row in enumerate(res["starters"]):
                self.assertAlmostEqual(row["z"], mu.cell(row=first + i, column=9).value, places=6)
                self.assertAlmostEqual(row["score"], mu.cell(row=first + i, column=10).value, places=6)
            self.assertAlmostEqual(res["starter_points"], mu.cell(row=6, column=col).value, places=6)
            self.assertEqual(res["variety_bonus"], mu.cell(row=7, column=col).value)
            self.assertAlmostEqual(res["bonus_points"], mu.cell(row=10, column=col).value, places=6)
            self.assertAlmostEqual(res["weekly_total"], mu.cell(row=8, column=col).value, places=6)
            self.assertEqual(res["sports_played"], mu.cell(row=9, column=col).value)
        winner, how = decide_winner(a, b, mu["M27"].value, mu["N27"].value)
        self.assertEqual({"A": mu["B5"].value, "B": mu["C5"].value}[winner], mu["B12"].value)
        self.assertEqual(how, mu["D12"].value)

    def test_bonus_points_match_workbook_rows(self):
        bc = self.wb["BonusChallenges"]
        checked = 0
        for r in range(8, 108):
            if bc.cell(row=r, column=1).value:
                status, place, tied = (bc.cell(row=r, column=c).value for c in (5, 6, 7))
                self.assertAlmostEqual(bonus_points(self.cfg, status, place, tied or 1), bc.cell(row=r, column=8).value, places=6)
                checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_bonus_draft_order_matches_workbook(self):
        bs = self.wb["BonusSetup"]
        teams = [{"name": bs.cell(row=r, column=1).value, "wins": bs.cell(row=r, column=2).value,
                  "cumulative": bs.cell(row=r, column=3).value, "slot": bs.cell(row=r, column=4).value}
                 for r in range(18, 30)]
        order = bonus_draft_order(teams)
        for r in range(18, 30):
            self.assertEqual(order.index(bs.cell(row=r, column=1).value) + 1, bs.cell(row=r, column=6).value)


if __name__ == "__main__":
    unittest.main()
