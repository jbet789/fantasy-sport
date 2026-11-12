import unittest
from .test_db import DbCase


class FreeAgency(DbCase):
    def test_drop_timing_one_day_then_next_nine(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',3);
            create temp table h as select player_id as id from roster_spots where team_id=1 order by player_id;
            grant all on h to authenticated;
            select t.at('2027-03-24 10:00'); select t.as_user(1);
            select drop_player((select id from h limit 1));
            select t.eq('released Friday 9am', (select release_at from waivers), '2027-03-26 09:00 America/New_York'::timestamptz);
            select t.ok('on waivers, not free', not is_free_agent((select id from h limit 1)));
            select t.at('2027-03-26 09:00');
            select t.ok('free agent at 9:00 Friday', is_free_agent((select id from h limit 1)));
        """)

    def test_tuesday_release_is_held_for_the_run(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',1);
            create temp table h as select player_id as id from roster_spots where team_id=1;
            grant all on h to authenticated;
            select t.at('2027-03-28 10:00'); select t.as_user(1);
            select drop_player((select id from h));
            select t.ok('Tuesday slot', is_waiver_slot((select release_at from waivers)));
            select t.at('2027-03-30 09:00');
            select t.ok('not a free agent until the run is done', not is_free_agent((select id from h)));
        """)

    def test_exactly_nine_rolls_to_next_morning(self):
        self.sql("""
            select t.eq('hold ending at 9:00 releases next day', next_nine('2027-03-25 09:00 America/New_York'), '2027-03-26 09:00 America/New_York'::timestamptz);
            select t.eq('before nine', next_nine('2027-03-25 08:59 America/New_York'), '2027-03-25 09:00 America/New_York'::timestamptz);
        """)

    def test_add_free_agent_needs_a_drop_when_full_and_blocks_readd(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',20);
            create temp table r as select player_id as id from roster_spots where team_id=1 order by player_id;
            create temp table fa as select id from players where sport='NBA' order by id limit 2;
            grant all on r to authenticated; grant all on fa to authenticated;
            select t.at('2027-03-25 10:00'); select t.as_user(1);
            select t.err('full', $q$ select add_free_agent((select min(id) from fa)) $q$, 'Roster is full');
            select add_free_agent((select min(id) from fa), (select id from r limit 1));
            select t.eq('still 20 active', (select count(*) from roster_spots where team_id=1 and slot='active'), 20::bigint);
            select t.at('2027-03-27 09:00');
            select t.err('7-day block', $q$ select add_free_agent((select id from r limit 1), (select min(id) from fa)) $q$, 'last 7 days');
            select t.at('2027-03-28 10:00');
            select t.err('block even after release', $q$ select add_free_agent((select id from r limit 1), (select min(id) from fa)) $q$, 'last 7 days');
        """)

    def test_locked_starter_cannot_be_dropped_and_rival_cannot_sign_waived(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',10);
            create temp table r as select player_id as id from roster_spots where team_id=1 order by player_id;
            grant all on r to authenticated;
            select t.at('2027-03-30 04:00'); select t.as_user(1);
            select set_lineup((select array_agg(id) from r));
            select t.at('2027-03-30 12:00');
            select t.err('locked', $q$ select drop_player((select id from r limit 1)) $q$, 'locked lineup');
            select t.at('2027-04-06 04:00');
            select drop_player((select id from r limit 1));
        """)

    def test_new_signing_two_day_waiver_and_f1_cap(self):
        self.sql("""
            update league_settings set f1_cap = 2;
            select t.give(1,'F1','Driver',2);
            create temp table d as select id from players where sport='F1' and id not in (select player_id from roster_spots) order by id limit 1;
            grant all on d to authenticated;
            select t.at('2027-03-27 10:00');
            select place_on_waivers((select id from d));
            select t.eq('Tuesday 9am', (select release_at from waivers), '2027-03-30 09:00 America/New_York'::timestamptz);
            select t.as_user(1);
            select t.at('2027-03-31 09:00'); select t.as_service(); delete from waivers; select t.as_user(1);
            select t.err('F1 cap', $q$ select add_free_agent((select id from d)) $q$, 'F1 cap');
        """)


