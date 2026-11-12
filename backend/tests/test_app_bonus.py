"""End to end: bonus events from the commissioner's side (create, draft, results, conclude) and the managers' Bonus tab."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_lineup as base
from test_app_lineup import AppCase, sql

FIELD = "\n".join(f"Runner {i}" for i in range(1, 31))


class Bonus(AppCase):
    def setUp(self):
        super().setUp()
        sql("""delete from draft_queue; delete from draft_picks; delete from draft_slots; delete from drafts;
               delete from bonus_picks; delete from bonus_results; delete from bonus_entrants; delete from bonus_events; delete from activity_log where false;""")

    def other(self, user):
        ctx = self.browser.new_context(viewport={"width": 1100, "height": 1400}); ctx.route("**/fonts.g*apis.com/**", lambda r: r.abort())
        self.addCleanup(ctx.close)
        q = ctx.new_page(); q.on("pageerror", lambda e: self.errors.append(str(e))); q.goto(base.URL)
        q.fill("#email", f"u{user}@example.test"); q.fill("#password", "test"); q.click("#login button[type=submit]"); q.wait_for_selector("#app:not([hidden])")
        return q

    def admin(self):
        p = self.login(13); p.click("#tab-admin"); p.wait_for_selector("#abCreate", state="attached"); return p

    def add_event(self, p, name="Masters", hold=False):
        p.click("#view-admin details summary")
        p.fill("#abName", name); p.fill("#abEnds", "2027-04-11T18:00"); p.fill("#abEntrants", FIELD)
        if hold: p.check("#abHold")
        p.click("#abCreate"); p.wait_for_selector("#amsg:has-text('Event added')")
        p.wait_for_selector(f'#abList .row:has-text("{name}")')

    def test_commissioner_creates_event_and_field_is_validated(self):
        p = self.admin()
        p.click("#view-admin details summary"); p.fill("#abName", "Tiny"); p.fill("#abEnds", "2027-04-11T18:00"); p.fill("#abEntrants", "A\nB\nC"); p.click("#abCreate")
        p.wait_for_selector("#amsg.err"); self.assertIn("at least 12", p.inner_text("#amsg"))
        p.fill("#abEntrants", FIELD); p.fill("#abName", "Masters"); p.click("#abCreate"); p.wait_for_selector("#amsg:has-text('Event added')")
        self.assertIn("30 entrants", p.inner_text("#abList"))
        self.assertEqual(sql("select count(*) from bonus_entrants"), "30")
        self.assertEqual(sql("select ends_at at time zone 'America/New_York' from bonus_events"), "2027-04-11 18:00:00")
        self.assertEqual(p.input_value("#abName"), "")

    def test_create_draft_then_it_appears_for_managers(self):
        p = self.admin(); self.add_event(p)
        p.click("[data-bdcreate]"); p.wait_for_selector("#amsg:has-text('Draft created')")
        self.assertEqual(sql("select count(*) from draft_slots"), "24")
        self.assertIn("Masters", p.inner_text("#adList"))             # the draft list names the event
        sql("select start_draft((select max(id) from drafts));")
        q = self.other(1); q.click("#tab-bonus"); q.wait_for_selector("#bnList .row:has-text('Masters')")
        self.assertIn("Draft is live", q.inner_text("#bnList"))
        q.click("[data-bdraft]"); q.wait_for_selector("#dBanner:has-text('on the clock')")
        self.assertIn("Runner 1", q.inner_text("#dAvail"))             # entrants, not players
        q.click("[data-dpick] >> nth=0"); q.wait_for_selector("#dmsg:has-text('You drafted Runner 1')")
        self.assertEqual(sql("select count(*) from bonus_picks"), "1")

    def test_results_entry_validation_and_points(self):
        p = self.admin(); self.add_event(p)
        sql("insert into bonus_picks (event_id, entrant_id, team_id) select e.id, n.id, 1 from bonus_events e join bonus_entrants n on n.event_id = e.id where n.name in ('Runner 1', 'Runner 2', 'Runner 3');")
        p.click("[data-bres]"); p.fill("#abText", "Runner 1 first"); p.click("#abSave")
        p.wait_for_selector("#amsg.err"); self.assertIn("Name: place", p.inner_text("#amsg"))
        p.fill("#abText", "Runner 1: banana"); p.click("#abSave"); p.wait_for_selector("#amsg:has-text('not a place')")
        p.fill("#abText", "Nobody Here: 1"); p.click("#abSave"); p.wait_for_selector("#amsg:has-text('Unknown entrant')")
        p.fill("#abText", "Runner 1: 2\nRunner 2: 2\nRunner 3: MC\nRunner 4: 1"); p.click("#abSave")
        p.wait_for_selector("#amsg:has-text('4 results saved')")
        self.assertTrue(p.locator("#abText").count() == 0 or p.locator("#abResults").is_hidden())
        q = self.other(1); q.click("#tab-bonus"); q.wait_for_selector("#bnList .row")
        self.assertIn("3 picks, 44.0 pts", q.inner_text("#bnList"))       # two tied for 2nd share (24 + 20) / 2 = 22 each; the missed cut scores 0
        q.click("[data-bopen]"); q.wait_for_selector("#bnEntrants tbody tr")
        self.assertIn("Team 1", q.inner_text("#bnTeams")); self.assertIn("Runner 4", q.inner_text("#bnEntrants"))
        q.fill("#bnSearch", "runner 29"); self.assertEqual(q.locator("#bnEntrants tbody tr").count(), 1)
        q.click("#bnBack"); q.wait_for_selector("#bnList .row")

    def test_held_event_releases_on_conclude(self):
        p = self.admin(); self.add_event(p, "Iditarod", hold=True)
        sql("insert into bonus_picks (event_id, entrant_id, team_id) select e.id, n.id, 1 from bonus_events e join bonus_entrants n on n.event_id = e.id where n.name = 'Runner 1';")
        p.click("[data-bres]"); p.fill("#abText", "Runner 1: 1"); p.click("#abSave"); p.wait_for_selector("#amsg:has-text('1 results saved')")
        q = self.other(1); q.click("#tab-bonus"); q.wait_for_selector("#bnList .row")
        self.assertIn("Points held", q.inner_text("#bnList")); self.assertIn("0.0 pts", q.inner_text("#bnList"))
        p.click("[data-bconclude]"); p.wait_for_selector("#amsg:has-text('concluded')")
        q.click("#tab-lineup"); q.click("#tab-bonus"); q.wait_for_selector("#bnList .row:has-text('30.0 pts')")


if __name__ == "__main__":
    unittest.main()
