-- 004: free agents, waivers and trades. Mirrors engine/transactions.py.

create table waivers (            -- a player in the pool but not yet released
  player_id bigint primary key references players,
  release_at timestamptz not null,
  kind text not null default 'dropped' check (kind in ('dropped', 'new'))
);
create table readd_blocks (       -- the team that dropped a player cannot add him back for 7 days
  team_id smallint not null references teams, player_id bigint not null references players,
  until timestamptz not null, primary key (team_id, player_id)
);
create table waiver_claims (
  id bigint generated always as identity primary key,
  team_id smallint not null references teams, player_id bigint not null references players,
  drop_player_id bigint references players,
  rank int not null,                                   -- the team's own ordering, 1 = most wanted
  run_at timestamptz not null,                         -- the Tuesday 9:00 a.m. run that decides it
  status text not null default 'pending' check (status in ('pending', 'won', 'lost', 'void')),
  reason text, created_at timestamptz not null default app_now(), resolved_at timestamptz
);
create unique index one_pending_claim on waiver_claims (team_id, player_id) where status = 'pending';

-- ---- helpers ----
create function owner_of(p_player bigint) returns smallint language sql stable as $$ select team_id from roster_spots where player_id = p_player $$;

create function is_free_agent(p_player bigint, ts timestamptz default app_now()) returns boolean language sql stable as $$
  select exists (select 1 from players where id = p_player and active)
     and not exists (select 1 from roster_spots where player_id = p_player)
     and not exists (select 1 from waivers w where w.player_id = p_player and (w.release_at > ts or is_waiver_slot(w.release_at))) $$;

-- Why this add would not work for the team, or null when it would.
create function add_problem(p_team smallint, p_player bigint, p_drop bigint, ts timestamptz) returns text language plpgsql stable as $$
declare s league_settings; n_active int; n_f1 int; b timestamptz; pname text;
begin
  select * into s from league_settings;
  select name into pname from players where id = p_player;
  select until into b from readd_blocks where team_id = p_team and player_id = p_player;
  if b is not null and b > ts then return format('Team %s dropped %s within the last 7 days', p_team, pname); end if;
  if p_drop is not null then
    if not exists (select 1 from roster_spots where team_id = p_team and player_id = p_drop) then
      return format('%s is not on your roster', (select name from players where id = p_drop)); end if;
    if is_locked_starter(p_team, p_drop, ts) then
      return format('%s is in the locked lineup and can''t be dropped until lineups reopen', (select name from players where id = p_drop)); end if;
  end if;
  select count(*) filter (where r.slot = 'active'), count(*) filter (where pl.sport = 'F1')
    into n_active, n_f1 from roster_spots r join players pl on pl.id = r.player_id
   where r.team_id = p_team and r.player_id is distinct from p_drop;
  if n_active >= s.active_limit then return 'Roster is full: name an active player to drop'; end if;
  if (select sport from players where id = p_player) = 'F1' and n_f1 >= s.f1_cap then
    return format('Adding %s would put you over the F1 cap of %s', pname, s.f1_cap); end if;
  return null;
end $$;

-- Release a player from a roster onto waivers (1 day minimum, then the next 9:00 a.m.).
create function do_drop(p_team smallint, p_player bigint, ts timestamptz) returns void language plpgsql as $$
begin
  delete from roster_spots where team_id = p_team and player_id = p_player;
  insert into waivers (player_id, release_at, kind) values (p_player, next_nine(ts + interval '1 day'), 'dropped')
    on conflict (player_id) do update set release_at = excluded.release_at, kind = 'dropped';
  insert into readd_blocks (team_id, player_id, until) values (p_team, p_player, ts + interval '7 days')
    on conflict (team_id, player_id) do update set until = excluded.until;
  perform log_activity('drop', format('Team %s dropped %s', p_team, (select name from players where id = p_player)), p_team, jsonb_build_object('player', p_player));
end $$;

create function drop_player(p_player bigint) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team();
begin
  if t is null then raise exception 'You do not own a team'; end if;
  if not exists (select 1 from roster_spots where team_id = t and player_id = p_player) then raise exception 'That player is not on your roster'; end if;
  if is_locked_starter(t, p_player) then
    raise exception '% is in the locked lineup and can''t be dropped until lineups reopen on Tuesday at 3:00 a.m. ET', (select name from players where id = p_player); end if;
  perform do_drop(t, p_player, app_now());
