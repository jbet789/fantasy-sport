"""Fuzz: the SQL waiver run must award exactly what engine/transactions.py awards."""
import random, unittest
from datetime import datetime
from engine.transactions import League
from .test_db import run_sql

RUN = datetime(2027, 3, 30, 9, 0)


def scenario(seed):
    rng = random.Random(seed)
    teams = list(range(1, 7))
    fill = [("MLB", "Hitter"), ("MLB", "SP"), ("MLB", "RP"), ("NBA", "Guard"), ("NBA", "Big"), ("NBA", "Wing"), ("NHL", "Skater"), ("NFL", "RB"), ("NFL", "WR")]
    hitters = [f"{s} {p} {i}" for s, p in fill for i in range(1, 41)]
    drivers = [f"F1 Driver {i}" for i in range(1, 21)]
    pool_names = [f"NFL QB {i}" for i in range(1, 6)] + [f"F1 Driver {i}" for i in range(15, 19)]
    players = {n: (n.split()[0], n.split()[1]) for n in hitters}
    players.update({n: ("F1", "Driver") for n in drivers})
    players.update({n: ("NFL", "QB") for n in pool_names if n.startswith("NFL")})
    rosters, free = {}, list(hitters)
    rng.shuffle(free)
    f1 = [d for d in drivers if d not in pool_names]
    rng.shuffle(f1)
    for t in teams:
        n = rng.choice([17, 18, 19, 20, 20, 20])
        k = rng.choice([0, 1, 2])
        rosters[t] = [free.pop() for _ in range(n - k)]
        for _ in range(k):
            rosters[t].append(f1.pop())
    claims = {}
    for t in teams:
        mine, used = [], set()
        for p in rng.sample(pool_names, rng.randint(0, 4)):
            drop = rng.choice(rosters[t]) if rng.random() < 0.7 else None
            if drop in used:
                drop = None
            if drop:
                used.add(drop)
            mine.append({"player": p, "drop": drop})
        if mine:
            claims[t] = mine
    order = teams[:]
    rng.shuffle(order)
    return players, rosters, pool_names, claims, order


def run_engine(players, rosters, pool, claims, order):
    lg = League(players, list(range(1, 7)))
    for t, names in rosters.items():
        for n in names:
            lg.active[t].add(n)
    for n in pool:
        lg.waivers[n] = RUN
    res = lg.process_waivers(RUN, order, claims)
    return {(r["team"], r["player"]): r["status"] for r in res}, {t: sorted(lg.active[t]) for t in lg.teams}


def run_db(rosters, pool, claims, order):
    sql = ["select t.at('2027-03-30 09:00'); select t.as_service();"]
    for t, names in rosters.items():
        for n in names:
            sql.append(f"insert into roster_spots (team_id, player_id) select {t}, id from players where name = '{n}';")
    for n in pool:
        sql.append(f"insert into waivers (player_id, release_at, kind) select id, '2027-03-30 09:00 America/New_York', 'new' from players where name = '{n}';")
    for t, lst in claims.items():
        for i, c in enumerate(lst, 1):
            drop = f"(select id from players where name = '{c['drop']}')" if c["drop"] else "null"
            sql.append(f"insert into waiver_claims (team_id, player_id, drop_player_id, rank, run_at) select {t}, id, {drop}, {i}, '2027-03-30 09:00 America/New_York' from players where name = '{c['player']}';")
    sql.append(f"select process_waivers('2027-03-30 09:00 America/New_York', array[{','.join(map(str, order))}]::smallint[]);")
    sql.append("\\pset tuples_only on\n\\pset format unaligned\n\\pset fieldsep '|'")
    sql.append("select 'C', c.team_id, p.name, c.status from waiver_claims c join players p on p.id = c.player_id order by 2, 3;")
    sql.append("select 'R', r.team_id, p.name from roster_spots r join players p on p.id = r.player_id order by 2, 3;")
    out = run_sql("\n".join(sql))
    claims_out, rosters_out = {}, {}
    for line in out.splitlines():
        parts = line.split("|")
        if parts[0] == "C":
            claims_out[(int(parts[1]), parts[2])] = parts[3]
        elif parts[0] == "R":
            rosters_out.setdefault(int(parts[1]), []).append(parts[2])
    return claims_out, {t: sorted(v) for t, v in rosters_out.items()}


class Parity(unittest.TestCase):
    def test_waiver_runs_match_the_engine(self):
        for seed in range(40):
            players, rosters, pool, claims, order = scenario(seed)
            e_claims, e_rosters = run_engine(players, rosters, pool, claims, order)
            d_claims, d_rosters = run_db(rosters, pool, claims, order)
            self.assertEqual(e_claims, d_claims, f"claim outcomes differ, seed {seed}")
            for t in range(1, 7):
                self.assertEqual(e_rosters[t], d_rosters.get(t, []), f"roster differs, team {t}, seed {seed}")


if __name__ == "__main__":
    unittest.main()
