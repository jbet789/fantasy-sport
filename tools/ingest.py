"""Stat and player loading (GitHub workflow 'Stats', or by hand).

  python3 tools/ingest.py stats --sport mlb|nhl|nba|nfl|f1      read recent games from the free feed, store them
  python3 tools/ingest.py injuries --sport nfl|mlb|nba|nhl|mls  read injury designations and store them (hand-set ones are left alone)
  python3 tools/ingest.py csv FILE.csv                          hand-fed stats (MLS): ext,name,position,date,stat,value
  python3 tools/ingest.py pool --sport mlb|nhl|nfl|f1 [--season 2026] [--apply]     player pool from last season (shows a summary; --apply writes)
  python3 tools/ingest.py pool-csv FILE.csv [--apply]           hand-fed pool (NBA, MLS): src,ext,name,sport,position,score|ranking
Add --dry-run to read and parse without writing. The database is DATABASE_URL (or the first argument of --db). Every stats run leaves a
heartbeat 'feed_<sport>' that the app's health page shows.
"""
import argparse, json, os, sys, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import scoring_job
from tools.feeds import mlb, nhl, nba, nfl, f1, csv_import, injuries as inj_mod, pool as pool_mod

READERS = {"mlb": mlb, "nhl": nhl, "nba": nba, "nfl": nfl, "f1": f1}


def q(text):
    return "'" + str(text).replace("'", "''") + "'"


def jsonb(obj):
    s = json.dumps(obj, ensure_ascii=False)
    tag = "j"
    while f"${tag}$" in s:
        tag += "x"
    return f"${tag}${s}${tag}$::jsonb"


def due_fn(db):
    def due(games):
        if not games:
            return []
        return [r["k"] for r in db.rows(f"select unnest(feed_due({jsonb(games)})) as k")]
    return due


def heartbeat(db, job, ok, error, detail):
    try:
        db.rows(f"select record_job({q(job)}, {'true' if ok else 'false'}, {q(error)}, {q(json.dumps(detail))}::jsonb) is null as ok")
    except Exception:
        traceback.print_exc()


def store_stats(db, lines, create=True, hold=True):
    if not lines:
        return {"applied": 0, "unmatched": [], "rejected": [], "created": 0}
    return db.rows(f"select ingest_stats({jsonb(lines)}, {str(create).lower()}, {str(hold).lower()}) as r")[0]["r"]


def run_stats(db, sport, lookback=None, dry=False):
    reader = READERS[sport]
    kw = {} if lookback is None else {"lookback_hours": lookback}
    lines, errors = reader.fetch_lines(due_fn(db), **kw)
    res = {"lines": len(lines), "fetch_errors": errors}
    if not dry:
        res["stored"] = store_stats(db, lines)
        bad = res["stored"].get("rejected") or []
        heartbeat(db, f"feed_{sport}", not errors, "; ".join(errors + [str(b) for b in bad[:3]]), {"lines": len(lines), "applied": res["stored"].get("applied", 0)})
    return res


def run_injuries(db, sport, dry=False):
    payload = inj_mod.FETCHERS[sport]()
    if payload is None:                                       # nothing current to report (NFL off-season): change nothing
        res = {"skipped": "no current injury report"}
        if not dry:
            heartbeat(db, f"feed_{sport}_injuries", True, "", res)
        return res
    res = {"listed": len(payload["players"])}
    if not dry:
        res["stored"] = db.rows(f"select ingest_injuries({jsonb(payload)}) as r")[0]["r"]
        s = res["stored"]
        problem = "refused to clear most injuries at once; the feed may be broken" if s.get("suspicious_clear_skipped") else ""
        heartbeat(db, f"feed_{sport}_injuries", not problem, problem, {"listed": res["listed"], "set": s["set"], "cleared": s["cleared"], "unmatched": len(s["unmatched"])})
    return res


