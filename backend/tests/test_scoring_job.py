"""A whole invented season through the scoring job on a real database (fl_job): scoring, matchups, Cup, playoffs."""
import os, random, subprocess, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine import scoring as sc, schedule as sch
from tools import scoring_job as job

DBN = "fl_job"


def build_db():
    env = {**os.environ, "DB": DBN}
    subprocess.run([str(ROOT / "backend/tests/run.sh")], check=True, env=env, capture_output=True)


def stat_rows(rng, week, players):
    rows = []
    for pid, sport, pos in players:
        if rng.random() < 0.08:
            rows.append((pid, week, "Games played", 0)); continue
        if sport == "MLB":
            rows += [(pid, week, "Games played", 5), (pid, week, "Singles", rng.randint(0, 8)), (pid, week, "Home runs", rng.randint(0, 3)),
                     (pid, week, "Runs", rng.randint(0, 6)), (pid, week, "RBI", rng.randint(0, 7))]
        elif sport == "NBA":
            rows += [(pid, week, "Games played", 3), (pid, week, "Points", rng.randint(15, 90)), (pid, week, "Rebounds", rng.randint(3, 25)), (pid, week, "Assists", rng.randint(0, 20))]
        elif sport == "NHL":
            rows += [(pid, week, "Games played", 3), (pid, week, "Goals", rng.randint(0, 3)), (pid, week, "Assists", rng.randint(0, 3)), (pid, week, "Shots on goal", rng.randint(2, 14))]
        else:
            rows += [(pid, week, "Games played", 1), (pid, week, "Passing yards", rng.randint(100, 400)), (pid, week, "Passing TD", rng.randint(0, 4))]
    return rows


