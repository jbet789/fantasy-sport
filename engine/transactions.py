"""Roster moves: free agents, waivers, trades and injured reserve (League Rulebook).

Rules implemented
- Roster: 20 active spots (10 starters + 10 bench) plus 4 IR. Every add needs a drop when the 20 active spots are full.
  Dropping an IR player does not free an active spot. No F1 driver limit (it was removed; the rule is kept switchable: F1_CAP).
- A starter in a locked lineup can't be dropped, traded or moved to IR until the lineup reopens (Tuesday 3:00 a.m.). Bench and IR players move freely.
  Anyone added by waiver, free agency or trade lands on the bench (they are never in the locked lineup).
- Dropped players go on waivers for at least 1 day, newly signed players and rookies for at least 2 days. The team that
  dropped a player can't add him back for 7 days.
- A player becomes available at the first 9:00 a.m. ET after his waiver minimum ends. If that 9:00 a.m. is a Tuesday, the
  weekly waiver run decides who gets him; any other day he is simply a free agent, first come, first served.
- Waivers are free. Priority is reverse standings (worst team first), reset every week. A team that wins a claim moves
  to the back of the line for the rest of that run.
- Trades are players only. The other team accepts, the commissioner has 24 hours to veto, then it completes.
  Acceptance must come before the trade deadline (end of week 44).
- IR needs an official injury designation, and a cleared player must leave IR within 3 days.

Reading choices confirmed with the commissioner
- The waiver minimum is a hold. The player is released at the next 9:00 a.m. ET strictly after it ends (a hold ending at
  exactly 9:00 a.m. releases the following morning). Claims only matter for players released at a Tuesday 9:00 a.m. run.
- A veto window that straddles the deadline still completes the trade; only acceptance has to beat the deadline.
All times are naive datetimes in US Eastern time.
"""
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .commissioner import week_lock

ACTIVE_LIMIT = 20
IR_LIMIT = 4
F1_CAP = 20          # the two-driver limit was removed; 20 is a full roster, so it never binds (tests set 2 to exercise the rule)
NINE_AM = time(9, 0)
WAIVER_DAY = 1                                  # Tuesday (Monday is 0)
WAIVER_DROPPED = timedelta(days=1)
WAIVER_NEW = timedelta(days=2)
READD_BLOCK = timedelta(days=7)
VETO_WINDOW = timedelta(hours=24)
IR_RETURN_WINDOW = timedelta(days=3)
TRADE_DEADLINE = week_lock(44)
# Labels to confirm against each feed once the feed probe has run.
OFFICIAL_INJURY = {"out", "ir", "il", "injured reserve", "injured list", "pup", "nfi"}


class RuleError(ValueError):
    pass


def next_nine(moment):
    """The first 9:00 a.m. strictly after `moment`."""
    nine = datetime.combine(moment.date(), NINE_AM)
    return nine if nine > moment else nine + timedelta(days=1)


def is_waiver_slot(moment):
    """A release time that falls on the weekly waiver run (Tuesday 9:00 a.m.)."""
    return moment.weekday() == WAIVER_DAY and moment.time() == NINE_AM


@dataclass
class Trade:
    id: int
    proposer: str
    partner: str
    give: list
    get: list
    drops: dict
    proposed_at: object
    status: str = "proposed"       # proposed, pending_veto, completed, vetoed, declined, withdrawn, expired, failed
    accepted_at: object = None
    veto_until: object = None
    note: str = ""


