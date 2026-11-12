"""Simulate one invented season with the tested schedule engine and write the data the standings page shows.

Run: python3 tools/build_season_data.py   (writes standings/season_sample.json, then standings/index.html from template.html)
Scores are random; every pairing, ranking, seed and advancement comes from engine/schedule.py.
"""
import json, random, sys
from pathlib import Path

sys.path.insert(0, ".")
from engine import schedule as S

SEED = 11
ME = 4
r = random.Random(SEED)
IDS = list(range(12))
strength = {t: r.gauss(250, 18) for t in IDS}
scores = {w: {t: round(max(-20.0, r.gauss(strength[t], 38)), 1) for t in IDS} for w in range(1, 50)}
reg = S.regular_season(IDS, seed=SEED)

matches = {w: [] for w in range(1, 52)}                       # week -> [{a,b,sa,sb,kind}]
def play(w, a, b, kind, **extra):
    m = {"a": a, "b": b, "sa": scores[w][a], "sb": scores[w][b], "kind": kind, **extra}
    matches[w].append(m); return m
def winner(m):
    return m["a"] if m["sa"] >= m["sb"] else m["b"]

for w in range(1, 45):
    for a, b in reg[w]:
        play(w, a, b, "regular")

def standings_through(week, cup_bonus_team=None):
    rows = {t: {"id": t, "wins": 0, "losses": 0, "cumulative": 0.0, "bonus": 0, "form": []} for t in IDS}
    for w in range(1, min(week, 44) + 1):
        for m in matches[w]:
            if m["kind"] != "regular":
                continue
            wn = winner(m)
            for t, sc in ((m["a"], m["sa"]), (m["b"], m["sb"])):
                rows[t]["cumulative"] = round(rows[t]["cumulative"] + sc, 1)
                if t == wn: rows[t]["wins"] += 1
                else: rows[t]["losses"] += 1
                rows[t]["form"].append("W" if t == wn else "L")
    if cup_bonus_team is not None:
        rows[cup_bonus_team]["wins"] += 1; rows[cup_bonus_team]["bonus"] = 1
    return rows

# ---- Cup ----
rank19 = S.rank_teams(list(standings_through(19).values()))
groups = S.cup_groups([t["id"] for t in rank19], seed=SEED)
gsched = S.cup_group_schedule(groups, seed=SEED)
group_results = [[] for _ in groups]
for w in S.CUP_GROUP_WEEKS:
    for a, b in gsched[w]:
        m = play(w, a, b, "cup-group")
        wn = winner(m)
        gi = next(i for i, g in enumerate(groups) if a in g)
        group_results[gi].append({"winner": wn, "loser": b if wn == a else a, "points": {a: m["sa"], b: m["sb"]}})
tables = [S.cup_group_table(g, res) for g, res in zip(groups, group_results)]
fallback = [t["id"] for t in rank19]
kseeds = S.cup_knockout_seeds(tables, fallback_rank=fallback)
semis = []
for a, b in S.cup_semifinals(kseeds):
    m = play(S.CUP_SEMI_WEEK, a, b, "cup-semi"); semis.append(winner(m))
fm = play(S.CUP_FINAL_WEEK, semis[0], semis[1], "cup-final")
cup_champ = winner(fm); cup_runner = fm["b"] if cup_champ == fm["a"] else fm["a"]

# ---- regular season final + playoffs ----
final_rows = standings_through(44, cup_bonus_team=cup_champ)
pseeds = S.playoff_seeds(list(final_rows.values()))
wc = []
for a, b in S.wildcard_round(pseeds):
    m = play(S.WILDCARD_WEEK, a, b, "wildcard"); wc.append(a if m["sa"] >= m["sb"] else b)   # higher seed listed first; exact ties go to it
sf_pairs = S.semifinal_round(pseeds, wc)
def two_leg(pair, weeks, kind):
    a, b = pair
    for w in weeks:
        play(w, a, b, kind)
    ta = [scores[w][a] for w in weeks]; tb = [scores[w][b] for w in weeks]
    return S.advance_aggregate(a, b, ta, tb, pseeds)
sf_winners = [two_leg(p, S.SEMI_WEEKS, "playoff-semi") for p in sf_pairs]
fin = (min(sf_winners, key=pseeds.index), max(sf_winners, key=pseeds.index))
champ = two_leg(fin, S.FINAL_WEEKS, "playoff-final")

# ---- snapshots ----
def snapshot(week):
    bonus = cup_champ if week >= S.CUP_FINAL_WEEK else None
    return [{"id": t["id"], "wins": t["wins"], "losses": t["losses"], "bonus": t["bonus"], "cumulative": t["cumulative"], "form": t["form"][-5:]}
            for t in S.rank_teams(list(standings_through(week, bonus).values()))]
data = {
    "me": ME, "teams": [{"id": t, "name": f"Team {t + 1}" + (" (you)" if t == ME else "")} for t in IDS],
    "calendar": S.season_calendar(), "matches": matches,
    "standings": {w: snapshot(w) for w in range(0, 52)},
    "cup": {"groups": groups, "tables": tables, "seeds": kseeds, "semis": [list(p) for p in S.cup_semifinals(kseeds)], "champion": cup_champ, "runner_up": cup_runner, "tier_rank": fallback},
    "playoffs": {"seeds": pseeds, "wildcard": [list(p) for p in S.wildcard_round(pseeds)], "wildcard_winners": wc, "semis": [list(p) for p in sf_pairs], "semi_winners": sf_winners, "final": list(fin), "champion": champ},
}
out = Path("standings")
(out / "season_sample.json").write_text(json.dumps(data, separators=(",", ":")))
tpl = (out / "template.html").read_text() if (out / "template.html").exists() else None
if tpl:
    (out / "index.html").write_text(tpl.replace("/*SEASON_DATA*/null", json.dumps(data, separators=(",", ":"))))
print("champion", champ, "cup", cup_champ, "seeds", pseeds, "bytes", len(json.dumps(data)))
