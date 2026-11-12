"""Launch preflight: is the league ready? Prints PASS / WARN / FAIL lines in plain English with what to do about each.

    DATABASE_URL=... python3 tools/preflight.py        (or the "Launch check" workflow in GitHub Actions)

FAIL = fix before launch. WARN = worth a look (some warnings are normal before launch). Exit code 1 only if something FAILs.
"""
import json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import scoring_job

MIG = ROOT / "backend" / "supabase" / "migrations"
MIN_PER_SPORT = {"NFL": 120, "NBA": 80, "MLB": 200, "NHL": 120, "MLS": 60, "F1": 12}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, what, fix=""):
        self.rows.append((level, what, fix))

    def fails(self):
        return [r for r in self.rows if r[0] == "FAIL"]

    def text(self):
        out = [f"{lv}  {what}" + (f"\n      -> {fix}" if fix and lv != "PASS" else "") for lv, what, fix in self.rows]
        f, w = len(self.fails()), sum(1 for r in self.rows if r[0] == "WARN")
        out.append("")
        out.append("READY TO LAUNCH" + (f" (with {w} warning{'s' if w != 1 else ''} to read)" if w else "") if not f else f"NOT READY: {f} problem{'s' if f != 1 else ''} to fix first")
        return "\n".join(out)


def safe(rep, label, fn):
    try:
        fn()
    except Exception as e:
        rep.add("FAIL", f"{label}: could not check ({str(e).splitlines()[0][:160]})", "Check the SUPABASE_DB_URL secret and that 'Update database' has been run.")


