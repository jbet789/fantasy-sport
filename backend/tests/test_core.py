import unittest
from .test_db import DbCase


class Rosters(DbCase):
    def test_active_limit_is_20(self):
        self.sql("""
            select t.give(1,'NFL','RB',20);
            select t.err('21st player', $q$ insert into roster_spots (team_id, player_id) values (1, (select id from players where sport='NFL' and position='QB' limit 1)) $q$, 'Roster is full');
            -- another team is unaffected
            select t.give(2,'NFL','QB',1);
        """)

    def test_a_player_can_be_on_one_roster_only(self):
        self.sql("""
            select t.give(1,'NFL','RB',1);
            select t.err('same player twice', $q$ insert into roster_spots (team_id, player_id) select 2, player_id from roster_spots limit 1 $q$, 'duplicate key');
        """)

    def test_no_f1_cap_by_default(self):
        self.sql("""
            select t.give(1,'F1','Driver',5);
            select t.eq('five drivers fine', (select count(*) from roster_spots r join players p on p.id = r.player_id where r.team_id = 1 and p.sport = 'F1'), 5::bigint);
        """)

    def test_f1_cap(self):
        self.sql("""
            update league_settings set f1_cap = 2;
            select t.give(1,'F1','Driver',2);
            select t.err('third F1', $q$ insert into roster_spots (team_id, player_id) select 1, id from players where sport='F1' and id not in (select player_id from roster_spots) limit 1 $q$, 'F1 cap');
        """)

    def test_f1_cap_counts_ir_players(self):
        self.sql("""
            update league_settings set f1_cap = 2;
            select t.give(1,'F1','Driver',2);
            update players set injury_status='Out' where id in (select player_id from roster_spots where team_id=1);
            update roster_spots set slot='ir' where team_id=1;
            select t.err('F1 on IR still counts', $q$ insert into roster_spots (team_id, player_id) select 1, id from players where sport='F1' and id not in (select player_id from roster_spots) limit 1 $q$, 'F1 cap');
        """)

    def test_ir_needs_official_designation_and_has_4_spots(self):
        self.sql("""
            select t.give(1,'NHL','Skater',6);
            select t.err('healthy to IR', $q$ update roster_spots set slot='ir' where player_id = (select min(player_id) from roster_spots where team_id=1) $q$, 'official');
            update players set injury_status='Questionable' where id in (select player_id from roster_spots where team_id=1);
            select t.err('questionable is not enough', $q$ update roster_spots set slot='ir' where player_id = (select min(player_id) from roster_spots where team_id=1) $q$, 'official');
            update players set injury_status='Out' where id in (select player_id from roster_spots where team_id=1);
            update roster_spots set slot='ir' where player_id in (select player_id from roster_spots where team_id=1 order by player_id limit 4);
            select t.err('5th IR', $q$ update roster_spots set slot='ir' where player_id = (select max(player_id) from roster_spots where team_id=1) $q$, 'Injured reserve is full');
            select t.eq('ir count', (select count(*) from roster_spots where team_id=1 and slot='ir'), 4::bigint);
        """)

    def test_ir_player_cleared_starts_three_day_clock(self):
        self.sql("""
            select t.give(1,'NHL','Skater',1);
            update players set injury_status='Out' where id in (select player_id from roster_spots where team_id=1);
            update roster_spots set slot='ir' where team_id=1;
            select t.at('2027-03-24 10:00');
            update players set injury_status=null where id in (select player_id from roster_spots where team_id=1);
            select t.ok('clock started', (select ir_cleared_at from roster_spots where team_id=1) is not null);
            select t.eq('not overdue yet', (select count(*) from ir_overdue), 0::bigint);
            select t.at('2027-03-27 10:01');
            select t.eq('overdue after 3 days', (select count(*) from ir_overdue), 1::bigint);
            -- a new designation stops the clock
            update players set injury_status='Out' where id in (select player_id from roster_spots where team_id=1);
            select t.eq('clock cleared', (select count(*) from ir_overdue), 0::bigint);
        """)


