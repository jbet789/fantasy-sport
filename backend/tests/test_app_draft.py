"""End to end: the live draft room, from the commissioner starting it to the clock running out."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_lineup import AppCase, sql, URL

T0 = "2027-01-02 12:00:00"


class Draft(AppCase):
    def setUp(self):
        super().setUp()
        sql(f"""select set_config('app.lineup_bypass', 'on', true);
                delete from lineups; delete from matchups; delete from roster_spots; delete from draft_queue; delete from draft_picks; delete from draft_slots; delete from drafts;
                update league_settings set preseason_open_at = null;
                delete from dev_clock; insert into dev_clock (at) values ('{T0} America/New_York');""")

    def start_draft(self):
        sql("select create_draft('main', 20); select start_draft((select max(id) from drafts));")

    def room(self, user=1):
        p = self.login(user); p.click("#tab-draft"); return p

    def test_no_draft_yet(self):
        p = self.room()
        p.wait_for_selector("#dBanner:has-text('No draft yet')")

    def test_commissioner_creates_and_starts_it(self):
        p = self.login(13); p.click("#tab-admin"); p.wait_for_selector("#adCreate:not([hidden])")
        p.click("#adCreate"); p.wait_for_selector("#adList .row:has-text('Main draft')")
        self.assertTrue(p.locator("#adCreate").is_hidden())
        p.click("[data-dstart]"); p.wait_for_selector("#amsg:has-text('live')")
        self.assertEqual(sql("select status from drafts"), "live")
        p.click("#tab-draft"); p.wait_for_selector("#dBanner:has-text('Team 1 is on the clock')")
        self.assertEqual(p.locator("[data-dpick]").count(), 0)             # not a team: read only

    def test_pick_in_turn_and_board(self):
        self.start_draft()
        p = self.room(1)
        p.wait_for_selector("#dBanner:has-text('You are on the clock')")
        self.assertRegex(p.inner_text("#dClock"), r"^\d+:\d\d$")
        name = p.locator("#dAvail .row .nm").first.inner_text()
        p.click("[data-dpick] >> nth=0"); p.wait_for_selector("#dmsg:has-text('You drafted')")
        self.assertEqual(sql("select count(*) from roster_spots where team_id = 1"), "1")
        p.wait_for_selector("#dBanner:has-text('Team 2 is on the clock')")
        self.assertEqual(p.locator("[data-dpick]").count(), 0)
        self.assertIn(name, p.inner_text("#dBoard")); self.assertIn(name, p.inner_text("#dMine")); self.assertIn("#1", p.inner_text("#dRecent"))
        self.assertIn("Your next pick is #24", p.inner_text("#dBanner"))     # snake: team 1 picks 1, then 24

    def test_queue_add_reorder_remove(self):
        self.start_draft()
        p = self.room(2)
        p.wait_for_selector("#dAvail .row")
        p.click("[data-dq] >> nth=0"); p.wait_for_selector("#dQueue .row")
        p.click("[data-dq]:not([disabled]) >> nth=0"); p.wait_for_function("document.querySelectorAll('#dQueue .row').length === 2")
        first = p.locator("#dQueue .row .nm").nth(0).inner_text()
        p.click("[data-qdown='0']"); p.wait_for_function(f"!document.querySelector('#dQueue .row .nm').textContent.includes({first.split('. ',1)[1]!r})")
        p.click("[data-qdel='0']"); p.wait_for_function("document.querySelectorAll('#dQueue .row').length === 1")
        self.assertEqual(sql("select count(*) from draft_queue where team_id = 2"), "1")

    def test_clock_runs_out_and_autopicks_the_queue_first(self):
        self.start_draft()
        p = self.room(1)
        p.wait_for_selector("#dAvail .row")
        p.click('#dAvail .row >> nth=3 >> [data-dq]'); p.wait_for_selector("#dQueue .row")
        queued = p.locator("#dQueue .row .nm").first.inner_text().split(". ", 1)[1]
        sql(f"delete from dev_clock; insert into dev_clock (at) values ('{T0} America/New_York'::timestamptz + interval '61 seconds');")
        p.wait_for_selector("#dBanner:has-text('Team 2 is on the clock')", timeout=15000)      # the room's own refresh made the autopick
        self.assertIn(queued, p.inner_text("#dBoard")); self.assertIn("Auto", p.inner_text("#dRecent"))
        self.assertEqual(sql("select auto from draft_picks where pick_no = 1"), "t")

    def test_legality_is_explained_on_the_card(self):
        self.start_draft()
        sql("update league_settings set f1_cap = 2; insert into roster_spots (team_id, player_id) select 1, id from players where sport = 'F1' order by id limit 2;")
        p = self.room(1)
        p.wait_for_selector("#dAvail .row")
        p.select_option("#dSport", "F1"); p.wait_for_selector("#dAvail .row:has-text('F1')")
        self.assertIn("At most 2 F1 drivers", p.inner_text("#dAvail"))
        self.assertEqual(p.locator("#dAvail [data-dpick]").count(), 0)

    def test_finished_draft_opens_week_one(self):
        sql("select create_draft('main', 1); select start_draft((select max(id) from drafts));")
        sql("""select run_draft_clock((select max(id) from drafts), timestamptz '2027-01-02 12:00:00 America/New_York' + interval '13 minutes');""")
        p = self.room(1); p.wait_for_selector("#dBanner:has-text('Draft complete')")
        self.assertIn("Week-1 lineups are open", p.inner_text("#dBanner"))


if __name__ == "__main__":
    unittest.main()
