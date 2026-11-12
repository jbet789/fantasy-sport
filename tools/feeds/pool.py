"""Player pool and draft ranking.

Ranking = a player's expected weekly score under OUR scoring, from last season's totals:
    z = (season points / weeks in that sport's season  -  position average) / position spread     (averages from the positions table)
Players are then ordered by z across all sports; position in that list is the draft ranking. It ignores the weekly floor and the
variety bonus, which is fine for ordering. F1 has no per-week points here, so drivers are placed by last season's standings.
"""
from . import mlb as _mlb  # noqa: F401  (keeps module names together)
from .common import jget, get, csv_rows, innings, num
from . import nfl as _nfl

SEASON_WEEKS = {"NFL": 17, "NBA": 25, "MLB": 26, "NHL": 26, "MLS": 33}
POOL_SIZE = {("NFL", "QB"): 40, ("NFL", "RB"): 80, ("NFL", "WR"): 100, ("NFL", "TE"): 40,
             ("NBA", "Guard"): 55, ("NBA", "Wing"): 55, ("NBA", "Big"): 45,
             ("MLB", "Hitter"): 300, ("MLB", "SP"): 120, ("MLB", "RP"): 100,
             ("NHL", "Skater"): 300, ("NHL", "Goalie"): 60,
             ("MLS", "Forward"): 60, ("MLS", "Midfielder"): 70, ("MLS", "Defender"): 70, ("MLS", "Goalkeeper"): 30, ("F1", "Driver"): 24}


def score(entry, weights, spread):
    """entry: {'sport','position','totals':{stat: value}}; weights {profile: {stat: w}}; spread {(sport, pos): (profile, avg, sd)}."""
    profile, avg, sd = spread[(entry["sport"], entry["position"])]
    pts = sum(weights.get(profile, {}).get(stat, 0) * v for stat, v in entry["totals"].items())
    full = SEASON_WEEKS[entry["sport"]]
    games = entry.get("games")
    per_week = pts / (full if not games else max(games, full * 0.4))      # per game played, but a short sample counts as a part-season, not a hot streak
    return (per_week - avg) / sd


def rank_pool(entries, weights, spread):
    """entries carry 'score' (already z) or 'totals'. Keep the top POOL_SIZE per position, then number them 1.. across sports."""
    for e in entries:
        if "score" not in e:
            e["score"] = score(e, weights, spread)
    keep = []
    for key in {(e["sport"], e["position"]) for e in entries}:
        group = sorted((e for e in entries if (e["sport"], e["position"]) == key), key=lambda e: -e["score"])
        keep += group[:POOL_SIZE.get(key, 50)]
    keep.sort(key=lambda e: (-e["score"], e["name"]))
    return [{"src": e["src"], "ext": e["ext"], "name": e["name"], "sport": e["sport"], "position": e["position"], "ranking": i + 1} for i, e in enumerate(keep)]


# ---- MLB -------------------------------------------------------------------------------------------
def mlb_pool(people_doc, hitting_doc, pitching_doc):
    pos = {str(p["id"]): (p.get("primaryPosition") or {}).get("abbreviation", "") for p in people_doc.get("people", [])}
    out = []
    for s in (hitting_doc.get("stats") or [{}])[0].get("splits", []):
        pl, st = s["player"], s["stat"]
        if pos.get(str(pl["id"])) == "P":
            continue
        h, d2, d3, hr = (st.get(k, 0) for k in ("hits", "doubles", "triples", "homeRuns"))
        out.append({"src": "mlb", "ext": str(pl["id"]), "name": pl["fullName"], "sport": "MLB", "position": "Hitter",
                    "totals": {"Singles": h - d2 - d3 - hr, "Doubles": d2, "Triples": d3, "Home runs": hr, "Runs": st.get("runs", 0), "RBI": st.get("rbi", 0),
                               "Walks": st.get("baseOnBalls", 0), "Stolen bases": st.get("stolenBases", 0), "Strikeouts": st.get("strikeOuts", 0)}, "games": st.get("gamesPlayed")})
    for s in (pitching_doc.get("stats") or [{}])[0].get("splits", []):
        pl, st = s["player"], s["stat"]
        gs, g = st.get("gamesStarted", 0), st.get("gamesPitched", st.get("gamesPlayed", 0)) or 1
        out.append({"src": "mlb", "ext": str(pl["id"]), "name": pl["fullName"], "sport": "MLB", "position": "SP" if gs / g >= 0.5 else "RP",
                    "totals": {"Innings pitched": innings(st.get("inningsPitched", 0)), "Strikeouts": st.get("strikeOuts", 0), "Earned runs": st.get("earnedRuns", 0),
                               "Hits allowed": st.get("hits", 0), "Walks allowed": st.get("baseOnBalls", 0), "Win": st.get("wins", 0), "Save": st.get("saves", 0)}, "games": g})
    return out


def fetch_mlb(season, getter=jget):
    base = "https://statsapi.mlb.com/api/v1"
    q = f"stats=season&season={season}&sportIds=1&playerPool=ALL&limit=3000"
    return mlb_pool(getter(f"{base}/sports/1/players?season={season}"), getter(f"{base}/stats?group=hitting&{q}"), getter(f"{base}/stats?group=pitching&{q}"))