class Lineups(DbCase):
    SETUP = """
        select t.give(1,'NFL','RB',10);
        select t.give(1,'NBA','Guard',10);
        create temp table mine as select player_id as id from roster_spots where team_id=1 order by player_id;
        grant all on mine to authenticated;
        create function pg_temp.starters(n int) returns bigint[] language sql as $f$ select array_agg(id) from (select id from mine order by id limit n) s $f$;
    """

    def test_open_window_allows_a_lineup_and_other_times_do_not(self):
        self.sql(self.SETUP + """
            select t.as_user(1);
            select t.at('2027-03-30 03:00');
            select set_lineup(pg_temp.starters(10));
            select t.eq('10 starters for week 13', (select count(*) from lineups where team_id=1 and week=13), 10::bigint);
            select t.at('2027-03-30 12:00');
            select t.err('locked at noon', 'select set_lineup(pg_temp.starters(9))', 'locked');
            select t.at('2027-03-30 02:59');
            select t.err('not yet open', 'select set_lineup(pg_temp.starters(9))', 'locked');
            select t.at('2027-03-28 10:00');
            select t.err('locked Sunday', 'select set_lineup(pg_temp.starters(9))', 'locked');
            select t.at('2027-03-30 11:59');
            select set_lineup(pg_temp.starters(9));
            select t.eq('replaced, not added to', (select count(*) from lineups where team_id=1 and week=13), 9::bigint);
        """)

    def test_at_most_10_starters_and_only_active_roster_players(self):
        self.sql(self.SETUP + """
            select t.as_user(1);
            select t.at('2027-03-30 08:00');
            select t.err('11 starters', 'select set_lineup(pg_temp.starters(11))', 'at most');
            select t.err('someone elses player', $q$ select set_lineup(array[(select player_id from roster_spots where team_id<>1 limit 1)]) $q$, 'on your roster');
            select t.give(2,'NFL','QB',1);
            select t.err('not on roster', $q$ select set_lineup(array[(select player_id from roster_spots where team_id=2 limit 1)]) $q$, 'Only active players');
        """)

    def test_first_week_lineup_opens_after_the_draft(self):
        self.sql(self.SETUP + """
            select t.as_user(1);
            select t.at('2026-12-31 20:00');
            select t.err('before the draft', 'select set_lineup(pg_temp.starters(10))', 'locked');
            select t.at('2027-01-02 12:00');
            select t.err('draft not finished', 'select set_lineup(pg_temp.starters(10))', 'locked');
            update league_settings set preseason_open_at = '2027-01-02 11:00 America/New_York';   -- what finishing the draft does
            select set_lineup(pg_temp.starters(10));
            select t.eq('week 1', (select min(week)::int from lineups where team_id=1), 1);
            select t.at('2027-01-05 12:00');
            select t.err('locked at the first lock', 'select set_lineup(pg_temp.starters(9))', 'locked');
        """)

    def test_locked_starters_cannot_be_dropped_traded_or_put_on_ir(self):
        self.sql(self.SETUP + """
            select t.as_user(1);
            select t.at('2027-03-30 08:00');
            select set_lineup(pg_temp.starters(10));
            select t.as_service();
            select t.at('2027-03-30 12:01');
            update players set injury_status='Out' where id = (select id from mine order by id limit 1);
            select t.err('drop locked starter', $q$ delete from roster_spots where player_id = (select id from mine order by id limit 1) $q$, 'locked lineup');
            select t.err('IR for locked starter', $q$ update roster_spots set slot='ir' where player_id = (select id from mine order by id limit 1) $q$, 'locked lineup');
            select t.err('trade a locked starter', $q$ update roster_spots set team_id=2 where player_id = (select id from mine order by id limit 1) $q$, 'locked lineup');
            -- bench players move freely
            delete from roster_spots where player_id = (select id from mine order by id desc limit 1);
            -- once lineups reopen the restriction is gone
            select t.at('2027-04-06 03:00');
            delete from roster_spots where player_id = (select id from mine order by id limit 1);
        """)

    def test_dropping_a_player_in_the_open_window_removes_him_from_the_new_lineup(self):
        self.sql(self.SETUP + """
            select t.as_user(1);
            select t.at('2027-03-30 04:00');
            select set_lineup(pg_temp.starters(10));
            select t.as_service();
            delete from roster_spots where player_id = (select id from mine order by id limit 1);
            select t.eq('lineup shrank', (select count(*) from lineups where team_id=1 and week=13), 9::bigint);
        """)

    def test_carry_over_copies_last_week_and_skips_departed_players(self):
        self.sql(self.SETUP + """
            select t.as_user(1);
            select t.at('2027-03-23 05:00');
            select set_lineup(pg_temp.starters(10));
            select t.as_service();
            select t.at('2027-03-30 05:00');
            delete from roster_spots where player_id = (select id from mine order by id limit 1);
            select t.eq('week 13 has none yet', (select count(*) from lineups where team_id=1 and week=13), 0::bigint);
            select carry_over_lineups(13);
            select t.eq('9 carried over', (select count(*) from lineups where team_id=1 and week=13), 9::bigint);
            -- a team that set its own lineup is left alone
            select carry_over_lineups(13);
            select t.eq('idempotent', (select count(*) from lineups where team_id=1 and week=13), 9::bigint);
        """)


if __name__ == "__main__":
    unittest.main()
