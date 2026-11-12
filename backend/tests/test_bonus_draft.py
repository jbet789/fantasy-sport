import unittest
from .test_db import DbCase


class Bonus(DbCase):
    SETUP = """
        insert into bonus_events (name, ends_at, hold) values ('Iditarod', '2027-03-14 12:00 America/New_York', true), ('Dubai', '2027-03-27 19:00 America/New_York', false);
        insert into bonus_entrants (event_id, name, rank) select e.id, 'E' || g, g from bonus_events e, generate_series(1, 12) g;
        select t.at('2027-03-28 10:00');
    """

    def test_points_bands_and_tie_averaging(self):
        self.sql("""
            select t.eq('1st', bonus_pts('Placed', 1, 1), 30::numeric);
            select t.eq('5th is in the 4-5 band', bonus_pts('Placed', 5, 1), 16::numeric);
            select t.eq('tie for 1st shares 1st and 2nd', bonus_pts('Placed', 1, 2), 27::numeric);
            select t.eq('tie 5th-7th', round(bonus_pts('Placed', 5, 3), 4), round((16 + 10 + 10) / 3.0, 4));
            select t.eq('missed cut scores zero', bonus_pts('Missed cut', null, 1), 0::numeric);
            select t.eq('beyond bands', bonus_pts('Placed', 40, 1), 2::numeric);
        """)

    def test_enter_results_and_view(self):
        self.sql(self.SETUP + """
            select t.as_user(13);
            select enter_bonus_results((select id from bonus_events where name='Dubai'),
              '[{"entrant":"E1","status":"Placed","place":1},{"entrant":"E2","status":"Placed","place":1},{"entrant":"E3","status":"Placed","place":3},{"entrant":"E4","status":"Withdrew"}]'::jsonb);
            select t.eq('tie shares', (select points from bonus_scores where name='E1' and event_id=(select id from bonus_events where name='Dubai')), 27::numeric);
            select t.eq('third', (select points from bonus_scores where name='E3' and event_id=(select id from bonus_events where name='Dubai')), 20::numeric);
            select t.eq('withdrew', (select points from bonus_scores where name='E4' and event_id=(select id from bonus_events where name='Dubai')), 0::numeric);
            select t.as_user(1);
            select t.err('manager', $q$ select enter_bonus_results(1, '[]') $q$, 'commissioner');
            select t.as_user(13);
            select t.err('placed needs place', $q$ select enter_bonus_results((select id from bonus_events where name='Dubai'), '[{"entrant":"E5","status":"Placed"}]') $q$, 'needs a place');
            select t.err('unknown entrant', $q$ select enter_bonus_results((select id from bonus_events where name='Dubai'), '[{"entrant":"Nobody","status":"Withdrew"}]') $q$, 'Unknown entrant');
        """)

    def test_held_event_releases_only_when_concluded(self):
        self.sql(self.SETUP + """
            select t.as_user(13);
            select t.at('2027-03-12 10:00');
            select enter_bonus_results((select id from bonus_events where name='Iditarod'), '[{"entrant":"E1","status":"Placed","place":1}]'::jsonb);
            select t.eq('held: zero points', (select points from bonus_scores where name='E1' and event_id=(select id from bonus_events where name='Iditarod')), 0::numeric);
            select t.at('2027-03-15 10:00');
            select conclude_bonus_event((select id from bonus_events where name='Iditarod'));
            select t.eq('released', (select points from bonus_scores where name='E1' and event_id=(select id from bonus_events where name='Iditarod')), 30::numeric);
        """)

    def test_results_lock_when_the_week_is_final(self):
        self.sql(self.SETUP + """
            select t.as_user(13);
            select t.at('2027-03-31 12:00');   -- Dubai ended Sat Mar 27 (week 12), final Wed Mar 31 noon
            select t.err('final', $q$ select enter_bonus_results((select id from bonus_events where name='Dubai'), '[{"entrant":"E1","status":"Placed","place":1}]') $q$, 'final');
        """)

    def test_bonus_draft_order_worst_first(self):
        self.sql("""
            insert into matchups (week, kind, team_a, team_b, score_a, score_b, winner) values (1,'regular',1,2,10,5,1),(1,'regular',3,4,10,5,3);
            insert into team_weeks (team_id, week, total) values (1,1,10),(2,1,5),(3,1,10),(4,1,5);
            select t.eq('no-points teams first, then record, then points, then slot', (bonus_draft_order())[1:4], array[5,6,7,8]::smallint[]);
            select t.eq('best record last', (bonus_draft_order())[9:12], array[2,4,1,3]::smallint[]);
        """)


