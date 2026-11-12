"""Weekly player scoring, matchups and tiebreaks. Mirrors the Excel workbook and the League Rulebook."""
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


class UnknownStat(ValueError):
    pass


def load_config(path=None):
    path = Path(path) if path else Path(__file__).with_name("config.json")
    return json.loads(path.read_text())


def profile_for(sport, position):
    """Which weight table applies to a player."""
    if sport == "MLB":
        return "MLB hitter" if position == "Hitter" else "MLB pitcher"
    if sport == "NHL":
        return "NHL goalie" if position == "Goalie" else "NHL skater"
    return sport


def native_points(cfg, sport, position, stats):
    """stats: {stat name: value} for one player for the week. Returns native points."""
    prof = profile_for(sport, position)
    total = 0.0
    for stat, val in stats.items():
        if stat == "Games played":
            continue
        if sport == "F1" and stat == "Race finish position":
            total += cfg["f1"]["race_points"].get(str(int(val)), 0)
        elif sport == "F1" and stat == "Qualifying position":
            total += cfg["f1"]["pole_bonus"] if val == 1 else (cfg["f1"]["p2_p3_bonus"] if val <= 3 else 0)
        elif sport == "F1" and stat == "Sprint finish position":
            sp = cfg["f1"]["sprint_points"]
            total += sp[int(val) - 1] if 1 <= val <= len(sp) else 0
        else:
            key = f"{prof}|{stat}"
            if key not in cfg["weights"]:
                raise UnknownStat(key)
            total += val * cfg["weights"][key]
    return total


@dataclass
class PlayerWeek:
    sport: str
    position: str
    native: float
    games: float


def week_totals(cfg, stat_lines):
    """stat_lines: iterable of (player, sport, position, stat, value). Returns {player: PlayerWeek}."""
    stats = defaultdict(dict)
    meta = {}
    for player, sport, position, stat, value in stat_lines:
        meta.setdefault(player, (sport, position))
        stats[player][stat] = stats[player].get(stat, 0) + value
    out = {}
    for player, s in stats.items():
        sport, position = meta[player]
        out[player] = PlayerWeek(sport, position, native_points(cfg, sport, position, s), s.get("Games played", 0))
    return out


@dataclass
class Baseline:
    avg: float
    sd: float
    pool: int
    fallback: bool


def compute_baselines(cfg, week):
    """Weekly baselines per sport|position from players who met the minimum games (rulebook: Baselines).

    Auto average/SD are used only when the qualifying pool has at least min_pool_size players,
    otherwise the fixed fallback values are used. SD is the sample standard deviation.
    """
    pools = defaultdict(list)
    for pw in week.values():
        key = f"{pw.sport}|{pw.position}"
        if pw.games >= cfg["min_games"].get(key, 1):
            pools[key].append(pw.native)
    result = {}
    for key, (fb_avg, fb_sd) in cfg["baselines_fallback"].items():
        vals = pools.get(key, [])
        n = len(vals)
        avg = sum(vals) / n if n else None
        sd = math.sqrt(max(0.0, (sum(v * v for v in vals) - n * avg * avg) / (n - 1))) if n > 1 else None
        use_avg = n >= cfg["min_pool_size"] and n > 0
        use_sd = use_avg and sd is not None and sd > 0
        result[key] = Baseline(avg if use_avg else fb_avg, sd if use_sd else fb_sd, n, not (use_avg and use_sd))
    return result


def frozen_baselines(cfg, history, week):
    """Baselines for `week`, frozen before it starts (rulebook: Baselines).

    history: {week number: {player: PlayerWeek}} for completed weeks. For each sport, pool the qualifying players
    (met the minimum games) from the last `baseline_weeks` weeks BEFORE `week` in which that sport had at least one
    qualifying player, so off-weeks and gaps are skipped. Average and SD are then taken as in compute_baselines,
    with the same fixed fallbacks when the pool is too small or has no spread.
    """
    n = cfg.get("baseline_weeks", 4)
    sports = {k.split("|")[0] for k in cfg["baselines_fallback"]}
    out = {}
    for sport in sports:
        pool, used = {}, 0
        for w in sorted((x for x in history if x < week), reverse=True):
            rows = {(w, p): pw for p, pw in history[w].items()
                    if pw.sport == sport and pw.games >= cfg["min_games"].get(f"{pw.sport}|{pw.position}", 1)}
            if not rows:
                continue
            pool.update(rows)
            used += 1
            if used == n:
                break
        part = compute_baselines(cfg, pool)
        out.update({k: v for k, v in part.items() if k.split("|")[0] == sport})
    return out


def score_player(cfg, native, baseline, played):
    """(native - avg) / sd x scale, floored. Only starters who played score."""
    if not played:
        return 0.0, 0.0
    z = (native - baseline.avg) / baseline.sd
    return z, max(cfg["floor"], z * cfg["scale"])


def score_lineup(cfg, lineup, week, baselines, bonus_points=0.0):
    """lineup: list of (name, sport, position, played). Returns a dict with every number the matchup needs."""
    rows, sports_played = [], set()
    for name, sport, position, played in lineup:
        native = week[name].native if name in week else 0.0
        z, score = score_player(cfg, native, baselines[f"{sport}|{position}"], played)
        rows.append({"name": name, "sport": sport, "position": position, "played": played,
                     "native": native, "z": z, "score": score, "has_stats": name in week})
        if played:
            sports_played.add(sport)
    starter_points = sum(r["score"] for r in rows)
    variety = min(cfg["variety_cap"], cfg["variety_per_sport"] * max(0, len(sports_played) - 1))
    return {
        "starters": rows,
        "starter_points": starter_points,
        "variety_bonus": variety,
        "sports_played": len(sports_played),
        "bonus_points": bonus_points,
        "weekly_total": starter_points + variety + bonus_points,
        "best_starter": max((r["score"] for r in rows), default=0.0),
        "f1_starters": sum(1 for r in rows if r["sport"] == "F1"),
    }


def decide_winner(a, b, cumulative_a, cumulative_b):
    """Rulebook tiebreaks: weekly total, then best single starter, then cumulative points.

    Bonus-challenge picks are not starters, so only starter scores feed the best-starter tiebreak.
    Returns ("A" | "B" | "Tie", decided_by).
    """
    d = round(a["weekly_total"] - b["weekly_total"], 6)
    if d != 0:
        return ("A" if d > 0 else "B"), "Weekly total"
    d = round(a["best_starter"] - b["best_starter"], 6)
    if d != 0:
        return ("A" if d > 0 else "B"), "Best starter"
    if cumulative_a != cumulative_b:
        return ("A" if cumulative_a > cumulative_b else "B"), "Cumulative pts"
    return "Tie", "Tie"
