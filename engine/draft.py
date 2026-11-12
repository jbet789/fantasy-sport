"""Snake draft rules: pick order, roster legality and autopick."""


def snake_order(order, rounds):
    """order: team ids in round-1 order. Returns [(round, pick_in_round, team)], reversing every other round."""
    picks = []
    for rnd in range(1, rounds + 1):
        teams = order if rnd % 2 == 1 else list(reversed(order))
        for i, team in enumerate(teams, start=1):
            picks.append((rnd, i, team))
    return picks


def can_draft(cfg, roster, player, rounds=20):
    """roster: list of dicts with 'sport'. Returns (ok, reason)."""
    if len(roster) >= rounds:
        return False, "Roster is full for the draft"
    if player["sport"] == "F1" and sum(1 for p in roster if p["sport"] == "F1") >= cfg["max_f1_per_team"]:
        return False, f"At most {cfg['max_f1_per_team']} F1 drivers per team"
    return True, ""


def autopick(cfg, roster, available, queue=(), rounds=20):
    """The first legal player in the team's queue, else the best-ranked legal player. available is rank-ordered."""
    by_id = {p["id"]: p for p in available}
    for pid in queue:
        p = by_id.get(pid)
        if p and can_draft(cfg, roster, p, rounds)[0]:
            return p
    for p in available:
        if can_draft(cfg, roster, p, rounds)[0]:
            return p
    return None