def run_csv(db, path, dry=False):
    lines = csv_import.parse_stats(Path(path).read_text(encoding="utf-8"))
    res = {"lines": len(lines)}
    if not dry:
        res["stored"] = store_stats(db, lines)
        heartbeat(db, "feed_mls", True, "", {"lines": len(lines), "applied": res["stored"].get("applied", 0)})
    return res


def load_weights(db):
    w = {}
    for r in db.rows("select profile, stat, weight::float8 as weight from scoring_weights"):
        w.setdefault(r["profile"], {})[r["stat"]] = r["weight"]
    spread = {(r["sport"], r["position"]): (r["profile"], r["fallback_avg"], r["fallback_sd"]) for r in
              db.rows("select sport::text as sport, position, profile, fallback_avg::float8 as fallback_avg, fallback_sd::float8 as fallback_sd from positions")}
    return w, spread


def store_pool(db, ranked, hold=False):
    return db.rows(f"select ingest_players({jsonb(ranked)}, {str(hold).lower()}) as r")[0]["r"]


def run_pool(db, entries, apply=False):
    weights, spread = load_weights(db)
    ranked = pool_mod.rank_pool(entries, weights, spread)
    by_sport = {}
    for r in ranked:
        by_sport[r["sport"]] = by_sport.get(r["sport"], 0) + 1
    res = {"players": len(ranked), "by_sport": by_sport, "top": [f"{r['ranking']}. {r['name']} ({r['sport']} {r['position']})" for r in ranked[:15]]}
    if apply:
        res["stored"] = store_pool(db, ranked)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["stats", "injuries", "csv", "pool", "pool-csv"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--sport", choices=sorted(set(READERS) | set(inj_mod.FETCHERS) | {"all"}))
    ap.add_argument("--season", type=int)
    ap.add_argument("--lookback-hours", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true", help="write the pool even if some sports failed")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db")
    a = ap.parse_args(argv)
    db = scoring_job.PsqlDb(a.db or os.environ.get("DATABASE_URL") or "fl_test")
    try:
        if a.cmd == "stats":
            res = run_stats(db, a.sport, a.lookback_hours, a.dry_run)
        elif a.cmd == "injuries":
            res = run_injuries(db, a.sport, a.dry_run)
        elif a.cmd == "csv":
            res = run_csv(db, a.file, a.dry_run)
        elif a.cmd == "pool":
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            wanted = ["MLB", "NHL", "NFL", "F1"] if a.sport in (None, "all") else [a.sport.upper()]
            entries, failed = [], []
            for sp in wanted:
                try:
                    entries += pool_mod.FETCHERS[sp](a.season or pool_mod.default_season(sp, now))
                except Exception as e:                    # one source down must not lose the others
                    failed.append(f"{sp}: {e}")
            if a.sport in (None, "all"):                  # NBA and MLS come from files kept in the repository (data/pool_nba.csv, data/pool_mls.csv)
                for sp in ("nba", "mls"):
                    f = Path(__file__).resolve().parent.parent / "data" / f"pool_{sp}.csv"
                    if f.exists():
                        entries += pool_mod.csv_pool(f.read_text(encoding="utf-8"))
                    else:
                        failed.append(f"{sp.upper()}: no file data/pool_{sp}.csv yet")
            res = run_pool(db, entries, a.apply and not (failed and not a.force))
            res["problems"] = failed
            if failed and a.apply and not a.force:
                res["not_applied"] = "some sports could not be loaded, so nothing was written (a partial pool would rank the sports unfairly). Fix them or add --force."
        else:
            res = run_pool(db, pool_mod.csv_pool(Path(a.file).read_text(encoding="utf-8")), a.apply)
    except Exception as e:
        traceback.print_exc()
        if a.cmd in ("stats", "injuries") and not a.dry_run:
            heartbeat(db, f"feed_{a.sport}" + ("_injuries" if a.cmd == "injuries" else ""), False, str(e)[:300], {})
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(res, indent=2, default=str))
    return 1 if res.get("fetch_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
