"""Practice draft: 12 bots draft all 20 rounds through the real draft functions and the real pick clock, on a scratch database.

    python3 tools/practice_draft.py [--db fl_practice] [--seed 1]

Builds the scratch database from the migrations (never touches the real league), then plays the draft with a mix of behaviours:
  eager      picks within seconds of being on the clock             (teams 1-4)
  wishlist   never acts; relies on the queue when the clock runs out (teams 5-8)
  asleep     never acts and has no queue; the ranking autopick        (teams 9-11)
  late       tries at the last second, and sometimes just too late    (team 12)
Then checks the result: snake order, 20 players each, no player twice, at most the F1 cap, each autopick used the queue first, a late pick lost to the autopick,
and week-1 lineups opened. Prints a report; exit code 1 if anything is wrong.
"""
import argparse, os, random, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import scoring_job

KIND = {**{i: "eager" for i in range(1, 5)}, **{i: "wishlist" for i in range(5, 9)}, **{i: "asleep" for i in range(9, 12)}, 12: "late"}


def build(db_name):
    env = {**os.environ, "DB": db_name, "PGHOST": os.environ.get("PGHOST", "/tmp/pgtest"), "PGPORT": os.environ.get("PGPORT", "5499"), "PGUSER": os.environ.get("PGUSER", "postgres")}
    subprocess.run([str(ROOT / "backend/tests/run.sh")], check=True, env=env, capture_output=True)
    return scoring_job.PsqlDb(db_name)


def as_user(db, team, sql):
    db.run(f"select set_config('request.jwt.claim.sub', t.uid({int(team)})::text, true); {sql}")


def clock(db, ts):
    db.run(f"delete from dev_clock; insert into dev_clock (at) values ('{ts}');")


def run(db_name="fl_practice", seed=1, log=print):
    rng = random.Random(seed)
    db = build(db_name)
    db.run("select t.fixture(); update dev_clock set at = '2027-01-01 11:00 America/New_York';")
    teams = [r["id"] for r in db.rows("select id from teams order by id")]
    pool = [r["id"] for r in db.rows("select id from players order by ranking")]
    did = db.rows("select create_draft('main', 20) as id")[0]["id"]
    for t in (t for t in teams if KIND[t] == "wishlist"):        # wishlist teams queue 30 random players, best-first by their own taste
        want = rng.sample(pool, 30)
        for rank, pid in enumerate(want, 1):
            db.run(f"insert into draft_queue (draft_id, team_id, player_id, rank) values ({did}, {t}, {pid}, {rank});")
    clock(db, "2027-01-01 12:00 America/New_York")
    db.run(f"select start_draft({did});")
    stats = {"manual": 0, "auto": 0, "late_lost": 0, "late_won": 0}
    guard = 0
    while True:
        d = db.rows(f"select d.current_pick, d.status, d.deadline, d.pick_seconds, s.team_id, extract(epoch from d.deadline)::float8 as dl "
                    f"from drafts d left join draft_slots s on s.draft_id = d.id and s.pick_no = d.current_pick where d.id = {did}")[0]
        if d["status"] != "live":
            break
        guard += 1
        if guard > 400:
            raise RuntimeError("draft did not finish")
        team, kind = d["team_id"], KIND[d["team_id"]]
        start = f"to_timestamp({d['dl']} - {d['pick_seconds']})"
        left = None
        if kind == "eager":
            left = rng.randint(2, 20)
        elif kind == "late":
            left = rng.randint(55, 59) if rng.random() < .5 else d["pick_seconds"] + 3                # sometimes after the clock
        if left is not None:
            db.run(f"delete from dev_clock; insert into dev_clock (at) values ({start} + interval '{int(left)} seconds');")
            legal = db.rows(f"select id from players p where active and draft_problem({did}, {team}::smallint, p.id) is null order by random() limit 1")          # a mixed taste, not just the top of the ranking
            pick = legal[0]["id"]
            try:
                as_user(db, team, f"select make_pick({did}, {pick});")
                stats["manual"] += 1
                if kind == "late":
                    stats["late_won"] += 1
                continue
            except RuntimeError:
                if kind == "late":
                    stats["late_lost"] += 1                 # the autopick got there first
        db.run(f"delete from dev_clock; insert into dev_clock (at) values (to_timestamp({d['dl']}) + interval '1 second'); select advance_if_due({did});")
        stats["auto"] += 1
    return report(db, did, stats, log)


