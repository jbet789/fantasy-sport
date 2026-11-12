"""NBA live-data files (cdn.nba.com). The probe tests whether they answer from a cloud address."""
from datetime import timedelta
from .common import jget, minutes, line, now_utc

SCHEDULE = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BOX = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{}.json"


def parse_schedule(doc):
    out = []
    for day in doc.get("leagueSchedule", {}).get("gameDates", []):
        for g in day.get("games", []):
            if g.get("gameStatus") in (2, 3):
                out.append({"id": g["gameId"], "at": g["gameDateTimeUTC"].replace("+00:00", "Z"), "final": g["gameStatus"] == 3})
    return out


def parse_boxscore(doc, gid, at, final):
    out = []
    game = doc.get("game", {})
    for side in ("homeTeam", "awayTeam"):
        for p in game.get(side, {}).get("players", []):
            s = p.get("statistics", {})
            if minutes(s.get("minutes") or s.get("minutesCalculated")) <= 0:
                continue
            stats = {"Games played": 1, "Points": s.get("points", 0), "Rebounds": s.get("reboundsTotal", 0), "Assists": s.get("assists", 0),
                     "Steals": s.get("steals", 0), "Blocks": s.get("blocks", 0), "Turnovers": s.get("turnovers", 0)}
            out.append(line("nba", p["personId"], f"nba:{gid}", at, final, stats))      # position is not reliable here: players come from the pool file
    return out


def fetch_lines(due, lookback_hours=30, get=jget):
    end = now_utc(); start = end - timedelta(hours=lookback_hours)
    cut = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    games = [g for g in parse_schedule(get(SCHEDULE)) if g["at"] >= cut]
    want = set(due([{"key": f"nba:{g['id']}", "final": g["final"]} for g in games]))
    lines, errors = [], []
    for g in games:
        if f"nba:{g['id']}" in want:
            try:
                lines += parse_boxscore(get(BOX.format(g["id"])), g["id"], g["at"], g["final"])
            except Exception as e:
                errors.append(f"nba game {g['id']}: {e}")
    return lines, errors
