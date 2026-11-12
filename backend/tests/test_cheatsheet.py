"""The one-liners in the Commissioner SQL Cheat Sheet, run exactly as written against a fresh league (as the database console would: no signed-in user)."""
import unittest
from backend.tests.test_db import DbCase


class Cheatsheet(DbCase):
    def test_owners_list_and_rename(self):
        self.sql("""
            select t.eq('12 rows', (select count(*) from (
              select t.id, t.name, p.display_name, u.email from teams t left join profiles p on p.id = t.owner_id left join auth.users u on u.id = t.owner_id order by t.id) x), 12::bigint);
            update teams set name = 'Renamed Team' where id = 3;
            select t.eq('renamed', (select name from teams where id = 3), 'Renamed Team');
        """)

    def test_hand_a_team_to_someone_else_even_after_the_draft_started(self):
        self.sql("""
            insert into auth.users (id, email) values (t.uid(20), 'pat@example.com');
            select create_draft('main', 20); select start_draft((select max(id) from drafts));
            insert into profiles (id, display_name) select id, 'Pat' from auth.users where email = 'pat@example.com'
              on conflict (id) do update set display_name = excluded.display_name;
            update teams set owner_id = (select id from auth.users where email = 'pat@example.com') where id = 3;
            select t.eq('pat owns team 3', (select owner_id from teams where id = 3), t.uid(20));
            select t.as_user(20); select t.eq('and the app agrees', my_team(), 3::smallint);
        """)

    def test_player_housekeeping(self):
        self.sql("""
            update players set injury_status = 'Out' where name = 'NFL QB 1';
            select t.ok('injury set', (select is_official_injury(injury_status) from players where name = 'NFL QB 1'));
            update players set injury_status = null where name = 'NFL QB 1';
            select t.eq('cleared', (select injury_status from players where name = 'NFL QB 1'), null::text);
            update players set active = false where name = 'NFL QB 2';
            select t.eq('hidden from the pool', (select active from players where name = 'NFL QB 2'), false);
            insert into players (name, sport, position, ranking) values ('Brand New Rookie', 'NFL', 'WR', 150);
            select t.ok('added', exists (select 1 from players where name = 'Brand New Rookie'));
            select t.give(3, 'NFL', 'RB', 1);
            delete from roster_spots where team_id = 3 and player_id = (select id from players where name = 'NFL RB 1');
            select t.eq('released', (select count(*) from roster_spots where team_id = 3), 0::bigint);
        """)

    def test_force_a_game_to_be_read_again(self):
        self.sql("""
            insert into feed_games (game_key, final, fetches, last_fetch) values ('mlb:745123', true, 3, app_now());
            select t.eq('done games are not due', cardinality(feed_due('[{"key":"mlb:745123","final":true}]')), 0);
            delete from feed_games where game_key = 'mlb:745123';
            select t.eq('now it is', cardinality(feed_due('[{"key":"mlb:745123","final":true}]')), 1);
        """)

    def test_a_stat_correction_from_the_console(self):
        self.sql("""
            select set_config('request.jwt.claim.sub', (select id::text from profiles where is_commissioner), true);
            select apply_correction((select id from players where name = 'NFL QB 1'), week_of(app_now()), 'Passing yards', 305, 'Official scorer changed the total');
            select t.eq('stored', (select value from stat_lines where week = week_of(app_now()) and stat = 'Passing yards'), 305::numeric);
        """)

    def test_the_job_heartbeat_listing(self):
        self.sql("""
            select record_job('tick', true);
            select t.eq('one row', (select count(*) from (select job, last_ok_at, last_run_at, last_error from job_runs order by job) x), 1::bigint);
        """)


if __name__ == "__main__":
    unittest.main()
