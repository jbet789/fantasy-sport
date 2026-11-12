"""One scheduled tick (GitHub workflow, every 10 minutes). Each step is safe to repeat and a failure in one does not stop the others.

  1. scoring job: schedule, lineup carry-over, live scores, winners, season advance
  2. settle_trades(): completes trades whose 24-hour veto window ended, expires proposals after the deadline
  3. process_waivers(): runs claims that are due (Tuesday 9:00 a.m. ET) and frees unclaimed players
  4. live drafts: makes any picks whose clock ran out, so a draft moves on even if nobody has the Draft tab open
The database clock decides what is due, so a late or doubled tick changes nothing.
"""
import json, os, sys, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import scoring_job


def _q(text):
    return "'" + str(text).replace("'", "''") + "'"


def tick(db):
    out, errors = {}, []
    for name, step in (
        ("scoring", lambda: scoring_job.run(db)),
        ("trades_completed", lambda: db.rows("select settle_trades() as n")[0]["n"]),
        ("waiver_wins", lambda: db.rows("select process_waivers() as n")[0]["n"]),
        ("draft_picks_made", lambda: sum(r["n"] for r in db.rows("select advance_if_due(id) as n from drafts where status = 'live' order by id"))),
    ):
        try:
            out[name] = step()
        except Exception as e:                      # keep going; report at the end
            errors.append(f"{name}: {e}")
            traceback.print_exc()
    out["errors"] = errors
    try:                                            # the heartbeat the app's health badge reads; never lets a tick fail
        db.rows("select record_job(%s, %s, %s, %s::jsonb) is null as ok" % (_q("tick"), "true" if not errors else "false", _q("; ".join(errors)), _q(json.dumps({k: v for k, v in out.items() if k != "errors"}))))
    except Exception:
        traceback.print_exc()
    return out


if __name__ == "__main__":
    res = tick(scoring_job.PsqlDb(os.environ.get("DATABASE_URL") or (sys.argv[1] if len(sys.argv) > 1 else "fl_test")))
    print(json.dumps(res))
    sys.exit(1 if res["errors"] else 0)
