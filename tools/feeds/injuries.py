"""Injury designations. Each reader returns {"src", "sport", "complete": bool, "players": [{"ext", "name", "status"}]} for ingest_injuries().

Only some words make a player eligible for injured reserve (the database's is_official_injury): Out, IR, IL, Injured Reserve, Injured List, PUP, NFI.
The others (Doubtful, Questionable, Day-to-day) are stored and shown as soft tags. Suspensions and personal leave are not injuries and are dropped.
"complete" means players missing from the list are healthy (the feed lists everyone who is hurt), so their old status is cleared.
"""
from datetime import datetime, timedelta, timezone
from .common import get, jget, csv_rows, now_utc
from . import nfl as _nfl

NFL_URL = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{}.csv"
MLB = "https://statsapi.mlb.com/api/v1"
ESPN = {"NBA": "basketball/nba", "NHL": "hockey/nhl", "MLS": "soccer/usa.1"}
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/{}/injuries"


def norm_status(text):
    """Words from any source -> the league's words, or None when it is not an injury."""
    t = (text or "").strip().lower().replace("_", " ").replace("-", " ")
    if not t or "suspen" in t or "personal" in t or "bereave" in t or "paternity" in t or "not injury" in t:
        return None
    if t in ("out", "o"):
        return "Out"
    if "injured reserve" in t or t in ("ir", "res", "reserve"):
        return "Injured Reserve"
    if "injured list" in t or t.endswith(" il") or t in ("il", "d10", "d15", "d60", "d7"):
        return "Injured List"
    if "out for season" in t or "season ending" in t:
        return "Injured Reserve"
    if t in ("pup", "pup r", "reserve pup") or "physically unable" in t:
        return "PUP"
    if t in ("nfi",) or "non football injury" in t:
        return "NFI"
    if "doubtful" in t:
        return "Doubtful"
    if "questionable" in t or "game time decision" in t:
        return "Questionable"
    if "day to day" in t or "dtd" == t:
        return "Day-to-day"
    return None


# ---- NFL: nflverse weekly injury report ------------------------------------------------------------
def parse_nfl(text, now=None, max_age_days=10):
    """Latest regular-season week of the current season. Returns None when that report is stale (off-season): then nothing is changed."""
    now = now or now_utc()
    rows = [r for r in csv_rows(text) if r.get("game_type", "REG") == "REG" and r.get("week")]
    if not rows:
        return None
    week = max(int(r["week"]) for r in rows)
    cur = [r for r in rows if int(r["week"]) == week]
    stamp = max((r.get("date_modified") or "")[:10] for r in cur)
    if stamp:
        try:
            if now - datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=timezone.utc) > timedelta(days=max_age_days):
                return None
        except ValueError:
            pass
    out, seen = [], set()
    for r in cur:
        st = norm_status(r.get("report_status"))
        ext = (r.get("gsis_id") or "").strip()
        if st and ext and ext not in seen and _nfl.POS.get(r.get("position", "")):
            seen.add(ext)
            out.append({"ext": ext, "name": r.get("full_name") or "", "status": st})
    return {"src": "nfl", "sport": "NFL", "complete": True, "players": out}


def fetch_nfl(getter=get):
    return parse_nfl(getter(NFL_URL.format(_nfl.season_of(now_utc()))).decode("utf-8", "replace"))


# ---- MLB: 40-man roster status codes ---------------------------------------------------------------
def parse_mlb_roster(doc):
    out = []
    for r in doc.get("roster", []):
        code = (r.get("status") or {}).get("code", "")
        if len(code) >= 2 and code[0] == "D" and code[1:].isdigit():            # D7 D10 D15 D60 = injured lists
            p = r.get("person", {})
            out.append({"ext": str(p.get("id")), "name": p.get("fullName", ""), "status": "Injured List"})
    return out


def fetch_mlb(getter=jget):
    teams = [t["id"] for t in getter(f"{MLB}/teams?sportId=1").get("teams", [])]
    players = []
    for tid in teams:
        players += parse_mlb_roster(getter(f"{MLB}/teams/{tid}/roster?rosterType=40Man"))
    if len(teams) < 25:                                                         # a partial answer must not clear anyone
        raise RuntimeError(f"only {len(teams)} MLB teams answered")
    return {"src": "mlb", "sport": "MLB", "complete": True, "players": players}


# ---- ESPN: NBA, NHL, MLS ---------------------------------------------------------------------------
def parse_espn(doc, sport):
    out = []
    for team in doc.get("injuries", []):
        for i in team.get("injuries", []):
            a = i.get("athlete") or {}
            st = norm_status(i.get("status") or (i.get("type") or {}).get("description"))
            name = a.get("displayName") or a.get("fullName") or ""
            if st and name:
                out.append({"ext": str(a.get("id") or name), "name": name, "status": st})
    return {"src": "espn", "sport": sport, "complete": True, "players": out}


def fetch_espn(sport, getter=jget):
    return parse_espn(getter(ESPN_URL.format(ESPN[sport])), sport)


FETCHERS = {"nfl": fetch_nfl, "mlb": fetch_mlb, "nba": lambda: fetch_espn("NBA"), "nhl": lambda: fetch_espn("NHL"), "mls": lambda: fetch_espn("MLS")}
