"""Commissioner tools: audited stat corrections and bonus-challenge result entry.

Rules implemented (League Rulebook):
- A week's results are final 24 hours after the week ends, i.e. Wednesday noon ET. Corrections after that are refused.
- Bonus points count in the scoring week in which the event finishes. Held events (the Iditarod) release nothing until
  the commissioner marks the race concluded; then all points are scored together in the week it concluded.
- Entrants who withdraw, miss the cut, do not start or are not classified score 0. Ties share the average of the
  places they cover. A pick that scores 0 is never replaced.
All times are naive datetimes in US Eastern time.
"""
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from .bonus import bonus_points
from .schedule import SEASON_START, week_end

LOCK_TIME = time(12, 0)
UNLOCK_TIME = time(3, 0)       # Tuesday 3:00 a.m.: lineups reopen for the next week (9 hours before the noon lock)
FINAL_AFTER = timedelta(hours=24)
NEGATIVE_OK = {"Plus/minus", "Positions gained"}
F1_STATS = {"Race finish position", "Qualifying position", "Sprint finish position"}


class WeekFinal(ValueError):
    """The week's results are already final."""


class BadCorrection(ValueError):
    pass


class BadResults(ValueError):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


# ---- calendar helpers -------------------------------------------------------------------------

def week_lock(week):
    """Tuesday noon ET that ends scoring week `week`."""
    return datetime.combine(week_end(week), LOCK_TIME)


