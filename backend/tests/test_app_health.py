"""End to end: the live badge for everyone and the System health panel for the commissioner."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_lineup import AppCase, sql


class Health(AppCase):
    def setUp(self):
        super().setUp()
        sql("delete from job_runs; select set_config('request.jwt.claim.sub', '', true);")

    def beat(self, tick_min_ago, backup_h_ago=None, error=None):
        sql(f"delete from job_runs; insert into job_runs (job, last_run_at, last_ok_at, last_error) values ('tick', app_now(), app_now() - interval '{tick_min_ago} minutes', {repr(error) if error else 'null'});")
        if backup_h_ago is not None:
            sql(f"insert into job_runs (job, last_run_at, last_ok_at) values ('backup', app_now(), app_now() - interval '{backup_h_ago} hours');")

    def test_badge_says_live_when_on_time_and_warns_when_scores_are_late_or_stopped(self):
        self.beat(3); p = self.login()
        p.wait_for_function("document.querySelector('#liveBadge').title.includes('3 min')")
        self.assertEqual(p.inner_text("#liveBadge"), "Live")
        self.beat(50); p.reload(); p.wait_for_selector("#app:not([hidden])"); p.wait_for_selector("#liveBadge.warn")
        self.assertEqual(p.inner_text("#liveBadge"), "Scores delayed")
        self.beat(600); p.reload(); p.wait_for_selector("#app:not([hidden])"); p.wait_for_selector("#liveBadge.bad")
        self.assertEqual(p.inner_text("#liveBadge"), "Scores paused")

    def test_before_launch_the_badge_stays_quiet(self):
        p = self.login(); p.wait_for_timeout(500)
        self.assertEqual(p.inner_text("#liveBadge"), "Live"); self.assertNotIn("warn", p.get_attribute("#liveBadge", "class"))

    def test_managers_do_not_get_the_health_panel(self):
        self.beat(3, 2, error="scoring: boom"); p = self.login()
        self.assertTrue(p.is_hidden("#tab-admin"))
        self.assertNotIn("boom", p.content())

    def test_commissioner_sees_each_job_its_last_error_and_the_setup_facts(self):
        self.beat(40, 70, error="waiver_wins: boom"); p = self.login(13)
        p.click("#tab-admin"); p.wait_for_selector("#healthList li")
        txt = p.inner_text("#healthPanel")
        self.assertIn("Scoring tick", txt); self.assertIn("Late", txt); self.assertIn("waiver_wins: boom", txt)
        self.assertIn("Nightly backup", txt); self.assertIn("Late", txt); self.assertIn("3 days ago", txt); self.assertEqual(txt.count("Late"), 2)
        self.assertIn("12 of 12 teams", txt); self.assertIn("A job is running late", txt)


if __name__ == "__main__":
    unittest.main()