end $$;

create function add_free_agent(p_player bigint, p_drop bigint default null) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team(); now_ timestamptz := app_now(); msg text; w waivers;
begin
  if t is null then raise exception 'You do not own a team'; end if;
  if not is_free_agent(p_player, now_) then
    select * into w from waivers where player_id = p_player;
    raise exception '% is not a free agent%', (select name from players where id = p_player),
      case when w.player_id is not null then format(' (on waivers until %s 9:00 a.m. ET)', to_char(w.release_at at time zone 'America/New_York', 'Dy Mon DD')) else '' end;
  end if;
  msg := add_problem(t, p_player, p_drop, now_);
  if msg is not null then raise exception '%', msg; end if;
  if p_drop is not null then perform do_drop(t, p_drop, now_); end if;
  delete from waivers where player_id = p_player;
  insert into roster_spots (team_id, player_id, acquired_at) values (t, p_player, now_);
  perform log_activity('add', format('Team %s signed %s (free agent)', t, (select name from players where id = p_player)), t, jsonb_build_object('player', p_player));
end $$;

-- A new signing or rookie enters the pool through a 2-day waiver. Called by the player-feed job.
create function place_on_waivers(p_player bigint) returns void language plpgsql security definer set search_path = public as $$
begin
  if exists (select 1 from roster_spots where player_id = p_player) then raise exception 'That player is on a roster'; end if;
  insert into waivers (player_id, release_at, kind) values (p_player, next_nine(app_now() + interval '2 days'), 'new')
    on conflict (player_id) do update set release_at = excluded.release_at, kind = 'new';
end $$;

-- ---- waiver claims ----
create function place_claim(p_player bigint, p_drop bigint default null) returns bigint language plpgsql security definer set search_path = public as $$
declare t smallint := my_team(); w waivers; msg text; cid bigint;
begin
  if t is null then raise exception 'You do not own a team'; end if;
  select * into w from waivers where player_id = p_player;
  if not found then raise exception 'That player is not on waivers%', case when is_free_agent(p_player) then ': add him directly' else '' end; end if;
  if not is_waiver_slot(w.release_at) then raise exception 'Only players released at the Tuesday 9:00 a.m. run take claims; this one is a free agent from %', to_char(w.release_at at time zone 'America/New_York', 'Dy Mon DD HH12:MI AM'); end if;
  if w.release_at <= app_now() then raise exception 'That waiver run is already underway'; end if;
  msg := add_problem(t, p_player, p_drop, w.release_at);
  if msg is not null then raise exception '%', msg; end if;
  insert into waiver_claims (team_id, player_id, drop_player_id, rank, run_at)
    values (t, p_player, p_drop, coalesce((select max(rank) from waiver_claims where team_id = t and status = 'pending'), 0) + 1, w.release_at)
    returning id into cid;
  return cid;
end $$;

create function cancel_claim(p_id bigint) returns void language plpgsql security definer set search_path = public as $$
begin
  delete from waiver_claims where id = p_id and team_id = my_team() and status = 'pending';
  if not found then raise exception 'No pending claim %', p_id; end if;
end $$;

create function reorder_claims(p_ids bigint[]) returns void language plpgsql security definer set search_path = public as $$
begin
  if exists (select 1 from waiver_claims where id = any (p_ids) and (team_id <> my_team() or status <> 'pending')) then raise exception 'You can only rank your own pending claims'; end if;
  update waiver_claims c set rank = o.n from unnest(p_ids) with ordinality o(id, n) where c.id = o.id;
end $$;