class Draft(DbCase):
    def test_snake_order(self):
        self.sql("""
            select create_draft('main', 3);
            select t.eq('36 slots', (select count(*) from draft_slots), 36::bigint);
            select t.eq('round 1 pick 12 is team 12', (select team_id from draft_slots where pick_no=12), 12::smallint);
            select t.eq('round 2 starts with team 12', (select team_id from draft_slots where pick_no=13), 12::smallint);
            select t.eq('round 2 ends with team 1', (select team_id from draft_slots where pick_no=24), 1::smallint);
            select t.eq('round 3 starts with team 1', (select team_id from draft_slots where pick_no=25), 1::smallint);
        """)

    def test_turns_legality_and_clock(self):
        self.sql("""
            select t.at('2027-01-01 12:00:00'); select create_draft('main', 20); select start_draft((select max(id) from drafts));
            create temp table f as select id from players where sport='F1' order by id limit 3;
            create temp table m as select id from players where sport='MLB' order by id limit 5;
            grant all on f to authenticated; grant all on m to authenticated;
            select t.at('2027-01-01 12:00:30');
            select t.as_user(2);
            select t.err('not your turn', $q$ select make_pick((select max(id) from drafts), (select min(id) from m)) $q$, 'not your turn');
            select t.as_user(1);
            select make_pick((select max(id) from drafts), (select min(id) from m));
            select t.eq('player on the roster', (select team_id from roster_spots where player_id=(select min(id) from m))::int, 1);
            select t.err('already drafted', $q$ select make_pick((select max(id) from drafts), (select min(id) from m)) $q$, 'not your turn');
            select t.as_user(2);
            select t.err('already drafted for team 2', $q$ select make_pick((select max(id) from drafts), (select min(id) from m)) $q$, 'already been drafted');
        """)

    def test_autopick_when_the_clock_runs_out_and_is_idempotent(self):
        self.sql("""
            select t.at('2027-01-01 12:00:00'); select create_draft('main', 20); select start_draft((select max(id) from drafts));
            select t.at('2027-01-01 12:00:59');
            select t.eq('nothing due yet', advance_if_due((select max(id) from drafts)), 0);
            select t.at('2027-01-01 12:01:01');
            select t.as_service();
            select t.eq('one autopick', advance_if_due((select max(id) from drafts)), 1);
            select t.eq('again is a no-op', advance_if_due((select max(id) from drafts)), 0);
            select t.eq('team 1 got the top-ranked player', (select player_id from draft_picks where pick_no=1), (select id from players order by ranking limit 1));
            select t.ok('marked auto', (select auto from draft_picks where pick_no=1));
            select t.at('2027-01-01 12:03:30');
            select t.eq('catches up two more picks', advance_if_due((select max(id) from drafts)), 2);
            select t.eq('pick 4 is next', (select current_pick from drafts), 4);
        """)

    def test_queue_first_and_f1_cap_in_autopick(self):
        self.sql("""
            select t.at('2027-01-01 12:00:00'); select create_draft('main', 20); select start_draft((select max(id) from drafts));
            create temp table w as select id from players where sport='NBA' order by id desc limit 2;
            grant all on w to authenticated;
            select t.as_user(1); select queue_set((select max(id) from drafts), (select array_agg(id) from w));
            select t.at('2027-01-01 12:01:01'); select t.as_service();
            select advance_if_due((select max(id) from drafts));
            select t.eq('queued player first', (select player_id from draft_picks where pick_no=1), (select max(id) from w));
            -- two F1 drivers already and a cap of two: autopick must skip F1
            update league_settings set f1_cap = 2;
            insert into roster_spots (team_id, player_id) select 12, id from players where sport='F1' order by id limit 2;
            update players set ranking = 0 where id = (select id from players where sport='F1' order by id offset 2 limit 1);
            select t.at('2027-01-01 12:12:00');
            select advance_if_due((select max(id) from drafts));
            select t.ok('team 12 did not get a third driver', (select count(*) from roster_spots r join players p on p.id=r.player_id where r.team_id=12 and p.sport='F1') = 2);
        """)

    def test_full_draft_fills_every_roster(self):
        self.sql("""
            select t.at('2027-01-01 12:00:00'); select create_draft('main', 20); select start_draft((select max(id) from drafts));
            update players set ranking = -ranking where sport = 'F1';     -- drivers are everyone's favorite, so the cap is what stops them
            select t.at('2027-01-02 12:00:00'); select t.as_service();
            select advance_if_due((select max(id) from drafts));
            select t.eq('draft done', (select status from drafts), 'done');
            select t.ok('week-1 lineups opened the moment the draft finished', (select preseason_open_at is not null from league_settings));
            select t.ok('and are open now', lineup_open());
            select t.eq('240 picks', (select count(*) from draft_picks), 240::bigint);
            select t.ok('every team has 20', (select min(c) = 20 and max(c) = 20 from (select count(*) c from roster_spots group by team_id) x));
            select t.eq('cap holds: most drivers on a team is 2', (select max(c) from (select count(*) c from roster_spots r join players p on p.id=r.player_id where p.sport='F1' group by team_id) x)::int, 2);
            select t.eq('all 20 drivers drafted', (select count(*) from roster_spots r join players p on p.id=r.player_id where p.sport='F1'), 20::bigint);
        """)

    def test_bonus_draft(self):
        self.sql("""
            insert into bonus_events (name, ends_at) values ('Dubai', '2027-03-27 19:00 America/New_York');
            insert into bonus_entrants (event_id, name, rank) select (select id from bonus_events), 'H' || g, g from generate_series(1, 30) g;
            select t.at('2027-01-01 12:00:00');
            select create_draft('bonus', 2, null, (select id from bonus_events));
            select start_draft((select max(id) from drafts));
            select t.as_user(1);
            select make_pick((select max(id) from drafts), (select id from bonus_entrants where name='H5'));
            select t.eq('pick recorded for the event', (select team_id from bonus_picks where entrant_id=(select id from bonus_entrants where name='H5'))::int, 1);
            select t.at('2027-01-01 12:00:00'); select t.at('2027-01-01 12:30:00'); select t.as_service();
            select advance_if_due((select max(id) from drafts));
            select t.eq('done', (select status from drafts), 'done');
            select t.eq('24 picks, each entrant once', (select count(distinct entrant_id) from draft_picks), 24::bigint);
        """)


if __name__ == "__main__":
    unittest.main()
