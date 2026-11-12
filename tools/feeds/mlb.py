"""MLB Stats API (statsapi.mlb.com). Unofficial: individual, non-commercial use only."""
from datetime import timedelta
from .common import jget, innings, line, now_utc

BASE = "https://statsapi.mlb.com/api/v1"


def parse_schedule(doc):
    """-> [{'pk', 'at', 'final', 'live'}] for games that have started."""
    out = []
    for d in doc.get("dates", []):
        for g in d.get("games", []):
            state = g.get("status", {}).get("abstractGameState")
            if state in ("Final", "Live") and g.get("status", {}).get("detailedState") not in ("Postponed", "Cancelled"):
                out.append({"pk": g["gamePk"], "at": g["gameDate"], "final": state == "Final"})
    return out


def parse_boxscore(doc, pk, at, final):
    """Players who played. Pitchers (and only pitchers) score pitching stats; everyone else scores batting."""
    out = []
    for side in ("home", "away"):
        for p in doc.get("teams", {}).get(side, {}).get("players", {}).values():
            person, pos = p.get("person", {}), p.get("position", {}).get("abbreviation", "")
            st = p.get("stats", {})
            bat, pit = st.get("batting") or {}, st.get("pitching") or {}
            if pos == "P":
                if not pit or not innings(pit.get("inningsPitched", 0)) and not pit.get("outs"):
                    continue
                stats = {"Games played": 1, "Innings pitched": innings(pit.get("inningsPitched", 0)), "Strikeouts": pit.get("strikeOuts", 0),
                         "Earned runs": pit.get("earnedRuns", 0), "Hits allowed": pit.get("hits", 0), "Walks allowed": pit.get("baseOnBalls", 0),
                         "Win": pit.get("wins", 0), "Save": pit.get("saves", 0)}
                position = "SP" if pit.get("gamesStarted", 0) else "RP"
            else:
                if not (bat.get("plateAppearances") or bat.get("atBats") or bat.get("baseOnBalls")):
                    continue
                h, d2, d3, hr = (bat.get(k, 0) for k in ("hits", "doubles", "triples", "homeRuns"))
                stats = {"Games played": 1, "Singles": h - d2 - d3 - hr, "Doubles": d2, "Triples": d3, "Home runs": hr, "Runs": bat.get("runs", 0),
                         "RBI": bat.get("rbi", 0), "Walks": bat.get("baseOnBalls", 0), "Stolen bases": bat.get("stolenBases", 0), "Strikeouts": bat.get("strikeOuts", 0)}
                position = "Hitter"
            out.append(line("mlb", person.get("id"), f"mlb:{pk}", at, final, stats, person.get("fullName"), "MLB", position))
    return out


def fetch_lines(due, lookback_hours=30, get=jget):
    """due(games) -> keys still needing a read (the database decides). Returns (lines, errors)."""
    end = now_utc(); start = end - timedelta(hours=lookback_hours)
    doc = get(f"{BASE}/schedule?sportId=1&startDate={start:%Y-%m-%d}&endDate={end:%Y-%m-%d}")
    games = [g for g in parse_schedule(doc) if g["at"] >= start.strftime("%Y-%m-%dT%H:%M:%SZ")]
    want = set(due([{"key": f"mlb:{g['pk']}", "final": g["final"]} for g in games]))
    lines, errors = [], []
    for g in games:
        if f"mlb:{g['pk']}" in want:
            try:
                lines += parse_boxscore(get(f"{BASE}/game/{g['pk']}/boxscore"), g["pk"], g["at"], g["final"])
            except Exception as e:
                errors.append(f"mlb game {g['pk']}: {e}")
    return lines, errors
