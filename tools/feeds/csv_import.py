"""Hand-fed data: MLS (no free feed found), or any fix-up. Plain CSV files the commissioner or Claude can prepare.

Stats file, one row per stat:   ext,name,position,date,stat,value        (position: Forward, Midfielder, Defender, Goalkeeper)
Pool file, one row per player:  src,ext,name,sport,position,ranking
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from .common import csv_rows, line, utc_iso

ET = ZoneInfo("America/New_York")


def parse_stats(text, src="mls", sport="MLS"):
    grouped = {}
    for r in csv_rows(text):
        if not (r.get("ext") or r.get("name")) or not r.get("stat"):
            continue
        ext = (r.get("ext") or r["name"]).strip()
        key = (ext, r["date"].strip())
        g = grouped.setdefault(key, {"name": (r.get("name") or "").strip(), "position": (r.get("position") or "").strip(), "stats": {}})
        g["stats"][r["stat"].strip()] = g["stats"].get(r["stat"].strip(), 0) + float(r["value"])
    out = []
    for (ext, date), g in grouped.items():
        at = utc_iso(datetime.strptime(date, "%Y-%m-%d").replace(hour=20, tzinfo=ET))
        stats = dict(g["stats"]); stats.setdefault("Games played", 1)
        out.append(line(src, ext, f"{src}:{date}:{ext}", at, True, stats, g["name"] or None, sport, g["position"] or None))
    return out


def parse_pool(text):
    out = []
    for r in csv_rows(text):
        if not r.get("name"):
            continue
        e = {"src": r["src"].strip(), "ext": r["ext"].strip(), "name": r["name"].strip(), "sport": r["sport"].strip(), "position": r["position"].strip()}
        if (r.get("ranking") or "").strip():
            e["ranking"] = int(float(r["ranking"]))
        out.append(e)
    return out