-- Why a claim cannot be awarded at this run: (kind, reason), or nulls when it can.
create function claim_problem(c waiver_claims, p_run timestamptz, out kind text, out why text) language plpgsql stable as $$
declare w waivers;
begin
  if exists (select 1 from waiver_claims x where x.player_id = c.player_id and x.status = 'won' and x.resolved_at = p_run) then
    kind := 'lost'; why := 'awarded to ' || (select team_id::text from waiver_claims x where x.player_id = c.player_id and x.status = 'won' and x.resolved_at = p_run limit 1) || ', who had priority'; return; end if;
  select * into w from waivers where player_id = c.player_id;
  if not (found and is_waiver_slot(w.release_at) and w.release_at <= p_run) then
    kind := 'void';
    if exists (select 1 from roster_spots where player_id = c.player_id) then why := 'already on a roster';
    elsif found and w.release_at > p_run then why := 'not claimable at this run: released ' || to_char(w.release_at at time zone 'America/New_York', 'Dy Mon DD') || ' at 9:00 a.m.';
    else why := 'already a free agent: add him directly'; end if;
    return; end if;
  why := add_problem(c.team_id, c.player_id, c.drop_player_id, p_run);
  if why is not null then kind := 'void'; end if;
end $$;

-- The Tuesday 9:00 a.m. run. Order = worst record first; a team that wins goes to the back of the line for the rest of the run.
create function process_waivers(p_run timestamptz default app_now(), p_order smallint[] default null) returns int
language plpgsql security definer set search_path = public as $$
declare q smallint[]; tm smallint; c waiver_claims; pr record; won int := 0; found_win boolean;
begin
  if auth.uid() is not null and not is_commissioner() then raise exception 'Only the waiver job can run waivers'; end if;
  q := coalesce(p_order, (select array_agg(team_id order by rank desc) from standings));
  loop
    found_win := false;
    foreach tm in array q loop
      select * into c from waiver_claims x where x.team_id = tm and x.status = 'pending' and x.run_at <= p_run
        and (claim_problem(x, p_run)).kind is null order by x.rank limit 1;
      if found then
        if c.drop_player_id is not null then perform do_drop(tm, c.drop_player_id, p_run); end if;
        delete from waivers where player_id = c.player_id;
        insert into roster_spots (team_id, player_id, acquired_at) values (tm, c.player_id, p_run);
        update waiver_claims set status = 'won', resolved_at = p_run where id = c.id;
        perform log_activity('add', format('Team %s won waiver claim for %s', tm, (select name from players where id = c.player_id)), tm, jsonb_build_object('player', c.player_id));
        q := array_remove(q, tm) || tm; won := won + 1; found_win := true; exit;
      end if;
    end loop;
    exit when not found_win;
  end loop;
  for c in select * from waiver_claims where status = 'pending' and run_at <= p_run order by team_id, rank loop
    select * into pr from claim_problem(c, p_run);
    update waiver_claims set status = coalesce(pr.kind, 'lost'), reason = coalesce(pr.why, 'not awarded'), resolved_at = p_run where id = c.id;
  end loop;
  delete from waivers where release_at <= p_run;     -- the run is done: unclaimed players are free agents
  return won;
end $$;

-- ---- trades ----
create table trades (
  id bigint generated always as identity primary key,
  proposer smallint not null references teams, partner smallint not null references teams,
  status text not null default 'proposed' check (status in ('proposed', 'pending_veto', 'completed', 'vetoed', 'declined', 'withdrawn', 'expired', 'failed')),
  proposed_at timestamptz not null default app_now(), accepted_at timestamptz, veto_until timestamptz,
  note text, check (proposer <> partner)
);
create table trade_items (       -- players changing hands; from_team is the sender
  trade_id bigint not null references trades on delete cascade, player_id bigint not null references players,
  from_team smallint not null references teams, primary key (trade_id, player_id)
);
create table trade_drops (       -- players a team releases to make room
  trade_id bigint not null references trades on delete cascade, team_id smallint not null references teams, player_id bigint not null references players,
  primary key (trade_id, player_id)
);

create function trade_deadline() returns timestamptz language sql stable as $$ select week_end_ts(44) $$;

