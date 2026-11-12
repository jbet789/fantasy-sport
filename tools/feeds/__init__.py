"""Stat feed readers. Each module has pure parse functions (JSON or CSV text in, game lines out) and a fetch function
that does the web requests, so the parsing is tested on saved samples and the web part stays thin.

A game line is the dict ingest_stats() takes:
  {"src": "mlb", "ext": "660271", "game": "mlb:745123", "at": "2027-04-02T23:10:00Z", "final": True,
   "stats": {"Home runs": 1, "Games played": 1}, "create": {"name": ..., "sport": "MLB", "position": "Hitter"}}
A pool entry is what ingest_players() takes: {"src", "ext", "name", "sport", "position", "ranking"?}.
"""
