"""The scoring job. Run every few minutes (GitHub scheduled workflow); every step is safe to repeat.

Each run, using the database clock:
  1. builds the 44-week schedule once;
  2. for the current week and the week that just ended: freezes the baselines (first run only), scores every player who has stats,
     scores every team's lineup, and updates the matchup scores; once a week is final it decides the winners;
  3. advances the season when a phase has finished: Cup groups, Cup knockouts, Cup champion bonus win and mulligan,
     playoff seeds, wildcard, semifinals, final.
All scoring maths comes from engine/ (the tested reference); this file only moves data in and out of Postgres.

Database access is behind PsqlDb (psql on the command line). In production the GitHub workflow passes the Supabase connection
string as DATABASE_URL, so the same class talks to the hosted database; the connecting role is the database owner, so the
functions' "only the commissioner" checks (which look at the signed-in user) do not apply to the job.
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import scoring as sc, schedule as sch

SEED = 2027          # drives the schedule and the Cup draw; change it before the season starts, never during
REGULAR_LAST = 44


class PsqlDb:
    def __init__(self, dbname="fl_test"):
        self.dbname = dbname
        self.env = {**os.environ, "PGHOST": os.environ.get("PGHOST", "/tmp/pgtest"), "PGPORT": os.environ.get("PGPORT", "5499"),
                    "PGUSER": os.environ.get("PGUSER", "postgres")}

    def _psql(self, args, stdin=None):
        p = subprocess.run(["psql", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", self.dbname, *args], input=stdin, text=True, capture_output=True, env=self.env)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip())
        return p.stdout

    def rows(self, sql):
        out = self._psql([], f"select coalesce(json_agg(t), '[]'::json) from ({sql}) t;")
        return json.loads(out.strip() or "[]")

    def run(self, sql):
        self._psql([], "begin;\n" + sql + "\ncommit;")


def num(x):
    return "null" if x is None else repr(round(float(x), 6))


# ---- reading ----------------------------------------------------------------------------------------
def load_week(db, cfg, week):
    rows = db.rows(f"select s.player_id, p.sport::text as sport, p.position, s.stat, s.value::float8 as value "
                   f"from stat_lines s join players p on p.id = s.player_id where s.week = {int(week)}")
    return sc.week_totals(cfg, [(r["player_id"], r["sport"], r["position"], r["stat"], r["value"]) for r in rows])


def team_week_results(db, week):
    return {r["team_id"]: {"weekly_total": float(r["total"]), "best_starter": float(r["best_starter"])}
            for r in db.rows(f"select team_id, total::float8 as total, best_starter::float8 as best_starter from team_weeks where week = {int(week)}")}


def cumulative_before(db, week):
    """Regular-season points before `week` (the third tiebreak)."""
    last = min(week - 1, REGULAR_LAST)
    return {r["team_id"]: float(r["c"]) for r in db.rows(
        f"select team_id, sum(total)::float8 as c from team_weeks where week <= {last} group by team_id")}


def standings_through(db, week):
    """[{'id','wins','cumulative'}] from regular-season games through `week`, plus the Cup champion's bonus win."""
    wins = {r["w"]: r["n"] for r in db.rows(
        f"select winner as w, count(*) as n from matchups where kind = 'regular' and week <= {int(week)} and winner is not null group by winner")}
    cum = {r["team_id"]: float(r["c"]) for r in db.rows(
        f"select team_id, sum(total)::float8 as c from team_weeks where week <= {int(week)} group by team_id")}
    bonus = {r["team_id"] for r in db.rows("select team_id from cup_bonus_wins")} if week >= REGULAR_LAST else set()
    teams = [r["id"] for r in db.rows("select id from teams order by id")]
    return [{"id": t, "wins": wins.get(t, 0) + (1 if t in bonus else 0), "cumulative": cum.get(t, 0.0)} for t in teams]


# ---- scoring a week -----------------------------------------------------------------------------------
def frozen_for(db, cfg, week):
    have = db.rows(f"select sport::text || '|' || position as k, avg::float8 as avg, sd::float8 as sd, pool, fallback from baselines where week = {int(week)}")
    if have:
        return {r["k"]: sc.Baseline(r["avg"], r["sd"], r["pool"], r["fallback"]) for r in have}
    weeks = [r["week"] for r in db.rows(f"select distinct week from stat_lines where week < {int(week)} order by week")]
    history = {w: load_week(db, cfg, w) for w in weeks}
    bl = sc.frozen_baselines(cfg, history, week)
    vals = ",".join(f"({int(week)},'{k.split('|')[0]}','{k.split('|')[1]}',{num(b.avg)},{num(b.sd)},{b.pool},{str(b.fallback).lower()})" for k, b in bl.items())
    db.run(f"insert into baselines (week, sport, position, avg, sd, pool, fallback) values {vals} on conflict do nothing;")
    return bl


