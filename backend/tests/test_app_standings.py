"""End to end: the standings tab (table, games by week, your season, Cup, playoffs)."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_lineup import AppCase, sql


class Standings(AppCase):
    def setUp(self):
        super().setUp()
        sql("""delete from matchups where week <> 12; delete from cup_groups; delete from cup_bonus_wins; delete from team_weeks;
               update matchups set winner = null, score_a = null, score_b = null;
               insert into matchups (week, kind, team_a, team_b, score_a, score_b, winner) values
                 (10, 'regular', 1, 2, 120, 100, 1), (11, 'regular', 3, 1, 90, 95, 1), (9, 'regular', 2, 3, 80, 70, 2);
               insert into team_weeks (team_id, week, total) values (1, 10, 120), (2, 10, 100), (3, 11, 90), (1, 11, 95), (2, 9, 80), (3, 9, 70), (1, 12, 25), (2, 12, 5);
               insert into cup_groups values (1, 1, 1), (2, 1, 1), (3, 1, 1);
               insert into cup_bonus_wins (team_id) values (2);""")

    def test_table_orders_by_wins_and_marks_the_playoff_line(self):
        p = self.login(); p.click("#tab-standings"); p.wait_for_selector("#stTable tbody tr")
        rows = p.locator("#stTable tbody tr")
        self.assertEqual(rows.count(), 12)
        first = rows.nth(0).inner_text()
        self.assertIn("Team 1", first); self.assertIn("2", first)           # 2 wins
        self.assertIn("Cup +1", p.inner_text("#stTable"))                    # Team 2's bonus win is shown
        self.assertIn("Seed 1", first)
        self.assertIn("Seed 6", p.inner_text("#stTable"))
        self.assertEqual(p.locator("#stTable tr.me").count(), 1)

    def test_games_by_week_and_live_scores(self):
        p = self.login(); p.click("#tab-standings"); p.wait_for_selector("#gList .game")
        self.assertIn("Week 12", p.inner_text("#gWeek"))
        self.assertIn("25.0 – 5.0", p.inner_text("#gList")); self.assertIn("Live", p.inner_text("#gList"))
        p.click("#gPrev"); p.wait_for_selector("#gWeek:has-text('Week 11')")
        self.assertIn("90.0 – 95.0", p.inner_text("#gList"))
        p.click("#gNext"); p.click("#gNext"); p.wait_for_selector("#gWeek:has-text('Week 13')")
        self.assertIn("No games", p.inner_text("#gList"))

    def test_your_season_shows_results(self):
        p = self.login(); p.click("#tab-standings"); p.wait_for_selector("#mineList .row")
        t = p.inner_text("#mineList")
        self.assertIn("Week 10 vs Team 2", t); self.assertIn("Week 11 vs Team 3", t)
        self.assertEqual(p.locator("#mineList .chip.ok").count(), 2)       # two wins
        self.assertEqual(p.locator("#mineList .chip.warn").count(), 1)     # week 12 is live

    def test_cup_group_table(self):
        p = self.login(); p.click("#tab-standings"); p.wait_for_selector("#cupBody table")
        self.assertIn("TIER 1", p.inner_text("#cupBody")); self.assertIn("Team 3", p.inner_text("#cupBody"))

    def test_commissioner_without_a_team_can_read_the_table(self):
        p = self.login(13); p.click("#tab-standings"); p.wait_for_selector("#stTable tbody tr")
        self.assertIn("do not own a team", p.inner_text("#mineList"))


if __name__ == "__main__":
    unittest.main()
