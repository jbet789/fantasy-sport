-- 002: people, players, rosters and weekly lineups. The rules here are enforced by triggers, so they hold
-- no matter which function, job or console session writes the rows.

create type sport as enum ('NFL', 'NBA', 'MLB', 'NHL', 'MLS', 'F1');

-- Reference data, loaded by seed.sql from the scoring workbook export.
create table positions (
  sport sport not null, position text not null, profile text not null,
  min_games numeric not null default 1, fallback_avg numeric not null, fallback_sd numeric not null,
  primary key (sport, position)
);
create table scoring_weights (profile text not null, stat text not null, weight numeric not null, primary key (profile, stat));
create table bonus_bands (start_place int primary key, points numeric not null);
create table f1_points (kind text not null check (kind in ('race', 'sprint', 'pole', 'front_row')), place int not null, points numeric not null, primary key (kind, place));

create table profiles (
  id uuid primary key references auth.users on delete cascade,
  display_name text not null,
  is_commissioner boolean not null default false
);
create unique index one_commissioner on profiles ((true)) where is_commissioner;   -- the league has exactly one

create table teams (
  id smallint primary key check (id between 1 and 12),
  name text not null,
  owner_id uuid unique references profiles,
  draft_slot smallint unique check (draft_slot between 1 and 12)
);

create table players (
  id bigint generated always as identity primary key,
  name text not null,
  sport sport not null,
  position text not null,
  injury_status text,                 -- the official designation text from the feed, null when healthy
  ranking int,                        -- default draft ranking, lower is better (used by autopick)
  external_ids jsonb not null default '{}',
  active boolean not null default true,
  created_at timestamptz not null default now(),
  foreign key (sport, position) references positions
);
create index players_sport_idx on players (sport);

create function my_team() returns smallint language sql stable security definer set search_path = public as
  $$ select id from teams where owner_id = auth.uid() $$;
create function is_commissioner() returns boolean language sql stable security definer set search_path = public as
  $$ select coalesce((select is_commissioner from profiles where id = auth.uid()), false) $$;
-- The official designations that make a player eligible for injured reserve (rulebook: IR).
create function is_official_injury(status text) returns boolean language sql immutable as
  $$ select lower(coalesce(status, '')) in ('out', 'ir', 'il', 'injured reserve', 'injured list', 'pup', 'nfi') $$;
create function profile_of(p_sport sport, p_position text) returns text language sql stable as
  $$ select profile from positions where sport = p_sport and position = p_position $$;

-- ---- activity log (public to the league) and a guard that makes it append-only ----
create table activity_log (
  id bigint generated always as identity primary key,
  at timestamptz not null default app_now(),
  kind text not null,
  team_id smallint references teams,
  commissioner boolean not null default false,      -- true for commissioner actions (the audit trail)
  actor uuid,
  detail jsonb not null default '{}',
  message text not null
);
create index activity_log_at_idx on activity_log (at desc);
create function log_activity(p_kind text, p_message text, p_team smallint default null, p_detail jsonb default '{}', p_commissioner boolean default false)
returns void language sql security definer set search_path = public as
  $$ insert into activity_log (kind, team_id, commissioner, actor, detail, message) values (p_kind, p_team, p_commissioner, auth.uid(), p_detail, p_message) $$;
create function forbid_change() returns trigger language plpgsql as
  $$ begin raise exception '% is append-only: % is not allowed', tg_table_name, tg_op; end $$;
create trigger activity_log_append_only before update or delete on activity_log for each row execute function forbid_change();

-- ---- rosters ----
create table roster_spots (
  player_id bigint primary key references players,        -- a player can be on only one roster
  team_id smallint not null references teams,
  slot text not null default 'active' check (slot in ('active', 'ir')),
  acquired_at timestamptz not null default app_now(),
  ir_cleared_at timestamptz                               -- set when an IR player loses his official designation
);
create index roster_spots_team_idx on roster_spots (team_id);

create table lineups (
  team_id smallint not null references teams,
  week smallint not null check (week between 1 and 51),
  player_id bigint not null references players,
  primary key (team_id, week, player_id)
);
create index lineups_player_idx on lineups (player_id);

-- A starter in a locked lineup can't be dropped, traded or moved to IR until lineups reopen.
create function is_locked_starter(p_team smallint, p_player bigint, ts timestamptz default app_now()) returns boolean
language sql stable as $$
  select not lineup_open(ts) and exists (select 1 from lineups l where l.team_id = p_team and l.player_id = p_player and l.week = week_of(ts)) $$;

create function roster_rules() returns trigger language plpgsql as $$
declare s league_settings; n_active int; n_ir int; n_f1 int; p players;
begin
  if tg_op = 'UPDATE' and new.team_id = old.team_id and new.slot = old.slot then return new; end if;   -- bookkeeping only
  select * into s from league_settings;
  perform 1 from teams where id = new.team_id for update;              -- one change per team at a time
  select * into p from players where id = new.player_id;
  select count(*) filter (where r.slot = 'active'), count(*) filter (where r.slot = 'ir'),
         count(*) filter (where pl.sport = 'F1')
    into n_active, n_ir, n_f1
    from roster_spots r join players pl on pl.id = r.player_id
   where r.team_id = new.team_id and r.player_id <> new.player_id;
  if new.slot = 'active' and n_active + 1 > s.active_limit then
    raise exception 'Roster is full: % active players is the limit', s.active_limit using errcode = 'P0001'; end if;
  if new.slot = 'ir' then
    if n_ir + 1 > s.ir_limit then raise exception 'Injured reserve is full: % spots', s.ir_limit; end if;
    if not is_official_injury(p.injury_status) then
      raise exception 'IR needs an official Out or injured-list designation; % has none', p.name; end if;
  end if;
  if p.sport = 'F1' and n_f1 + 1 > s.f1_cap then
    raise exception 'F1 cap: a team can hold at most % F1 drivers', s.f1_cap; end if;
  return new;