def report(db, did, stats, log):
    problems = []
    q = lambda sql: db.rows(sql)
    picks = q(f"select count(*) as n, count(distinct player_id) as p from draft_picks where draft_id = {did}")[0]
    if picks["n"] != 240 or picks["p"] != 240:
        problems.append(f"expected 240 distinct picks, got {picks['n']} / {picks['p']}")
    short = q("select t.id from teams t where (select count(*) from roster_spots r where r.team_id = t.id) <> 20")
    if short:
        problems.append(f"teams without exactly 20 players: {[r['id'] for r in short]}")
    f1 = q("select r.team_id, count(*) as n from roster_spots r join players p on p.id = r.player_id where p.sport = 'F1' group by 1 having count(*) > (select f1_cap from league_settings)")
    if f1:
        problems.append(f"F1 cap exceeded: {f1}")
    snake = q(f"select count(*) as n from draft_picks k join draft_slots s using (draft_id, pick_no) where k.draft_id = {did} and k.team_id <> s.team_id")[0]["n"]
    if snake:
        problems.append(f"{snake} picks went to the wrong team")
    # queue rule: an autopick for a wishlist team must be the first queued player still available
    bad_q = q(f"select k.pick_no, k.team_id from draft_picks k where k.draft_id = {did} and k.auto and exists (select 1 from draft_queue u where u.draft_id = k.draft_id and u.team_id = k.team_id) "
              f"and k.player_id <> (select u.player_id from draft_queue u where u.draft_id = k.draft_id and u.team_id = k.team_id and not exists "
              f"(select 1 from draft_picks y where y.draft_id = u.draft_id and y.player_id = u.player_id and y.pick_no < k.pick_no) order by u.rank limit 1) "
              f"and exists (select 1 from draft_queue u where u.draft_id = k.draft_id and u.team_id = k.team_id and not exists (select 1 from draft_picks y where y.draft_id = u.draft_id and y.player_id = u.player_id and y.pick_no < k.pick_no))")
    if bad_q:
        problems.append(f"autopicks that skipped the queue: {bad_q[:5]}")
    if not q("select preseason_open_at is not null as o from league_settings")[0]["o"]:
        problems.append("week-1 lineups did not open at the end of the draft")
    if q(f"select status from drafts where id = {did}")[0]["status"] != "done":
        problems.append("draft is not marked done")
    log(f"Practice draft: {picks['n']} picks. By hand: {stats['manual']}; by the clock: {stats['auto']}. "
        f"Last-second picks that made it: {stats['late_won']}; that lost to the autopick: {stats['late_lost']}.")
    mix = q("select r.team_id as id, string_agg(x.s || ' ' || x.n, ', ' order by x.s) as x from "
            "(select r2.team_id, p.sport::text as s, count(*) as n from roster_spots r2 join players p on p.id = r2.player_id group by 1, 2) x join (select distinct team_id from roster_spots) r on r.team_id = x.team_id group by r.team_id order by 1")
    for r in mix:
        log(f"  team {r['id']:>2} ({KIND[r['id']]:<8}): {r['x']}")
    if problems:
        log("PROBLEMS:"); [log("  - " + p) for p in problems]
    else:
        log("All checks passed.")
    return {"problems": problems, **stats, "picks": picks["n"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--db", default="fl_practice"); ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    res = run(a.db, a.seed)
    sys.exit(1 if res["problems"] else 0)
