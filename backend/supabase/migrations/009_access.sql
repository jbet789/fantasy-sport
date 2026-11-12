-- 007: who can see and do what. Clients read through row-level security and write only through the functions below,
-- so every rule in migrations 002-006 applies to every write. The scheduled jobs use the service role.

-- Close everything first.
revoke all on all tables in schema public from public, anon, authenticated;
revoke all on all sequences in schema public from public, anon, authenticated;
revoke execute on all functions in schema public from public, anon, authenticated;
-- Anything created after this file starts closed. Supabase grants new tables, sequences and functions to the client roles by
-- default, and a function is executable by PUBLIC unless told otherwise; both are switched off here (for the role that runs
-- migrations), so a later migration must grant explicitly what clients may use.
alter default privileges revoke execute on functions from public;
alter default privileges in schema public revoke all on functions from anon, authenticated;
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;

do $$ declare r record; begin
  for r in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table %I enable row level security', r.tablename);
  end loop;
end $$;

-- League data everyone in the league can read.
do $$ declare t text; begin
  foreach t in array array['league_settings','positions','scoring_weights','bonus_bands','f1_points','profiles','teams','players','roster_spots',
    'stat_lines','stat_corrections','baselines','player_scores','team_weeks','matchups','cup_groups','cup_bonus_wins','mulligans',
    'waivers','bonus_events','bonus_entrants','bonus_results','bonus_picks','drafts','draft_slots','draft_picks','activity_log']
  loop
    execute format('grant select on %I to authenticated', t);
    execute format('create policy %I on %I for select to authenticated using (true)', t || '_read', t);
  end loop;
end $$;
grant select on standings, bonus_scores, ir_overdue to authenticated;

-- Lineups are public to the whole league at all times, including a rival's lineup for the coming week.
grant select on lineups to authenticated;
create policy lineups_read on lineups for select to authenticated using (true);

-- Claims and queues are private to the team (the commissioner sees claims to settle disputes).
grant select on waiver_claims, draft_queue to authenticated;
create policy claims_read on waiver_claims for select to authenticated using (team_id = my_team() or is_commissioner() or status <> 'pending');
create policy queue_read on draft_queue for select to authenticated using (team_id = my_team());

-- Trades: only the two teams and the commissioner, plus completed trades for everyone.
grant select on trades, trade_items, trade_drops to authenticated;
create policy trades_read on trades for select to authenticated using (proposer = my_team() or partner = my_team() or is_commissioner() or status = 'completed');
create policy trade_items_read on trade_items for select to authenticated using (exists (select 1 from trades t where t.id = trade_id));
create policy trade_drops_read on trade_drops for select to authenticated using (exists (select 1 from trades t where t.id = trade_id));

-- The only direct writes: your own display name and your own team's name.
grant update (display_name) on profiles to authenticated;
create policy profiles_self on profiles for update to authenticated using (id = auth.uid()) with check (id = auth.uid());
grant update (name) on teams to authenticated;
create policy teams_rename on teams for update to authenticated using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- Helpers the policies and views call.
grant execute on function my_team(), is_commissioner(), lineup_open(timestamptz), lineup_week(timestamptz), week_of(timestamptz), is_final(int, timestamptz),
  week_start_ts(int), week_end_ts(int), final_ts(int), is_waiver_slot(timestamptz), next_nine(timestamptz), is_locked_starter(smallint, bigint, timestamptz),
  is_official_injury(text), profile_of(sport, text), trade_deadline(), band_points(int), app_now(), is_free_agent(bigint, timestamptz), owner_of(bigint), bonus_pts(text, int, int), bonus_draft_order() to authenticated;

-- Everything a manager can do.
grant execute on function set_lineup(bigint[]), use_mulligan(bigint, bigint),
  drop_player(bigint), add_free_agent(bigint, bigint), place_claim(bigint, bigint), cancel_claim(bigint), reorder_claims(bigint[]),
  propose_trade(smallint, bigint[], bigint[], bigint[], bigint[]), accept_trade(bigint), decline_trade(bigint), withdraw_trade(bigint),
  make_pick(bigint, bigint), queue_set(bigint, bigint[]), advance_if_due(bigint),
  move_to_ir(bigint), return_from_ir(bigint), screen_roster(smallint), screen_matchup(int), screen_side(smallint, int), screen_market(text, text, int), screen_trades(), screen_activity(int), screen_standings(int), screen_draft(bigint, text, text, int), screen_bonus(bigint) to authenticated;
-- Commissioner actions: the function itself checks the role.
grant execute on function create_bonus_event(text, timestamptz, boolean, text[]), create_bonus_draft(bigint, int), screen_admin(), admin_player_stats(text, int), grant_mulligan(smallint), apply_correction(bigint, int, text, numeric, text), revert_correction(bigint), veto_trade(bigint, text),
  enter_bonus_results(bigint, jsonb), conclude_bonus_event(bigint), create_draft(text, int, smallint[], bigint), start_draft(bigint) to authenticated;

-- Jobs only (service role): runs on a schedule or after a feed update.
grant all on all tables in schema public to service_role;
grant all on all sequences in schema public to service_role;
grant execute on all functions in schema public to service_role;

-- Live updates for the app.
do $$ declare t text; begin
  foreach t in array array['roster_spots','lineups','matchups','team_weeks','player_scores','activity_log','waivers','trades','drafts','draft_picks','bonus_picks','mulligans']
  loop execute format('alter publication supabase_realtime add table %I', t); end loop;
end $$;