end $$;
create trigger roster_rules_trg before insert or update on roster_spots for each row execute function roster_rules();

create function roster_lock_guard() returns trigger language plpgsql as $$
begin
  if is_locked_starter(old.team_id, old.player_id)
     and (tg_op = 'DELETE' or new.team_id <> old.team_id or new.slot = 'ir') then
    raise exception '% is in the locked lineup and can''t be dropped, traded or moved to IR until lineups reopen on Tuesday at 3:00 a.m. ET',
      (select name from players where id = old.player_id);
  end if;
  return coalesce(new, old);
end $$;
create trigger roster_lock_guard_trg before update or delete on roster_spots for each row execute function roster_lock_guard();

-- A player who leaves a team, or goes to IR, comes out of that team's not-yet-locked lineups.
create function roster_cleanup() returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' or new.team_id <> old.team_id or new.slot = 'ir' then
    perform set_config('app.lineup_bypass', 'on', true);
    delete from lineups where team_id = old.team_id and player_id = old.player_id and week >= week_of(app_now()) + 1;
    perform set_config('app.lineup_bypass', 'off', true);
  end if;
  return null;
end $$;
create trigger roster_cleanup_trg after update or delete on roster_spots for each row execute function roster_cleanup();

-- When an IR player's official designation goes away, start the 3-day clock.
create function injury_cleared() returns trigger language plpgsql as $$
begin
  if is_official_injury(old.injury_status) and not is_official_injury(new.injury_status) then
    update roster_spots set ir_cleared_at = app_now() where player_id = new.id and slot = 'ir' and ir_cleared_at is null;
  elsif is_official_injury(new.injury_status) then
    update roster_spots set ir_cleared_at = null where player_id = new.id and slot = 'ir';
  end if;
  return new;
end $$;
create trigger players_injury_cleared after update of injury_status on players for each row execute function injury_cleared();

-- Cleared IR players who are still on IR after the return window.
create view ir_overdue as
  select r.team_id, r.player_id, r.ir_cleared_at, r.ir_cleared_at + s.ir_return_window as due_at
  from roster_spots r, league_settings s
  where r.slot = 'ir' and r.ir_cleared_at is not null and app_now() >= r.ir_cleared_at + s.ir_return_window;

-- ---- lineup rules ----
create function lineup_rules() returns trigger language plpgsql as $$
declare s league_settings; r lineups; n int;
begin
  if current_setting('app.lineup_bypass', true) = 'on' then return coalesce(new, old); end if;
  select * into s from league_settings;
  r := coalesce(new, old);
  if lineup_week() is distinct from r.week then
    raise exception 'Lineups are locked. They reopen Tuesday at 3:00 a.m. ET for nine hours' using errcode = 'P0001'; end if;
  if tg_op = 'DELETE' then return old; end if;
  perform 1 from teams where id = new.team_id for update;
  if not exists (select 1 from roster_spots where player_id = new.player_id and team_id = new.team_id and slot = 'active') then
    raise exception 'Only active players on your roster can start: %', (select name from players where id = new.player_id); end if;
  if exists (select 1 from trade_items i join trades t on t.id = i.trade_id where i.player_id = new.player_id and i.from_team = new.team_id and t.status = 'pending_veto') then
    raise exception '% is in an accepted trade awaiting the veto window and can''t start until it settles', (select name from players where id = new.player_id); end if;
  select count(*) into n from lineups where team_id = new.team_id and week = new.week and player_id <> new.player_id;
  if n + 1 > s.starter_slots then raise exception 'A lineup has % starters at most', s.starter_slots; end if;
  return new;
end $$;
create trigger lineup_rules_trg before insert or update or delete on lineups for each row execute function lineup_rules();

-- Set the whole lineup for the coming week in one step (the only way a manager writes a lineup).
create function set_lineup(p_players bigint[]) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team(); w int := lineup_week(); pid bigint;
begin
  if t is null then raise exception 'You do not own a team'; end if;
  if w is null then raise exception 'Lineups are locked. They reopen Tuesday at 3:00 a.m. ET for nine hours'; end if;
  delete from lineups where team_id = t and week = w;
  foreach pid in array coalesce(p_players, '{}') loop insert into lineups (team_id, week, player_id) values (t, w, pid); end loop;
end $$;

-- At the Tuesday noon lock, a team that set no lineup keeps last week's. A starter who left the roster leaves an empty slot.
create function carry_over_lineups(p_week int) returns int language plpgsql security definer set search_path = public as $$
declare t record; n int := 0;
begin
  perform set_config('app.lineup_bypass', 'on', true);
  for t in select id from teams where not exists (select 1 from lineups l where l.team_id = teams.id and l.week = p_week) loop
    insert into lineups (team_id, week, player_id)
      select l.team_id, p_week, l.player_id from lineups l
       join roster_spots r on r.player_id = l.player_id and r.team_id = l.team_id and r.slot = 'active'
       where l.team_id = t.id and l.week = p_week - 1
         and not exists (select 1 from trade_items i join trades tr on tr.id = i.trade_id
                          where i.player_id = l.player_id and i.from_team = l.team_id and tr.status = 'pending_veto');
    n := n + 1;
  end loop;
  perform set_config('app.lineup_bypass', 'off', true);
  return n;
end $$;