# ---- NHL -------------------------------------------------------------------------------------------
def nhl_pool(skaters, blocks, goalies):
    blk = {r["playerId"]: r.get("blockedShots", 0) for r in blocks.get("data", [])}
    out = []
    for r in skaters.get("data", []):
        out.append({"src": "nhl", "ext": str(r["playerId"]), "name": r["skaterFullName"], "sport": "NHL", "position": "Skater",
                    "totals": {"Goals": r.get("goals", 0), "Assists": r.get("assists", 0), "Plus/minus": r.get("plusMinus", 0), "Shots on goal": r.get("shots", 0),
                               "Blocked shots": blk.get(r["playerId"], 0)}, "games": r.get("gamesPlayed")})
    for r in goalies.get("data", []):
        out.append({"src": "nhl", "ext": str(r["playerId"]), "name": r["goalieFullName"], "sport": "NHL", "position": "Goalie",
                    "totals": {"Saves": r.get("saves", 0), "Goals against": r.get("goalsAgainst", 0), "Wins": r.get("wins", 0), "Shutouts": r.get("shutouts", 0)}, "games": r.get("gamesPlayed")})
    return out


def fetch_nhl(season, getter=jget):
    sid = f"{season}{season + 1}"
    base = "https://api.nhle.com/stats/rest/en"
    q = f"?limit=-1&cayenneExp=seasonId={sid}%20and%20gameTypeId=2"
    return nhl_pool(getter(f"{base}/skater/summary{q}"), getter(f"{base}/skater/realtime{q}"), getter(f"{base}/goalie/summary{q}"))


# ---- NFL -------------------------------------------------------------------------------------------
def nfl_pool(week_stats_text):
    totals, names, games = {}, {}, {}
    for l in _nfl.parse_week_stats(week_stats_text, {(s, w): "x" for s in range(2000, 2100) for w in range(1, 23)}):
        k = l["ext"]
        names[k] = l["create"]
        t = totals.setdefault(k, {})
        games[k] = games.get(k, 0) + 1
        for stat, v in l["stats"].items():
            if stat != "Games played":
                t[stat] = t.get(stat, 0) + v
    return [{"src": "nfl", "ext": k, "name": names[k]["name"], "sport": "NFL", "position": names[k]["position"], "totals": t, "games": games[k]} for k, t in totals.items()]


def fetch_nfl(season, getter=get):
    return nfl_pool(getter(_nfl.STATS_URL.format(season)).decode("utf-8", "replace"))


# ---- F1 --------------------------------------------------------------------------------------------
def f1_pool(standings_doc, drivers_doc=None):
    lists = standings_doc.get("MRData", {}).get("StandingsTable", {}).get("StandingsLists", [])
    out, seen = [], set()
    for s in (lists[0]["DriverStandings"] if lists else []):
        d = s["Driver"]; seen.add(d["driverId"])
        out.append({"src": "f1", "ext": d["driverId"], "name": f"{d['givenName']} {d['familyName']}", "sport": "F1", "position": "Driver", "score": 2.0 - 0.18 * (int(s["position"]) - 1)})
    low = (min([o["score"] for o in out]) if out else 0) - 0.2
    for d in (drivers_doc or {}).get("MRData", {}).get("DriverTable", {}).get("Drivers", []):
        if d["driverId"] not in seen:
            out.append({"src": "f1", "ext": d["driverId"], "name": f"{d['givenName']} {d['familyName']}", "sport": "F1", "position": "Driver", "score": low})
    return out


def fetch_f1(season, getter=jget):
    base = "https://api.jolpi.ca/ergast/f1"
    try:
        drivers = getter(f"{base}/{season}/drivers.json?limit=100")
    except Exception:
        drivers = None
    return f1_pool(getter(f"{base}/{season - 1}/driverStandings.json"), drivers)


# ---- hand-fed (NBA, MLS) ---------------------------------------------------------------------------
def csv_pool(text):
    """Columns src,ext,name,sport,position and either score (expected weekly z) or ranking (best = 1 within its sport)."""
    rows = [r for r in csv_rows(text) if (r.get("name") or "").strip()]
    n = {}
    for r in rows:
        n[r["sport"].strip()] = n.get(r["sport"].strip(), 0) + 1
    out = []
    for r in rows:
        sp = r["sport"].strip()
        e = {"src": (r.get("src") or sp.lower()).strip(), "ext": (r.get("ext") or r["name"]).strip(), "name": r["name"].strip(), "sport": sp, "position": r["position"].strip()}
        if (r.get("score") or "").strip():
            e["score"] = num(r["score"])
        else:
            e["score"] = 2.5 - 2.5 * (num(r.get("ranking"), n[sp]) - 1) / max(n[sp], 1)
        out.append(e)
    return out


def default_season(sport, now):
    """The most recent finished (or nearly finished) season, as each source numbers it. Drafting in January 2027 -> MLB 2026, NHL 2025-26, NFL 2026, F1 2026 standings."""
    y, m = now.year, now.month
    return {"MLB": y if m >= 11 else y - 1, "NHL": y - 1 if m >= 7 else y - 2, "NFL": y if m >= 3 else y - 1, "F1": y}[sport]


FETCHERS = {"MLB": fetch_mlb, "NHL": fetch_nhl, "NFL": fetch_nfl, "F1": fetch_f1}
