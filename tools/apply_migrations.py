"""Apply the database files to a hosted Postgres, each exactly once, in order.

  DATABASE_URL=postgresql://... python3 tools/apply_migrations.py

Keeps a table schema_migrations of what has run. A deployed migration is never edited: a change is a new, higher-numbered
file. The seed (scoring weights and positions) is an upsert and runs every time, so edits to engine/config.json flow through.
"""
import os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIG = ROOT / "backend" / "supabase" / "migrations"
SEED = ROOT / "backend" / "supabase" / "seed.sql"


def psql(url, *args, stdin=None):
    p = subprocess.run(["psql", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", url, *args], input=stdin, text=True, capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"psql failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout


def main(url):
    psql(url, "-c", "create table if not exists public.schema_migrations (name text primary key, applied_at timestamptz not null default now())")
    done = set(psql(url, "-c", "select name from public.schema_migrations").split())
    ran = []
    for f in sorted(MIG.glob("*.sql")):
        if f.name in done:
            continue
        # one transaction per file, and the record is written in the same transaction
        psql(url, "-1", "-f", str(f), "-c", f"insert into public.schema_migrations (name) values ('{f.name}')")
        ran.append(f.name)
        print("applied", f.name)
    psql(url, "-f", str(SEED))
    print("seed applied;", "nothing new to migrate" if not ran else f"{len(ran)} migration(s) applied")
    # the schema_migrations table is the deployer's bookkeeping: clients never see it
    psql(url, "-c", "revoke all on public.schema_migrations from public, anon, authenticated; alter table public.schema_migrations enable row level security")


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not url:
        raise SystemExit("Set DATABASE_URL")
    main(url)
