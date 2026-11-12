"""Nightly backup and restore of the league's data.

  python3 tools/backup.py dump OUT        write an encrypted, compressed copy of every league table
  python3 tools/backup.py verify FILE     decrypt it and list what is inside (nothing is changed)
  python3 tools/backup.py restore FILE --yes    replace the league's data with the copy

Environment: DATABASE_URL (the database; a plain database name works for a local one) and BACKUP_PASSPHRASE.
Only the data in the `public` schema is saved. The table layout comes from the migrations, so to rebuild from nothing:
run "Update database" first (empty tables), then restore. Sign-in accounts live in Supabase's own auth tables and are
not part of the copy; the passwords never are. If those are lost, run setup_league again for the 12 emails.
The copy is encrypted because a public repository's workflow files can be downloaded by anyone with a GitHub login.
"""
import gzip, os, re, subprocess, sys, tempfile
from pathlib import Path

SKIP = ("schema_migrations", "dev_clock", "job_runs")          # layout bookkeeping and the test clock are not league data
REQUIRED = ("league_settings", "scoring_weights")  # filled by the seed; if absent the copy is not a real one


def conn():
    url = os.environ.get("DATABASE_URL") or "fl_test"
    return url                                         # pg_dump and psql take either a URL or a database name


def env():
    e = dict(os.environ)
    if "://" not in conn():                            # a local test database
        e.setdefault("PGHOST", "/tmp/pgtest"); e.setdefault("PGPORT", "5499"); e.setdefault("PGUSER", "postgres")
    return e


def passphrase_file():
    pw = os.environ.get("BACKUP_PASSPHRASE")
    if not pw or len(pw) < 12:
        sys.exit("BACKUP_PASSPHRASE is missing or shorter than 12 characters")
    f = tempfile.NamedTemporaryFile("w", delete=False); os.chmod(f.name, 0o600); f.write(pw); f.close()
    return f.name


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, **kw)
    if p.returncode != 0:
        sys.exit(f"{cmd[0]} failed: " + (p.stderr or b"").decode(errors="replace").strip()[:800])
    return p.stdout


def dump_sql():
    cmd = ["pg_dump", "--data-only", "--schema=public", "--no-owner", "--no-privileges", *[f"--exclude-table=public.{t}" for t in SKIP], conn()]
    return run(cmd, env=env())


def encrypt(data, out):
    pf = passphrase_file()
    try:
        enc = run(["gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback", "--passphrase-file", pf, "--symmetric", "--cipher-algo", "AES256", "--compress-algo", "none"], input=gzip.compress(data, 9))
    finally:
        os.unlink(pf)
    Path(out).write_bytes(enc)


def decrypt(path):
    pf = passphrase_file()
    try:
        plain = run(["gpg", "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase-file", pf, "--decrypt", str(path)])
    finally:
        os.unlink(pf)
    return gzip.decompress(plain)


def inventory(sql):
    """{table: rows} read from the COPY blocks of a data-only dump."""
    counts, cur = {}, None
    for line in sql.decode("utf-8", "replace").splitlines():
        m = re.match(r"COPY public\.(\w+) ", line)
        if m:
            cur = m.group(1); counts[cur] = 0
        elif line == "\\." and cur:
            cur = None
        elif cur:
            counts[cur] += 1
    return counts


def check(counts):
    missing = [t for t in REQUIRED if not counts.get(t)]
    if missing:
        sys.exit("This does not look like a league backup; empty or missing: " + ", ".join(missing))


def restore(sql):
    tables = [t for t in run(["psql", "-At", "-d", conn(), "-c", "select tablename from pg_tables where schemaname = 'public' order by 1"], env=env()).decode().split() if t not in SKIP]
    script = ("begin;\nset local session_replication_role = replica;\n"          # the league's rule triggers must not re-judge old rows
              "truncate " + ", ".join(f"public.{t}" for t in tables) + " restart identity cascade;\n").encode() + sql + b"\ncommit;\n"
    run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-1", "-d", conn()], env=env(), input=script)


def note_health(size, rows):
    """Tell the app the backup ran (its health page shows when). Never fails the backup."""
    try:
        detail = '{"bytes": %d, "rows": %d}' % (size, rows)
        run(["psql", "-q", "-At", "-d", conn(), "-c", f"select record_job('backup', true, null, '{detail}'::jsonb)"], env=env())
    except SystemExit as e:
        print("note: could not record the run for the health page:", e, file=sys.stderr)


def main(argv):
    if len(argv) < 2 or argv[0] not in ("dump", "verify", "restore"):
        sys.exit(__doc__)
    cmd, path = argv[0], argv[1]
    if cmd == "dump":
        sql = dump_sql(); counts = inventory(sql); check(counts)
        encrypt(sql, path)
        print(f"backup written: {path} ({Path(path).stat().st_size} bytes, {sum(counts.values())} rows in {len(counts)} tables)")
        note_health(Path(path).stat().st_size, sum(counts.values()))
    else:
        counts = inventory(decrypt(path)); check(counts)
        if cmd == "verify":
            for t, n in sorted(counts.items()):
                print(f"{t:20} {n}")
        else:
            if "--yes" not in argv:
                sys.exit("restore replaces ALL current league data; add --yes to go ahead")
            restore(decrypt(path))
            print(f"restored {sum(counts.values())} rows in {len(counts)} tables")


if __name__ == "__main__":
    main(sys.argv[1:])