class League:
    def __init__(self, players, teams):
        """players: {player: (sport, position)}. teams: list of team ids."""
        self.players = dict(players)
        self.teams = list(teams)
        self.active = {t: set() for t in teams}
        self.ir = {t: set() for t in teams}
        self.locked_starters = {t: set() for t in teams}
        self.waivers = {}              # player -> the 9:00 a.m. he is released
        self.blocked = {}              # (team, player) -> time the re-add block ends
        self.trades = {}
        self.log = []

    # ---- lookups ----------------------------------------------------------------------------
    def owner_of(self, player):
        for t in self.teams:
            if player in self.active[t] or player in self.ir[t]:
                return t
        return None

    def is_free_agent(self, player, now):
        """Not on a roster and released from waivers. A player released at a Tuesday run stays held until the run is done."""
        if player not in self.players or self.owner_of(player) is not None:
            return False
        release = self.waivers.get(player)
        return release is None or (release <= now and not is_waiver_slot(release))

    def _f1(self, group):
        return sum(1 for p in group if self.players[p][0] == "F1")

    def _record(self, when, kind, **info):
        self.log.append({"when": when, "kind": kind, **info})

    # ---- setup (draft results, tests) ----------------------------------------------------------
    def add_to_roster(self, team, player):
        if self.owner_of(player):
            raise RuleError(f"{player} is already on a roster")
        if len(self.active[team]) >= ACTIVE_LIMIT:
            raise RuleError("roster is full")
        if self.players[player][0] == "F1" and self._f1(self.active[team] | self.ir[team]) >= F1_CAP:
            raise RuleError("F1 cap reached")
        self.waivers.pop(player, None)
        self.active[team].add(player)

    # ---- adds and drops ---------------------------------------------------------------------
    def _add_problem(self, team, player, drop, now):
        if self.blocked.get((team, player), now) > now:
            return f"{team} dropped {player} within the last 7 days"
        active, ir = set(self.active[team]), set(self.ir[team])
        if drop is not None:
            if drop not in active and drop not in ir:
                return f"{drop} is not on {team}'s roster"
            if drop in self.locked_starters[team]:
                return f"{drop} is in the locked lineup and can't be dropped until the next lock"
            active.discard(drop)
            ir.discard(drop)
        if len(active) >= ACTIVE_LIMIT:
            return "roster is full: name an active player to drop"
        if self.players[player][0] == "F1" and self._f1(active | ir) >= F1_CAP:
            return f"adding {player} would put {team} over the F1 cap of {F1_CAP}"
        return None

    def _drop(self, team, player, now):
        self.active[team].discard(player)
        self.ir[team].discard(player)
        self.locked_starters[team].discard(player)
        self.waivers[player] = next_nine(now + WAIVER_DROPPED)
        self.blocked[(team, player)] = now + READD_BLOCK
        self._record(now, "drop", team=team, player=player)

    def drop_player(self, team, player, now):
        if player not in self.active[team] and player not in self.ir[team]:
            raise RuleError(f"{player} is not on {team}'s roster")
        if player in self.locked_starters[team]:
            raise RuleError(f"{player} is in the locked lineup and can't be dropped until the next lock")
        self._drop(team, player, now)

    def place_on_waivers(self, player, now):
        """A newly signed player or rookie enters the pool through a 2-day waiver period."""
        if player not in self.players:
            raise RuleError(f"unknown player {player}")
        if self.owner_of(player):
            raise RuleError(f"{player} is on a roster")
        self.waivers[player] = next_nine(now + WAIVER_NEW)

    def add_free_agent(self, team, player, now, drop=None):
        if not self.is_free_agent(player, now):
            raise RuleError(f"{player} is not a free agent" + (f" (on waivers until {self.waivers[player]:%a %b %d} 9:00 a.m.)" if player in self.waivers else ""))
        problem = self._add_problem(team, player, drop, now)
        if problem:
            raise RuleError(problem)
        if drop is not None:
            self._drop(team, drop, now)
        self.active[team].add(player)
        self._record(now, "add", team=team, player=player, how="free agent")

    # ---- waivers ----------------------------------------------------------------------------
    def process_waivers(self, run_time, priority_order, claims):
        """priority_order: team ids, worst record first (recompute every week).
        claims: {team: [{'player', 'drop'}, ...]} each list in the team's own ranking.
        Returns one outcome per claim: {'team', 'player', 'drop', 'status': won|lost|void, 'reason'}."""
        eligible = {p for p, rel in self.waivers.items() if is_waiver_slot(rel) and rel <= run_time}
        queue = [t for t in priority_order if t in self.teams]
        pending = {t: list(c) for t, c in claims.items()}
        taken, results = {}, []

        def problem(team, claim):
            p = claim["player"]
            if p in taken:
                return "lost", f"awarded to {taken[p]}, who had priority"
            if p not in eligible:
                if self.owner_of(p) is not None:
                    return "void", "already on a roster"
                if p in self.waivers and self.waivers[p] > run_time:
                    return "void", f"not claimable at this run: released {self.waivers[p]:%a %b %d} at 9:00 a.m."
                return "void", "already a free agent: add him directly"
            msg = self._add_problem(team, p, claim.get("drop"), run_time)
            return ("void", msg) if msg else None

        while True:
            for team in queue:
                won = next((c for c in pending.get(team, []) if problem(team, c) is None), None)
                if won:
                    if won.get("drop") is not None:
                        self._drop(team, won["drop"], run_time)
                    self.active[team].add(won["player"])
                    self.waivers.pop(won["player"], None)
                    taken[won["player"]] = team
                    pending[team].remove(won)
                    queue.remove(team)
                    queue.append(team)
                    results.append({"team": team, "player": won["player"], "drop": won.get("drop"), "status": "won", "reason": ""})
                    self._record(run_time, "add", team=team, player=won["player"], how="waiver claim")
                    break
            else:
                break
        for team, rest in pending.items():
            for c in rest:
                kind, why = problem(team, c) or ("lost", "not awarded")
                results.append({"team": team, "player": c["player"], "drop": c.get("drop"), "status": kind, "reason": why})
        for p in [p for p, rel in self.waivers.items() if rel <= run_time]:   # run is done: unclaimed players are free agents
            del self.waivers[p]
        return results

    # ---- injured reserve --------------------------------------------------------------------
    def move_to_ir(self, team, player, designation):
        if player not in self.active[team]:
            raise RuleError(f"{player} is not an active player on {team}")
        if player in self.locked_starters[team]:
            raise RuleError(f"{player} is in the locked lineup; use the injury mulligan instead")
        if str(designation or "").strip().lower() not in OFFICIAL_INJURY:
            raise RuleError("IR needs an official injury designation")
        if len(self.ir[team]) >= IR_LIMIT:
            raise RuleError("all 4 IR spots are in use")
        self.active[team].discard(player)
        self.ir[team].add(player)

    def return_from_ir(self, team, player):
        if player not in self.ir[team]:
            raise RuleError(f"{player} is not on {team}'s IR")
        if len(self.active[team]) >= ACTIVE_LIMIT:
            raise RuleError("no active spot free: drop someone first")
        self.ir[team].discard(player)
        self.active[team].add(player)

    @staticmethod
    def ir_overdue(cleared_at, now):
        """A player cleared to play must be activated or dropped within 3 days."""
        return now - cleared_at > IR_RETURN_WINDOW

    # ---- trades -----------------------------------------------------------------------------
    def _trade_problem(self, t, check_deadline, now):
        if check_deadline and now >= TRADE_DEADLINE:
            return "trades closed at the end of week 44"
        if t.proposer == t.partner:
            return "a team can't trade with itself"
        if not t.give or not t.get:
            return "both sides must send at least one player"
        sides = {t.proposer: (t.give, t.get), t.partner: (t.get, t.give)}
        for team, (out, _) in sides.items():
            for p in out:
                if p not in self.active[team] and p not in self.ir[team]:
                    return f"{p} is not on {team}'s roster"
                if p in self.locked_starters[team]:
                    return f"{p} is in {team}'s locked lineup and can't be traded until the next lock"
            for p in t.drops.get(team, []):
                if p in out or (p not in self.active[team] and p not in self.ir[team]):
                    return f"{p} can't be used as a drop for {team}"
                if p in self.locked_starters[team]:
                    return f"{p} is in the locked lineup and can't be dropped"
        for team, (out, inc) in sides.items():
            gone = set(out) | set(t.drops.get(team, []))
            active = (self.active[team] - gone) | set(inc)
            ir = self.ir[team] - gone
            if len(active) > ACTIVE_LIMIT:
                return f"{team} would have {len(active)} active players; name drops to get to {ACTIVE_LIMIT}"
            if self._f1(active | ir) > F1_CAP:
                return f"{team} would be over the F1 cap of {F1_CAP}"
        return None

    def propose_trade(self, proposer, partner, give, get, now, drops=None):
        t = Trade(len(self.trades) + 1, proposer, partner, list(give), list(get), dict(drops or {}), now)
        problem = self._trade_problem(t, True, now)
        if problem:
            raise RuleError(problem)
        self.trades[t.id] = t
        return t

    def accept_trade(self, trade_id, by, now):
        t = self.trades[trade_id]
        if t.status != "proposed":
            raise RuleError(f"trade {trade_id} is {t.status}")
        if by != t.partner:
            raise RuleError("only the other team can accept")
        problem = self._trade_problem(t, True, now)
        if problem:
            raise RuleError(problem)
        t.status, t.accepted_at, t.veto_until = "pending_veto", now, now + VETO_WINDOW
        return t

    def decline_trade(self, trade_id, by, now):
        t = self.trades[trade_id]
        if t.status != "proposed" or by != t.partner:
            raise RuleError("only the other team can decline a proposed trade")
        t.status = "declined"
        return t

    def withdraw_trade(self, trade_id, by, now):
        t = self.trades[trade_id]
        if t.status != "proposed" or by != t.proposer:
            raise RuleError("only the proposer can withdraw a trade that is not yet accepted")
        t.status = "withdrawn"
        return t

    def veto_trade(self, trade_id, now, reason):
        """Commissioner only (the backend checks the role). Allowed during the 24-hour window."""
        t = self.trades[trade_id]
        if t.status != "pending_veto" or now >= t.veto_until:
            raise RuleError("a trade can only be vetoed during its 24-hour window")
        if not reason or not reason.strip():
            raise RuleError("a reason is required")
        t.status, t.note = "vetoed", reason.strip()
        self._record(now, "veto", trade=t.id, reason=t.note)
        return t

    def settle_trades(self, now):
        """Complete trades whose veto window ended and expire unaccepted proposals after the deadline."""
        done = []
        for t in self.trades.values():
            if t.status == "proposed" and now >= TRADE_DEADLINE:
                t.status = "expired"
            elif t.status == "pending_veto" and now >= t.veto_until:
                problem = self._trade_problem(t, False, now)
                if problem:
                    t.status, t.note = "failed", problem
                else:
                    self._execute(t, now)
                    done.append(t)
        return done

    def _execute(self, t, now):
        for team, out, inc in ((t.proposer, t.give, t.get), (t.partner, t.get, t.give)):
            for p in t.drops.get(team, []):
                self._drop(team, p, now)
            for p in out:
                self.active[team].discard(p)
                self.ir[team].discard(p)
        for team, inc in ((t.proposer, t.get), (t.partner, t.give)):
            for p in inc:
                self.active[team].add(p)
        t.status = "completed"
        self._record(now, "trade", trade=t.id, proposer=t.proposer, partner=t.partner, give=t.give, get=t.get)
