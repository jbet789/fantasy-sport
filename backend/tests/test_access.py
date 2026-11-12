import unittest
from .test_db import DbCase


class Access(DbCase):
    def test_clients_cannot_write_tables_directly(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',1);
            select t.as_user(1); set local role authenticated;
            select t.err('roster insert', $q$ insert into roster_spots (team_id, player_id) values (1, 999999) $q$, 'permission denied');
            select t.err('roster delete', $q$ delete from roster_spots $q$, 'permission denied');
            select t.err('lineups', $q$ insert into lineups values (1, 12, 1) $q$, 'permission denied');
            select t.err('stat lines', $q$ update stat_lines set value = 1 $q$, 'permission denied');
            select t.err('activity log', $q$ delete from activity_log $q$, 'permission denied');
            select t.err('standings', $q$ insert into matchups (week, kind, team_a, team_b) values (1,'regular',1,2) $q$, 'permission denied');
            select t.err('players', $q$ update players set injury_status = 'Out' $q$, 'permission denied');
            select t.err('commissioner flag', $q$ update profiles set is_commissioner = true $q$, 'permission denied');
            reset role;
        """)

    def test_job_functions_are_not_callable_by_clients(self):
        self.sql("""
            select t.as_user(1); set local role authenticated;
            select t.err('waiver run', $q$ select process_waivers() $q$, 'permission denied');
            select t.err('settle', $q$ select settle_trades() $q$, 'permission denied');
            select t.err('place on waivers', $q$ select place_on_waivers(1) $q$, 'permission denied');
            select t.err('carry over', $q$ select carry_over_lineups(5) $q$, 'permission denied');
            select t.err('internal pick', $q$ select record_pick(1, 1, false, now()) $q$, 'permission denied');
            select t.err('drop helper', $q$ select do_drop(1::smallint, 1, now()) $q$, 'permission denied');
            select t.err('clock with a made-up time', $q$ select run_draft_clock(1, now() + interval '9 days') $q$, 'permission denied');
            reset role;
        """)

    def test_manager_rpc_still_works_through_the_api_role(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',2);
            create temp table h as select player_id as id from roster_spots where team_id=1 order by player_id;
            grant all on h to authenticated;
            select t.at('2027-03-30 04:00'); select t.as_user(1); set local role authenticated;
            select set_lineup((select array_agg(id) from h));
            select t.eq('own lineup visible', (select count(*) from lineups), 2::bigint);
            select t.err('commissioner only', $q$ select apply_correction(1, 12, 'Runs', 1, 'x') $q$, 'commissioner');
            reset role;
        """)

    def test_lineups_are_public_at_all_times(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',2);
            create temp table h as select player_id as id from roster_spots where team_id=1 order by player_id;
            grant all on h to authenticated;
            select t.at('2027-03-30 04:00'); select t.as_user(1); set local role authenticated;
            select set_lineup((select array_agg(id) from h));
            reset role; select t.as_user(2); set local role authenticated;
            select t.eq('rival sees the new lineup while lineups are open', (select count(*) from lineups), 2::bigint);
            reset role; select t.at('2027-03-30 12:00'); set local role authenticated;
            select t.eq('and after the lock', (select count(*) from lineups), 2::bigint);
            select t.err('but cannot change it', $q$ delete from lineups $q$, 'permission denied');
            reset role;
        """)

    def test_claims_queues_and_unaccepted_trades_are_private(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',1); select t.give(2,'MLB','Hitter',1,2);
            select t.at('2027-03-25 10:00'); select t.as_service();
            insert into waivers (player_id, release_at) select id, '2027-03-30 09:00 America/New_York' from players where name='NFL QB 1';
            insert into waiver_claims (team_id, player_id, rank, run_at) select 1, id, 1, '2027-03-30 09:00 America/New_York' from players where name='NFL QB 1';
            insert into trades (proposer, partner) values (1, 2), (3, 4);
            insert into trade_items select (select max(id) from trades where proposer=3), id, 3 from players where name='MLB Hitter 40';
            select t.as_user(5); set local role authenticated;
            select t.eq('other team cannot see a claim', (select count(*) from waiver_claims), 0::bigint);
            select t.eq('uninvolved team cannot see proposals', (select count(*) from trades), 0::bigint);
            select t.eq('nor their items', (select count(*) from trade_items), 0::bigint);
            reset role; select t.as_user(1); set local role authenticated;
            select t.eq('own claim visible', (select count(*) from waiver_claims), 1::bigint);
            select t.eq('own trade visible', (select count(*) from trades), 1::bigint);
            reset role; select t.as_user(13); set local role authenticated;
            select t.eq('commissioner sees both trades', (select count(*) from trades), 2::bigint);
            reset role;
        """)

    def test_rename_only_your_own_team(self):
        self.sql("""
            select t.as_user(1); set local role authenticated;
            update teams set name = 'Mine' where true;
            select t.eq('only team 1 renamed', (select count(*) from teams where name = 'Mine'), 1::bigint);
            reset role;
        """)

    def test_draft_clock_cannot_be_pushed_by_clients(self):
        self.sql("""
            select t.at('2027-01-01 12:00:00'); select create_draft('main', 20); select start_draft((select max(id) from drafts));
            select t.as_user(3); set local role authenticated;
            select t.eq('client call uses the server clock', advance_if_due((select max(id) from drafts)), 0);
            reset role;
        """)


if __name__ == "__main__":
    unittest.main()
