import unittest
from datetime import datetime, timedelta
from engine import transactions as T
from engine.transactions import League, RuleError

TUE_RUN = datetime(2027, 3, 9, 9, 0)           # a Tuesday, waiver run time
MON = datetime(2027, 3, 8, 10, 0)


def make(n_per_team=20, teams=("A", "B", "C")):
    players = {}
    for t in teams:
        for i in range(n_per_team):
            players[f"{t}{i}"] = ("NBA", "Wing")
    for i in range(30):
        players[f"fa{i}"] = ("NFL", "WR")
    for i in range(6):
        players[f"f1_{i}"] = ("F1", "Driver")
    lg = League(players, list(teams))
    for t in teams:
        for i in range(n_per_team):
            lg.add_to_roster(t, f"{t}{i}")
    return lg


class Drops(unittest.TestCase):
    def test_drop_goes_on_waivers_and_blocks_the_dropper(self):
        lg = make()
        lg.drop_player("A", "A0", MON)
        self.assertEqual(lg.waivers["A0"], datetime(2027, 3, 10, 9, 0))    # minimum ends Tue 10:00, released Wed 9:00
        self.assertFalse(lg.is_free_agent("A0", datetime(2027, 3, 10, 8, 59)))
        self.assertTrue(lg.is_free_agent("A0", datetime(2027, 3, 10, 9, 0)))
        lg.waivers["A0"] = MON                          # pretend the period ended and it cleared
        del lg.waivers["A0"]
        with self.assertRaises(RuleError):
            lg.add_free_agent("A", "A0", MON + timedelta(days=3))
        lg.add_free_agent("B", "A0", MON + timedelta(days=3), drop="B0")      # other teams may
        self.assertEqual(lg.owner_of("A0"), "B")

    def test_block_lasts_seven_days(self):
        lg = make()
        lg.drop_player("A", "A0", MON)
        del lg.waivers["A0"]
        lg.add_free_agent("A", "A0", MON + timedelta(days=7))

    def test_locked_starter_cannot_be_dropped(self):
        lg = make()
        lg.locked_starters["A"].add("A0")
        with self.assertRaises(RuleError):
            lg.drop_player("A", "A0", MON)
        lg.drop_player("A", "A1", MON)                 # bench players move freely


class FreeAgents(unittest.TestCase):
    def test_full_roster_needs_a_drop(self):
        lg = make()
        with self.assertRaises(RuleError):
            lg.add_free_agent("A", "fa0", MON)
        lg.add_free_agent("A", "fa0", MON, drop="A0")
        self.assertEqual(len(lg.active["A"]), 20)
        self.assertIn("A0", lg.waivers)

    def test_open_active_spot_needs_no_drop(self):
        lg = make()
        lg.move_to_ir("A", "A0", "Out")
        lg.add_free_agent("A", "fa0", MON)
        self.assertEqual((len(lg.active["A"]), len(lg.ir["A"])), (20, 1))

    def test_dropping_an_ir_player_does_not_free_an_active_spot(self):
        lg = make()
        lg.move_to_ir("A", "A0", "Out")
        lg.add_free_agent("A", "fa0", MON)             # fills the spot IR freed
        with self.assertRaises(RuleError):
            lg.add_free_agent("A", "fa1", MON, drop="A0")

    def test_player_on_waivers_is_not_a_free_agent(self):
        lg = make()
        lg.place_on_waivers("fa0", MON)
        with self.assertRaises(RuleError):
            lg.add_free_agent("A", "fa0", MON, drop="A0")
        self.assertEqual(lg.waivers["fa0"], datetime(2027, 3, 11, 9, 0))   # minimum ends Wed 10:00, released Thu 9:00

    def test_f1_cap_counts_ir(self):
        T.F1_CAP = 2; self.addCleanup(setattr, T, 'F1_CAP', 20)         # the league has no limit now; the rule still works when set
        lg = make(n_per_team=18)
        lg.add_free_agent("A", "f1_0", MON)
        lg.add_free_agent("A", "f1_1", MON)
        lg.move_to_ir("A", "f1_1", "IR")
        with self.assertRaises(RuleError):
            lg.add_free_agent("A", "f1_2", MON)
        lg.add_free_agent("B", "f1_2", MON)

    def test_locked_starter_cannot_be_the_drop(self):
        lg = make()
        lg.locked_starters["A"].add("A0")
        with self.assertRaises(RuleError):
            lg.add_free_agent("A", "fa0", MON, drop="A0")