create function trade_problem(p_id bigint, p_check_deadline boolean, ts timestamptz) returns text language plpgsql stable as $$
declare t trades; s league_settings; side record; r record; n_active int; n_f1 int;
begin
  select * into t from trades where id = p_id;
  select * into s from league_settings;
  if p_check_deadline and ts >= trade_deadline() then return 'Trades closed at the end of week 44'; end if;
  if not exists (select 1 from trade_items where trade_id = p_id and from_team = t.proposer)
     or not exists (select 1 from trade_items where trade_id = p_id and from_team = t.partner) then return 'Both sides must send at least one player'; end if;
  for side in select unnest(array[t.proposer, t.partner]) as team loop
    for r in select i.player_id from trade_items i where i.trade_id = p_id and i.from_team = side.team loop
      if not exists (select 1 from roster_spots where team_id = side.team and player_id = r.player_id) then
        return format('%s is not on team %s''s roster', (select name from players where id = r.player_id), side.team); end if;
      if is_locked_starter(side.team, r.player_id, ts) then
        return format('%s is in team %s''s locked lineup and can''t be traded until lineups reopen', (select name from players where id = r.player_id), side.team); end if;
      if p_check_deadline and exists (select 1 from lineups l where l.team_id = side.team and l.player_id = r.player_id and l.week = lineup_week(ts)) then
        return format('%s is in team %s''s lineup for next week; take him out of the lineup first', (select name from players where id = r.player_id), side.team); end if;
    end loop;
    for r in select d.player_id from trade_drops d where d.trade_id = p_id and d.team_id = side.team loop
      if exists (select 1 from trade_items where trade_id = p_id and player_id = r.player_id)
         or not exists (select 1 from roster_spots where team_id = side.team and player_id = r.player_id) then
        return format('%s can''t be used as a drop for team %s', (select name from players where id = r.player_id), side.team); end if;
      if is_locked_starter(side.team, r.player_id, ts) then
        return format('%s is in the locked lineup and can''t be dropped', (select name from players where id = r.player_id)); end if;
    end loop;
    select count(*) into n_active from (
        select r2.player_id from roster_spots r2 where r2.team_id = side.team and r2.slot = 'active'
           and r2.player_id not in (select player_id from trade_items where trade_id = p_id and from_team = side.team)
           and r2.player_id not in (select player_id from trade_drops where trade_id = p_id and team_id = side.team)
        union all select player_id from trade_items where trade_id = p_id and from_team <> side.team) x;
    if n_active > s.active_limit then return format('Team %s would have %s active players; name drops to get to %s', side.team, n_active, s.active_limit); end if;
    select count(*) into n_f1 from (
        select r2.player_id from roster_spots r2 where r2.team_id = side.team
           and r2.player_id not in (select player_id from trade_items where trade_id = p_id and from_team = side.team)
           and r2.player_id not in (select player_id from trade_drops where trade_id = p_id and team_id = side.team)
        union all select player_id from trade_items where trade_id = p_id and from_team <> side.team) x join players pl on pl.id = x.player_id where pl.sport = 'F1';
    if n_f1 > s.f1_cap then return format('Team %s would be over the F1 cap of %s', side.team, s.f1_cap); end if;
  end loop;
  return null;
end $$;

create function propose_trade(p_partner smallint, p_give bigint[], p_get bigint[], p_my_drops bigint[] default '{}', p_their_drops bigint[] default '{}')
returns bigint language plpgsql security definer set search_path = public as $$
declare t smallint := my_team(); tid bigint; msg text;
begin
  if t is null then raise exception 'You do not own a team'; end if;
  if p_partner = t then raise exception 'A team can''t trade with itself'; end if;
  if coalesce(array_length(p_give, 1), 0) > 24 or coalesce(array_length(p_get, 1), 0) > 24 or coalesce(array_length(p_my_drops, 1), 0) > 24 or coalesce(array_length(p_their_drops, 1), 0) > 24 then
    raise exception 'A trade can include at most 24 players on each list'; end if;
  if (select count(*) from trades where proposer = t and status = 'proposed') >= 10 then
    raise exception 'You already have 10 open proposals; withdraw one first'; end if;
  insert into trades (proposer, partner) values (t, p_partner) returning id into tid;
  insert into trade_items select tid, p, t from unnest(p_give) p;
  insert into trade_items select tid, p, p_partner from unnest(p_get) p;
  insert into trade_drops select tid, t, p from unnest(p_my_drops) p;
  insert into trade_drops select tid, p_partner, p from unnest(p_their_drops) p;
  msg := trade_problem(tid, true, app_now());
  if msg is not null then raise exception '%', msg; end if;
  perform log_activity('trade_proposed', format('Team %s proposed a trade to team %s', t, p_partner), t, jsonb_build_object('trade', tid));
  return tid;