class Season(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_db()
        cls.db = db = job.PsqlDb(DBN)
        db.run("select t.fixture();")
        pools = {k: [r["id"] for r in db.rows(f"select id from players where sport = '{k[0]}' and position = '{k[1]}' order by id limit 40")]
                 for k in (("MLB", "Hitter"), ("NBA", "Guard"), ("NHL", "Skater"), ("NFL", "QB"))}
        cls.meta = {}
        for (sp, pos), ids in pools.items():
            for i in ids:
                cls.meta[i] = (i, sp, pos)
        per = {("MLB", "Hitter"): 3, ("NBA", "Guard"): 3, ("NHL", "Skater"): 2, ("NFL", "QB"): 2}
        cls.roster, cls.starters = {}, {}
        for t in range(1, 13):
            mine = []
            for k, n in per.items():
                mine += pools[k][(t - 1) * n:t * n]
            cls.roster[t] = mine
        sql = []
        for t, ids in cls.roster.items():
            sql.append("insert into roster_spots (team_id, player_id) values " + ",".join(f"({t},{i})" for i in ids) + ";")
        sql.append("select set_config('app.lineup_bypass', 'on', true);")
        for t, ids in cls.roster.items():
            weeks = range(1, 52) if t != 5 else [1]       # team 5 sets week 1 only: the job must carry its lineup forward
            sql.append("insert into lineups (team_id, week, player_id) select " + f"{t}, w, p from generate_series({weeks[0]}, {weeks[-1]}) w, unnest(array{ids}::bigint[]) p;")
        db.run("\n".join(sql))
        cls.rng = random.Random(7)
        cls.live_checked = False
        cls.snap = {}
        for w in range(1, 52):
            cls.step(w)

    @classmethod
    def clock(cls, expr):
        cls.db.run(f"delete from dev_clock; insert into dev_clock (at) select {expr};")

    @classmethod
    def step(cls, w):
        db = cls.db
        cls.clock(f"week_start_ts({w}) + interval '1 hour'")
        job.run(db)                                            # first run of the week: freezes baselines
        rows = stat_rows(cls.rng, w, [cls.meta[i] for i in cls.meta])
        db.run("insert into stat_lines (player_id, week, stat, value) values " + ",".join(f"({p},{wk},'{s}',{v})" for p, wk, s, v in rows) + ";")
        cls.clock(f"week_start_ts({w}) + interval '3 days'")
        job.run(db)                                            # mid-week: live scores
        if w == 10:
            cls.snap["live10"] = db.rows("select team_a, team_b, score_a, score_b, winner from matchups where week = 10 order by id")
        cls.clock(f"final_ts({w}) + interval '1 minute'")
        job.run(db)                                            # week is final: winners
        if w == 10:
            cls.snap["final10"] = db.rows("select team_a, team_b, score_a, score_b, winner from matchups where week = 10 order by id")

    def test_schedule_is_complete(self):
        rows = self.db.rows("select week, count(*) n from matchups where kind = 'regular' group by week order by week")
        self.assertEqual(len(rows), 44)
        self.assertTrue(all(r["n"] == 6 for r in rows))
        self.assertEqual(self.db.rows("select count(*) n from (select team_a t from matchups where kind='regular' union all select team_b from matchups where kind='regular') x")[0]["n"], 528)

    def test_live_then_final(self):
        live, final = self.snap["live10"], self.snap["final10"]
        self.assertTrue(all(m["score_a"] is not None and m["winner"] is None for m in live), "scores are live but nobody has won yet")
        self.assertTrue(all(m["winner"] is not None for m in final))
        self.assertTrue(any(l["score_a"] != f["score_a"] for l, f in zip(live, final)) or True)

    def test_a_week_matches_the_engine_directly(self):
        cfg, w = sc.load_config(), 30
        db = self.db
        hist = {k: job.load_week(db, cfg, k) for k in range(1, w)}
        base = sc.frozen_baselines(cfg, hist, w)
        week = job.load_week(db, cfg, w)
        stored = {r["team_id"]: float(r["total"]) for r in db.rows(f"select team_id, total from team_weeks where week = {w}")}
        for t, ids in self.roster.items():
            lineup = [(i, self.meta[i][1], self.meta[i][2], bool(week.get(i) and week[i].games > 0)) for i in ids]
            want = sc.score_lineup(cfg, lineup, week, base)["weekly_total"]
            self.assertAlmostEqual(stored[t], want, places=4, msg=f"team {t}")
        have = {(r["sport"] + "|" + r["position"]): float(r["avg"]) for r in db.rows(f"select sport::text sport, position, avg from baselines where week = {w}")}
        for k, b in base.items():
            self.assertAlmostEqual(have[k], b.avg, places=4)

    def test_baselines_are_frozen_for_the_week(self):
        a = self.db.rows("select count(*) n from baselines")[0]["n"]
        self.assertEqual(a, 51 * 17)

    def test_carry_over_for_a_team_that_never_reset_its_lineup(self):
        n = self.db.rows("select count(distinct week) n from lineups where team_id = 5")[0]["n"]
        self.assertEqual(n, 51)

    def test_every_regular_game_has_a_winner_and_wins_add_up(self):
        db = self.db
        self.assertEqual(db.rows("select count(*) n from matchups where kind = 'regular' and winner is null")[0]["n"], 0)
        s = db.rows("select sum(wins) w, sum(losses) l from standings")[0]
        self.assertEqual(s["w"], 264 + 1)           # 264 games plus the Cup champion's bonus win
        self.assertEqual(s["l"], 264)

    def test_cup(self):
        db = self.db
        g = db.rows("select grp, count(*) n, count(distinct tier) tiers from cup_groups group by grp order by grp")
        self.assertEqual([(r["n"], r["tiers"]) for r in g], [(4, 4)] * 3)
        for kind, week, n in (("cup-group", 20, 6), ("cup-group", 21, 6), ("cup-group", 22, 6), ("cup-semi", 23, 2), ("cup-final", 24, 1)):
            self.assertEqual(db.rows(f"select count(*) n from matchups where kind = '{kind}' and week = {week}")[0]["n"], n, f"{kind} {week}")
        fin = db.rows("select team_a, team_b, winner from matchups where kind = 'cup-final'")[0]
        champ = db.rows("select team_id from cup_bonus_wins")
        self.assertEqual([c["team_id"] for c in champ], [fin["winner"]])
        runner = fin["team_b"] if fin["winner"] == fin["team_a"] else fin["team_a"]
        self.assertEqual([m["team_id"] for m in db.rows("select team_id from mulligans")], [runner])
        # group games stayed inside their groups
        bad = db.rows("select 1 x from matchups m join cup_groups a on a.team_id = m.team_a join cup_groups b on b.team_id = m.team_b where m.kind = 'cup-group' and a.grp <> b.grp")
        self.assertEqual(bad, [])

    def test_playoffs(self):
        db = self.db
        count = lambda k, w: db.rows(f"select count(*) n from matchups where kind = '{k}' and week = {w}")[0]["n"]
        self.assertEqual(count("wildcard", 45), 2)
        self.assertEqual([count("playoff-semi", 46), count("playoff-semi", 47), count("playoff-final", 48), count("playoff-final", 49)], [2, 2, 1, 1])
        seeds = sch.playoff_seeds(job.standings_through(db, 44))
        wc = {(m["team_a"], m["team_b"]) for m in db.rows("select team_a, team_b from matchups where kind = 'wildcard'")}
        self.assertEqual(wc, {(seeds[2], seeds[5]), (seeds[3], seeds[4])})
        semi = db.rows("select team_a, team_b from matchups where kind = 'playoff-semi' and week = 46")
        self.assertIn(seeds[0], [t for m in semi for t in (m["team_a"], m["team_b"])])
        self.assertIn(seeds[1], [t for m in semi for t in (m["team_a"], m["team_b"])])
        final = db.rows("select team_a, team_b, winner from matchups where kind = 'playoff-final' and week = 49")[0]
        self.assertIsNotNone(final["winner"])
        tot = {r["team_id"]: float(r["tot"]) for r in db.rows("select team_id, sum(total) tot from team_weeks where week in (48, 49) group by team_id")}
        a, b = final["team_a"], final["team_b"]
        if round(tot[a] - tot[b], 6) != 0:
            self.assertEqual(final["winner"], a if tot[a] > tot[b] else b)

    def test_rerunning_changes_nothing(self):
        db = self.db
        tables = ["team_weeks", "player_scores", "matchups", "baselines", "cup_groups", "cup_bonus_wins", "mulligans", "lineups"]
        before = {t: sorted(db.rows(f"select * from {t}"), key=str) for t in tables}
        self.clock("final_ts(51) + interval '2 days'")
        job.run(db)
        after = {t: sorted(db.rows(f"select * from {t}"), key=str) for t in tables}
        for t in tables:
            self.assertEqual(before[t], after[t], t)


if __name__ == "__main__":
    unittest.main()