def score_week(db, cfg, week):
    baselines = frozen_for(db, cfg, week)
    pweek = load_week(db, cfg, week)
    stmts = []
    for pid, pw in pweek.items():
        played = pw.games > 0
        z, score = sc.score_player(cfg, pw.native, baselines[f"{pw.sport}|{pw.position}"], played)
        stmts.append(f"({int(week)},{pid},{num(pw.native)},{num(z) if played else 'null'},{num(score)},{str(played).lower()})")
    if stmts:
        db.run("insert into player_scores (week, player_id, native, z, score, played) values " + ",".join(stmts) +
               " on conflict (week, player_id) do update set native = excluded.native, z = excluded.z, score = excluded.score, played = excluded.played;")
    lineups = {}
    for r in db.rows(f"select l.team_id, l.player_id, p.sport::text as sport, p.position from lineups l join players p on p.id = l.player_id where l.week = {int(week)}"):
        pw = pweek.get(r["player_id"])
        lineups.setdefault(r["team_id"], []).append((r["player_id"], r["sport"], r["position"], bool(pw and pw.games > 0)))
    bonus = {r["team_id"]: float(r["b"]) for r in db.rows(
        f"select team_id, sum(points)::float8 as b from bonus_scores where team_id is not null and week_of(scored_at) = {int(week)} group by team_id")}
    rows = []
    for t in [r["id"] for r in db.rows("select id from teams order by id")]:
        res = sc.score_lineup(cfg, lineups.get(t, []), pweek, baselines, bonus.get(t, 0.0))
        rows.append(f"({t},{int(week)},{num(res['starter_points'])},{num(res['variety_bonus'])},{num(res['bonus_points'])},{num(res['weekly_total'])},{num(res['best_starter'])})")
    db.run("insert into team_weeks (team_id, week, starter_points, variety_bonus, bonus_points, total, best_starter) values " + ",".join(rows) +
           " on conflict (team_id, week) do update set starter_points = excluded.starter_points, variety_bonus = excluded.variety_bonus, "
           "bonus_points = excluded.bonus_points, total = excluded.total, best_starter = excluded.best_starter;")
    db.run(f"update matchups m set score_a = (select total from team_weeks where team_id = m.team_a and week = m.week), "
           f"score_b = (select total from team_weeks where team_id = m.team_b and week = m.week) where m.week = {int(week)};")


def single_result(db, week, a, b):
    """('A'|'B'|'Tie', reason) for one game between teams a and b in `week`."""
    res, cum = team_week_results(db, week), cumulative_before(db, week)
    return sc.decide_winner(res[a], res[b], cum.get(a, 0.0), cum.get(b, 0.0))


def set_winner(db, matchup_id, team):
    db.run(f"update matchups set winner = {'null' if team is None else int(team)} where id = {int(matchup_id)};")


def decide_simple_winners(db, week):
    """Regular season and Cup group games: a full tie has no winner."""
    for m in db.rows(f"select id, team_a, team_b from matchups where week = {int(week)} and kind in ('regular', 'cup-group') and winner is null"):
        who, _ = single_result(db, week, m["team_a"], m["team_b"])
        if who != "Tie":
            set_winner(db, m["id"], m["team_a"] if who == "A" else m["team_b"])


# ---- schedule and season ------------------------------------------------------------------------------
def insert_matchups(db, rows):
    vals = ",".join(f"({w},'{k}',{a},{b})" for w, k, a, b in rows)
    db.run(f"insert into matchups (week, kind, team_a, team_b) values {vals} on conflict do nothing;")


def ensure_schedule(db):
    if db.rows("select 1 as x from matchups where kind = 'regular' limit 1"):
        return
    teams = [r["id"] for r in db.rows("select id from teams order by id")]
    insert_matchups(db, [(w, "regular", a, b) for w, pairs in sch.regular_season(teams, seed=SEED).items() for a, b in pairs])


def games(db, kind, week):
    return db.rows(f"select id, team_a, team_b, winner from matchups where kind = '{kind}' and week = {int(week)} order by id")


def knockout_winner(db, m, week, seeds):
    who, _ = single_result(db, week, m["team_a"], m["team_b"])
    w = sch.advance_single(m["team_a"], m["team_b"], (who,), seeds)
    set_winner(db, m["id"], w)
    return w


def aggregate_winner(db, kind, weeks, a, b, seeds):
    res = {w: team_week_results(db, w) for w in weeks}
    return sch.advance_aggregate(a, b, [res[w][a]["weekly_total"] for w in weeks], [res[w][b]["weekly_total"] for w in weeks], seeds)


def cup_seeds(db):
    """Group winners plus the best runner-up, from the group games."""
    groups = [[r["team_id"] for r in db.rows(f"select team_id from cup_groups where grp = {g} order by tier")] for g in (1, 2, 3)]
    tables = []
    for g in groups:
        results = []
        for w in sch.CUP_GROUP_WEEKS:
            for m in games(db, "cup-group", w):
                if m["team_a"] in g:
                    res = team_week_results(db, w)
                    results.append({"winner": m["winner"], "points": {m["team_a"]: res[m["team_a"]]["weekly_total"], m["team_b"]: res[m["team_b"]]["weekly_total"]}})
        tables.append(sch.cup_group_table(g, results))
    fallback = [t["id"] for t in sch.rank_teams(standings_through(db, 19))]
    return sch.cup_knockout_seeds(tables, fallback_rank=fallback)


