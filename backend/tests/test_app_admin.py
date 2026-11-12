"""End to end: the commissioner tab (veto, stat corrections, mulligans) against the real database rules."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_lineup import AppCase, sql, URL


class Admin(AppCase):
    def setUp(self):
        super().setUp()
        sql("""delete from trade_drops; delete from trade_items; delete from trades; delete from mulligans;
               delete from stat_corrections where false;
               delete from stat_lines where player_id in (select id from players where name = 'MLB Hitter 20') and week = 12;
               insert into stat_lines values ((select id from players where name = 'MLB Hitter 20'), 12, 'Home runs', 2);
               insert into trades (proposer, partner, status, accepted_at, veto_until)
                 values (1, 2, 'pending_veto', app_now(), app_now() + interval '20 hours');
               insert into trade_items select (select max(id) from trades), id, 1 from players where name = 'MLB Hitter 5';
               insert into trade_items select (select max(id) from trades), id, 2 from players where name = 'MLB Hitter 21';""")

    def test_only_the_commissioner_sees_the_tab(self):
        p = self.login(1)
        self.assertTrue(p.locator("#tab-admin").is_hidden())
        self.ctx.clear_cookies(); p.evaluate("localStorage.clear()")
        p = self.login(13)
        self.assertTrue(p.locator("#tab-admin").is_visible())
        self.assertTrue(p.locator("#tab-players").is_hidden())          # no team: no roster tabs

    def test_veto_needs_a_reason_and_is_logged(self):
        p = self.login(13); p.click("#tab-admin")
        p.wait_for_selector("#vetoList .trade")
        self.assertIn("MLB Hitter 5", p.inner_text("#vetoList")); self.assertRegex(p.inner_text("#vetoList"), r"(19|20) h")
        p.click("[data-veto]"); p.wait_for_selector("#amsg.err")
        self.assertIn("reason", p.inner_text("#amsg").lower())
        self.assertEqual(sql("select status from trades"), "pending_veto")
        p.fill("[data-vreason]", "Collusion concern"); p.click("[data-veto]")
        p.wait_for_selector("#amsg:has-text('vetoed')")
        self.assertEqual(sql("select status || '|' || note from trades"), "vetoed|Collusion concern")
        self.assertEqual(sql("select count(*) from activity_log where kind = 'trade_veto' and commissioner"), "1")
        self.assertIn("No trades are waiting", p.inner_text("#vetoList"))

    def test_correct_a_stat_then_revert(self):
        p = self.login(13); p.click("#tab-admin"); p.wait_for_selector("#cWeek option", state="attached")
        p.select_option("#cWeek", "12")
        p.fill("#cSearch", "MLB Hitter 20"); p.wait_for_selector('#cPlayers .row:has-text("Home runs 2")')
        p.click("[data-cpick]")
        p.fill("#cStat", "Home runs"); p.fill("#cValue", "3"); p.click("#cApply"); p.wait_for_selector("#amsg.err")      # no reason
        p.fill("#cStat", "Home runs"); p.fill("#cValue", "3"); p.fill("#cReason", "Scorer changed an error to a hit"); p.click("#cApply")
        p.wait_for_selector("#amsg:has-text('Correction applied')")
        self.assertEqual(sql("select value from stat_lines s join players p on p.id = s.player_id where p.name = 'MLB Hitter 20' and week = 12 and stat = 'Home runs'"), "3")
        self.assertIn("2 to 3", p.inner_text("#corrList"))
        p.click("#corrList [data-revert]"); p.wait_for_selector("#amsg:has-text('reverted')")
        self.assertEqual(sql("select value from stat_lines s join players p on p.id = s.player_id where p.name = 'MLB Hitter 20' and week = 12 and stat = 'Home runs'"), "2")
        self.assertEqual(p.locator("#corrList [data-revert]").count(), 0)

    def test_final_week_correction_is_refused(self):
        sql("delete from dev_clock; insert into dev_clock (at) values ('2027-04-02 10:00 America/New_York');")      # week 12 is final
        p = self.login(13); p.click("#tab-admin"); p.wait_for_selector("#cWeek option", state="attached"); p.select_option("#cWeek", "12")
        p.fill("#cSearch", "MLB Hitter 20"); p.wait_for_selector('#cPlayers [data-cpick]'); p.click("[data-cpick]")
        self.assertIn("final", p.inner_text("#cForm").lower())
        p.fill("#cStat", "Home runs"); p.fill("#cValue", "9"); p.fill("#cReason", "late"); p.click("#cApply")
        p.wait_for_selector("#amsg.err"); self.assertIn("final", p.inner_text("#amsg").lower())

    def test_grant_mulligan(self):
        p = self.login(13); p.click("#tab-admin"); p.wait_for_selector("#mTeam option", state="attached")
        p.select_option("#mTeam", "3"); p.click("#mGrant"); p.wait_for_selector("#amsg:has-text('Mulligan granted')")
        self.assertEqual(sql("select count(*) from mulligans where team_id = 3"), "1")
        self.assertEqual(p.locator("#mTeam option[value='3']").count(), 0)


if __name__ == "__main__":
    unittest.main()
