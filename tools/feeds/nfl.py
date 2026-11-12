"""NFL: nflverse weekly player stats (CSV release files) plus its game schedule for dates."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .common import csv_rows, get, num, line, utc_iso, now_utc

ET = ZoneInfo("America/New_York")
STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{}.csv"
GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
POS = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE"}
COLUMNS = ["passing_yards", "passing_tds", "passing_interceptions", "rushing_yards", "rushing_tds", "receiving_yards", "receptions", "receiving_tds",
           "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost", "passing_2pt_conversions", "rushing_2pt_conversions", "receiving_2pt_conversions"]


def season_of(now):
    return now.year if now.month >= 3 else now.year - 1


def parse_games(text):
    """-> {(season, week): UTC time} using the last game day of each regular-season week at noon Eastern."""
    last = {}
    for r in csv_rows(text):
        if r.get("game_type") == "REG" and r.get("gameday"):
            k = (int(r["season"]), int(r["week"]))
            last[k] = max(last.get(k, ""), r["gameday"])
    return {k: utc_iso(datetime.strptime(d, "%Y-%m-%d").replace(hour=12, tzinfo=ET)) for k, d in last.items()}


def parse_week_stats(text, when, since=None, until=None):
    """Regular-season player-weeks whose date falls in [since, until] (UTC strings)."""
    out = []
    for r in csv_rows(text):
        pos = POS.get(r.get("position", ""))
        if r.get("season_type", "REG") != "REG" or not pos:
            continue
        at = when.get((int(r["season"]), int(r["week"])))
        if not at or (since and at < since) or (until and at > until):
            continue
        g = lambda k: num(r.get(k))
        stats = {"Games played": 1, "Passing yards": g("passing_yards"), "Passing TD": g("passing_tds"), "Interception thrown": g("passing_interceptions"),
                 "Rushing yards": g("rushing_yards"), "Rushing TD": g("rushing_tds"), "Receiving yards": g("receiving_yards"), "Receiving TD": g("receiving_tds"),
                 "Receptions (half-PPR)": g("receptions"),
                 "Fumble lost": g("rushing_fumbles_lost") + g("receiving_fumbles_lost") + g("sack_fumbles_lost"),
                 "2-point conversion": g("passing_2pt_conversions") + g("rushing_2pt_conversions") + g("receiving_2pt_conversions")}
        name = r.get("player_display_name") or r.get("player_name")
        out.append(line("nfl", r["player_id"], f"nfl:{r['season']}:{r['week']}", at, True, stats, name, "NFL", pos))
    return out


def fetch_lines(due=None, lookback_hours=240, getter=get):
    now = now_utc(); season = season_of(now)
    when = parse_games(getter(GAMES_URL).decode("utf-8", "replace"))
    since = utc_iso(now - timedelta(hours=lookback_hours)); until = utc_iso(now + timedelta(hours=12))
    if not any(since <= v <= until for v in when.values()):          # no NFL week in range (off-season): nothing to download
        return [], []
    return parse_week_stats(getter(STATS_URL.format(season)).decode("utf-8", "replace"), when, since, until), []
