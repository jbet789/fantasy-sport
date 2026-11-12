import csv, gzip, io, json, time, urllib.request
from datetime import datetime, timezone

UA = {"User-Agent": "league-stats/0.1 (private fantasy league, non-commercial)", "Accept-Encoding": "gzip"}
PAUSE = 0.4          # seconds between requests: polite to free, unofficial services


def get(url, tries=3, timeout=30):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            time.sleep(PAUSE)
            return raw
        except Exception as e:                      # retry transient failures, then tell the caller
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def jget(url, **kw):
    return json.loads(get(url, **kw))


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text if isinstance(text, str) else text.decode("utf-8", "replace"))))


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def innings(s):
    """Baseball notation: '6.1' is six and one third innings."""
    try:
        whole, _, third = str(s).partition(".")
        return int(whole or 0) + (int(third or 0) % 3) / 3
    except ValueError:
        return 0.0


def minutes(s):
    """'18:33' -> 18.55; 'PT32M10.00S' -> 32.17; blank -> 0."""
    s = str(s or "")
    try:
        if s.startswith("PT"):
            m, _, rest = s[2:].partition("M")
            return float(m or 0) + float(rest.rstrip("S") or 0) / 60
        a, _, b = s.partition(":")
        return float(a or 0) + float(b or 0) / 60
    except ValueError:
        return 0.0


def clean(stats):
    """Drop zeros and round, except 'Games played'."""
    return {k: round(v, 4) for k, v in stats.items() if v or k == "Games played"}


def line(src, ext, game, at, final, stats, name=None, sport=None, position=None):
    out = {"src": src, "ext": str(ext), "game": game, "at": at, "final": bool(final), "stats": clean(stats)}
    if name and sport and position:
        out["create"] = {"name": name, "sport": sport, "position": position}
    return out


def utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    return datetime.now(timezone.utc)
