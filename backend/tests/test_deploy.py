"""The production pieces: the scheduled tick and the one-time-each migration runner (both against real Postgres)."""
import os, subprocess, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import scoring_job as job, tick as tk

ENV = {**os.environ, "PGHOST": os.environ.get("PGHOST", "/tmp/pgtest"), "PGPORT": os.environ.get("PGPORT", "5499"), "PGUSER": os.environ.get("PGUSER", "postgres")}


def psql(db, q):
    p = subprocess.run(["psql", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", q], text=True, capture_output=True, env=ENV)
    if p.returncode:
        raise AssertionError(p.stderr)
    return p.stdout.strip()


class Tick(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([str(ROOT / "backend/tests/run.sh")], check=True, env={**ENV, "DB": "fl_tick"}, capture_output=True)
        cls.db = job.PsqlDb("fl_tick")
        cls.db.run("select t.fixture(); select t.give(1,'MLB','Hitter',3); select t.give(2,'MLB','Hitter',3);")

    def test_tick_completes_due_trades_and_reports_each_step(self):
        d = self.db
        d.run("""delete from dev_clock; insert into dev_clock (at) values ('2027-03-24 10:00 America/New_York');
                 delete from trade_items; delete from trades;
                 insert into trades (proposer, partner, status, accepted_at, veto_until) values (1, 2, 'pending_veto', app_now() - interval '30 hours', app_now() - interval '6 hours');
                 insert into trade_items select (select max(id) from trades), (select min(player_id) from roster_spots where team_id = 1), 1;
                 insert into trade_items select (select max(id) from trades), (select min(player_id) from roster_spots where team_id = 2), 2;""")
        out = tk.tick(d)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["trades_completed"], 1)
        self.assertEqual(d.rows("select status from trades")[0]["status"], "completed")
        self.assertEqual(tk.tick(d)["trades_completed"], 0)                  # repeating changes nothing
        beat = d.rows("select last_ok_at is not null as ok, last_error from job_runs where job = 'tick'")[0]
        self.assertTrue(beat["ok"]); self.assertIsNone(beat["last_error"])      # the health page's heartbeat

    def test_tick_makes_the_picks_of_a_draft_nobody_is_watching(self):
        d = self.db
        d.run("""delete from dev_clock; insert into dev_clock (at) values ('2027-01-01 12:00 America/New_York');
                 delete from draft_picks; delete from draft_queue; delete from draft_slots; delete from drafts;
                 select create_draft('main', 10); select start_draft((select max(id) from drafts));
                 update dev_clock set at = at + interval '5 minutes';""")      # five 60-second clocks ran out
        out = tk.tick(d)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["draft_picks_made"], 5)
        self.assertEqual(tk.tick(d)["draft_picks_made"], 0)
        d.run("delete from draft_picks; delete from draft_queue; delete from draft_slots; delete from drafts; delete from roster_spots where team_id > 2;")

    def test_one_failing_step_does_not_stop_the_others(self):
        class Flaky(job.PsqlDb):
            def rows(self, sql):
                if "settle_trades" in sql:
                    raise RuntimeError("boom")
                return super().rows(sql)
        out = tk.tick(Flaky("fl_tick"))
        self.assertEqual(len(out["errors"]), 1); self.assertIn("trades", out["errors"][0])
        self.assertIn("waiver_wins", out)
        beat = self.db.rows("select last_error from job_runs where job = 'tick'")[0]
        self.assertIn("boom", beat["last_error"])                              # a failing step is reported, not hidden


class Migrations(unittest.TestCase):
    URL = "postgresql:///fl_mig?host=/tmp/pgtest&port=5499&user=postgres"

    @classmethod
    def setUpClass(cls):
        subprocess.run(["psql", "-q", "-d", "postgres", "-c", "drop database if exists fl_mig", "-c", "create database fl_mig"], check=True, env=ENV)
        subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", "fl_mig", "-f", str(ROOT / "backend/tests/stubs.sql")], check=True, env=ENV)

    def run_tool(self):
        return subprocess.run([sys.executable, str(ROOT / "tools/apply_migrations.py")], env={**ENV, "DATABASE_URL": self.URL}, text=True, capture_output=True)

    def test_each_file_runs_once_and_a_rerun_is_quiet(self):
        first = self.run_tool(); self.assertEqual(first.returncode, 0, first.stderr)
        n = len(list((ROOT / "backend/supabase/migrations").glob("*.sql")))
        self.assertEqual(first.stdout.count("applied 0"), n)
        self.assertEqual(psql("fl_mig", "select count(*) from schema_migrations"), str(n))
        second = self.run_tool(); self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("nothing new to migrate", second.stdout)
        self.assertEqual(psql("fl_mig", "select count(*) from positions"), "17")
        self.assertEqual(psql("fl_mig", "select has_table_privilege('authenticated', 'schema_migrations', 'select')"), "f")

    def test_a_failing_file_is_rolled_back_and_not_recorded(self):
        subprocess.run(["psql", "-q", "-d", "postgres", "-c", "drop database if exists fl_mig2", "-c", "create database fl_mig2"], check=True, env=ENV)
        subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", "fl_mig2", "-f", str(ROOT / "backend/tests/stubs.sql")], check=True, env=ENV)
        import tools.apply_migrations as am
        real = am.MIG
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "mig_fail"; tmp.mkdir(exist_ok=True)
        for f in tmp.glob("*.sql"): f.unlink()
        (tmp / "001_ok.sql").write_text("create table a(x int);")
        (tmp / "002_bad.sql").write_text("create table b(x int); select 1/0;")
        am.MIG = tmp
        try:
            with self.assertRaises(SystemExit):
                am.main("postgresql:///fl_mig2?host=/tmp/pgtest&port=5499&user=postgres")
        finally:
            am.MIG = real
        self.assertEqual(psql("fl_mig2", "select string_agg(name, ',') from schema_migrations"), "001_ok.sql")
        self.assertEqual(psql("fl_mig2", "select to_regclass('b') is null"), "t")


if __name__ == "__main__":
    unittest.main()


class Ingest(unittest.TestCase):
    """tools/ingest.py against real Postgres, with a fake feed in place of the web."""
    @classmethod
    def setUpClass(cls):
        subprocess.run([str(ROOT / "backend/tests/run.sh")], check=True, env={**ENV, "DB": "fl_ingest"}, capture_output=True)
        cls.db = job.PsqlDb("fl_ingest")
        cls.db.run("delete from dev_clock; insert into dev_clock (at) values ('2027-04-07 12:00 America/New_York');")

    def test_stats_run_stores_once_and_leaves_a_heartbeat(self):
        from tools import ingest
        from tools.feeds import mlb
        at = "2027-04-06T23:10:00Z"
        line = {"src": "mlb", "ext": "9001", "game": "mlb:9001", "at": at, "final": True, "stats": {"Games played": 1, "Home runs": 2},
                "create": {"name": "Ingest Slugger", "sport": "MLB", "position": "Hitter"}}

        class Reader:
            @staticmethod
            def fetch_lines(due, **kw):
                keys = due([{"key": "mlb:9001", "final": True}])
                return ([line] if keys else []), []
        ingest.READERS["mlb"] = Reader
        try:
            r1 = ingest.run_stats(self.db, "mlb")
            self.assertEqual((r1["lines"], r1["stored"]["applied"]), (1, 2))   # two stats
            hr = self.db.rows("select s.value::int as v from stat_lines s join players p on p.id = s.player_id where p.name = 'Ingest Slugger' and s.stat = 'Home runs'")
            self.assertEqual([x["v"] for x in hr], [2])
            beat = self.db.rows("select last_ok_at is not null as ok from job_runs where job = 'feed_mlb'")
            self.assertEqual(beat, [{"ok": True}])
            self.assertEqual(self.db.rows("select count(*) as n from players where name = 'Ingest Slugger'")[0]["n"], 1)
        finally:
            ingest.READERS["mlb"] = mlb

    def test_injuries_run_sets_statuses_and_leaves_a_heartbeat(self):
        from tools import ingest
        from tools.feeds import injuries
        self.db.run("insert into players (name, sport, position, ranking) values ('Hurt Hitter', 'MLB', 'Hitter', 500) on conflict do nothing;")
        orig = injuries.FETCHERS["mlb"]
        injuries.FETCHERS["mlb"] = lambda: {"src": "mlb", "sport": "MLB", "complete": True, "players": [{"ext": "777", "name": "Hurt Hitter", "status": "Injured List"}]}
        try:
            r = ingest.run_injuries(self.db, "mlb")
            self.assertEqual((r["listed"], r["stored"]["set"]), (1, 1))
            self.assertEqual(self.db.rows("select injury_status, injury_source from players where name = 'Hurt Hitter'"), [{"injury_status": "Injured List", "injury_source": "feed"}])
            self.assertEqual(self.db.rows("select last_ok_at is not null as ok from job_runs where job = 'feed_mlb_injuries'"), [{"ok": True}])
            injuries.FETCHERS["mlb"] = lambda: None                      # an off-season report: change nothing, still report healthy
            self.assertIn("skipped", ingest.run_injuries(self.db, "mlb"))
            self.assertEqual(self.db.rows("select injury_status from players where name = 'Hurt Hitter'")[0]["injury_status"], "Injured List")
        finally:
            injuries.FETCHERS["mlb"] = orig

    def test_pool_dry_run_then_apply(self):
        from tools import ingest
        from tools.feeds import pool
        entries = pool.csv_pool("src,ext,name,sport,position,ranking\nnba,5001,Pool Guard,NBA,Guard,1\nnba,5002,Pool Big,NBA,Big,2\n")
        res = ingest.run_pool(self.db, entries, apply=False)
        self.assertEqual(res["players"], 2)
        self.assertEqual(self.db.rows("select count(*) as n from players where name like 'Pool %'")[0]["n"], 0)
        res = ingest.run_pool(self.db, pool.csv_pool("src,ext,name,sport,position,ranking\nnba,5001,Pool Guard,NBA,Guard,1\nnba,5002,Pool Big,NBA,Big,2\n"), apply=True)
        self.assertEqual(res["stored"]["created"], 2)
        r = self.db.rows("select name, ranking from players where name like 'Pool %' order by ranking")
        self.assertEqual([x["name"] for x in r], ["Pool Guard", "Pool Big"])


class Practice(unittest.TestCase):
    def test_a_full_practice_draft_with_bots_passes_every_check(self):
        from tools import practice_draft
        res = practice_draft.run("fl_practice", seed=7, log=lambda *_: None)
        self.assertEqual(res["problems"], [])
        self.assertEqual(res["picks"], 240)
        self.assertGreater(res["auto"], 0); self.assertGreater(res["manual"], 0)


class Preflight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([str(ROOT / "backend/tests/run.sh")], check=True, env={**ENV, "DB": "fl_preflight"}, capture_output=True)
        cls.db = job.PsqlDb("fl_preflight")
        cls.db.run("select t.fixture();")

    def levels(self, rep):
        return {what: lv for lv, what, _ in rep.rows}

    def test_a_fresh_league_is_not_ready_and_each_problem_is_named_then_it_becomes_ready(self):
        import tempfile
        from tools import preflight
        root = Path(tempfile.mkdtemp()); (root / "app").mkdir()
        (root / "app" / "config.js").write_text('window.FL_CONFIG = { url: "", anonKey: "" };')
        names = [f.name for f in sorted((ROOT / "backend/supabase/migrations").glob("*.sql"))]
        self.db.run("create table if not exists public.schema_migrations (name text primary key, applied_at timestamptz default now()); delete from public.schema_migrations;")
        rep = preflight.run(self.db, env={}, root=root)
        text = rep.text()
        self.assertIn("NOT READY", text)
        for needle in ("app/config.js is blank", "database is missing", "TEST CLOCK", "player pool is too small", "scoring tick has never run"):
            self.assertIn(needle, text)
        self.assertIn("12 teams, all with an owner", text)                      # what is right is reported as right
        # now make everything right
        (root / "app" / "config.js").write_text('window.FL_CONFIG = { url: "https://abc.supabase.co", anonKey: "' + "k" * 40 + '" };')
        vals = ",".join(f"('{n}')" for n in names)
        self.db.run(f"insert into public.schema_migrations (name) values {vals}; delete from dev_clock;")
        self.db.run("insert into players (name, sport, position, ranking) select p.sport || ' extra ' || g, p.sport, p.position, 1000 + g from positions p, generate_series(1, 300) g where p.sport <> 'F1';")
        self.db.run("select record_job('tick', true); select record_job('backup', true); select record_job('feed_mlb', true);")
        self.db.run("select create_draft('main', 20);")
        rep = preflight.run(self.db, env={"BACKUP_PASSPHRASE": "x"}, root=root)
        self.assertEqual([r for r in rep.rows if r[0] == "FAIL"], [], rep.text())
        self.assertIn("READY TO LAUNCH", rep.text())
