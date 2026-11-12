"""Security review: what a hostile (or just buggy) client can and cannot do, through the same roles Supabase uses.

Static checks first (the exact set of things clients may call, no table writes, no definer function without a pinned
search_path), then attacks as a manager, as a user with no team, and as nobody.
"""
import os, subprocess, unittest
from .test_db import DbCase, ENV

# Everything a signed-in client may call. A new function that is not listed here fails the test until someone decides it is safe.
CLIENT_FUNCTIONS = {
    "accept_trade", "add_free_agent", "admin_player_stats", "advance_if_due", "app_now", "apply_correction", "band_points", "bonus_draft_order", "bonus_pts",
    "cancel_claim", "conclude_bonus_event", "create_bonus_draft", "create_bonus_event", "create_draft", "decline_trade", "drop_player", "enter_bonus_results",
    "final_ts", "grant_mulligan", "is_commissioner", "is_final", "is_free_agent", "is_locked_starter", "is_official_injury", "is_waiver_slot", "lineup_open",
    "lineup_week", "make_pick", "move_to_ir", "my_team", "next_nine", "owner_of", "place_claim", "profile_of", "propose_trade", "queue_set", "reorder_claims",
    "return_from_ir", "revert_correction", "screen_activity", "screen_admin", "screen_bonus", "screen_draft", "screen_market", "screen_matchup", "screen_roster",
    "screen_side", "screen_standings", "screen_trades", "set_lineup", "start_draft", "trade_deadline", "use_mulligan", "veto_trade", "week_end_ts", "week_of",
    "week_start_ts", "withdraw_trade", "screen_health",
}


