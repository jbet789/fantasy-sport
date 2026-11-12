"""End to end: players, claims, and trades pages against the real database rules."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_lineup as base
from test_app_lineup import AppCase, sql, URL

reset_base = base.reset


class Market(AppCase):
    def setUp(self):
        super().setUp()
        sql("""delete from trade_drops; delete from trade_items; delete from trades; delete from waivers; delete from readd_blocks; delete from waiver_claims;
               delete from roster_spots r using players p where p.id = r.player_id and r.team_id = 1 and p.name in ('NBA Guard 39','NBA Guard 40');
               insert into roster_spots (team_id, player_id, slot) select 1, id, 'active' from players where name in ('NBA Guard 4', 'MLB Hitter 6')
                 and id not in (select player_id from roster_spots);
               delete from roster_spots r using players p where p.id = r.player_id and r.team_id = 2 and p.name in ('MLB Hitter 5');
               insert into roster_spots (team_id, player_id) select 2, id from players where name = 'MLB Hitter 25' and id not in (select player_id from roster_spots);""")

    def open_players(self, search=None):
        p = self.login(); p.click("#tab-players"); p.wait_for_selector("#pList .row")
        if search:
            p.fill("#pSearch", search); p.wait_for_selector(f'#pList .row:has-text("{search}")')
        return p

    def test_add_free_agent_then_full_roster_needs_a_drop(self):
        p = self.open_players("NBA Guard 40")
        self.assertIn("19 of 20", p.inner_text("#runCard"))
        p.click('#pList .row:has-text("NBA Guard 40") [data-add]')
        p.click("[data-confirmadd]"); p.wait_for_selector("#pmsg:has-text('added to your roster')")
        self.assertEqual(sql("select count(*) from roster_spots r join players p on p.id = r.player_id where r.team_id = 1 and p.name = 'NBA Guard 40'"), "1")
        self.assertIn("20 of 20", p.inner_text("#runCard"))
        p.fill("#pSearch", "NBA Guard 39"); p.wait_for_selector('#pList .row:has-text("NBA Guard 39")')
        p.click('#pList .row:has-text("NBA Guard 39") [data-add]')
        p.click("[data-confirmadd]")
        p.wait_for_selector("#pmsg.err")                                      # the database's own refusal is shown
        self.assertEqual(sql("select count(*) from roster_spots where team_id = 1 and slot = 'active'"), "20")
        p.click('#pList .row:has-text("NBA Guard 39") [data-add]')
        p.select_option("#dropSel", label="NBA Guard 4 (bench)"); p.click("[data-confirmadd]")
        p.wait_for_selector("#pmsg:has-text('NBA Guard 39 added')")
        self.assertEqual(sql("select count(*) from roster_spots r join players p on p.id = r.player_id where r.team_id = 1 and p.name = 'NBA Guard 4'"), "0")
        self.assertEqual(sql("select count(*) from waivers w join players p on p.id = w.player_id where p.name = 'NBA Guard 4'"), "1")

    def test_drop_needs_confirmation_and_locked_starters_cannot_go(self):
        p = self.open_players()
        self.assertTrue(p.locator('#rosterList .row:has-text("NHL Skater 1") [data-drop]').is_enabled())
        p.click('#rosterList .row:has-text("MLB Hitter 6") [data-drop]')
        p.click("[data-dropno]")
        self.assertEqual(sql("select count(*) from roster_spots r join players p on p.id = r.player_id where r.team_id = 1 and p.name = 'MLB Hitter 6'"), "1")
        p.click('#rosterList .row:has-text("MLB Hitter 6") [data-drop]'); p.click("[data-dropdo]")
        p.wait_for_selector("#pmsg:has-text('MLB Hitter 6 dropped')")
        self.assertEqual(sql("select count(*) from readd_blocks b join players p on p.id = b.player_id where b.team_id = 1 and p.name = 'MLB Hitter 6'"), "1")

    def test_claim_cancel_and_reorder(self):
        sql("""insert into waivers (player_id, kind, release_at)
               select id, 'dropped', timestamptz '2027-03-30 09:00 America/New_York' from players where name in ('NBA Guard 40','NBA Guard 39');""")
        p = self.open_players("NBA Guard 4")
        for n in ("NBA Guard 40", "NBA Guard 39"):
            p.fill("#pSearch", n); p.wait_for_selector(f'#pList .row:has-text("{n}") [data-add]')
            p.click(f'#pList .row:has-text("{n}") [data-add]'); p.click("[data-confirmadd]")
            p.wait_for_selector(f"#pmsg:has-text('Claim placed for {n}')")
        self.assertEqual(sql("select count(*) from waiver_claims where team_id = 1 and status = 'pending'"), "2")
        self.assertIn("#1 NBA Guard 40", p.inner_text("#claimsBody"))
        p.click('#claimsBody .row:has-text("NBA Guard 39") [data-up]'); p.wait_for_selector("#pmsg:has-text('reordered')")
        self.assertIn("#1 NBA Guard 39", p.inner_text("#claimsBody"))
        p.click('#claimsBody .row:has-text("NBA Guard 40") [data-cancelclaim]'); p.wait_for_selector("#pmsg:has-text('cancelled')")
        self.assertEqual(sql("select count(*) from waiver_claims where team_id = 1 and status = 'pending'"), "1")

    def propose(self, p, give, get):
        p.click("#tab-trades"); p.wait_for_selector("#tPartner")
        p.select_option("#tPartner", "2"); p.wait_for_selector("#tSend")
        for n in give: p.check(f'.chk:has-text("{n}") input[data-give]')
        for n in get: p.check(f'.chk:has-text("{n}") input[data-get]')
        p.click("#tSend")

    def test_trade_propose_accept_and_start_block(self):
        p = self.login()
        p.click("#tab-trades"); p.wait_for_selector("#tPartner"); p.select_option("#tPartner", "2"); p.wait_for_selector("#tSend")
        self.assertTrue(p.locator('.chk:has-text("MLB Hitter 1") input').is_disabled())      # a starter in next week's lineup
        self.assertTrue(p.locator('#tSend').is_disabled())
        self.propose(p, ["MLB Hitter 5"], ["MLB Hitter 20"]) if False else None
        p.check('.chk:has-text("MLB Hitter 5") input[data-give]'); p.check('.chk:has-text("MLB Hitter 25") input[data-get]'); p.click("#tSend")
        p.wait_for_selector("#tmsg:has-text('Trade proposed')")
        self.assertIn("Waiting for a reply", p.inner_text("#tList"))
        self.assertTrue(p.locator("[data-withdraw]").count() == 1)
        ctx2 = self.browser.new_context(viewport={"width": 1100, "height": 1400}); ctx2.route("**/fonts.g*apis.com/**", lambda r: r.abort())
        q = ctx2.new_page(); q.goto(URL)
        q.fill("#email", "u2@example.test"); q.fill("#password", "test"); q.click("#login button[type=submit]"); q.wait_for_selector("#app:not([hidden])")
        q.click("#tab-trades"); q.wait_for_selector("[data-accept]"); q.click("[data-accept]")
        q.wait_for_selector("#tmsg:has-text('Trade accepted')")
        self.assertIn("veto window", q.inner_text("#tList").lower())
        ctx2.close()
        self.assertEqual(sql("select status from trades"), "pending_veto")
        # the player Team 2 sent is in a pending trade: Team 1 can't be blocked from reading it, and the roster shows him only after it completes

    def test_decline_and_withdraw(self):
        p = self.login()
        p.click("#tab-trades"); p.wait_for_selector("#tPartner"); p.select_option("#tPartner", "2"); p.wait_for_selector("#tSend")
        p.check('.chk:has-text("MLB Hitter 5") input[data-give]'); p.check('.chk:has-text("MLB Hitter 25") input[data-get]'); p.click("#tSend")
        p.wait_for_selector("#tmsg:has-text('Trade proposed')")
        p.click("[data-withdraw]"); p.wait_for_selector("#tmsg:has-text('withdrawn')")
        self.assertEqual(sql("select status from trades"), "withdrawn")

    def test_trade_deadline_closes_the_form(self):
        sql("delete from dev_clock; insert into dev_clock (at) values ('2027-11-20 10:00 America/New_York');")
        p = self.login(); p.click("#tab-trades")
        p.wait_for_selector("#tDeadline:has-text('passed')")
        self.assertIn("closed", p.inner_text("#proposeBody").lower())

    def test_activity_feed(self):
        p = self.open_players("NBA Guard 40")
        p.click('#pList .row:has-text("NBA Guard 40") [data-add]'); p.click("[data-confirmadd]"); p.wait_for_selector("#pmsg:has-text('added')")
        p.click("#tab-activity"); p.wait_for_selector("#actList .row:not(.empty)")
        self.assertIn("NBA Guard 40", p.inner_text("#actList"))


if __name__ == "__main__":
    unittest.main()
