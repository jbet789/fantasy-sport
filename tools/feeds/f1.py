"""Formula 1 via Jolpica (Ergast-compatible). Race, qualifying and sprint results."""
from .common import jget, line, now_utc

BASE = "https://api.jolpi.ca/ergast/f1"


def _races(doc):
    return doc.get("MRData", {}).get("RaceTable", {}).get("Races", [])


def _at(race):
    return f"{race['date']}T{(race.get('time') or '14:00:00Z').rstrip('Z')}Z"


def _driver(r):
    d = r["Driver"]
    return d["driverId"], f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()


def parse_race(doc):
    out = []
    for race in _races(doc):
        rows = race.get("Results", [])
        for r in rows:
            ext, name = _driver(r)
            pos = int(r["position"]); grid = int(r.get("grid") or 0)
            grid = grid if grid > 0 else len(rows)                       # pit-lane start counts as last
            status = r.get("status", "")
            dnf = not (status == "Finished" or status.startswith("+") or status == "Lapped")
            fastest = 1 if (r.get("FastestLap") or {}).get("rank") == "1" else 0
            stats = {"Games played": 1, "Race finish position": pos, "Positions gained": grid - pos, "Did not finish": 1 if dnf else 0, "Fastest lap": fastest}
            out.append(line("f1", ext, f"f1:{race['season']}:{race['round']}:race", _at(race), True, stats, name, "F1", "Driver"))
    return out


def _session(doc, key, session, stat):
    out = []
    for race in _races(doc):
        for r in race.get(key, []):
            ext, name = _driver(r)
            out.append(line("f1", ext, f"f1:{race['season']}:{race['round']}:{session}", _at(race), True, {stat: int(r["position"])}, name, "F1", "Driver"))
    return out


def parse_qualifying(doc):
    return _session(doc, "QualifyingResults", "quali", "Qualifying position")


def parse_sprint(doc):
    return _session(doc, "SprintResults", "sprint", "Sprint finish position")


def fetch_lines(due=None, lookback_hours=None, get=jget, season=None):
    """The most recent race weekend, or a whole season with season=YYYY (paged)."""
    lines, errors = [], []
    for ext, parse in (("results", parse_race), ("qualifying", parse_qualifying), ("sprint", parse_sprint)):
        try:
            if season:
                off = 0
                while True:
                    doc = get(f"{BASE}/{season}/{ext}.json?limit=100&offset={off}")
                    lines += parse(doc); off += 100
                    if off >= int(doc["MRData"].get("total", 0)):
                        break
            else:
                lines += parse(get(f"{BASE}/current/last/{ext}.json"))
        except Exception as e:                                               # a weekend without a sprint answers with nothing or an error
            if ext != "sprint":
                errors.append(f"f1 {ext}: {e}")
    return [l for l in lines if l["at"] <= now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")], errors
