"""NHL web API (api-web.nhle.com). Unofficial and undocumented; field names checked by tools/feed_probe.py."""
from datetime import timedelta
from .common import jget, minutes, line, now_utc

BASE = "https://api-web.nhle.com/v1"


def parse_schedule(doc):
    out = []
    for day in doc.get("gameWeek", []):
        for g in day.get("games", []):
            state = g.get("gameState", "")
            if g.get("gameType") in (2, 3) and state in ("LIVE", "CRIT", "FINAL", "OFF"):
                out.append({"id": g["id"], "at": g["startTimeUTC"], "final": state in ("FINAL", "OFF")})
    return out


def parse_boxscore(doc, gid, at, final):
    out = []
    for side in ("homeTeam", "awayTeam"):
        team = doc.get("playerByGameStats", {}).get(side, {})
        for p in team.get("forwards", []) + team.get("defense", []):
            if minutes(p.get("toi")) <= 0:
                continue
            stats = {"Games played": 1, "Goals": p.get("goals", 0), "Assists": p.get("assists", 0), "Plus/minus": p.get("plusMinus", 0),
                     "Shots on goal": p.get("sog", 0), "Blocked shots": p.get("blockedShots", 0)}
            out.append(line("nhl", p["playerId"], f"nhl:{gid}", at, final, stats, (p.get("name") or {}).get("default"), "NHL", "Skater"))
        for p in team.get("goalies", []):
            toi = minutes(p.get("toi"))
            if toi <= 0:
                continue
            ga = p.get("goalsAgainst", 0)
            stats = {"Games played": 1, "Saves": p.get("saves", 0), "Goals against": ga, "Wins": 1 if p.get("decision") == "W" else 0,
                     "Shutouts": 1 if ga == 0 and toi >= 59 else 0}
            out.append(line("nhl", p["playerId"], f"nhl:{gid}", at, final, stats, (p.get("name") or {}).get("default"), "NHL", "Goalie"))
    return out


def fetch_lines(due, lookback_hours=30, get=jget):
    end = now_utc(); start = end - timedelta(hours=lookback_hours)
    cut = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    games, seen = [], set()
    for d in sorted({start.date(), end.date()}):
        for g in parse_schedule(get(f"{BASE}/schedule/{d:%Y-%m-%d}")):
            if g["at"] >= cut and g["id"] not in seen:
                seen.add(g["id"]); games.append(g)
    want = set(due([{"key": f"nhl:{g['id']}", "final": g["final"]} for g in games]))
    lines, errors = [], []
    for g in games:
        if f"nhl:{g['id']}" in want:
            try:
                lines += parse_boxscore(get(f"{BASE}/gamecenter/{g['id']}/boxscore"), g["id"], g["at"], g["final"])
            except Exception as e:
                errors.append(f"nhl game {g['id']}: {e}")
    return lines, errors