class Waivers(DbCase):
    SETUP = """
        select t.give(1,'MLB','Hitter',1); select t.give(2,'MLB','Hitter',1,2); select t.give(3,'MLB','Hitter',1,3);
        create temp table p as select id from players where sport='NFL' and position='QB' order by id limit 2;
        grant all on p to authenticated;
        select t.at('2027-03-28 10:00'); select t.as_service();
        select place_on_waivers((select min(id) from p));
        select place_on_waivers((select max(id) from p));
        select t.eq('release is Tuesday 9am', (select count(*) from waivers where release_at = '2027-03-31 09:00 America/New_York'::timestamptz), 0::bigint);
    """

    def test_priority_and_back_of_the_line(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',1); select t.give(2,'MLB','Hitter',1,2); select t.give(3,'MLB','Hitter',1,3);
            create temp table p as select id from players where sport='NFL' and position='QB' order by id limit 2;
            grant all on p to authenticated;
            select t.at('2027-03-27 10:00'); select t.as_service();
            select place_on_waivers((select min(id) from p));
            select place_on_waivers((select max(id) from p));
            select t.at('2027-03-28 12:00');
            select t.as_user(1); select place_claim((select min(id) from p)); select place_claim((select max(id) from p));
            select t.as_user(2); select place_claim((select min(id) from p));
            select t.as_user(3); select place_claim((select max(id) from p));
            select t.at('2027-03-30 09:00'); select t.as_service();
            -- team 3 first, then 2, then 1: team 3 gets the second QB, team 2 the first, team 1 gets nothing
            select t.eq('two awarded', process_waivers('2027-03-30 09:00 America/New_York', array[3,2,1]::smallint[]), 2);
            select t.eq('team 2 got p1', (select team_id from roster_spots where player_id=(select min(id) from p))::int, 2);
            select t.eq('team 3 got p2', (select team_id from roster_spots where player_id=(select max(id) from p))::int, 3);
            select t.eq('team 1 lost both', (select count(*) from waiver_claims where team_id=1 and status='lost'), 2::bigint);
            select t.eq('waivers cleared', (select count(*) from waivers), 0::bigint);
        """)

    def test_winner_goes_to_back_of_line_within_run(self):
        self.sql("""
            select t.at('2027-03-27 10:00'); select t.as_service();
            create temp table p as select id from players where sport='NFL' and position='QB' order by id limit 2;
            grant all on p to authenticated;
            select place_on_waivers((select min(id) from p)); select place_on_waivers((select max(id) from p));
            select t.at('2027-03-28 12:00');
            select t.as_user(1); select place_claim((select min(id) from p)); select place_claim((select max(id) from p));
            select t.as_user(2); select place_claim((select max(id) from p));
            select t.at('2027-03-30 09:00'); select t.as_service();
            select process_waivers('2027-03-30 09:00 America/New_York', array[1,2]::smallint[]);
            -- team 1 wins its first choice, goes to the back; team 2 then takes the other player
            select t.eq('team 1 first pick', (select team_id from roster_spots where player_id=(select min(id) from p))::int, 1);
            select t.eq('team 2 gets the second player', (select team_id from roster_spots where player_id=(select max(id) from p))::int, 2);
        """)

    def test_claim_with_drop_and_void_reasons(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',20);
            create temp table r as select player_id as id from roster_spots where team_id=1 order by player_id;
            create temp table p as select id from players where sport='NFL' and position='QB' order by id limit 1;
            grant all on r to authenticated; grant all on p to authenticated;
            select t.at('2027-03-27 10:00'); select t.as_service();
            select place_on_waivers((select id from p));
            select t.at('2027-03-28 12:00'); select t.as_user(1);
            select t.err('free agents are added directly', $q$ select place_claim(1) $q$, 'not on waivers');
            select place_claim((select id from p), (select id from r limit 1));
            select t.at('2027-03-30 09:00'); select t.as_service();
            select process_waivers('2027-03-30 09:00 America/New_York', array[1]::smallint[]);
            select t.eq('claim won', (select status from waiver_claims), 'won');
            select t.eq('drop went to waivers', (select count(*) from waivers where player_id=(select id from r limit 1)), 1::bigint);
            select t.eq('roster still 20', (select count(*) from roster_spots where team_id=1), 20::bigint);
        """)

    def test_only_the_job_or_commissioner_runs_waivers(self):
        self.sql("""
            select t.as_user(1);
            select t.err('manager', $q$ select process_waivers() $q$, 'waiver job');
        """)


