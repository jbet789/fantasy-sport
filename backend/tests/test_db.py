"""Database tests. Each case runs inside one transaction that is rolled back, against a database built by tests/run.sh.

Run:  backend/tests/run.sh && python3 -m unittest backend.tests.test_db   (from the project root)
"""
import os, subprocess, unittest
from pathlib import Path

ENV = {**os.environ, "PGHOST": os.environ.get("PGHOST", "/tmp/pgtest"), "PGPORT": os.environ.get("PGPORT", "5499"), "PGUSER": os.environ.get("PGUSER", "postgres")}


def run_sql(body, db="fl_test"):
    script = "begin;\nselect t.fixture();\n" + body + "\nrollback;\n"
    p = subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", db], input=script, text=True, capture_output=True, env=ENV)
    if p.returncode != 0:
        raise AssertionError((p.stderr or p.stdout).strip())
    return p.stdout


class DbCase(unittest.TestCase):
    def sql(self, body):
        run_sql(body)