class Waivers(unittest.TestCase):
    def setUp(self):
        self.lg = make()
        for p in ("fa0", "fa1", "fa2"):
            self.lg.waivers[p] = TUE_RUN                  # released at this Tuesday's run

    def claim(self, player, drop):
        return {"player": player, "drop": drop}

    def test_worst_team_gets_the_player(self):
        res = self.lg.process_waivers(TUE_RUN, ["C", "B", "A"],
                                      {"A": [self.claim("fa0", "A0")], "C": [self.claim("fa0", "C0")]})
        won = [r for r in res if r["status"] == "won"]
        self.assertEqual([(r["team"], r["player"]) for r in won], [("C", "fa0")])
        lost = next(r for r in res if r["team"] == "A")
        self.assertEqual(lost["status"], "lost")
        self.assertEqual(self.lg.owner_of("fa0"), "C")

    def test_winner_goes_to_back_of_line(self):
        # C is first in line; after winning fa0 it falls behind B, so B beats C for fa1
        res = self.lg.process_waivers(TUE_RUN, ["C", "B", "A"], {
            "C": [self.claim("fa0", "C0"), self.claim("fa1", "C1")],
            "B": [self.claim("fa1", "B0")]})
        owners = {r["player"]: r["team"] for r in res if r["status"] == "won"}
        self.assertEqual(owners, {"fa0": "C", "fa1": "B"})

    def test_ranking_inside_a_team_is_respected_and_fallback_used(self):
        res = self.lg.process_waivers(TUE_RUN, ["A", "B", "C"], {
            "B": [self.claim("fa0", "B0"), self.claim("fa1", "B1")],
            "A": [self.claim("fa0", "A0")]})
        owners = {r["player"]: r["team"] for r in res if r["status"] == "won"}
        self.assertEqual(owners, {"fa0": "A", "fa1": "B"})      # B's first choice went to A, so its second comes through

    def test_invalid_claims_are_void_with_a_reason(self):
        self.lg.locked_starters["A"].add("A0")
        res = self.lg.process_waivers(TUE_RUN, ["A", "B", "C"], {
            "A": [self.claim("fa0", "A0")],            # locked starter as the drop
            "B": [self.claim("fa9", "B0")]})           # not on waivers
        by_team = {r["team"]: r for r in res}
        self.assertEqual(by_team["A"]["status"], "void")
        self.assertIn("locked lineup", by_team["A"]["reason"])
        self.assertEqual(by_team["B"]["status"], "void")
        self.assertEqual(self.lg.owner_of("fa0"), None)

    def test_players_are_held_until_the_run_then_unclaimed_ones_are_free_agents(self):
        self.assertFalse(self.lg.is_free_agent("fa1", TUE_RUN))                      # 9:00 reached, run not done yet
        self.assertFalse(self.lg.is_free_agent("fa1", TUE_RUN + timedelta(minutes=5)))
        with self.assertRaises(RuleError):
            self.lg.add_free_agent("B", "fa1", TUE_RUN + timedelta(minutes=5), drop="B0")
        self.lg.process_waivers(TUE_RUN + timedelta(minutes=5), ["A", "B", "C"], {"A": [self.claim("fa0", "A0")]})
        self.assertTrue(self.lg.is_free_agent("fa1", TUE_RUN + timedelta(minutes=6)))   # now first come, first served
        self.lg.add_free_agent("B", "fa1", TUE_RUN + timedelta(minutes=6), drop="B0")

    def test_player_whose_hold_ended_before_the_run_is_already_a_free_agent(self):
        self.lg.drop_player("B", "B5", MON - timedelta(days=3))                      # dropped Fri 10:00, released Sun 9:00
        res = self.lg.process_waivers(TUE_RUN, ["A", "B", "C"], {"A": [self.claim("B5", "A0")]})
        self.assertEqual(res[0]["status"], "void")
        self.assertIn("free agent", res[0]["reason"])
        self.lg.add_free_agent("A", "B5", TUE_RUN, drop="A0")

    def test_player_released_at_the_run_is_claimable(self):
        self.lg.drop_player("B", "B5", MON - timedelta(days=1))                      # dropped Sun 10:00, minimum ends Mon 10:00, released Tue 9:00
        res = self.lg.process_waivers(TUE_RUN, ["A", "B", "C"], {"A": [self.claim("B5", "A0")]})
        self.assertEqual(res[0]["status"], "won")

    def test_player_released_after_the_run_cannot_be_claimed(self):
        self.lg.drop_player("B", "B5", MON)                                          # released Wednesday 9:00
        res = self.lg.process_waivers(TUE_RUN, ["A", "B", "C"], {"A": [self.claim("B5", "A0")]})
        self.assertEqual(res[0]["status"], "void")
        self.assertIn("not claimable", res[0]["reason"])
        self.assertTrue(self.lg.is_free_agent("B5", datetime(2027, 3, 10, 9, 0)))

    def test_next_nine_is_strictly_after(self):
        self.assertEqual(T.next_nine(datetime(2027, 3, 9, 8, 59)), datetime(2027, 3, 9, 9, 0))
        self.assertEqual(T.next_nine(datetime(2027, 3, 9, 9, 0)), datetime(2027, 3, 10, 9, 0))
        self.assertEqual(T.next_nine(datetime(2027, 3, 9, 23, 0)), datetime(2027, 3, 10, 9, 0))

    def test_dropper_cannot_claim_back_within_seven_days(self):
        self.lg.drop_player("B", "B5", MON - timedelta(days=1))
        res = self.lg.process_waivers(TUE_RUN, ["B", "A", "C"], {"B": [self.claim("B5", "B6")]})
        self.assertEqual(res[0]["status"], "void")
        self.assertIn("7 days", res[0]["reason"])

    def test_priority_is_reset_by_the_caller_each_week(self):
        for p in ("fa3", "fa4"):
            self.lg.waivers[p] = TUE_RUN
        week1 = self.lg.process_waivers(TUE_RUN, ["A", "B", "C"], {"A": [self.claim("fa0", "A0")], "B": [self.claim("fa0", "B0")]})
        self.assertEqual(next(r for r in week1 if r["status"] == "won")["team"], "A")
        nxt = TUE_RUN + timedelta(days=7)
        for p in ("fa3", "fa4"):
            self.lg.waivers[p] = nxt
        week2 = self.lg.process_waivers(nxt, ["B", "C", "A"], {"A": [self.claim("fa3", "A1")], "B": [self.claim("fa3", "B1")]})
        self.assertEqual(next(r for r in week2 if r["status"] == "won")["team"], "B")   # A won last week but order reset

    def test_dropped_player_goes_to_waivers_during_the_run(self):
        self.lg.process_waivers(TUE_RUN, ["A", "B", "C"], {"A": [self.claim("fa0", "A0")]})
        self.assertEqual(self.lg.waivers["A0"], datetime(2027, 3, 11, 9, 0))   # dropped Tue 9:00, minimum ends Wed 9:00, released Thu 9:00


