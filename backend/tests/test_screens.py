import unittest
from .test_db import DbCase


class Screens(DbCase):
    SETUP = """
        select t.give(1,'MLB','Hitter',12); select t.give(2,'MLB','Hitter',2,13);
        create temp table r as select player_id as id from roster_spots where team_id=1 order by player_id;
        grant all on r to authenticated;
        update players set injury_status='Out' where id = (select id from r offset 11 limit 1);
        update players set injury_status='Questionable' where id = (select id from r offset 10 limit 1);
        select t.at('2027-03-30 04:00');
    """

    def test_ir_moves_follow_the_rules(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select t.err('needs a designation', $q$ select move_to_ir((select id from r offset 10 limit 1)) $q$, 'official');
            select move_to_ir((select id from r offset 11 limit 1));
            select t.eq('on IR', (select slot from roster_spots where player_id=(select id from r offset 11 limit 1)), 'ir');
            select return_from_ir((select id from r offset 11 limit 1));
            select t.eq('back', (select slot from roster_spots where player_id=(select id from r offset 11 limit 1)), 'active');
            select t.err('not yours', $q$ select move_to_ir(1) $q$, 'not an active player');
            reset role;
        """)

    def test_a_starter_put_on_ir_leaves_the_lineup_and_locked_starters_cannot(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select set_lineup(array[(select id from r offset 11 limit 1), (select id from r limit 1)]);
            select move_to_ir((select id from r offset 11 limit 1));
            select t.eq('left the lineup', (select count(*) from lineups), 1::bigint);
            select return_from_ir((select id from r offset 11 limit 1));
            select set_lineup(array[(select id from r offset 11 limit 1)]);
            reset role; select t.at('2027-03-30 12:30'); set local role authenticated;
            select t.err('locked', $q$ select move_to_ir((select id from r offset 11 limit 1)) $q$, 'locked lineup');
            reset role;
        """)

    def test_roster_screen(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select set_lineup(array[(select id from r limit 1)]);
            select t.eq('12 players', jsonb_array_length(screen_roster()->'players'), 12);
            select t.ok('open', (screen_roster()->>'open')::boolean);
            select t.eq('lineup week is next week', (screen_roster()->>'lineup_week')::int, 13);
            select t.eq('one starter', (select count(*) from jsonb_array_elements(screen_roster()->'players') p where (p->>'starter')::boolean), 1::bigint);
            select t.eq('opens Tuesday 3 am ET', (screen_roster()->>'opens_at')::timestamptz, '2027-03-30 03:00 America/New_York'::timestamptz);
            select t.eq('locks noon', (screen_roster()->>'locks_at')::timestamptz, '2027-03-30 12:00 America/New_York'::timestamptz);
            reset role; select t.as_user(2); set local role authenticated;
            select t.eq('a rival sees my starters even while lineups are open', (select count(*) from jsonb_array_elements(screen_roster(1::smallint)->'players') p where (p->>'starter')::boolean), 1::bigint);
            reset role;
        """)

    def test_matchup_screen(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select set_lineup(array[(select id from r limit 1)]);
            reset role; select t.as_service();
            select carry_over_lineups(13);
            insert into matchups (week, kind, team_a, team_b) values (13, 'regular', 1, 2);
            select t.at('2027-03-31 12:00');
            insert into player_scores values (13, (select id from r limit 1), 20, 1.2, 12, true);
            insert into team_weeks (team_id, week, starter_points, total, best_starter) values (1, 13, 12, 12, 12);
            select t.as_user(1); set local role authenticated;
            select t.eq('opponent is team 2', (screen_matchup(13)->'sides'->1->>'team')::int, 2);
            select t.eq('my total', (screen_matchup(13)->'sides'->0->>'total')::numeric, 12::numeric);
            select t.eq('my starter row scored', (screen_matchup(13)->'sides'->0->'rows'->0->>'score')::numeric, 12::numeric);
            select t.ok('no game', (screen_matchup(14)->>'none')::boolean);
            reset role;
        """)