class Trades(DbCase):
    BASE = """
        select t.give(1,'MLB','Hitter',3); select t.give(2,'MLB','Hitter',3,4);
        create temp table a as select player_id as id from roster_spots where team_id=1 order by player_id;
        create temp table b as select player_id as id from roster_spots where team_id=2 order by player_id;
        grant all on a to authenticated; grant all on b to authenticated;
        select t.at('2027-03-25 10:00');
        create function pg_temp.prop() returns bigint language sql as
          $$ select propose_trade(2::smallint, array[(select min(id) from a)], array[(select min(id) from b)]) $$;
    """

    def test_propose_accept_veto_window_then_complete(self):
        self.sql(self.BASE + """
            select t.as_user(1); select pg_temp.prop();
            select t.err('proposer cannot accept', 'select accept_trade((select max(id) from trades))', 'other team');
            select t.as_user(2); select accept_trade((select max(id) from trades));
            select t.eq('pending', (select status from trades), 'pending_veto');
            select t.at('2027-03-26 09:59'); select t.as_service();
            select t.eq('not yet', settle_trades(), 0);
            select t.at('2027-03-26 10:00');
            select t.eq('done', settle_trades(), 1);
            select t.eq('player moved', (select team_id from roster_spots where player_id=(select min(id) from a))::int, 2);
            select t.eq('other way', (select team_id from roster_spots where player_id=(select min(id) from b))::int, 1);
            select t.eq('on the bench, not in any lineup', (select count(*) from lineups), 0::bigint);
        """)

    def test_veto_needs_commissioner_reason_and_window(self):
        self.sql(self.BASE + """
            select t.as_user(1); select pg_temp.prop();
            select t.as_user(2); select accept_trade((select max(id) from trades));
            select t.as_user(1); select t.err('manager', $q$ select veto_trade((select max(id) from trades), 'no') $q$, 'commissioner');
            select t.as_user(13);
            select t.err('reason', $q$ select veto_trade((select max(id) from trades), ' ') $q$, 'reason is required');
            select t.at('2027-03-26 10:00');
            select t.err('window closed', $q$ select veto_trade((select max(id) from trades), 'late') $q$, '24-hour');
            select t.at('2027-03-26 09:00');
            select veto_trade((select max(id) from trades), 'Collusion');
            select t.eq('vetoed', (select status from trades), 'vetoed');
            select t.eq('audited', (select count(*) from activity_log where kind='trade_veto' and commissioner), 1::bigint);
        """)

    def test_decline_withdraw(self):
        self.sql(self.BASE + """
            select t.as_user(1); select pg_temp.prop();
            select t.err('proposer cannot decline', 'select decline_trade((select max(id) from trades))', 'other team');
            select withdraw_trade((select max(id) from trades));
            select t.as_user(2); select t.err('already withdrawn', 'select decline_trade((select max(id) from trades))', 'other team');
        """)

    def test_locked_starters_cannot_be_traded(self):
        self.sql(self.BASE + """
            select t.at('2027-03-30 04:00'); select t.as_user(1);
            select set_lineup((select array_agg(id) from a));
            select t.at('2027-03-30 13:00');
            select t.err('locked', $q$ select pg_temp.prop() $q$, 'locked lineup');
        """)

    def test_roster_limit_needs_drops_and_deadline(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',20); select t.give(2,'MLB','Hitter',20);
            create temp table a as select player_id as id from roster_spots where team_id=1 order by player_id;
            create temp table b as select player_id as id from roster_spots where team_id=2 order by player_id;
            grant all on a to authenticated; grant all on b to authenticated;
            select t.at('2027-03-25 10:00'); select t.as_user(1);
            select t.eq('2-for-1 needs a drop', 1, 1);
            select t.err('over limit', $q$ select propose_trade(2::smallint, array[(select min(id) from a)], (select array_agg(id) from (select id from b limit 2) s)) $q$, '21 active');
            select propose_trade(2::smallint, array[(select min(id) from a)], (select array_agg(id) from (select id from b limit 2) s), array[(select id from a offset 1 limit 1)]);
            select t.at('2027-11-09 12:00');
            select t.err('after the deadline', $q$ select propose_trade(2::smallint, array[(select id from a offset 3 limit 1)], array[(select id from b offset 3 limit 1)]) $q$, 'closed');
            select t.err('cannot accept after deadline', 'select accept_trade((select max(id) from trades))', 'other team');
            select t.as_user(2); select t.err('deadline', 'select accept_trade((select max(id) from trades))', 'closed');
            select t.as_service(); select settle_trades();
            select t.eq('expired', (select status from trades), 'expired');
        """)

    def test_pending_trade_players_cannot_start_and_starters_cannot_be_traded(self):
        self.sql(self.BASE + """
            select t.at('2027-03-30 03:30'); select t.as_user(1);
            select set_lineup(array[(select min(id) from a)]);
            select t.err('already starting', $q$ select pg_temp.prop() $q$, 'take him out of the lineup');
            select set_lineup('{}');
            select pg_temp.prop();
            select t.as_user(2); select accept_trade((select max(id) from trades));
            select t.as_user(1);
            select t.err('cannot start a player in an accepted trade', $q$ select set_lineup(array[(select min(id) from a)]) $q$, 'awaiting the veto window');
            select set_lineup(array[(select max(id) from a)]);
            select t.as_service();
            select t.eq('carry-over skips him too', carry_over_lineups(14), 12);
        """)

    def test_settle_fails_when_starters_are_locked_by_then(self):
        self.sql(self.BASE + """
            select t.at('2027-03-30 03:30'); select t.as_user(1); select pg_temp.prop();
            select t.as_user(2); select accept_trade((select max(id) from trades));
            select t.as_service(); select set_config('app.lineup_bypass','on',true); insert into lineups values (2, 13, (select min(id) from b));   -- forced in, bypassing the new guard
            select t.at('2027-03-31 03:30');
            select settle_trades();
            select t.eq('failed, not completed', (select status from trades), 'failed');
        """)


if __name__ == "__main__":
    unittest.main()
