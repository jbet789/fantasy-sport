"""Bonus challenges: finish points and the worst-record-first draft order."""


def _place_points(cfg, place):
    pts = 0
    for start, p in sorted(cfg["bonus_bands"]):
        if place >= start:
            pts = p
    return pts


def bonus_points(cfg, status, place=None, tied=1):
    """Points for one pick. Only entrants with an official place ('Placed') score; ties share the average."""
    if status != "Placed" or place is None:
        return 0.0
    tied = max(1, tied)
    return sum(_place_points(cfg, place + i) for i in range(tied)) / tied


def bonus_draft_order(teams):
    """teams: list of dicts with name, wins, cumulative, slot (round-1 draft slot).

    Worst record picks first; ties go to lowest cumulative points, then the lower round-1 slot.
    Returns the round-1 order (list of names). Round 2 reverses it (snake).
    """
    return [t["name"] for t in sorted(teams, key=lambda t: (t["wins"], t["cumulative"], t["slot"]))]