if __name__ == "__main__":
    unittest.main()


class Market(DbCase):
    def test_market_lists_free_agents_waivers_claims_and_blocks(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',3);
            create temp table r as select player_id as id from roster_spots where team_id=1 order by player_id;
            grant all on r to authenticated;
            select t.at('2027-03-27 10:00'); select t.as_service();
            select place_on_waivers((select id from players where name='NFL QB 1'));       -- 2-day hold ends Tue 9am: claimable at the run
            select t.at('2027-03-28 12:00');
            select t.as_user(1); set local role authenticated;
            select drop_player((select id from r limit 1));                                  -- releases Tue 9am too (1 day + next 9am)
            select place_claim((select id from players where name='NFL QB 1'));
            select t.eq('next run is Tuesday 9am', (screen_market()->>'next_run')::timestamptz, '2027-03-30 09:00 America/New_York'::timestamptz);
            select t.eq('roster shows 2', jsonb_array_length(screen_market()->'roster'), 2);
            select t.eq('QB 1 is on waivers with my claim', (select (p->>'claim_slot')::boolean and (p->>'claim_id') is not null from jsonb_array_elements(screen_market('NFL', 'QB 1')->'players') p limit 1), true);
            select t.eq('claims listed', jsonb_array_length(screen_market()->'claims'), 1);
            select t.ok('my dropped player shows a re-add block', (select count(*) from jsonb_array_elements(screen_market()->'players') p where p->>'blocked_until' is not null) = 1);
            select t.eq('search filters', jsonb_array_length(screen_market(null, 'NBA Guard 40')->'players'), 1);
            reset role; select t.as_user(2); set local role authenticated;
            select t.eq('other teams do not see my claims or blocks', jsonb_array_length(screen_market()->'claims'), 0);
            select t.eq('no block for them', (select count(*) from jsonb_array_elements(screen_market()->'players') p where p->>'blocked_until' is not null), 0::bigint);
            reset role;
        """)

    def test_trades_screen_and_activity(self):
        self.sql("""
            select t.give(1,'MLB','Hitter',2); select t.give(2,'MLB','Hitter',2,3); select t.give(3,'MLB','Hitter',1,5); select t.give(4,'MLB','Hitter',1,6);
            select t.at('2027-03-25 10:00'); select t.as_user(1); set local role authenticated;
            select propose_trade(2::smallint, array[(select player_id from roster_spots where team_id=1 order by player_id limit 1)], array[(select player_id from roster_spots where team_id=2 order by player_id limit 1)]);
            select t.eq('proposer sees it', jsonb_array_length(screen_trades()->'trades'), 1);
            select t.eq('with both sides', jsonb_array_length(screen_trades()->'trades'->0->'give'), 1);
            reset role; select t.as_user(3); set local role authenticated;
            select t.eq('outsider does not see a proposal', jsonb_array_length(screen_trades()->'trades'), 0);
            select t.ok('activity is public', jsonb_array_length(screen_activity(10)) >= 1);
            reset role;
        """)


class Admin(DbCase):
    def test_commissioner_screens_are_closed_to_managers(self):
        self.sql("""
            select t.at('2027-03-30 04:00');
            select t.as_user(1); set local role authenticated;
            select t.eq('manager sees nothing', (screen_admin()->>'commissioner'), 'false');
            select t.err('no stat search', $q$ select admin_player_stats('a', 12) $q$, 'Only the commissioner');
            select t.err('no mulligan', $q$ select grant_mulligan(3::smallint) $q$, 'Only the commissioner');
            reset role;
            select t.as_user(13); set local role authenticated;
            select t.eq('commissioner sees it', (screen_admin()->>'commissioner'), 'true');
            select t.eq('twelve teams', jsonb_array_length(screen_admin()->'teams'), 12);
            select t.eq('search finds players', jsonb_array_length(admin_player_stats('MLB', 12)->'players') > 0, true);
            reset role;
        """)


class StandingsScreen(DbCase):
    def test_seeds_are_top_four_then_best_points(self):
        self.sql("""
            select t.at('2027-03-30 04:00');
            insert into matchups (week, kind, team_a, team_b, score_a, score_b, winner)
              select 1, 'regular', i, i + 6, 10, 5, i from generate_series(1, 6) i;
            insert into team_weeks (team_id, week, total) select i, 1, 100 - i from generate_series(1, 12) i;
            update team_weeks set total = 500 where team_id = 12 and week = 1;       -- an 0-win team with the most points
            select t.as_user(1); set local role authenticated;
            select t.eq('twelve rows', jsonb_array_length(screen_standings()->'standings')::text, '12');
            select t.eq('top four seeded by record', (select count(*) from jsonb_array_elements(screen_standings()->'standings') e where e->>'seed' is not null and (e->>'rank')::int <= 4)::text, '4');
            select t.eq('six seeds in all', (select count(*) from jsonb_array_elements(screen_standings()->'standings') e where e->>'seed' is not null)::text, '6');
            select t.eq('best points among the rest is seed 5', (select e->>'team_id' from jsonb_array_elements(screen_standings()->'standings') e where e->>'seed' = '5'), '12');
            reset role;
        """)


class DraftScreen(DbCase):
    def test_room_state_and_clock_through_the_screen(self):
        self.sql("""
            select t.at('2027-01-01 12:00:00'); select create_draft('main', 20);
            select t.as_user(1); set local role authenticated;
            select t.eq('scheduled draft is found', screen_draft()->'draft'->>'status', 'scheduled');
            reset role; select t.as_service(); select start_draft((select max(id) from drafts));
            select t.as_user(1); set local role authenticated;
            select t.eq('team 1 is on the clock', screen_draft()->>'on_clock', '1');
            select t.eq('can draft the top player', (screen_draft()->'available'->0->>'problem'), null);
            select queue_set((select max(id) from drafts), array[(select id from players order by ranking offset 1 limit 1)]);
            select t.eq('queue is mine', jsonb_array_length(screen_draft()->'queue')::text, '1');
            select make_pick((select max(id) from drafts), (select id from players order by ranking limit 1));
            select t.eq('pick recorded', jsonb_array_length(screen_draft()->'picks')::text, '1');
            select t.eq('team 2 now', screen_draft()->>'on_clock', '2');
            select t.eq('drafted player gone from the pool', (select count(*) from jsonb_array_elements(screen_draft()->'available') e where (e->>'id')::bigint = (select id from players order by ranking limit 1))::text, '0');
            reset role;
            select t.at('2027-01-01 12:01:30');
            select t.as_user(3); set local role authenticated;
            select t.eq('opening the room runs the expired clock', jsonb_array_length(screen_draft()->'picks')::text, '2');
            select t.ok('autopick flagged', (screen_draft()->'picks'->1->>'auto')::boolean);
            reset role;
        """)


class LeagueSetup(DbCase):
    def test_setup_creates_profiles_teams_and_refuses_bad_input(self):
        self.sql("""
            insert into auth.users (id, email) values (gen_random_uuid(), 'boss@x.test');
            insert into auth.users (id, email) select gen_random_uuid(), 'M' || i || '@X.test' from generate_series(1, 12) i;   -- case differs on purpose
            delete from teams; delete from profiles;
            create temp table ppl as select ('[{"email": "boss@x.test", "name": "Boss", "commissioner": true}, ' ||
              (select string_agg(format('{"email": "m%s@x.test", "name": "M%s", "team": "Team %s"}', i, i, i), ', ') from generate_series(1, 12) i) || ']')::jsonb as j;
            select t.as_user(5);
            select t.err('not from the app', $q$ select setup_league((select j from ppl)) $q$, 'database console');
            select t.as_service();
            select t.err('missing email', $q$ select setup_league(replace((select j from ppl)::text, 'm12@x.test', 'ghost@x.test')::jsonb) $q$, 'not under Authentication');
            select t.eq('result', setup_league((select j from ppl)) like '12 teams set up%', true);
            select t.eq('twelve teams', (select count(*) from teams)::text, '12');
            select t.eq('commissioner is not on a team', (select count(*) from teams where owner_id = (select id from profiles where is_commissioner))::text, '0');
            select t.eq('draft slots are 1..12', (select string_agg(draft_slot::text, ',' order by draft_slot) from teams), '1,2,3,4,5,6,7,8,9,10,11,12');
            select t.eq('again is a no-op', setup_league((select j from ppl)) like '12 teams set up%', true);
            select setup_league((select j from ppl), true);
            select t.eq('shuffled still a permutation', (select count(distinct draft_slot) from teams)::text, '12');
            select t.err('only one commissioner', $q$ select setup_league('[{"email":"m1@x.test","commissioner":true},{"email":"m2@x.test","commissioner":true}]') $q$, 'Exactly');
        """)


class BonusScreens(DbCase):
    FIELD = "(select array_agg('Runner ' || i order by i) from generate_series(1, 30) i)"

    def test_event_lifecycle_through_the_functions(self):
        self.sql(f"""
            select t.at('2027-04-01 10:00');
            select t.as_user(1); set local role authenticated;
            select t.err('managers cannot create', $q$ select create_bonus_event('X', timestamptz '2027-04-10 12:00 America/New_York', false, array['a']) $q$, 'Only the commissioner');
            reset role; select t.as_user(13); set local role authenticated;
            select t.err('field too small', $q$ select create_bonus_event('Small', timestamptz '2027-04-10 12:00 America/New_York', false, array['a','b']) $q$, 'at least 12');
            select t.err('duplicate names', $q$ select create_bonus_event('Dup', timestamptz '2027-04-10 12:00 America/New_York', false, array['a','A','b','c','d','e','f','g','h','i','j','k']) $q$, 'different');
            create temp table ev as select create_bonus_event('Masters', timestamptz '2027-04-11 18:00 America/New_York', false, {self.FIELD}) as id;
            grant all on ev to authenticated;
            select t.err('too many rounds', $q$ select create_bonus_draft((select id from ev), 3) $q$, 'need at least 36');
            select create_bonus_draft((select id from ev), 2);
            select t.err('only one draft', $q$ select create_bonus_draft((select id from ev), 2) $q$, 'already has a draft');
            select t.eq('listed in the commissioner screen', jsonb_array_length(screen_admin()->'events')::text, '1');
            select t.eq('draft carries the event name', screen_admin()->'drafts'->0->>'event', 'Masters');
            select enter_bonus_results((select id from ev), '[{{"entrant":"Runner 1","status":"Placed","place":1}},{{"entrant":"Runner 2","status":"Missed cut"}}]');
            reset role; select t.as_service();
            insert into bonus_picks (event_id, entrant_id, team_id) select (select id from ev), id, 1 from bonus_entrants where name = 'Runner 1';
            select t.as_user(1); set local role authenticated;
            select t.eq('state open', screen_bonus()->'events'->0->>'state', 'open');
            select t.eq('draft is scheduled', screen_bonus()->'events'->0->'draft'->>'status', 'scheduled');
            select t.eq('my picks', screen_bonus()->'events'->0->>'my_picks', '1');
            select t.eq('winner scores 30', (screen_bonus()->'events'->0->>'my_points')::float8::text, '30');
            select t.eq('detail lists the field', jsonb_array_length(screen_bonus((select id from ev))->'detail'->'entrants')::text, '30');
            select t.eq('winner first, with team', screen_bonus((select id from ev))->'detail'->'entrants'->0->>'team', 'Team 1');
            select t.eq('team table sorted by points', (screen_bonus((select id from ev))->'detail'->'teams'->0->>'points')::float8::text, '30');
            reset role;
        """)


class Health(DbCase):
    def test_status_follows_the_age_of_the_last_good_run(self):
        self.sql("""
            select t.eq('never run', screen_health()->'scores'->>'status', 'waiting');
            select t.at('2027-03-30 10:00'); select record_job('tick', true);
            select t.eq('fresh', screen_health()->'scores'->>'status', 'ok');
            select t.at('2027-03-30 10:20'); select t.eq('two missed runs is still fine', screen_health()->'scores'->>'status', 'ok');
            select t.at('2027-03-30 10:40'); select t.eq('late', screen_health()->'scores'->>'status', 'late');
            select t.eq('minutes', screen_health()->'scores'->>'minutes', '40');
            select t.at('2027-03-30 13:00'); select t.eq('down', screen_health()->'scores'->>'status', 'down');
            select record_job('tick', true); select t.eq('recovers', screen_health()->'scores'->>'status', 'ok');
        """)

    def test_a_failed_run_keeps_the_last_good_time_and_shows_the_error_to_the_commissioner_only(self):
        self.sql("""
            select t.at('2027-03-30 10:00'); select record_job('tick', true);
            select t.at('2027-03-30 10:10'); select record_job('tick', false, 'scoring: boom');
            select t.eq('still counted as on time', screen_health()->'scores'->>'status', 'ok');
            select t.as_user(1);
            select t.eq('managers see no job detail', screen_health() ? 'jobs', false);
            select t.as_user(13);
            select t.eq('commissioner sees the error', (screen_health()->'jobs'->0->>'error'), 'scoring: boom');
            select t.eq('and the setup facts', (screen_health()->'facts'->>'teams_with_owner'), '12');
            select t.eq('backup has not run', screen_health()->'jobs'->1->>'status', 'waiting');
            select t.as_service();
            select record_job('backup', true, null, '{"rows": 5}');
            select t.as_user(13);
            select t.eq('backup is ok', (select j->>'status' from jsonb_array_elements(screen_health()->'jobs') j where j->>'job' = 'backup'), 'ok');
            select t.as_service(); select record_job('tick', true);
            select t.eq('a good run clears the error', (select last_error from job_runs where job = 'tick') is null, true);
        """)

    def test_only_the_jobs_can_record_a_run(self):
        self.sql("""
            select t.as_user(13);
            select t.err('commissioner cannot fake it either', $q$ select record_job('tick', true) $q$, 'scheduled jobs');
            select t.err('no such job', $q$ select record_job('x', true) $q$, '');
        """)


class FeedHealth(DbCase):
    def test_feed_heartbeats_join_the_commissioners_list(self):
        self.sql("""
            select t.at('2027-04-07 10:00'); select t.as_service();
            select record_job('feed_mlb', true, null, '{"lines": 40}'); select record_job('feed_mls', true);
            select t.as_user(13);
            select t.eq('mlb listed, mls not', (select string_agg(j->>'job', ',' order by j->>'job') from jsonb_array_elements(screen_health()->'jobs') j where j->>'job' like 'feed%'), 'feed_mlb');
            select t.eq('label', (select j->>'label' from jsonb_array_elements(screen_health()->'jobs') j where j->>'job' = 'feed_mlb'), 'MLB stats feed');
            select t.eq('on time', (select j->>'status' from jsonb_array_elements(screen_health()->'jobs') j where j->>'job' = 'feed_mlb'), 'ok');
            select t.at('2027-04-07 14:00');
            select t.eq('stopped after 4 hours', (select j->>'status' from jsonb_array_elements(screen_health()->'jobs') j where j->>'job' = 'feed_mlb'), 'late');
            select t.as_user(1);
            select t.eq('managers still see no jobs', screen_health() ? 'jobs', false);
        """)


class Injuries(DbCase):
    def test_feed_sets_clears_and_leaves_hand_set_statuses_alone(self):
        self.sql("""
            select t.as_service();
            select t.eq('listed players are set', (ingest_injuries('{"src":"espn","sport":"NBA","complete":true,"players":[
                {"ext":"e1","name":"NBA Guard 1","status":"Out"},{"ext":"e2","name":"NBA Wing 1","status":"Day-to-day"},{"ext":"e3","name":"Nobody Here","status":"Out"}]}'))->>'set', '2');
            select t.eq('source is feed', (select injury_source from players where name = 'NBA Guard 1'), 'feed');
            select t.ok('official', (select is_official_injury(injury_status) from players where name = 'NBA Guard 1'));
            select t.ok('soft tag is not official', not (select is_official_injury(injury_status) from players where name = 'NBA Wing 1'));
            select t.eq('feed id remembered', (select external_ids->>'espn' from players where name = 'NBA Guard 1'), 'e1');
            select t.eq('unmatched reported', jsonb_array_length(ingest_injuries('{"src":"espn","sport":"NBA","players":[{"ext":"e3","name":"Nobody Here","status":"Out"}]}')->'unmatched'), 1);
            -- healthy again: no longer listed -> cleared, and the IR return clock starts
            select t.give(1, 'NBA', 'Guard', 1);
            update roster_spots set slot = 'ir' where player_id = (select id from players where name = 'NBA Guard 1');
            select t.eq('cleared', (ingest_injuries('{"src":"espn","sport":"NBA","complete":true,"players":[{"ext":"e2","name":"NBA Wing 1","status":"Day-to-day"}]}')->>'cleared'), '1');
            select t.eq('status gone', (select injury_status from players where name = 'NBA Guard 1'), null::text);
            -- a hand-set status wins over the feed, both ways
            update players set injury_status = 'Out' where name = 'NBA Big 1';
            select t.eq('manual', (select injury_source from players where name = 'NBA Big 1'), 'manual');
            select ingest_injuries('{"src":"espn","sport":"NBA","complete":true,"players":[{"ext":"e4","name":"NBA Big 1","status":"Questionable"},{"ext":"e2","name":"NBA Wing 1","status":"Day-to-day"}]}');
            select t.eq('feed did not overwrite', (select injury_status from players where name = 'NBA Big 1'), 'Out');
            select ingest_injuries('{"src":"espn","sport":"NBA","complete":true,"players":[{"ext":"e2","name":"NBA Wing 1","status":"Day-to-day"}]}');
            select t.eq('nor clear it', (select injury_status from players where name = 'NBA Big 1'), 'Out');
        """)

    def test_a_feed_that_suddenly_reports_nobody_clears_nothing(self):
        self.sql("""
            select t.as_service();
            select set_config('app.feed_injury', 'on', true);
            update players set injury_status = 'Out' where sport = 'NHL' and position = 'Skater' and id in (select id from players where sport = 'NHL' and position = 'Skater' order by id limit 12);
            select set_config('app.feed_injury', 'off', true);
            select t.eq('refused', ingest_injuries('{"src":"espn","sport":"NHL","complete":true,"players":[]}')->>'suspicious_clear_skipped', 'true');
            select t.eq('all 12 still hurt', (select count(*) from players where sport = 'NHL' and injury_status = 'Out'), 12::bigint);
        """)

    def test_only_the_jobs_can_load_injuries_and_health_names_them(self):
        self.sql("""
            select t.as_user(13);
            select t.err('not even the commissioner in the app', $q$ select ingest_injuries('{"src":"x","sport":"NBA","players":[]}') $q$, 'feed jobs');
            select t.as_service(); select record_job('feed_nfl_injuries', true);
            select t.as_user(13);
            select t.eq('label', (select j->>'label' from jsonb_array_elements(screen_health()->'jobs') j where j->>'job' = 'feed_nfl_injuries'), 'NFL injury feed');
        """)


class FeedIngest(DbCase):
    P = "{\"src\":\"mlb\",\"ext\":\"%s\",\"name\":\"%s\",\"sport\":\"MLB\",\"position\":\"%s\"}"

    def test_players_are_created_linked_and_updated(self):
        self.sql("""
            select t.as_service();
            select t.eq('created', ingest_players('[{"src":"mlb","ext":"1","name":"Brand New","sport":"MLB","position":"Hitter","ranking":5}]')->>'created', '1');
            select t.eq('same feed id updates', ingest_players('[{"src":"mlb","ext":"1","name":"Brand New Jr","sport":"MLB","position":"Hitter","ranking":2}]')->>'updated', '1');
            select t.eq('renamed, ranked', (select name || ranking from players where external_ids->>'mlb' = '1'), 'Brand New Jr2');
            select t.eq('same name without a feed id is linked, not duplicated', ingest_players('[{"src":"mlb","ext":"99","name":"MLB Hitter 7","sport":"MLB","position":"Hitter"}]')->>'linked', '1');
            select t.eq('one row', (select count(*) from players where name = 'MLB Hitter 7'), 1::bigint);
            select t.eq('bad position is reported', jsonb_array_length(ingest_players('[{"src":"mlb","ext":"5","name":"X","sport":"MLB","position":"Goalie"}]')->'rejected'), 1);
            select ingest_players('[{"src":"mlb","ext":"2","name":"Rookie","sport":"MLB","position":"SP"}]', true);
            select t.eq('rookie enters through a waiver', (select kind from waivers w join players p on p.id = w.player_id where p.name = 'Rookie'), 'new');
        """)

    def test_games_add_up_once_and_a_reread_changes_nothing(self):
        self.sql("""
            select t.as_service();
            update players set external_ids = '{"mlb":"10"}' where name = 'MLB Hitter 1';
            select t.at('2027-04-06 10:00');
            select ingest_stats('[{"src":"mlb","ext":"10","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Home runs":1,"Games played":1,"Runs":2}},
                                  {"src":"mlb","ext":"10","game":"mlb:2","at":"2027-04-03T23:10:00Z","stats":{"Home runs":2,"Games played":1}}]');
            select t.eq('two games', (select value from stat_lines s join players p on p.id = s.player_id where p.name = 'MLB Hitter 1' and stat = 'Home runs' and week = 13), 3::numeric);
            select ingest_stats('[{"src":"mlb","ext":"10","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Home runs":1,"Games played":1,"Runs":2}}]');
            select t.eq('re-reading a game does not double count', (select value from stat_lines s join players p on p.id = s.player_id where p.name = 'MLB Hitter 1' and stat = 'Home runs'), 3::numeric);
            select ingest_stats('[{"src":"mlb","ext":"10","game":"mlb:2","at":"2027-04-03T23:10:00Z","stats":{"Home runs":0,"Games played":1}}]');
            select t.eq('a feed revision lowers it', (select value from stat_lines s join players p on p.id = s.player_id where p.name = 'MLB Hitter 1' and stat = 'Home runs'), 1::numeric);
            select ingest_stats('[{"src":"mlb","ext":"10","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Games played":1}}]');
            select t.eq('a stat that disappeared is removed', (select count(*) from stat_lines s join players p on p.id = s.player_id where p.name = 'MLB Hitter 1' and stat = 'Runs'), 0::bigint);
        """)

    def test_a_commissioner_correction_is_not_overwritten_until_reverted(self):
        self.sql("""
            select t.as_service();
            update players set external_ids = '{"mlb":"10"}' where name = 'MLB Hitter 1';
            select t.at('2027-04-06 10:00');
            select ingest_stats('[{"src":"mlb","ext":"10","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Home runs":1}}]');
            select t.as_user(13);
            select apply_correction((select id from players where name = 'MLB Hitter 1'), 13, 'Home runs', 4, 'feed missed three');
            select t.as_service();
            select ingest_stats('[{"src":"mlb","ext":"10","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Home runs":2}}]');
            select t.eq('correction stands', (select value from stat_lines where stat = 'Home runs' and week = 13), 4::numeric);
            select t.as_user(13);
            select revert_correction((select max(id) from stat_corrections));
            select t.as_service();
            select ingest_stats('[{"src":"mlb","ext":"10","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Home runs":2}}]');
            select t.eq('feed takes over again after a revert', (select value from stat_lines where stat = 'Home runs' and week = 13), 2::numeric);
        """)

    def test_bad_input_is_reported_not_fatal(self):
        self.sql("""
            select t.as_service();
            update players set external_ids = '{"mlb":"10"}' where name = 'MLB Hitter 1';
            select t.at('2027-04-06 10:00');
            select t.eq('mix of good and bad', (ingest_stats('[
               {"src":"mlb","ext":"404","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Home runs":1}},
               {"src":"mlb","ext":"10","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Saves":1,"Home runs":1,"Strikeouts":-2}},
               {"src":"mlb","ext":"10","game":"mlb:0","at":"2026-12-01T23:10:00Z","stats":{"Home runs":1}},
               {"src":"mlb","ext":"10","game":"mlb:9","at":"2027-03-20T23:10:00Z","stats":{"Home runs":1}}]')) - 'rejected',
               '{"applied":1,"created":0,"unmatched":["mlb:404"],"out_of_season":1,"final_weeks":1}'::jsonb);
            select t.eq('invalid and negative stats are named', jsonb_array_length(ingest_stats('[{"src":"mlb","ext":"10","game":"mlb:1","at":"2027-04-02T23:10:00Z","stats":{"Saves":1,"Strikeouts":-2}}]')->'rejected'), 2);
        """)

    def test_unknown_players_can_be_created_from_game_lines(self):
        self.sql("""
            select t.as_service(); select t.at('2027-04-06 10:00');
            select t.eq('created with their game', ingest_stats('[{"src":"nhl","ext":"8","game":"nhl:1","at":"2027-04-02T23:10:00Z","stats":{"Goals":1},"create":{"name":"New Skater","sport":"NHL","position":"Skater"}}]', true, true)->>'created', '1');
            select t.eq('and waived as a new player', (select count(*) from waivers w join players p on p.id = w.player_id where p.name = 'New Skater'), 1::bigint);
            select t.eq('without permission they are only reported', ingest_stats('[{"src":"nhl","ext":"9","game":"nhl:1","at":"2027-04-02T23:10:00Z","stats":{"Goals":1},"create":{"name":"Other","sport":"NHL","position":"Skater"}}]')->'unmatched', '["nhl:9"]'::jsonb);
        """)

    def test_feed_due_skips_games_that_are_finished_and_already_read(self):
        self.sql("""
            select t.as_service(); select t.at('2027-04-06 10:00');
            select ingest_stats('[{"src":"mlb","ext":"1","game":"mlb:1","at":"2027-04-02T23:10:00Z","final":true,"stats":{"Home runs":1}},
                                  {"src":"mlb","ext":"1","game":"mlb:2","at":"2027-04-02T23:10:00Z","final":false,"stats":{"Home runs":1}}]', true);
            select t.eq('never seen is due; live is due; fresh final is not',
                        feed_due('[{"key":"mlb:3","final":true},{"key":"mlb:2","final":false},{"key":"mlb:1","final":true}]'), array['mlb:3','mlb:2']);
            select t.at('2027-04-06 12:00');
            select t.eq('a final read once, 90+ minutes ago, is read again', feed_due('[{"key":"mlb:1","final":true}]'), array['mlb:1']);
        """)

    def test_clients_cannot_feed_the_league(self):
        self.sql("""
            select t.as_user(13); set local role authenticated;
            select t.err('manager', $q$ select ingest_stats('[]') $q$, 'permission denied');
            select t.err('players', $q$ select ingest_players('[]') $q$, 'permission denied');
            select t.err('tables', $q$ select * from game_stats $q$, 'permission denied');
        """)
