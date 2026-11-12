"""Side-by-side of three ways to set the weekly baseline, on invented data (no real feeds needed).

A  same-week    : baseline from the players who play that very week (current rulebook; provisional until week ends)
B  previous-week: baseline frozen from the previous calendar week (live and final)
C  trailing-4   : baseline frozen from the last 4 weeks in which that sport actually played (live and final)

Run: python3 tools/baseline_compare.py [seasons]
"""
import math, random, sys
from collections import defaultdict
from statistics import mean, pstdev

sys.path.insert(0, ".")
from engine import load_config
from engine.scoring import PlayerWeek, compute_baselines, score_player
from engine.schedule import regular_season

CFG = load_config()
KEYS = list(CFG["baselines_fallback"])
SPORT = lambda k: k.split("|")[0]
POOL = {k: (20 if k.startswith("F1") else 60) for k in KEYS}
WEEKS = 44
SL = float(__import__("os").environ.get("SLATE", "1"))

def active(sport, w):
    """Rough calendar-year activity (week 1 = Jan 5)."""
    return {"NFL": w <= 4 or w >= 36, "NBA": w <= 24 or w >= 43, "NHL": w <= 24 or w >= 41,
            "MLB": 13 <= w <= 39, "MLS": 8 <= w <= 43, "F1": 10 <= w <= 44 and w % 2 == 0}[sport]

def season(seed):
    r = random.Random(seed)
    players = {}                      # id -> (key, skill)
    for k in KEYS:
        for i in range(POOL[k]):
            players[(k, i)] = r.gauss(0, 0.6)
    slate = {(s, w): SL * (r.gauss(0, 0.35) + 0.25 * math.sin(w / 5 + hash(s) % 7)) for s in {SPORT(k) for k in KEYS} for w in range(1, WEEKS + 1)}
    weeks = {}                       # w -> {pid: PlayerWeek}
    for w in range(1, WEEKS + 1):
        wk = {}
        for (k, i), skill in players.items():
            s = SPORT(k)
            if not active(s, w) or r.random() > 0.8:
                continue
            fa, fs = CFG["baselines_fallback"][k]
            wk[(k, i)] = PlayerWeek(s, k.split("|")[1], max(0.0, fa + fs * (skill + slate[(s, w)] + r.gauss(0, 0.8))), CFG["min_games"][k])
        weeks[w] = wk
    return r, players, weeks

def pooled(weeks, ws):
    d = {}
    for w in ws:
        for pid, pw in weeks[w].items():
            d[(w, pid)] = pw
    return d

def baselines_for(method, weeks, w):
    if method == "A":
        return compute_baselines(CFG, weeks[w])
    if method == "B":
        return compute_baselines(CFG, pooled(weeks, [w - 1]) if w > 1 else {})
    out = {}
    for s in {SPORT(k) for k in KEYS}:
        prev = [x for x in range(w - 1, 0, -1) if active(s, x)][:4]
        sub = {pid: pw for pid, pw in pooled(weeks, prev).items() if pw.sport == s}
        part = compute_baselines(CFG, sub)
        out.update({k: v for k, v in part.items() if SPORT(k) == s})
    return out

def spearman(a, b):
    def rank(v):
        o = sorted(range(len(v)), key=lambda i: v[i]); r = [0] * len(v)
        for p, i in enumerate(o): r[i] = p
        return r
    ra, rb = rank(a), rank(b)
    ma = mean(ra)
    num = sum((x - ma) * (y - ma) for x, y in zip(ra, rb)); den = sum((x - ma) ** 2 for x in ra)
    return num / den

