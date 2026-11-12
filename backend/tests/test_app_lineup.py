"""End to end: the real app page in a real browser, talking to the dev server, which talks to the real database rules."""
import os, subprocess, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import dev_server

DBN, PORT = "fl_app", 8791
URL = f"http://127.0.0.1:{PORT}/"
ENV = {**os.environ, "PGHOST": os.environ.get("PGHOST", "/tmp/pgtest"), "PGPORT": os.environ.get("PGPORT", "5499"), "PGUSER": os.environ.get("PGUSER", "postgres")}

STARTERS = ["MLB Hitter 1", "MLB Hitter 2", "NBA Guard 1", "NBA Guard 2", "NBA Guard 3", "NHL Skater 1", "NFL QB 1", "MLS Forward 1", "F1 Driver 1"]
BENCH = ["MLB Hitter 3", "MLB Hitter 4", "MLB Hitter 5", "MLB Hitter 6", "NBA Guard 4", "NHL Skater 2", "NHL Skater 3", "NFL QB 2", "NFL QB 3", "MLS Forward 2"]
OPEN, LOCKED = "2027-03-30 04:00", "2027-03-30 12:30"


def sql(q):
    p = subprocess.run(["psql", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", DBN], input="begin;\n" + q + "\n;\ncommit;", text=True, capture_output=True, env=ENV)
    if p.returncode != 0:
        raise AssertionError(p.stderr)
    return p.stdout.strip()


def names(lst):
    return "(" + ",".join(f"'{n}'" for n in lst) + ")"


def reset(clock=OPEN):
    sql(f"""
      delete from dev_clock; insert into dev_clock (at) values ('{clock} America/New_York');
      delete from mulligans; delete from waiver_claims;
      update players set injury_status = null where id in (select player_id from roster_spots where team_id in (1, 2));
      update players set injury_status = 'Out' where name in ('MLB Hitter 3', 'MLB Hitter 2', 'MLB Hitter 7');
      update roster_spots r set slot = case when p.name = 'MLB Hitter 7' then 'ir' else 'active' end, ir_cleared_at = null
        from players p where p.id = r.player_id and r.team_id = 1 and r.slot <> case when p.name = 'MLB Hitter 7' then 'ir' else 'active' end;
      update players set injury_status = 'Questionable' where name = 'MLB Hitter 4';
      select set_config('app.lineup_bypass', 'on', true);
      delete from lineups where week in (12, 13);
      insert into lineups (team_id, week, player_id) select 1, w, id from players, generate_series(12, 13) w where name in {names(STARTERS)};
      insert into lineups (team_id, week, player_id) select 2, w, id from players, generate_series(12, 13) w where name in ('MLB Hitter 20','MLB Hitter 21','MLB Hitter 22','MLB Hitter 23','MLB Hitter 24');
    """)


class AppCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        subprocess.run([str(ROOT / "backend/tests/run.sh")], check=True, env={**ENV, "DB": DBN}, capture_output=True)
        sql("select t.fixture();")
        t1 = ["MLB Hitter 1","MLB Hitter 2","MLB Hitter 3","MLB Hitter 4","MLB Hitter 5","MLB Hitter 6","NBA Guard 1","NBA Guard 2","NBA Guard 3","NBA Guard 4",
              "NHL Skater 1","NHL Skater 2","NHL Skater 3","NFL QB 1","NFL QB 2","NFL QB 3","MLS Forward 1","MLS Forward 2","F1 Driver 1"]
        sql(f"""
          update players set injury_status = 'Out' where name = 'MLB Hitter 7';
          insert into roster_spots (team_id, player_id, slot) select 1, id, 'active' from players where name in {names(t1)};
          insert into roster_spots (team_id, player_id, slot) select 1, id, 'ir' from players where name = 'MLB Hitter 7';
          insert into roster_spots (team_id, player_id) select 2, id from players where name in ('MLB Hitter 20','MLB Hitter 21','MLB Hitter 22','MLB Hitter 23','MLB Hitter 24');
          insert into matchups (week, kind, team_a, team_b) values (12, 'regular', 1, 2);
        """)
        cls.srv = dev_server.serve(PORT, DBN)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        reset()
        self.ctx = self.browser.new_context(viewport={"width": 1100, "height": 1400})
        self.ctx.route("**/fonts.g*apis.com/**", lambda r: r.abort())
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def tearDown(self):
        self.ctx.close()
        self.assertEqual(self.errors, [])

    def login(self, user=1):
        p = self.page
        p.goto(URL)
        p.fill("#email", f"u{user}@example.test"); p.fill("#password", "test"); p.click("#login button[type=submit]")
        p.wait_for_selector("#app:not([hidden])")
        return p

    def pick(self, name):
        self.page.click(f'#view-lineup .pick[aria-label="Select {name}"]')

    def counts(self):
        p = self.page
        return [p.locator(f"#{z} .row:not(.empty)").count() for z in ("starters", "bench", "ir")]


class Lineup(AppCase):
    def test_sign_in_errors_then_success(self):
        p = self.page; p.goto(URL)
        p.fill("#email", "u1@example.test"); p.fill("#password", "wrong"); p.click("#login button[type=submit]")
        p.wait_for_selector("#loginMsg:has-text('Invalid login')")
        p.fill("#password", "test"); p.click("#login button[type=submit]")
        p.wait_for_selector("#app:not([hidden])")
        self.assertIn("Team 1", p.inner_text("#teamName"))
        self.assertEqual(self.counts(), [9, 10, 1])
        self.assertIn("setting the lineup for week 13", p.inner_text("#weekLine").lower())
        self.assertIn("Locks in 8 hours", p.inner_text("#lockCard"))

    def test_change_password_form(self):
        p = self.login()
        p.click("#pwOpen"); p.fill("#pwNew", "short"); p.fill("#pwAgain", "short"); p.evaluate("document.querySelector('#pwForm').requestSubmit()")
        self.assertTrue(p.locator("#pwMsg.err").count() == 1 or p.evaluate("!document.querySelector('#pwNew').checkValidity()"))
        p.fill("#pwNew", "a-long-password"); p.fill("#pwAgain", "different-one"); p.click("#pwForm button[type=submit]")
        p.wait_for_selector("#pwMsg.err:has-text('do not match')")
        p.fill("#pwAgain", "a-long-password"); p.click("#pwForm button[type=submit]")
        p.wait_for_selector("#pwMsg:has-text('Password changed')")
        self.assertEqual(p.input_value("#pwNew"), "")
        p.click("#pwCancel"); self.assertTrue(p.locator("#pwForm").is_hidden())

    def test_move_to_starters_is_saved_in_the_database(self):
        p = self.login()
        self.pick("MLB Hitter 5"); p.click('[data-moveto="starters"]')
        p.wait_for_selector("#msg:has-text('moved to starters')")
        self.assertEqual(self.counts(), [10, 9, 1])
        self.assertEqual(sql("select count(*) from lineups where team_id = 1 and week = 13"), "10")
        p.reload(); p.wait_for_selector("#app:not([hidden])")
        self.assertEqual(self.counts(), [10, 9, 1])

    def test_full_starters_need_a_swap(self):
        p = self.login()
        self.pick("MLB Hitter 5"); p.click('[data-moveto="starters"]'); p.wait_for_selector("#msg:has-text('moved')")
        self.pick("MLB Hitter 6")
        self.assertTrue(p.locator('[data-moveto="starters"]').is_disabled())
        p.click('#starters .row:has-text("NHL Skater 1") [data-swap]')
        p.wait_for_selector("#msg:has-text('swapped')")
        self.assertEqual(sql("select count(*) from lineups l join players p on p.id = l.player_id where l.team_id = 1 and l.week = 13 and p.name in ('MLB Hitter 6')"), "1")
        self.assertEqual(sql("select count(*) from lineups l join players p on p.id = l.player_id where l.team_id = 1 and l.week = 13 and p.name = 'NHL Skater 1'"), "0")
        self.assertEqual(self.counts()[0], 10)

    def test_ir_rules(self):
        p = self.login()
        self.pick("MLB Hitter 4")                       # Questionable: not an official designation
        self.assertTrue(p.locator('[data-moveto="ir"]').is_disabled())
        p.click("[data-cancel]")
        self.pick("MLB Hitter 3"); p.click('[data-moveto="ir"]'); p.wait_for_selector("#msg:has-text('moved to ir')")
        self.assertEqual(self.counts(), [9, 9, 2])
        self.assertEqual(sql("select slot from roster_spots rs join players p on p.id = rs.player_id where p.name = 'MLB Hitter 3'"), "ir")
        self.pick("MLB Hitter 7"); p.click('[data-moveto="bench"]'); p.wait_for_selector("#msg:has-text('moved to bench')")
        self.assertEqual(self.counts(), [9, 10, 1])

    def test_a_starter_sent_to_ir_leaves_the_lineup(self):
        p = self.login()
        self.pick("MLB Hitter 2"); p.click('[data-moveto="ir"]'); p.wait_for_selector("#msg:has-text('moved to ir')")
        self.assertEqual(self.counts(), [8, 10, 2])
        self.assertEqual(sql("select count(*) from lineups where team_id = 1 and week = 13"), "8")

    def test_stale_page_gets_the_databases_answer(self):
        p = self.login()
        sql(f"delete from dev_clock; insert into dev_clock (at) values ('{LOCKED} America/New_York');")    # noon passed while the page sat open
        self.pick("MLB Hitter 5"); p.click('[data-moveto="starters"]')
        p.wait_for_selector("#msg.err")
        self.assertIn("locked", p.inner_text("#msg").lower())
        p.wait_for_selector("#lockCard.locked")
        self.assertEqual(sql("select count(*) from lineups where team_id = 1 and week = 13"), "9")

    def test_locked_lineup_allows_bench_and_ir_only(self):
        sql(f"delete from dev_clock; insert into dev_clock (at) values ('{LOCKED} America/New_York');")
        p = self.login()
        self.assertIn("Locked", p.inner_text("#lockCard"))
        self.pick("MLB Hitter 5")
        self.assertTrue(p.locator('[data-moveto="starters"]').is_disabled())
        p.click("[data-cancel]")
        self.pick("MLB Hitter 3"); p.click('[data-moveto="ir"]'); p.wait_for_selector("#msg:has-text('moved to ir')")
        p.click('#view-lineup .pick[aria-label="Select MLB Hitter 1"]')
        self.assertTrue(p.locator('[data-moveto="ir"]').is_disabled())     # a locked starter can't go to IR (and has no designation anyway)

    def test_rival_lineup_is_always_visible_but_read_only(self):
        p = self.login()
        p.select_option("#teamSel", "2"); p.wait_for_function("document.querySelector('#teamName').textContent.includes('Team 2')")
        self.assertEqual(self.counts()[0], 5)                                  # visible even while lineups are open
        self.assertEqual(p.locator("#view-lineup .pick").count(), 0)           # read-only: no select buttons
        self.assertIn("only the owner", p.inner_text("#msg"))
        sql(f"delete from dev_clock; insert into dev_clock (at) values ('{LOCKED} America/New_York');")
        p.select_option("#teamSel", "1"); p.wait_for_function("document.querySelector('#teamName').textContent.includes('Team 1')")
        p.select_option("#teamSel", "2"); p.wait_for_function("document.querySelector('#teamName').textContent.includes('Team 2')")
        self.assertEqual(self.counts()[0], 5)

    def test_mulligan_after_the_lock(self):
        sql("insert into mulligans (team_id) values (1);")
        sql(f"delete from dev_clock; insert into dev_clock (at) values ('{LOCKED} America/New_York');")
        p = self.login()
        p.click('#starters .row:has-text("MLB Hitter 2") [data-mull]')
        p.click('#bench .row:has-text("MLB Hitter 5") [data-mullto]')
        p.wait_for_selector("#msg:has-text('Mulligan used')")
        self.assertEqual(sql("select count(*) from lineups l join players p on p.id = l.player_id where l.team_id = 1 and l.week = 13 and p.name = 'MLB Hitter 5'"), "1")
        self.assertEqual(p.locator("#view-lineup [data-mull]").count(), 0)
        self.assertIn("Used", p.inner_text("#mullBody"))

    def test_matchup_tab_shows_live_scores(self):
        sql("""
          insert into player_scores select 12, id, 10, 1.0, 11.5, true from players where name in ('MLB Hitter 1','NBA Guard 1');
          insert into player_scores select 12, id, 10, 1.0, 5.0, true from players where name = 'MLB Hitter 20';
          insert into team_weeks (team_id, week, starter_points, variety_bonus, total, best_starter) values (1, 12, 23, 2, 25, 11.5), (2, 12, 5, 0, 5, 5);
        """)
        p = self.login()
        p.click("#tab-matchup")
        p.wait_for_selector("#matchBanner:has-text('leading')")
        self.assertIn("20.0", p.inner_text("#matchBanner"))
        self.assertIn("25.0", p.inner_text("#matchCols"))
        self.assertIn("Live", p.inner_text("#matchBanner"))
        p.select_option("#weekSel", "30"); p.wait_for_selector("#matchBanner:has-text('No game')")


if __name__ == "__main__":
    unittest.main()