end $$;

create function accept_trade(p_id bigint) returns void language plpgsql security definer set search_path = public as $$
declare tr trades; msg text; now_ timestamptz := app_now();
begin
  select * into tr from trades where id = p_id for update;
  if not found or tr.status <> 'proposed' then raise exception 'Trade % is %', p_id, coalesce(tr.status, 'missing'); end if;
  if my_team() is distinct from tr.partner then raise exception 'Only the other team can accept'; end if;
  msg := trade_problem(p_id, true, now_);
  if msg is not null then raise exception '%', msg; end if;
  update trades set status = 'pending_veto', accepted_at = now_, veto_until = now_ + interval '24 hours' where id = p_id;
  perform log_activity('trade_accepted', format('Team %s accepted trade %s; the commissioner has 24 hours to veto', tr.partner, p_id), tr.partner, jsonb_build_object('trade', p_id));
end $$;

create function decline_trade(p_id bigint) returns void language plpgsql security definer set search_path = public as $$
begin
  update trades set status = 'declined' where id = p_id and status = 'proposed' and partner = my_team();
  if not found then raise exception 'Only the other team can decline a proposed trade'; end if;
end $$;

create function withdraw_trade(p_id bigint) returns void language plpgsql security definer set search_path = public as $$
begin
  update trades set status = 'withdrawn' where id = p_id and status = 'proposed' and proposer = my_team();
  if not found then raise exception 'Only the proposer can withdraw a trade that is not yet accepted'; end if;
end $$;

create function veto_trade(p_id bigint, p_reason text) returns void language plpgsql security definer set search_path = public as $$
declare tr trades;
begin
  if not is_commissioner() then raise exception 'Only the commissioner can veto a trade'; end if;
  select * into tr from trades where id = p_id for update;
  if not found or tr.status <> 'pending_veto' or app_now() >= tr.veto_until then raise exception 'A trade can only be vetoed during its 24-hour window'; end if;
  if p_reason is null or length(trim(p_reason)) = 0 then raise exception 'A reason is required'; end if;
  update trades set status = 'vetoed', note = trim(p_reason) where id = p_id;
  perform log_activity('trade_veto', format('Trade %s vetoed: %s', p_id, trim(p_reason)), null, jsonb_build_object('trade', p_id), true);
end $$;

-- Run every few minutes by the scheduler: completes trades past their veto window, expires proposals after the deadline.
create function settle_trades(p_now timestamptz default app_now()) returns int language plpgsql security definer set search_path = public as $$
declare tr trades; msg text; done int := 0; i record;
begin
  if auth.uid() is not null and not is_commissioner() then raise exception 'Only the scheduler can settle trades'; end if;
  for tr in select * from trades where status in ('proposed', 'pending_veto') order by id loop
    if tr.status = 'proposed' then
      if p_now >= trade_deadline() then update trades set status = 'expired' where id = tr.id; end if;
    elsif p_now >= tr.veto_until then
      msg := trade_problem(tr.id, false, p_now);
      if msg is not null then update trades set status = 'failed', note = msg where id = tr.id;
      else
        for i in select team_id, player_id from trade_drops where trade_id = tr.id loop perform do_drop(i.team_id, i.player_id, p_now); end loop;
        delete from roster_spots where player_id in (select player_id from trade_items where trade_id = tr.id);
        for i in select player_id, case from_team when tr.proposer then tr.partner else tr.proposer end as to_team from trade_items where trade_id = tr.id loop
          insert into roster_spots (team_id, player_id, acquired_at) values (i.to_team, i.player_id, p_now);
        end loop;
        update trades set status = 'completed' where id = tr.id;
        perform log_activity('trade', format('Trade %s completed between teams %s and %s', tr.id, tr.proposer, tr.partner), null, jsonb_build_object('trade', tr.id));
        done := done + 1;
      end if;
    end if;
  end loop;
  return done;
end $$;