def advance_season(db, now_final):
    """now_final(week) says whether a week is final. Creates the next phase's matchups when the previous phase is done."""
    have = lambda kind, week: bool(games(db, kind, week))
    # Cup
    if now_final(19) and not db.rows("select 1 as x from cup_groups limit 1"):
        ranked = [t["id"] for t in sch.rank_teams(standings_through(db, 19))]
        groups = sch.cup_groups(ranked, seed=SEED)
        vals = ",".join(f"({t},{ranked.index(t) // 3 + 1},{gi + 1})" for gi, g in enumerate(groups) for t in g)
        db.run(f"insert into cup_groups (team_id, tier, grp) values {vals} on conflict do nothing;")
        insert_matchups(db, [(w, "cup-group", a, b) for w, pairs in sch.cup_group_schedule(groups, seed=SEED).items() for a, b in pairs])
    if now_final(22) and not have("cup-semi", sch.CUP_SEMI_WEEK):
        insert_matchups(db, [(sch.CUP_SEMI_WEEK, "cup-semi", a, b) for a, b in sch.cup_semifinals(cup_seeds(db))])
    if now_final(23) and not have("cup-final", sch.CUP_FINAL_WEEK):
        seeds = cup_seeds(db)
        wins = [knockout_winner(db, m, sch.CUP_SEMI_WEEK, seeds) for m in games(db, "cup-semi", sch.CUP_SEMI_WEEK)]
        a, b = sorted(wins, key=seeds.index)
        insert_matchups(db, [(sch.CUP_FINAL_WEEK, "cup-final", a, b)])
    if now_final(24) and not db.rows("select 1 as x from cup_bonus_wins limit 1"):
        seeds = cup_seeds(db)
        m = games(db, "cup-final", sch.CUP_FINAL_WEEK)[0]
        champ = knockout_winner(db, m, sch.CUP_FINAL_WEEK, seeds)
        runner = m["team_b"] if champ == m["team_a"] else m["team_a"]
        db.run(f"insert into cup_bonus_wins (team_id) values ({champ}) on conflict do nothing; select grant_mulligan({runner}::smallint);")
    # Playoffs
    if now_final(REGULAR_LAST) and not have("wildcard", sch.WILDCARD_WEEK):
        seeds = sch.playoff_seeds(standings_through(db, REGULAR_LAST))
        insert_matchups(db, [(sch.WILDCARD_WEEK, "wildcard", a, b) for a, b in sch.wildcard_round(seeds)])
    if now_final(sch.WILDCARD_WEEK) and not have("playoff-semi", sch.SEMI_WEEKS[0]):
        seeds = sch.playoff_seeds(standings_through(db, REGULAR_LAST))
        winners = [knockout_winner(db, m, sch.WILDCARD_WEEK, seeds) for m in games(db, "wildcard", sch.WILDCARD_WEEK)]
        pairs = sch.semifinal_round(seeds, winners)
        insert_matchups(db, [(w, "playoff-semi", a, b) for w in sch.SEMI_WEEKS for a, b in pairs])
    if now_final(sch.SEMI_WEEKS[-1]) and not have("playoff-final", sch.FINAL_WEEKS[0]):
        seeds = sch.playoff_seeds(standings_through(db, REGULAR_LAST))
        winners = []
        for m in games(db, "playoff-semi", sch.SEMI_WEEKS[-1]):
            w = aggregate_winner(db, "playoff-semi", sch.SEMI_WEEKS, m["team_a"], m["team_b"], seeds)
            set_winner(db, m["id"], w)
            winners.append(w)
        a, b = sorted(winners, key=seeds.index)
        insert_matchups(db, [(w, "playoff-final", a, b) for w in sch.FINAL_WEEKS])
    if now_final(sch.FINAL_WEEKS[-1]):
        seeds = sch.playoff_seeds(standings_through(db, REGULAR_LAST))
        for m in games(db, "playoff-final", sch.FINAL_WEEKS[-1]):
            if m["winner"] is None:
                set_winner(db, m["id"], aggregate_winner(db, "playoff-final", sch.FINAL_WEEKS, m["team_a"], m["team_b"], seeds))


# ---- one run -----------------------------------------------------------------------------------------------
def run(db):
    cfg = sc.load_config()
    cur = db.rows("select week_of(app_now()) as w")[0]["w"]
    final = {}
    is_final = lambda w: final.setdefault(w, db.rows(f"select is_final({int(w)}) as f")[0]["f"])
    ensure_schedule(db)
    if 1 <= cur <= 51:
        db.run(f"select carry_over_lineups({int(cur)});")
    done = []
    for w in range(max(1, cur - 1), min(cur, 51) + 1):
        score_week(db, cfg, w)
        if is_final(w):
            decide_simple_winners(db, w)
        done.append(w)
    advance_season(db, lambda w: w <= cur and is_final(w))
    return {"week": cur, "scored": done}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL") or "fl_test")
    print(json.dumps(run(PsqlDb(ap.parse_args().db))))