def week_of(moment):
    """Scoring week containing `moment` (week n runs Tuesday noon to the next Tuesday noon)."""
    start = datetime.combine(SEASON_START, LOCK_TIME)
    if moment < start:
        raise ValueError("before the season starts")
    return int((moment - start) // timedelta(days=7)) + 1


def lineup_locked(moment):
    """Lineups are locked from Tuesday noon until the next Tuesday 3:00 a.m.; they are open Tuesday 3:00 a.m. to noon."""
    open_window = moment.weekday() == 1 and UNLOCK_TIME <= moment.time() < LOCK_TIME
    return not open_window


def final_at(week):
    return week_lock(week) + FINAL_AFTER


def is_final(week, now):
    return now >= final_at(week)


# ---- stat corrections -------------------------------------------------------------------------

class StatBook:
    """Weekly stat values with an append-only correction log.

    values: {(player, week, stat): number}. players: {player: (sport, position)}.
    """

    def __init__(self, cfg, players, values=None):
        self.cfg = cfg
        self.players = dict(players)
        self.values = dict(values or {})
        self.log = []

    def _check_stat(self, player, stat):
        from .scoring import profile_for
        if player not in self.players:
            raise BadCorrection(f"unknown player {player!r}")
        sport, pos = self.players[player]
        if stat == "Games played" or (sport == "F1" and stat in F1_STATS):
            return
        if f"{profile_for(sport, pos)}|{stat}" not in self.cfg["weights"]:
            raise BadCorrection(f"{stat!r} is not a scored stat for {sport} {pos}")

    def correct(self, player, week, stat, new_value, reason, by, now):
        """Change one value. Returns the log entry; the caller must recompute that week's baselines and scores."""
        if is_final(week, now):
            raise WeekFinal(f"week {week} became final {final_at(week):%a %b %d %I:%M %p} ET; corrections are ignored")
        self._check_stat(player, stat)
        if not reason or not reason.strip():
            raise BadCorrection("a reason is required")
        if new_value < 0 and stat not in NEGATIVE_OK:
            raise BadCorrection(f"{stat!r} cannot be negative")
        key = (player, week, stat)
        entry = {"id": len(self.log) + 1, "when": now, "by": by, "player": player, "week": week, "stat": stat,
                 "old": self.values.get(key), "new": new_value, "reason": reason.strip(), "reverts": None,
                 "recompute_week": week}
        self.values[key] = new_value
        self.log.append(entry)
        return entry

    def revert(self, entry_id, by, now):
        """Undo a correction by appending a new entry that restores the old value (the log is never edited)."""
        orig = next((e for e in self.log if e["id"] == entry_id), None)
        if orig is None:
            raise BadCorrection(f"no correction {entry_id}")
        if any(e["reverts"] == entry_id for e in self.log):
            raise BadCorrection(f"correction {entry_id} was already reverted")
        if is_final(orig["week"], now):
            raise WeekFinal(f"week {orig['week']} is final")
        key = (orig["player"], orig["week"], orig["stat"])
        if orig["old"] is None:
            self.values.pop(key, None)
        else:
            self.values[key] = orig["old"]
        entry = {"id": len(self.log) + 1, "when": now, "by": by, "player": orig["player"], "week": orig["week"],
                 "stat": orig["stat"], "old": orig["new"], "new": orig["old"], "reason": f"Reverted correction {entry_id}",
                 "reverts": entry_id, "recompute_week": orig["week"]}
        self.log.append(entry)
        return entry


# ---- bonus challenge results ------------------------------------------------------------------

def validate_results(cfg, entries):
    """entries: {participant: (status, place)}. Returns a list of problems (empty if fine).

    Placed entrants need a positive whole place. Tied entrants share a place, and the next place skips the tie:
    three entrants at 2nd means the next one is 5th.
    """
    problems = []
    zero = set(cfg["bonus_statuses_scoring_zero"])
    placed = {}
    for who, (status, place) in entries.items():
        if status == "Placed":
            if not isinstance(place, int) or place < 1:
                problems.append(f"{who}: placed entrants need a place of 1 or more")
            else:
                placed[who] = place
        elif status not in zero:
            problems.append(f"{who}: unknown status {status!r}")
        elif place is not None:
            problems.append(f"{who}: {status} entrants have no place")
    counts = Counter(placed.values())
    expected = 1
    for p in sorted(counts):
        if p != expected:
            problems.append(f"places jump from {expected} to {p}" if p > expected else f"place {p} overlaps an earlier tie")
        expected = p + counts[p]
    return problems


class BonusEvent:
    def __init__(self, cfg, name, ends, picks, hold_until_concluded=False):
        """ends: when the event finishes (ET). picks: {team: [participants]} from the bonus draft."""
        self.cfg, self.name, self.ends = cfg, name, ends
        self.picks = {t: list(p) for t, p in picks.items()}
        self.hold = hold_until_concluded
        self.entries = {}
        self.concluded_at = None if hold_until_concluded else ends

    def enter_results(self, entries):
        problems = validate_results(self.cfg, entries)
        if problems:
            raise BadResults(problems)
        self.entries = dict(entries)

    def conclude(self, when):
        """Commissioner marks a held event (the Iditarod) as finished; points release together."""
        if not self.hold:
            raise ValueError(f"{self.name} is not a held event")
        self.concluded_at = when

    @property
    def released(self):
        return bool(self.entries) and self.concluded_at is not None

    def participant_points(self):
        if not self.released:
            return {}
        ties = Counter(pl for st, pl in self.entries.values() if st == "Placed")
        return {who: bonus_points(self.cfg, st, pl, ties[pl] if st == "Placed" else 1)
                for who, (st, pl) in self.entries.items()}

    def team_points(self):
        """{team: points}. A pick not in the results (never entered) scores 0. Empty while held."""
        if not self.released:
            return {}
        pts = self.participant_points()
        return {t: sum(pts.get(p, 0.0) for p in picks) for t, picks in self.picks.items()}

    @property
    def scoring_week(self):
        return week_of(self.concluded_at) if self.concluded_at else None

    def status(self):
        if self.hold and self.concluded_at is None:
            return "Held until the race concludes"
        return "Scored" if self.entries else "Waiting for results"


def bonus_by_week(events):
    """Fold released events into {week: {team: points}}; the matchup sheet adds these to each team's weekly total."""
    out = defaultdict(lambda: defaultdict(float))
    for e in events:
        if e.released:
            for t, p in e.team_points().items():
                out[e.scoring_week][t] += p
    return {w: dict(t) for w, t in out.items()}