class IR(unittest.TestCase):
    def test_rules(self):
        lg = make()
        with self.assertRaises(RuleError):
            lg.move_to_ir("A", "A0", "Questionable")
        with self.assertRaises(RuleError):
            lg.move_to_ir("A", "A0", None)
        for i in range(4):
            lg.move_to_ir("A", f"A{i}", "Injured List")
        with self.assertRaises(RuleError):
            lg.move_to_ir("A", "A4", "Out")
        lg.locked_starters["B"].add("B0")
        with self.assertRaises(RuleError):
            lg.move_to_ir("B", "B0", "Out")

    def test_return_needs_an_active_spot_and_three_day_window(self):
        lg = make()
        lg.move_to_ir("A", "A0", "Out")
        lg.add_free_agent("A", "fa0", MON)
        with self.assertRaises(RuleError):
            lg.return_from_ir("A", "A0")
        lg.drop_player("A", "A1", MON)
        lg.return_from_ir("A", "A0")
        cleared = datetime(2027, 3, 1, 12, 0)
        self.assertFalse(League.ir_overdue(cleared, cleared + timedelta(days=3)))
        self.assertTrue(League.ir_overdue(cleared, cleared + timedelta(days=3, minutes=1)))


class Trades(unittest.TestCase):
    def setUp(self):
        self.lg = make()
        self.t0 = datetime(2027, 3, 8, 12, 0)

    def test_full_flow_with_veto_window(self):
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0"], self.t0)
        self.lg.accept_trade(t.id, "B", self.t0)
        self.assertEqual(t.veto_until, self.t0 + timedelta(hours=24))
        self.assertEqual(self.lg.settle_trades(self.t0 + timedelta(hours=23)), [])
        self.assertEqual(self.lg.owner_of("A0"), "A")
        done = self.lg.settle_trades(self.t0 + timedelta(hours=24))
        self.assertEqual([x.id for x in done], [t.id])
        self.assertEqual((self.lg.owner_of("A0"), self.lg.owner_of("B0")), ("B", "A"))

    def test_veto_in_window_only_and_needs_reason(self):
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0"], self.t0)
        self.lg.accept_trade(t.id, "B", self.t0)
        with self.assertRaises(RuleError):
            self.lg.veto_trade(t.id, self.t0 + timedelta(hours=1), "  ")
        self.lg.veto_trade(t.id, self.t0 + timedelta(hours=1), "Looks like collusion")
        self.assertEqual(t.status, "vetoed")
        self.lg.settle_trades(self.t0 + timedelta(days=2))
        self.assertEqual(self.lg.owner_of("A0"), "A")
        t2 = self.lg.propose_trade("A", "B", ["A1"], ["B1"], self.t0)
        self.lg.accept_trade(t2.id, "B", self.t0)
        with self.assertRaises(RuleError):
            self.lg.veto_trade(t2.id, self.t0 + timedelta(hours=24), "too late")

    def test_only_partner_accepts(self):
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0"], self.t0)
        with self.assertRaises(RuleError):
            self.lg.accept_trade(t.id, "A", self.t0)
        with self.assertRaises(RuleError):
            self.lg.accept_trade(t.id, "C", self.t0)

    def test_deadline(self):
        late = T.TRADE_DEADLINE
        with self.assertRaises(RuleError):
            self.lg.propose_trade("A", "B", ["A0"], ["B0"], late)
        just_before = late - timedelta(minutes=1)
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0"], just_before)
        self.lg.accept_trade(t.id, "B", just_before)
        self.assertEqual(len(self.lg.settle_trades(late + timedelta(hours=24))), 1)   # veto window straddles the deadline

    def test_unaccepted_proposals_expire_at_the_deadline(self):
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0"], self.t0)
        self.lg.settle_trades(T.TRADE_DEADLINE)
        self.assertEqual(t.status, "expired")

    def test_locked_starters_cannot_be_traded(self):
        self.lg.locked_starters["A"].add("A0")
        with self.assertRaises(RuleError):
            self.lg.propose_trade("A", "B", ["A0"], ["B0"], self.t0)
        self.lg.locked_starters["B"].add("B0")
        with self.assertRaises(RuleError):
            self.lg.propose_trade("A", "B", ["A1"], ["B0"], self.t0)

    def test_lineup_lock_before_completion_fails_the_trade(self):
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0"], self.t0)
        self.lg.accept_trade(t.id, "B", self.t0)
        self.lg.locked_starters["A"].add("A0")          # new lineup locked during the veto window
        self.lg.settle_trades(self.t0 + timedelta(days=1))
        self.assertEqual(t.status, "failed")
        self.assertEqual(self.lg.owner_of("A0"), "A")

    def test_uneven_trade_needs_drops(self):
        with self.assertRaises(RuleError):
            self.lg.propose_trade("A", "B", ["A0"], ["B0", "B1"], self.t0)
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0", "B1"], self.t0, drops={"A": ["A1"]})
        self.lg.accept_trade(t.id, "B", self.t0)
        self.lg.settle_trades(self.t0 + timedelta(days=1))
        self.assertEqual(t.status, "completed")
        self.assertEqual(len(self.lg.active["A"]), 20)
        self.assertEqual(len(self.lg.active["B"]), 19)
        self.assertIn("A1", self.lg.waivers)

    def test_f1_cap_applies_to_trades(self):
        T.F1_CAP = 2; self.addCleanup(setattr, T, 'F1_CAP', 20)
        lg = make(n_per_team=17)
        for p in ("f1_0", "f1_1"):
            lg.add_free_agent("A", p, self.t0)
        lg.add_free_agent("B", "f1_2", self.t0)
        with self.assertRaises(RuleError):
            lg.propose_trade("A", "B", ["A0"], ["f1_2"], self.t0)
        lg.propose_trade("A", "B", ["f1_0"], ["f1_2"], self.t0)       # driver for driver is fine

    def test_traded_players_are_not_in_the_locked_lineup(self):
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0"], self.t0)
        self.lg.accept_trade(t.id, "B", self.t0)
        self.lg.settle_trades(self.t0 + timedelta(days=1))
        self.assertNotIn("B0", self.lg.locked_starters["A"])           # sits on the bench until the next lock

    def test_decline_and_withdraw(self):
        t = self.lg.propose_trade("A", "B", ["A0"], ["B0"], self.t0)
        self.lg.decline_trade(t.id, "B", self.t0)
        self.assertEqual(t.status, "declined")
        t2 = self.lg.propose_trade("A", "B", ["A1"], ["B1"], self.t0)
        with self.assertRaises(RuleError):
            self.lg.withdraw_trade(t2.id, "B", self.t0)
        self.lg.withdraw_trade(t2.id, "A", self.t0)
        self.assertEqual(t2.status, "withdrawn")

    def test_deadline_matches_rulebook(self):
        self.assertEqual(T.TRADE_DEADLINE, datetime(2027, 11, 9, 12, 0))


if __name__ == "__main__":
    unittest.main()