def run(db, env=None, root=ROOT):
    env = os.environ if env is None else env
    rep = Report()
    q = db.rows

    # secrets and files
    rep.add("PASS" if env.get("BACKUP_PASSPHRASE") else "WARN", "BACKUP_PASSPHRASE secret is set" if env.get("BACKUP_PASSPHRASE") else "BACKUP_PASSPHRASE secret is missing",
            "Add it under Settings > Secrets and variables > Actions. Backups cannot be encrypted without it.")
    cfg = (root / "app" / "config.js").read_text(encoding="utf-8") if (root / "app" / "config.js").exists() else ""
    url_ok = bool(re.search(r'url:\s*"https://[^"]+"', cfg)) and bool(re.search(r'anonKey:\s*"[^"]{20,}"', cfg))
    rep.add("PASS" if url_ok else "FAIL", "app/config.js has the Supabase URL and anon key" if url_ok else "app/config.js is blank (the website cannot find the database)",
            "Paste the Project URL and the anon public key from Supabase > Settings > API into app/config.js and commit it.")

    def migrations():
        want = sorted(f.name for f in MIG.glob("*.sql"))
        have = {r["name"] for r in q("select name from public.schema_migrations")}
        missing = [w for w in want if w not in have]
        rep.add("PASS" if not missing else "FAIL", f"database is up to date ({len(want)} migrations)" if not missing else f"database is missing: {', '.join(missing)}", "Run Actions > Update database.")
    safe(rep, "migrations", migrations)

    def settings():
        s = q("select count(*) as n from league_settings")[0]["n"]
        pos = q("select count(*) as n from positions")[0]["n"]
        rep.add("PASS" if s == 1 and pos >= 17 else "FAIL", f"league settings and scoring are loaded ({pos} positions)" if s == 1 and pos >= 17 else "league settings or scoring are missing",
                "Run Actions > Update database (it loads the scoring seed).")
        clock = q("select count(*) as n from dev_clock")[0]["n"]
        rep.add("PASS" if clock == 0 else "FAIL", "no test clock is set" if clock == 0 else "a TEST CLOCK is set: the league would run on pretend time",
                "In the Supabase SQL Editor run:  delete from dev_clock;")
    safe(rep, "settings", settings)

    def teams():
        t = q("select count(*) as n, count(owner_id) as owned from teams")[0]
        rep.add("PASS" if t["n"] == 12 and t["owned"] == 12 else "FAIL", "12 teams, all with an owner" if t["n"] == 12 and t["owned"] == 12 else f"{t['n']} teams, {t['owned']} with an owner (need 12 and 12)",
                "Create each person under Authentication > Users, then run setup_league (Launch Guide, step on League setup).")
        c = q("select count(*) as n from profiles where is_commissioner")[0]["n"]
        rep.add("PASS" if c == 1 else "FAIL", "one commissioner" if c == 1 else f"{c} commissioners (need exactly 1)", "Re-run setup_league with the commissioner marked.")
        slots = q("select count(distinct draft_slot) as n from teams where draft_slot is not null")[0]["n"]
        rep.add("PASS" if slots == 12 else "FAIL", "draft order is set (12 slots)" if slots == 12 else f"draft order has {slots} slots (need 12)", "Run setup_league with p_shuffle true, or set draft_slot per team.")
    safe(rep, "teams", teams)

    def pool():
        rows = {r["sport"]: r["n"] for r in q("select sport::text as sport, count(*) as n from players where active group by 1")}
        total = sum(rows.values())
        short = [f"{s} {rows.get(s, 0)} (want {m}+)" for s, m in MIN_PER_SPORT.items() if rows.get(s, 0) < m]
        rep.add("PASS" if not short else "FAIL", f"player pool is loaded ({total} active players)" if not short else "player pool is too small: " + "; ".join(short),
                "Load it: python3 tools/ingest.py pool --sport mlb|nhl|nfl|f1 --apply, and pool-csv for NBA and MLS (see the Launch Guide).")
        unranked = q("select count(*) as n from players where active and ranking is null")[0]["n"]
        rep.add("PASS" if unranked == 0 else "WARN", "every player has a draft ranking" if unranked == 0 else f"{unranked} players have no ranking (autopick puts them last)", "Fine for stragglers; re-run the pool loader for the rest.")
        dup = q("select count(*) as n from (select lower(name), sport from players where active group by 1, 2 having count(*) > 1) x")[0]["n"]
        rep.add("PASS" if dup == 0 else "WARN", "no duplicate player names" if dup == 0 else f"{dup} player names appear twice in one sport", "Check the pool for a player loaded from two sources; hide one with active = false.")
    safe(rep, "pool", pool)

    def draft():
        d = q("select status, count(*) as n from drafts where kind = 'main' group by 1")
        st = {r["status"]: r["n"] for r in d}
        if not st:
            rep.add("WARN", "the main draft has not been created yet", "Commissioner tab > Create main draft (do it a day before).")
        elif st.get("live") or st.get("done"):
            rep.add("WARN", "the main draft has already started or finished", "Expected once the league is under way.")
        else:
            rep.add("PASS", "the main draft is created and waiting")
    safe(rep, "draft", draft)

    def jobs():
        rows = q("select job, last_ok_at is not null as ever, job_status(last_ok_at, case when job = 'tick' then interval '10 minutes' when job = 'backup' then interval '1 day' else interval '1 hour' end) as st, last_error "
                 "from job_runs order by job")
        seen = {r["job"]: r for r in rows}
        for job, label in (("tick", "scoring tick"), ("backup", "nightly backup")):
            r = seen.get(job)
            if not r:
                rep.add("FAIL" if job == "tick" else "WARN", f"{label} has never run", f"Actions > {'Scoring tick' if job == 'tick' else 'Nightly backup'} > Run workflow, then check its log.")
            else:
                rep.add("PASS" if r["st"] == "ok" else "FAIL" if (r["st"] == "down" and job == "tick") else "WARN", f"{label} is {r['st']}" + (f" ({r['last_error']})" if r["last_error"] else ""),
                        "Open the Actions tab and read the latest run.")
        feeds = [r for r in rows if r["job"].startswith("feed_") and r["job"] != "feed_mls"]
        if not feeds:
            rep.add("WARN", "the stat and injury feeds have never run", "Actions > Stats > Run workflow. Before the seasons start they find no games, which is fine.")
        for r in feeds:
            rep.add("PASS" if r["st"] == "ok" else "WARN", f"{r['job']} is {r['st']}" + (f" ({r['last_error']})" if r["last_error"] else ""),
                    "Open the Actions tab > Stats and read the failing step; the Launch Guide lists the common causes.")
    safe(rep, "jobs", jobs)
    return rep


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not url:
        raise SystemExit("Set DATABASE_URL")
    rep = run(scoring_job.PsqlDb(url))
    print(rep.text())
    sys.exit(1 if rep.fails() else 0)