def q(sql, db="fl_test"):
    p = subprocess.run(["psql", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", sql], text=True, capture_output=True, env=ENV)
    if p.returncode:
        raise AssertionError(p.stderr)
    return p.stdout.strip()


class Static(unittest.TestCase):
    def test_clients_can_call_exactly_the_listed_functions(self):
        got = set(q("select proname from pg_proc where pronamespace = 'public'::regnamespace and has_function_privilege('authenticated', oid, 'execute')").split())
        self.assertEqual(got - CLIENT_FUNCTIONS, set(), "a function became callable by clients without being reviewed")
        self.assertEqual(CLIENT_FUNCTIONS - got, set(), "a listed function is no longer callable (update the list)")

    def test_anonymous_visitors_get_nothing(self):
        self.assertEqual(q("select count(*) from pg_proc where pronamespace = 'public'::regnamespace and has_function_privilege('anon', oid, 'execute')"), "0")
        self.assertEqual(q("select count(*) from pg_class where relnamespace = 'public'::regnamespace and relkind in ('r','v','S') and has_table_privilege('anon', oid, 'select')"), "0")

    def test_no_client_write_privileges_and_every_table_has_row_security(self):
        self.assertEqual(q("select count(*) from pg_class c where relnamespace = 'public'::regnamespace and relkind in ('r','v') and (has_table_privilege('authenticated', oid, 'insert') or has_table_privilege('authenticated', oid, 'update') or has_table_privilege('authenticated', oid, 'delete') or has_table_privilege('authenticated', oid, 'truncate'))"), "0")
        self.assertEqual(q("select coalesce(string_agg(relname, ','), '') from pg_class where relnamespace = 'public'::regnamespace and relkind = 'r' and not relrowsecurity"), "")
        self.assertEqual(q("select count(*) from pg_class where relnamespace = 'public'::regnamespace and relkind = 'S' and (case when relkind = 'S' then has_sequence_privilege('authenticated', oid, 'usage') end)"), "0")

    def test_every_definer_function_pins_its_search_path(self):
        self.assertEqual(q("select coalesce(string_agg(proname, ','), '') from pg_proc where pronamespace = 'public'::regnamespace and prosecdef and not exists (select 1 from unnest(coalesce(proconfig, '{}')) c where c like 'search_path=%')"), "")

    def test_the_test_clock_is_not_reachable_by_clients(self):
        self.assertEqual(q("select has_table_privilege('authenticated', 'dev_clock', 'select') or has_table_privilege('authenticated', 'dev_clock', 'insert')"), "f")

    def test_later_functions_are_closed_by_default(self):
        # Supabase grants every new function to clients unless told otherwise; migrations after the access file must not inherit that.
        out = q("begin; create function public.zz_probe() returns int language sql as 'select 1'; "
                "select has_function_privilege('anon', 'public.zz_probe()', 'execute') or has_function_privilege('authenticated', 'public.zz_probe()', 'execute'); rollback;").splitlines()
        self.assertIn("f", out)


class Attacks(DbCase):
    SETUP = """
        select t.give(1,'MLB','Hitter',12); select t.give(2,'MLB','Hitter',5,13);
        create temp table mine as select player_id as id from roster_spots where team_id = 1 order by player_id;
        create temp table theirs as select player_id as id from roster_spots where team_id = 2 order by player_id;
        grant all on mine, theirs to authenticated, anon;
        select t.at('2027-03-30 04:00');
    """

    def test_lineup_attacks(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select t.err('someone else''s player', $q$ select set_lineup(array[(select id from theirs limit 1)]) $q$, '');
            select t.err('a player that does not exist', $q$ select set_lineup(array[999999999]) $q$, '');
            select t.err('eleven starters', $q$ select set_lineup((select array_agg(id) from mine) || (select id from mine limit 1)) $q$, '');
            select t.err('the same player twice', $q$ select set_lineup(array[(select id from mine limit 1), (select id from mine limit 1)]) $q$, '');
            select t.err('a hundred thousand ids is refused quickly', $q$ select set_lineup((select array_agg(g) from generate_series(1, 100000) g)) $q$, '');
            select t.eq('nothing was saved by the failed attempts', (select count(*) from lineups where team_id = 1), 0::bigint);
            reset role;
        """)

    def test_roster_attacks_on_other_teams_players(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select t.err('drop a rival', $q$ select drop_player((select id from theirs limit 1)) $q$, '');
            select t.err('IR a rival', $q$ select move_to_ir((select id from theirs limit 1)) $q$, '');
            select t.err('return a rival from IR', $q$ select return_from_ir((select id from theirs limit 1)) $q$, '');
            select t.err('use a rival as my drop', $q$ select add_free_agent((select id from players where name = 'NFL QB 1'), (select id from theirs limit 1)) $q$, '');
            select t.err('claim with a rival as my drop', $q$ select place_claim((select id from players where name = 'NFL QB 1'), (select id from theirs limit 1)) $q$, '');
            reset role;
            select t.eq('the rival kept every player', (select count(*) from roster_spots where team_id = 2), 5::bigint);
        """)

    def test_claim_attacks(self):
        self.sql(self.SETUP + """
            select t.as_service();
            insert into waivers (player_id, kind, release_at) select id, 'dropped', '2027-04-06 09:00 America/New_York' from players where name in ('NFL QB 1', 'NFL QB 2');
            select t.as_user(2); set local role authenticated;
            select place_claim((select id from players where name = 'NFL QB 1'), null);
            select place_claim((select id from players where name = 'NFL QB 2'), null);
            create temp table c2 as select id, rank from waiver_claims order by rank;
            grant all on c2 to authenticated;
            reset role; select t.as_user(1); set local role authenticated;
            select t.err('cancel a rival''s claim', $q$ select cancel_claim((select min(id) from c2)) $q$, '');
            select t.err('reorder a rival''s claims', $q$ select reorder_claims((select array_agg(id order by id desc) from c2)) $q$, 'your own');
            reset role; select t.as_service();
            select t.eq('rival''s claims untouched', (select string_agg(id::text || ':' || rank, ',' order by id) from waiver_claims where team_id = 2), (select string_agg(id::text || ':' || rank, ',' order by id) from c2));
            select t.eq('rival''s claims still pending', (select count(*) from waiver_claims where team_id = 2 and status = 'pending'), 2::bigint);
        """)

    def test_trade_attacks(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select t.err('trade with myself', $q$ select propose_trade(1::smallint, array[(select id from mine limit 1)], array[(select id from mine offset 1 limit 1)], '{}', '{}') $q$, '');
            select t.err('trade with a team that does not exist', $q$ select propose_trade(99::smallint, array[(select id from mine limit 1)], array[(select id from theirs limit 1)], '{}', '{}') $q$, '');
            select t.err('give a player I do not own', $q$ select propose_trade(2::smallint, array[(select id from theirs limit 1)], array[(select id from theirs offset 1 limit 1)], '{}', '{}') $q$, '');
            select t.err('ask for a player they do not own', $q$ select propose_trade(2::smallint, array[(select id from mine limit 1)], array[(select id from mine offset 1 limit 1)], '{}', '{}') $q$, '');
            select t.err('send nothing', $q$ select propose_trade(2::smallint, '{}', array[(select id from theirs limit 1)], '{}', '{}') $q$, '');
            select t.err('make them drop my player', $q$ select propose_trade(2::smallint, array[(select id from mine limit 1)], array[(select id from theirs limit 1)], '{}', array[(select id from mine offset 1 limit 1)]) $q$, '');
            select t.eq('no trade rows from the failed attempts', (select count(*) from trades), 0::bigint);
            reset role;
        """)

    def test_trade_state_attacks(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select propose_trade(2::smallint, array[(select id from mine offset 11 limit 1)], array[(select id from theirs limit 1)], '{}', '{}');
            select t.err('proposer cannot accept their own trade', $q$ select accept_trade((select max(id) from trades)) $q$, '');
            reset role; select t.as_user(3); set local role authenticated;
            select t.err('uninvolved team cannot accept', $q$ select accept_trade((select max(id) from trades)) $q$, '');
            select t.err('uninvolved team cannot decline', $q$ select decline_trade((select max(id) from trades)) $q$, '');
            select t.err('uninvolved team cannot withdraw', $q$ select withdraw_trade((select max(id) from trades)) $q$, '');
            reset role; select t.as_user(2); set local role authenticated;
            select t.err('partner cannot withdraw', $q$ select withdraw_trade((select max(id) from trades)) $q$, '');
            select accept_trade((select max(id) from trades));
            select t.err('cannot accept twice', $q$ select accept_trade((select max(id) from trades)) $q$, '');
            select t.err('cannot decline after accepting', $q$ select decline_trade((select max(id) from trades)) $q$, '');
            select t.err('a manager cannot veto', $q$ select veto_trade((select max(id) from trades), 'no') $q$, 'commissioner');
            reset role;
        """)

    def test_commissioner_functions_refuse_managers(self):
        self.sql(self.SETUP + """
            select t.as_user(1); set local role authenticated;
            select t.err('correct', $q$ select apply_correction(1, 12, 'Runs', 1, 'x') $q$, 'commissioner');
            select t.err('revert', $q$ select revert_correction(1) $q$, 'commissioner');
            select t.err('enter results', $q$ select enter_bonus_results(1, '[]') $q$, 'commissioner');
            select t.err('conclude', $q$ select conclude_bonus_event(1) $q$, 'commissioner');
            select t.err('new event', $q$ select create_bonus_event('x', now(), false, array['a']) $q$, 'commissioner');
            select t.err('bonus draft', $q$ select create_bonus_draft(1, 2) $q$, 'commissioner');
            select t.err('create draft', $q$ select create_draft('main', 20) $q$, 'commissioner');
            select t.err('start draft', $q$ select start_draft(1) $q$, 'commissioner');
            select t.err('grant mulligan', $q$ select grant_mulligan(1::smallint) $q$, 'commissioner');
            select t.err('stat search', $q$ select admin_player_stats('a', 1) $q$, 'commissioner');
            select t.eq('admin screen is empty for managers', screen_admin() ->> 'commissioner', 'false');
            reset role;
        """)

    def test_a_user_with_no_team_cannot_play_but_nothing_crashes(self):
        self.sql(self.SETUP + """
            select t.as_user(13); set local role authenticated;
            select t.err('lineup', $q$ select set_lineup(array[(select id from mine limit 1)]) $q$, 'own a team');
            select t.err('add', $q$ select add_free_agent((select id from players where name = 'NFL QB 1'), null) $q$, '');
            select t.err('claim', $q$ select place_claim((select id from players where name = 'NFL QB 1'), null) $q$, '');
            select t.err('trade', $q$ select propose_trade(2::smallint, array[(select id from mine limit 1)], array[(select id from theirs limit 1)], '{}', '{}') $q$, '');
            select t.err('queue', $q$ select queue_set(1, array[1]) $q$, '');
            select t.err('mulligan', $q$ select use_mulligan(1, 2) $q$, '');
            select t.err('ir', $q$ select move_to_ir((select id from mine limit 1)) $q$, '');
            reset role;
            select t.as_service();
            insert into auth.users (id, email) values ('11111111-1111-1111-1111-111111111111', 'stranger@x.test');
            select t.as_user(0);
            select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', true);
            set local role authenticated;
            select t.eq('a stranger is not a commissioner', is_commissioner(), false);
            select t.err('a stranger cannot play', $q$ select drop_player((select id from mine limit 1)) $q$, '');
            select t.eq('screens still answer', (screen_roster(1::smallint) -> 'team' ->> 'id'), '1');
            reset role;
        """)

    def test_anonymous_calls_are_refused(self):
        self.sql(self.SETUP + """
            set local role anon;
            select t.err('screen', $q$ select screen_roster(1::smallint) $q$, 'permission denied');
            select t.err('lineups table', $q$ select count(*) from lineups $q$, 'permission denied');
            select t.err('players table', $q$ select count(*) from players $q$, 'permission denied');
            select t.err('rpc', $q$ select set_lineup(array[1]) $q$, 'permission denied');
            reset role;
        """)

    def test_private_data_stays_private(self):
        self.sql(self.SETUP + """
            select t.as_service();
            insert into draft_queue (draft_id, team_id, player_id, rank) select 1, 2, id, 1 from players limit 0;
            select t.at('2027-01-01 12:00:00'); select create_draft('main', 20);
            insert into draft_queue (draft_id, team_id, player_id, rank) select (select max(id) from drafts), 2, (select min(id) from players), 1;
            select t.as_user(1); set local role authenticated;
            select t.eq('cannot read a rival''s draft queue directly', (select count(*) from draft_queue), 0::bigint);
            select t.eq('nor through the draft screen', jsonb_array_length(screen_draft() -> 'queue'), 0);
            reset role;
        """)

    def test_queue_abuse_is_bounded_and_bad_ids_are_refused(self):
        self.sql("""
            select t.at('2027-01-01 12:00:00'); select create_draft('main', 20); select start_draft((select max(id) from drafts));
            select t.as_user(1); set local role authenticated;
            select t.err('a huge queue of real players', $q$ select queue_set((select max(id) from drafts), (select array_agg((select min(id) from players)) from generate_series(1, 200000))) $q$, 'at most');
            select t.err('ids that are not players', $q$ select queue_set((select max(id) from drafts), array[-5, 999999999]) $q$, '');
            select t.err('the same player twice', $q$ select queue_set((select max(id) from drafts), array[(select min(id) from players), (select min(id) from players)]) $q$, '');
            select queue_set((select max(id) from drafts), array[(select id from players order by ranking limit 1)]);
            reset role; select t.as_service();
            select t.at('2027-01-01 12:01:30');
            select t.ok('autopick uses the queue', advance_if_due((select max(id) from drafts)) >= 1);
            select t.eq('and took the queued player', (select player_id from draft_picks where pick_no = 1), (select id from players order by ranking limit 1));
        """)

    def test_open_trade_proposals_are_capped(self):
        self.sql(self.SETUP + """
            select t.give(3,'MLB','Hitter',1); select t.give(4,'MLB','Hitter',1); select t.give(5,'MLB','Hitter',1);
            create temp table partner as select team_id, player_id as id from roster_spots where team_id in (3,4,5);
            grant all on partner to authenticated;
            select t.as_user(1); set local role authenticated;
            do $d$ begin
              for i in 1..10 loop
                perform propose_trade((3 + (i % 3))::smallint, array[(select id from mine offset 11 limit 1)], array[(select id from partner where team_id = 3 + (i % 3) limit 1)], '{}', '{}');
              end loop;
            end $d$;
            select t.err('the eleventh open proposal is refused', $q$ select propose_trade(3::smallint, array[(select id from mine offset 11 limit 1)], array[(select id from partner where team_id = 3 limit 1)], '{}', '{}') $q$, 'open proposals');
            reset role;
        """)
