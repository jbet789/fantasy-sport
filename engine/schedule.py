"""Season structure from the League Rulebook: regular season, in-season Cup, playoffs.

Pure logic, no data feeds. Teams are plain ids (any hashable). Randomness takes an explicit seed so a
schedule can be regenerated or audited. Matchups are (home, away) pairs, but "home" has no meaning in this
league; the order only keeps the output stable.

Calendar: week n starts Tuesday noon ET on Jan 5, 2027 + 7*(n-1) days.
"""
import random
from datetime import date, timedelta

TEAMS = 12
REGULAR_WEEKS = 44
CUP_GROUP_WEEKS = (20, 21, 22)
CUP_SEMI_WEEK, CUP_FINAL_WEEK = 23, 24
WILDCARD_WEEK = 45
SEMI_WEEKS = (46, 47)
FINAL_WEEKS = (48, 49)
BUFFER_WEEKS = (50, 51)
SEASON_START = date(2027, 1, 5)


def week_start(week):
    return SEASON_START + timedelta(days=7 * (week - 1))


def week_end(week):
    return week_start(week + 1)


# ---- regular season ----------------------------------------------------------------------------

def round_robin(teams):
    """Circle method: n-1 rounds, every team plays every other team exactly once."""
    t = list(teams)
    n = len(t)
    assert n % 2 == 0
    rounds = []
    for r in range(n - 1):
        rounds.append([(t[i], t[n - 1 - i]) for i in range(n // 2)])
        t = [t[0]] + [t[-1]] + t[1:-1]
    return rounds


def regular_season(teams, seed=0, blocks=4):
    """Four round-robins back to back (44 weeks for 12 teams).

    Round order is reshuffled inside each block so the same opponents don't meet in the same weeks every block,
    and a block boundary never repeats the previous week's matchups. Returns {week: [(a, b), ...]}.
    """
    rng = random.Random(seed)
    teams = list(teams)
    rng.shuffle(teams)
    base = round_robin(teams)
    schedule, prev = {}, None
    week = 1
    for b in range(blocks):
        while True:
            order = list(range(len(base)))
            rng.shuffle(order)
            first = {frozenset(m) for m in base[order[0]]}
            if prev is None or not (first & prev):
                break
        for idx in order:
            schedule[week] = [tuple(sorted(m, key=str)) for m in base[idx]]
            week += 1
        prev = {frozenset(m) for m in base[order[-1]]}
    return schedule


# ---- standings -------------------------------------------------------------------------------

def rank_teams(teams):
    """teams: [{'id', 'wins', 'cumulative'}]. Wins (Cup bonus win included), then cumulative points, then id."""
    return sorted(teams, key=lambda t: (-t["wins"], -t["cumulative"], str(t["id"])))


# ---- Cup --------------------------------------------------------------------------------------

def cup_groups(ranked_ids, seed=0):
    """After week 19: 4 tiers of 3 by standings; each of 3 groups of 4 gets one team per tier (random within tier)."""
    assert len(ranked_ids) == 12
    rng = random.Random(seed)
    groups = [[], [], []]
    for tier in range(4):
        chunk = list(ranked_ids[tier * 3:(tier + 1) * 3])
        rng.shuffle(chunk)
        for g, team in zip(groups, chunk):
            g.append(team)
    return groups


def cup_group_schedule(groups, seed=0):
    """Weeks 20-22: each team plays the other three in its group once, one opponent per week."""
    rng = random.Random(seed)
    sched = {w: [] for w in CUP_GROUP_WEEKS}
    for g in groups:
        a, b, c, d = g
        rounds = [[(a, b), (c, d)], [(a, c), (b, d)], [(a, d), (b, c)]]
        rng.shuffle(rounds)
        for w, rd in zip(CUP_GROUP_WEEKS, rounds):
            sched[w].extend(rd)
    return sched


def cup_group_table(group, results):
    """results: [{'winner', 'loser', 'points': {team: pts}}]. Returns teams ranked by group record, then group points."""
    wins = {t: 0 for t in group}
    pts = {t: 0.0 for t in group}
    for r in results:
        if r["winner"] in wins:
            wins[r["winner"]] += 1
        for t, p in r["points"].items():
            if t in pts:
                pts[t] += p
    rows = [{"id": t, "wins": wins[t], "points": round(pts[t], 6)} for t in group]
    return sorted(rows, key=lambda r: (-r["wins"], -r["points"]))


def cup_knockout_seeds(group_tables, fallback_rank=None):
    """Three group winners plus the best runner-up (by group points). Seeds ranked by group record, then group points.

    fallback_rank: ordered list of ids (regular-season standings) used only if record and points both tie, which the
    rulebook does not cover.
    """
    winners = [t[0] for t in group_tables]
    runner = max((t[1] for t in group_tables), key=lambda r: (r["points"], r["wins"]))
    rank = {t: i for i, t in enumerate(fallback_rank or [])}
    q = sorted(winners + [runner], key=lambda r: (-r["wins"], -r["points"], rank.get(r["id"], 99)))
    return [r["id"] for r in q]


def cup_semifinals(seeds):
    return [(seeds[0], seeds[3]), (seeds[1], seeds[2])]


# ---- playoffs ---------------------------------------------------------------------------------

def playoff_seeds(standings):
    """Top 4 by record, then the top 2 by cumulative points among the rest. Seeds 5 and 6 go in cumulative order.

    The rulebook does not say how seeds 5 and 6 are ordered; cumulative points is the natural reading.
    """
    ranked = rank_teams(standings)
    top4 = ranked[:4]
    rest = sorted(ranked[4:], key=lambda t: (-t["cumulative"], -t["wins"], str(t["id"])))
    return [t["id"] for t in top4 + rest[:2]]


def wildcard_round(seeds):
    return [(seeds[2], seeds[5]), (seeds[3], seeds[4])]


def semifinal_round(seeds, wildcard_winners):
    """Seed 1 plays the lowest remaining seed, seed 2 plays the other."""
    pos = {s: i for i, s in enumerate(seeds)}
    low, high = sorted(wildcard_winners, key=lambda s: pos[s], reverse=True)
    return [(seeds[0], low), (seeds[1], high)]


def advance_aggregate(a, b, totals_a, totals_b, seeds):
    """Multi-week round: weekly totals add up. A tie goes to the higher (better) seed."""
    sa, sb = sum(totals_a), sum(totals_b)
    if round(sa - sb, 6) != 0:
        return a if sa > sb else b
    return a if seeds.index(a) < seeds.index(b) else b


def advance_single(a, b, result, seeds):
    """Single week (wildcard). result is the engine's decide_winner output; a full tie goes to the higher seed."""
    who = result[0]
    if who == "A":
        return a
    if who == "B":
        return b
    return a if seeds.index(a) < seeds.index(b) else b


def season_calendar():
    """Every scoring week with its phase and the Tuesday it starts."""
    rows = []
    for w in range(1, 52):
        if w <= REGULAR_WEEKS:
            phase = "Regular season"
            if w in CUP_GROUP_WEEKS:
                phase += " + Cup group stage"
            elif w == CUP_SEMI_WEEK:
                phase += " + Cup semifinals"
            elif w == CUP_FINAL_WEEK:
                phase += " + Cup final"
        elif w == WILDCARD_WEEK:
            phase = "Playoffs: wildcard"
        elif w in SEMI_WEEKS:
            phase = "Playoffs: semifinals"
        elif w in FINAL_WEEKS:
            phase = "Playoffs: championship"
        else:
            phase = "Buffer"
        rows.append({"week": w, "starts": week_start(w).isoformat(), "ends": week_end(w).isoformat(), "phase": phase})
    return rows