def run(seed):
    r, players, weeks = season(seed)
    base = {m: {w: baselines_for(m, weeks, w) for w in range(1, WEEKS + 1)} for m in "ABC"}
    # 12 teams, 20 players each (random across sports); starters = best 10 by last week's score among active sports
    ids = list(players)
    teams = {t: r.sample(ids, 20) for t in range(12)}
    last = defaultdict(float)
    res = {m: defaultdict(dict) for m in "ABC"}                    # res[m][w][team] = total
    pts = {m: [] for m in "ABC"}                                    # per-starter scores (played)
    zst = {m: defaultdict(list) for m in "ABC"}                     # (sport,w) -> z values
    fb = {m: [0, 0] for m in "ABC"}
    for w in range(1, WEEKS + 1):
        for t, roster in teams.items():
            avail = [p for p in roster if p in weeks[w]]
            starters = sorted(avail, key=lambda p: -last[p])[:10]
            sports = {m: set() for m in "ABC"}
            tot = {m: 0.0 for m in "ABC"}
            for p in starters:
                for m in "ABC":
                    k = p[0]; bl = base[m][w][k]
                    z, sc = score_player(CFG, weeks[w][p].native, bl, True)
                    tot[m] += sc; sports[m].add(SPORT(k)); pts[m].append(sc)
                    zst[m][(SPORT(k), w)].append(z)
            for m in "ABC":
                res[m][w][t] = tot[m] + min(CFG["variety_cap"], CFG["variety_per_sport"] * max(0, len(sports[m]) - 1))
        for p, pw in weeks[w].items():
            last[p] = (pw.native - CFG["baselines_fallback"][p[0]][0]) / CFG["baselines_fallback"][p[0]][1]
        for m in "ABC":
            for k, b in base[m][w].items():
                if active(SPORT(k), w):
                    fb[m][0] += b.fallback; fb[m][1] += 1
    sched = regular_season(list(range(12)), seed=seed)
    wins = {m: [0] * 12 for m in "ABC"}; games = 0
    for w, ms in sched.items():
        for a, b in ms:
            games += 1
            for m in "ABC":
                wins[m][a if res[m][w][a] > res[m][w][b] else b] += 1
    flips = {m: 0 for m in "BC"}
    for w, ms in sched.items():
        for a, b in ms:
            wa = a if res["A"][w][a] > res["A"][w][b] else b
            for m in "BC":
                flips[m] += (a if res[m][w][a] > res[m][w][b] else b) != wa
    cum = {m: [sum(res[m][w][t] for w in range(1, WEEKS + 1)) for t in range(12)] for m in "ABC"}
    def top6(m):
        return set(sorted(range(12), key=lambda t: (-wins[m][t], -cum[m][t]))[:6])
    out = {"flip": {m: flips[m] / games for m in "BC"},
           "rho": {m: spearman(wins["A"], wins[m]) for m in "BC"},
           "playoff": {m: len(top6("A") & top6(m)) for m in "BC"},
           "champ_same": {m: sorted(range(12), key=lambda t: (-wins["A"][t], -cum["A"][t]))[0] == sorted(range(12), key=lambda t: (-wins[m][t], -cum[m][t]))[0] for m in "BC"},
           "pt_corr": {}, "pt_mad": {}, "zmean": {}, "zsd": {}, "fallback": {m: fb[m][0] / fb[m][1] for m in "ABC"}}
    for m in "BC":
        a, b = pts["A"], pts[m]
        ma, mb = mean(a), mean(b)
        out["pt_corr"][m] = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) * pstdev(a) * pstdev(b))
        out["pt_mad"][m] = mean(abs(x - y) for x, y in zip(a, b))
    for m in "ABC":
        ms = [abs(mean(v)) for v in zst[m].values() if len(v) >= 5]
        out["zmean"][m] = mean(ms)
        out["zsd"][m] = mean(pstdev(v) for v in zst[m].values() if len(v) >= 5)
    out["wk_ex"] = None
    return out

def get(runs, *path):
    vals = []
    for x in runs:
        for p in path: x = x[p]
        vals.append(float(x))
    return mean(vals)

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    runs = [run(s) for s in range(n)]
    g = lambda *path: get(runs, *path)
    print(f"{n} simulated seasons, 12 teams, 44 weeks, invented stats with league-wide hot/cold weeks and off-seasons\n")
    print("Share of active sport-weeks on fixed fallback baselines: A %.0f%%  B %.0f%%  C %.0f%%" % tuple(100 * g("fallback", m) for m in "ABC"))
    print("Average |mean z| of starters per sport-week (0 = centred):  A %.2f  B %.2f  C %.2f" % tuple(g("zmean", m) for m in "ABC"))
    print("Average SD of z per sport-week (1 = well scaled):           A %.2f  B %.2f  C %.2f" % tuple(g("zsd", m) for m in "ABC"))
    print("\nVersus same-week method A:                B prev-week   C trailing-4")
    print("  Correlation of starter points            %.3f         %.3f" % (g("pt_corr", "B"), g("pt_corr", "C")))
    print("  Average points difference per starter    %.2f          %.2f" % (g("pt_mad", "B"), g("pt_mad", "C")))
    print("  Weekly matchups with a different winner  %.1f%%         %.1f%%" % (100 * g("flip", "B"), 100 * g("flip", "C")))
    print("  Standings rank correlation               %.3f         %.3f" % (g("rho", "B"), g("rho", "C")))
    print("  Same 6 playoff teams (average of 6)      %.2f          %.2f" % (g("playoff", "B"), g("playoff", "C")))
    print("  Same top seed                            %.0f%%           %.0f%%" % (100 * g("champ_same", "B"), 100 * g("champ_same", "C")))
