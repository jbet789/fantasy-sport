"""The nightly backup: dump, encrypt, verify, and restore into a fresh database."""
import os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PGHOST": os.environ.get("PGHOST", "/tmp/pgtest"), "PGPORT": os.environ.get("PGPORT", "5499"), "PGUSER": os.environ.get("PGUSER", "postgres")}
A, B = "fl_bk_a", "fl_bk_b"
PASS = "correct horse battery staple"


def psql(db, q):
    p = subprocess.run(["psql", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", db], input=q, text=True, capture_output=True, env=ENV)
    if p.returncode: raise AssertionError(p.stderr)
    return p.stdout.strip()


def backup(*args, db=A, pw=PASS):
    return subprocess.run([sys.executable, str(ROOT / "tools/backup.py"), *args], capture_output=True, text=True, env={**ENV, "DATABASE_URL": db, "BACKUP_PASSPHRASE": pw})


class Backup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for db in (A, B):
            subprocess.run([str(ROOT / "backend/tests/run.sh")], check=True, env={**ENV, "DB": db}, capture_output=True)
        psql(A, """select t.fixture(); select t.give(1,'MLB','Hitter',12); select t.give(2,'NBA','Guard',3);
                   insert into matchups (week, kind, team_a, team_b) values (12, 'regular', 1, 2);
                   insert into activity_log (kind, message) values ('note', 'Team 1 signed a hitter');""")
        cls.dir = tempfile.mkdtemp(); cls.file = os.path.join(cls.dir, "league.sql.gz.gpg")

    def counts(self, db):
        return psql(db, "select (select count(*) from players), (select count(*) from roster_spots), (select count(*) from teams), (select count(*) from matchups), (select count(*) from activity_log)")

    def test_round_trip_into_a_fresh_database(self):
        r = backup("dump", self.file); self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("rows in", r.stdout)
        want = self.counts(A)
        self.assertEqual(psql(A, "select count(*) from job_runs where job = 'backup' and last_ok_at is not null"), "1")     # the health page sees it
        self.assertNotEqual(want, self.counts(B))
        r = backup("verify", self.file, db=B); self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("roster_spots", r.stdout)
        r = backup("restore", self.file, db=B); self.assertNotEqual(r.returncode, 0); self.assertIn("--yes", r.stderr)      # asks first
        self.assertNotEqual(want, self.counts(B))
        r = backup("restore", self.file, "--yes", db=B); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.counts(B), want)
        # the numbering carries on after the restored rows instead of colliding with them
        psql(B, "insert into matchups (week, kind, team_a, team_b) values (13, 'regular', 3, 4)")
        # restoring over a changed database puts it back as it was
        psql(B, "delete from roster_spots")
        backup("restore", self.file, "--yes", db=B); self.assertEqual(self.counts(B), want)

    def test_file_is_unreadable_without_the_passphrase(self):
        backup("dump", self.file)
        raw = Path(self.file).read_bytes()
        self.assertNotIn(b"Team 1", raw); self.assertNotIn(b"COPY", raw); self.assertNotIn(b"u1@example.test", raw)
        r = backup("verify", self.file, pw="a completely different phrase"); self.assertNotEqual(r.returncode, 0)

    def test_short_or_missing_passphrase_is_refused(self):
        r = backup("dump", self.file, pw="short"); self.assertNotEqual(r.returncode, 0); self.assertIn("12 characters", r.stderr)

    def test_an_empty_dump_is_not_accepted_as_a_backup(self):
        psql(B, "truncate league_settings, scoring_weights cascade")
        try:
            r = backup("dump", os.path.join(self.dir, "bad.gpg"), db=B)
            self.assertNotEqual(r.returncode, 0); self.assertIn("does not look like a league backup", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.dir, "bad.gpg")))
        finally:
            subprocess.run([str(ROOT / "backend/tests/run.sh")], check=True, env={**ENV, "DB": B}, capture_output=True)


if __name__ == "__main__":
    unittest.main()
