"""Live feed probe. Run from your own computer (the build sandbox cannot reach these hosts):
    python3 tools/feed_probe.py
Prints, per sport, whether the feed answers and which of our scoring stats it exposes.
Standard library only. Uses small, polite requests (a handful per feed)."""
import json, sys, time, urllib.request

UA = {"User-Agent": "league-feed-check/0.1 (personal, non-commercial test)"}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()

def jget(url):
    return json.loads(get(url))

def check(name, fn):
    try:
        print(f"[ OK ] {name}: {fn()}")
    except Exception as e:  # report and carry on
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    time.sleep(1)

def mlb():
    d = jget("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-09-20")
    pk = d["dates"][0]["games"][0]["gamePk"]
    b = jget(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")
    p = next(iter(b["teams"]["home"]["players"].values()))
    bat = p.get("stats", {}).get("batting", {})
    pit = next((v["stats"]["pitching"] for v in b["teams"]["home"]["players"].values() if v["stats"].get("pitching")), {})
    need_b = ["singles", "doubles", "triples", "homeRuns", "runs", "rbi", "baseOnBalls", "stolenBases", "strikeOuts"]
    need_p = ["inningsPitched", "strikeOuts", "earnedRuns", "hits", "baseOnBalls", "wins", "saves"]
    # singles is usually absent: hits - doubles - triples - homeRuns
    return f"game {pk}; batting missing={[k for k in need_b if k not in bat]}; pitching missing={[k for k in need_p if k not in pit]}"

def nhl():
    s = jget("https://api-web.nhle.com/v1/schedule/2026-01-15")
    gid = s["gameWeek"][0]["games"][0]["id"]
    b = jget(f"https://api-web.nhle.com/v1/gamecenter/{gid}/boxscore")
    sk = b["playerByGameStats"]["homeTeam"]["forwards"][0]
    gl = b["playerByGameStats"]["homeTeam"]["goalies"][0]
    return f"game {gid}; skater keys={sorted(sk)}; goalie keys={sorted(gl)}"

def f1():
    r = jget("https://api.jolpi.ca/ergast/f1/2026/results.json?limit=30")
    n = len(r["MRData"]["RaceTable"]["Races"])
    q = jget("https://api.jolpi.ca/ergast/f1/2026/qualifying.json?limit=30")
    sp = jget("https://api.jolpi.ca/ergast/f1/2026/sprint.json?limit=30")
    res = r["MRData"]["RaceTable"]["Races"][0]["Results"][0] if n else {}
    return f"races so far={n}; result keys={sorted(res)}; qualifying rows={q['MRData']['total']}; sprint rows={sp['MRData']['total']}"

def nfl():
    url = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2025.csv"
    head = get(url)[:4000].decode("utf-8", "ignore").splitlines()[0].split(",")
    need = ["passing_yards", "passing_tds", "passing_interceptions", "rushing_yards", "rushing_tds",
            "receiving_yards", "receptions", "receiving_tds", "rushing_fumbles_lost", "receiving_fumbles_lost",
            "sack_fumbles_lost", "passing_2pt_conversions", "rushing_2pt_conversions", "receiving_2pt_conversions"]
    return f"{len(head)} columns; missing={[k for k in need if k not in head]}"

def injuries_nfl():
    url = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_2025.csv"
    head = get(url)[:4000].decode("utf-8", "ignore").splitlines()[0].split(",")
    need = ["season", "game_type", "week", "gsis_id", "position", "full_name", "report_status", "date_modified"]
    return f"{len(head)} columns; missing={[k for k in need if k not in head]}"

def injuries_mlb():
    t = jget("https://statsapi.mlb.com/api/v1/teams?sportId=1")["teams"]
    r = jget(f"https://statsapi.mlb.com/api/v1/teams/{t[0]['id']}/roster?rosterType=40Man")["roster"]
    codes = sorted({x.get("status", {}).get("code", "?") for x in r})
    return f"{len(t)} teams; first roster {len(r)} players; status codes seen={codes} (injured lists look like D10/D15/D60)"

def injuries_espn():
    out = []
    for name, path in (("NBA", "basketball/nba"), ("NHL", "hockey/nhl"), ("MLS", "soccer/usa.1")):
        try:
            d = jget(f"https://site.api.espn.com/apis/site/v2/sports/{path}/injuries")
            teams = d.get("injuries", [])
            first = next((i for t in teams for i in t.get("injuries", [])), {})
            out.append(f"{name}: {sum(len(t.get('injuries', [])) for t in teams)} listed; keys={sorted(first)}; athlete keys={sorted(first.get('athlete', {}))[:6]}; status={first.get('status')!r}")
        except Exception as e:
            out.append(f"{name}: {type(e).__name__}: {e}")
        time.sleep(1)
    return " | ".join(out)

def nba():
    d = jget("https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json")
    gid = d["scoreboard"]["games"][0]["gameId"]
    b = jget(f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json")
    p = b["game"]["homeTeam"]["players"][0]["statistics"]
    need = ["points", "reboundsTotal", "assists", "steals", "blocks", "turnovers"]
    return f"game {gid}; missing={[k for k in need if k not in p]}"

if __name__ == "__main__":
    for name, fn in [("MLB Stats API", mlb), ("NHL web API", nhl), ("Jolpica F1", f1), ("nflverse", nfl), ("NBA CDN live data", nba),
                     ("Injuries: nflverse", injuries_nfl), ("Injuries: MLB rosters", injuries_mlb), ("Injuries: ESPN (NBA, NHL, MLS)", injuries_espn)]:
        check(name, fn)
    print("\nMLS has no free feed here: see the findings doc. Nothing to probe.")
