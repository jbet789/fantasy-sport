-- 001: league settings and the calendar. Every rule below reads its numbers from here, so a change is one row, not a code edit.
-- All times are stored as timestamptz. Week boundaries are computed on US Eastern wall-clock time, so they stay at
-- "Tuesday noon ET" across daylight-saving changes (same behaviour as engine/commissioner.py).

create table league_settings (
  id boolean primary key default true check (id),            -- exactly one row
  season_start date not null default date '2027-01-05',      -- Tuesday; week 1 starts at lock_time that day
  tz text not null default 'America/New_York',
  lock_time time not null default time '12:00',               -- lineups lock; weeks start and end here
  lineup_open_time time not null default time '03:00',        -- lineups reopen on Tuesday at this time
  preseason_open_at timestamptz,                              -- set when the main draft finishes; week-1 lineups open from then
  team_count smallint not null default 12,
  active_limit smallint not null default 20,
  ir_limit smallint not null default 4,
  f1_cap smallint not null default 2,
  starter_slots smallint not null default 10,
  regular_weeks smallint not null default 44,
  total_weeks smallint not null default 51,
  trade_deadline_week smallint not null default 44,
  waiver_dropped interval not null default interval '1 day',
  waiver_new interval not null default interval '2 days',
  readd_block interval not null default interval '7 days',
  veto_window interval not null default interval '24 hours',
  ir_return_window interval not null default interval '3 days',
  correction_window interval not null default interval '24 hours',  -- results are final this long after the week ends
  main_pick_clock int not null default 60,
  bonus_pick_clock int not null default 30
);
insert into league_settings default values;

-- A test clock. Production leaves this table empty. Clients cannot write to it (no policy, no grant).
create table dev_clock (id boolean primary key default true check (id), at timestamptz);

create function app_now() returns timestamptz language sql stable security definer set search_path = public as
  $$ select coalesce((select at from dev_clock), now()) $$;

create function week_start_ts(n int) returns timestamptz language sql stable as $$
  select ((s.season_start + 7 * (n - 1))::timestamp + s.lock_time) at time zone s.tz from league_settings s $$;

create function week_end_ts(n int) returns timestamptz language sql stable as $$ select week_start_ts(n + 1) $$;

-- Results for week n are final 24 hours (wall clock) after the week ends: Wednesday noon ET.
create function final_ts(n int) returns timestamptz language sql stable as $$
  select ((s.season_start + 7 * n)::timestamp + s.lock_time + s.correction_window) at time zone s.tz from league_settings s $$;

-- Scoring week containing a moment; 0 before the season starts.
create function week_of(ts timestamptz) returns int language sql stable as $$
  select case when (ts at time zone s.tz) < (s.season_start::timestamp + s.lock_time) then 0
         else floor(extract(epoch from ((ts at time zone s.tz) - (s.season_start::timestamp + s.lock_time))) / 604800)::int + 1 end
  from league_settings s $$;

create function is_final(n int, ts timestamptz default app_now()) returns boolean language sql stable as
  $$ select ts >= final_ts(n) $$;

-- Tuesday 9:00 a.m. ET, the weekly waiver run.
create function is_waiver_slot(ts timestamptz) returns boolean language sql stable as $$
  select extract(dow from (ts at time zone s.tz)) = 2 and (ts at time zone s.tz)::time = time '09:00' from league_settings s $$;

-- The first 9:00 a.m. ET strictly after a moment.
create function next_nine(ts timestamptz) returns timestamptz language sql stable as $$
  select (case when date_trunc('day', ts at time zone s.tz) + interval '9 hours' > (ts at time zone s.tz)
               then date_trunc('day', ts at time zone s.tz) + interval '9 hours'
               else date_trunc('day', ts at time zone s.tz) + interval '33 hours' end) at time zone s.tz
  from league_settings s $$;

-- Lineups are locked from Tuesday noon until the next Tuesday 3:00 a.m., then open for nine hours.
-- Before the season they are open from the moment the main draft finishes until the first lock.
create function lineup_open(ts timestamptz default app_now()) returns boolean language sql stable as $$
  select (extract(dow from (ts at time zone s.tz)) = 2
          and (ts at time zone s.tz)::time >= s.lineup_open_time and (ts at time zone s.tz)::time < s.lock_time)
      or (s.preseason_open_at is not null and ts >= s.preseason_open_at and ts < week_start_ts(1))
  from league_settings s $$;

-- The week a lineup written right now applies to (the week that starts at the coming noon), or null when locked.
create function lineup_week(ts timestamptz default app_now()) returns int language sql stable as $$
  select case when lineup_open(ts) and week_of(ts) + 1 <= s.total_weeks then week_of(ts) + 1 end from league_settings s $$;
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
-- 003: stats, commissioner corrections, scores, matchups, standings and the injury mulligan.
-- Raw stats freeze for good 24 hours after a week ends (Wednesday noon ET), for everyone, including the jobs.

create function valid_stat(p_player bigint, p_stat text) returns boolean language sql stable as $$
  select p_stat = 'Games played'
      or (pl.sport = 'F1' and p_stat in ('Race finish position', 'Qualifying position', 'Sprint finish position'))
      or exists (select 1 from scoring_weights w where w.profile = profile_of(pl.sport, pl.position) and w.stat = p_stat)
  from players pl where pl.id = p_player $$;

create table stat_lines (
  player_id bigint not null references players,
  week smallint not null check (week between 1 and 51),
  stat text not null,
  value numeric not null,
  primary key (player_id, week, stat)
);

create function stat_line_rules() returns trigger language plpgsql as $$
declare r stat_lines := coalesce(new, old);
begin
  if is_final(r.week) then
    raise exception 'Week % became final % ET; stat changes are ignored', r.week, to_char(final_ts(r.week) at time zone 'America/New_York', 'Dy Mon DD HH12:MI AM');
  end if;
  if tg_op <> 'DELETE' then
    if not valid_stat(new.player_id, new.stat) then
      raise exception '% is not a scored stat for %', new.stat, (select sport || ' ' || position from players where id = new.player_id); end if;
    if new.value < 0 and new.stat not in ('Plus/minus', 'Positions gained') then
      raise exception '% cannot be negative', new.stat; end if;
  end if;
  return coalesce(new, old);
end $$;
create trigger stat_line_rules_trg before insert or update or delete on stat_lines for each row execute function stat_line_rules();

-- Every commissioner correction, kept forever. A revert is a new row that points at the one it undoes.
create table stat_corrections (
  id bigint generated always as identity primary key,
  at timestamptz not null default app_now(),
  actor uuid,
  player_id bigint not null references players,
  week smallint not null,
  stat text not null,
  old_value numeric,
  new_value numeric,
  reason text not null check (length(trim(reason)) > 0),
  reverts_id bigint unique references stat_corrections      -- unique: a correction can be reverted once
);
create trigger stat_corrections_append_only before update or delete on stat_corrections for each row execute function forbid_change();

create function apply_correction(p_player bigint, p_week int, p_stat text, p_value numeric, p_reason text) returns bigint
language plpgsql security definer set search_path = public as $$
declare old numeric; cid bigint;
begin
  if not is_commissioner() then raise exception 'Only the commissioner can correct stats'; end if;
  if is_final(p_week) then
    raise exception 'Week % became final % ET; corrections are ignored', p_week, to_char(final_ts(p_week) at time zone 'America/New_York', 'Dy Mon DD HH12:MI AM'); end if;
  if p_reason is null or length(trim(p_reason)) = 0 then raise exception 'A reason is required'; end if;
  select value into old from stat_lines where player_id = p_player and week = p_week and stat = p_stat;
  insert into stat_lines (player_id, week, stat, value) values (p_player, p_week, p_stat, p_value)
    on conflict (player_id, week, stat) do update set value = excluded.value;
  insert into stat_corrections (actor, player_id, week, stat, old_value, new_value, reason)
    values (auth.uid(), p_player, p_week, p_stat, old, p_value, trim(p_reason)) returning id into cid;
  perform log_activity('stat_correction', format('Stat correction #%s: %s, week %s, %s: %s to %s. %s',
    cid, (select name from players where id = p_player), p_week, p_stat, coalesce(old::text, 'none'), p_value, trim(p_reason)), null,
    jsonb_build_object('correction', cid), true);
  return cid;
end $$;

create function revert_correction(p_id bigint) returns bigint language plpgsql security definer set search_path = public as $$
declare c stat_corrections; rid bigint;
begin
  if not is_commissioner() then raise exception 'Only the commissioner can revert corrections'; end if;
  select * into c from stat_corrections where id = p_id;
  if not found then raise exception 'No correction %', p_id; end if;
  if exists (select 1 from stat_corrections where reverts_id = p_id) then raise exception 'Correction % was already reverted', p_id; end if;
  if c.reverts_id is not null then raise exception 'A revert cannot be reverted'; end if;
  if is_final(c.week) then raise exception 'Week % is final', c.week; end if;
  if c.old_value is null then delete from stat_lines where player_id = c.player_id and week = c.week and stat = c.stat;
  else update stat_lines set value = c.old_value where player_id = c.player_id and week = c.week and stat = c.stat; end if;
  insert into stat_corrections (actor, player_id, week, stat, old_value, new_value, reason, reverts_id)
    values (auth.uid(), c.player_id, c.week, c.stat, c.new_value, c.old_value, 'Reverted correction ' || p_id, p_id) returning id into rid;
  perform log_activity('stat_correction', format('Correction #%s reverted by #%s', p_id, rid), null, jsonb_build_object('correction', rid), true);
  return rid;
end $$;

-- ---- scores (written by the scoring job with the service role; clients only read) ----
create table baselines (
  week smallint not null, sport sport not null, position text not null,
  avg numeric not null, sd numeric not null, pool int not null, fallback boolean not null,
  primary key (week, sport, position), foreign key (sport, position) references positions
);
create table player_scores (
  week smallint not null, player_id bigint not null references players,
  native numeric not null, z numeric, score numeric not null default 0, played boolean not null default false,
  primary key (week, player_id)
);
create table team_weeks (
  team_id smallint not null references teams, week smallint not null,
  starter_points numeric not null default 0, variety_bonus numeric not null default 0, bonus_points numeric not null default 0,
  total numeric not null default 0, best_starter numeric not null default 0,
  primary key (team_id, week)
);
create table matchups (
  id bigint generated always as identity primary key,
  week smallint not null check (week between 1 and 51),
  kind text not null check (kind in ('regular', 'cup-group', 'cup-semi', 'cup-final', 'wildcard', 'playoff-semi', 'playoff-final')),
  team_a smallint not null references teams, team_b smallint not null references teams check (team_a <> team_b),
  score_a numeric, score_b numeric,
  winner smallint references teams,
  unique (week, kind, team_a, team_b)
);
create table cup_groups (team_id smallint primary key references teams, tier smallint not null check (tier between 1 and 4), grp smallint not null check (grp between 1 and 3));
create table cup_bonus_wins (team_id smallint primary key references teams, granted_at timestamptz not null default app_now());   -- the Cup champion's extra win

-- Ranking: wins (including the Cup bonus win), then cumulative points over the 44 regular-season weeks, then team number.
create view standings as
  with res as (
    select team_a as team_id, (winner = team_a)::int as w, (winner = team_b)::int as l from matchups where kind = 'regular' and winner is not null
    union all
    select team_b, (winner = team_b)::int, (winner = team_a)::int from matchups where kind = 'regular' and winner is not null),
  rec as (select team_id, sum(w) as wins, sum(l) as losses from res group by team_id),
  cum as (select team_id, sum(total) as cumulative from team_weeks where week <= 44 group by team_id)
  select t.id as team_id, coalesce(rec.wins, 0) + coalesce(cb.bonus, 0) as wins, coalesce(rec.losses, 0) as losses,
         coalesce(cum.cumulative, 0) as cumulative, coalesce(cb.bonus, 0) as cup_bonus,
         rank() over (order by coalesce(rec.wins, 0) + coalesce(cb.bonus, 0) desc, coalesce(cum.cumulative, 0) desc, t.id) as rank
  from teams t left join rec on rec.team_id = t.id left join cum on cum.team_id = t.id
  left join (select team_id, 1 as bonus from cup_bonus_wins) cb on cb.team_id = t.id;

-- ---- injury mulligan (the Cup runner-up's prize) ----
create table mulligans (
  team_id smallint primary key references teams,
  source text not null default 'cup_runner_up',
  granted_at timestamptz not null default app_now(),
  used_at timestamptz, week smallint, out_player bigint references players, in_player bigint references players
);
create function grant_mulligan(p_team smallint) returns void language plpgsql security definer set search_path = public as $$
begin
  if not (is_commissioner() or auth.uid() is null) then raise exception 'Only the commissioner can grant a mulligan'; end if;
  insert into mulligans (team_id) values (p_team) on conflict do nothing;
  perform log_activity('mulligan_granted', format('Team %s earned the injury mulligan', p_team), p_team, '{}', true);
end $$;

-- After the lock: swap one injured starter for any bench player. The bench player's full-week score replaces his.
create function use_mulligan(p_out bigint, p_in bigint) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team(); w int := week_of(app_now()); m mulligans;
begin
  if t is null then raise exception 'You do not own a team'; end if;
  select * into m from mulligans where team_id = t;
  if not found then raise exception 'Your team does not hold an injury mulligan'; end if;
  if m.used_at is not null then raise exception 'Your mulligan has already been used'; end if;
  if lineup_open() or w < 1 then raise exception 'A mulligan can only be used after the lineup lock'; end if;
  if not exists (select 1 from lineups where team_id = t and week = w and player_id = p_out) then raise exception 'That player is not in your locked lineup'; end if;
  if not is_official_injury((select injury_status from players where id = p_out)) then raise exception 'The starter needs an official injury designation'; end if;
  if not exists (select 1 from roster_spots where team_id = t and player_id = p_in and slot = 'active') then raise exception 'The replacement must be an active player on your roster'; end if;
  if exists (select 1 from lineups where team_id = t and week = w and player_id = p_in) then raise exception 'The replacement must come from your bench'; end if;
  perform set_config('app.lineup_bypass', 'on', true);
  delete from lineups where team_id = t and week = w and player_id = p_out;
  insert into lineups (team_id, week, player_id) values (t, w, p_in);
  perform set_config('app.lineup_bypass', 'off', true);
  update mulligans set used_at = app_now(), week = w, out_player = p_out, in_player = p_in where team_id = t;
  perform log_activity('mulligan_used', format('Team %s used its injury mulligan: %s replaces %s', t,
    (select name from players where id = p_in), (select name from players where id = p_out)), t, jsonb_build_object('week', w));
end $$;
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
-- 005: bonus challenges (outside events like the Iditarod). Teams draft entrants, the commissioner enters finishes, ties share points.

create table bonus_events (
  id bigint generated always as identity primary key,
  name text not null unique,
  ends_at timestamptz not null,                       -- scheduled end; points land in the week containing the end
  hold boolean not null default false,                -- true: no points until the commissioner marks the event concluded
  concluded_at timestamptz
);
create table bonus_entrants (
  id bigint generated always as identity primary key,
  event_id bigint not null references bonus_events on delete cascade,
  name text not null, rank int not null,               -- default order for autopick
  unique (event_id, name)
);
create table bonus_results (
  entrant_id bigint primary key references bonus_entrants on delete cascade,
  status text not null check (status in ('Placed', 'Missed cut', 'Withdrew', 'Did not start', 'Not classified')),
  place int check (place >= 1),
  check ((status = 'Placed') = (place is not null))
);

create function band_points(p_place int) returns numeric language sql stable as
  $$ select coalesce((select points from bonus_bands where start_place <= p_place order by start_place desc limit 1), 0) $$;

-- Ties share the average of the places they cover (engine/bonus.py).
create function bonus_pts(p_status text, p_place int, p_tied int) returns numeric language sql stable as $$
  select case when p_status <> 'Placed' or p_place is null then 0
    else (select sum(band_points(p_place + i)) / greatest(1, p_tied) from generate_series(0, greatest(1, p_tied) - 1) i) end $$;

create function bonus_event_state(e bonus_events) returns text language sql stable as $$
  select case when e.hold and e.concluded_at is null then 'held'
              when e.concluded_at is not null or app_now() >= e.ends_at then 'scored' else 'open' end $$;

create table bonus_picks (        -- filled by the bonus draft
  event_id bigint not null references bonus_events, entrant_id bigint primary key references bonus_entrants,
  team_id smallint not null references teams
);

-- Points for every entrant. A held event shows nothing until it is marked concluded.
create view bonus_scores as
  select e.id as event_id, n.id as entrant_id, n.name, r.status, r.place, bp.team_id,
         case when e.hold and e.concluded_at is null then 0
              else bonus_pts(r.status, r.place, case when r.status = 'Placed' then (select count(*) from bonus_results r2 join bonus_entrants n2 on n2.id = r2.entrant_id
                                                                                       where n2.event_id = e.id and r2.status = 'Placed' and r2.place = r.place)::int else 1 end) end as points,
         coalesce(e.concluded_at, e.ends_at) as scored_at
    from bonus_events e join bonus_entrants n on n.event_id = e.id join bonus_results r on r.entrant_id = n.id
    left join bonus_picks bp on bp.entrant_id = n.id;

create function enter_bonus_results(p_event bigint, p_results jsonb) returns int language plpgsql security definer set search_path = public as $$
declare r jsonb; n int := 0; eid bigint; st text; pl int; w int;
begin
  if not is_commissioner() then raise exception 'Only the commissioner can enter bonus results'; end if;
  select week_of(coalesce(concluded_at, ends_at)) into w from bonus_events where id = p_event;
  if w is not null and w >= 1 and is_final(w) then raise exception 'Week % is final; results can no longer change', w; end if;
  for r in select * from jsonb_array_elements(p_results) loop
    select id into eid from bonus_entrants where event_id = p_event and name = r->>'entrant';
    if eid is null then raise exception 'Unknown entrant %', r->>'entrant'; end if;
    st := r->>'status'; pl := nullif(r->>'place', '')::int;
    if st = 'Placed' and pl is null then raise exception '% needs a place', r->>'entrant'; end if;
    insert into bonus_results (entrant_id, status, place) values (eid, st, case when st = 'Placed' then pl end)
      on conflict (entrant_id) do update set status = excluded.status, place = excluded.place;
    n := n + 1;
  end loop;
  perform log_activity('bonus_results', format('Bonus results for %s: %s entrants entered', (select name from bonus_events where id = p_event), n), null, jsonb_build_object('event', p_event), true);
  return n;
end $$;

create function conclude_bonus_event(p_event bigint) returns void language plpgsql security definer set search_path = public as $$
begin
  if not is_commissioner() then raise exception 'Only the commissioner can conclude an event'; end if;
  if not exists (select 1 from bonus_results r join bonus_entrants n on n.id = r.entrant_id where n.event_id = p_event) then raise exception 'Enter results first'; end if;
  update bonus_events set concluded_at = app_now() where id = p_event and concluded_at is null;
  perform log_activity('bonus_concluded', format('%s concluded; held points released', (select name from bonus_events where id = p_event)), null, jsonb_build_object('event', p_event), true);
end $$;

-- Draft order for a bonus draft: worst record first, then fewer cumulative points, then lower round-1 draft slot.
create function bonus_draft_order() returns smallint[] language sql stable as $$
  select array_agg(t.id order by s.wins, s.cumulative, t.draft_slot) from teams t join standings s on s.team_id = t.id $$;
-- 006: drafts. The main draft (20 rounds, snake) and bonus drafts share one engine. The clock is enforced by the database:
-- advance_if_due() makes the autopick for anyone whose time ran out, safe to call from any number of places.

create table drafts (
  id bigint generated always as identity primary key,
  kind text not null check (kind in ('main', 'bonus')),
  event_id bigint references bonus_events,
  rounds int not null check (rounds >= 1),
  pick_seconds int not null,
  status text not null default 'scheduled' check (status in ('scheduled', 'live', 'done')),
  current_pick int not null default 1,
  deadline timestamptz,
  started_at timestamptz,
  check ((kind = 'bonus') = (event_id is not null))
);
create table draft_slots (draft_id bigint not null references drafts on delete cascade, pick_no int not null, round int not null, team_id smallint not null references teams, primary key (draft_id, pick_no));
create table draft_picks (
  draft_id bigint not null references drafts on delete cascade, pick_no int not null,
  team_id smallint not null references teams,
  player_id bigint references players, entrant_id bigint references bonus_entrants,
  auto boolean not null default false, at timestamptz not null default app_now(),
  primary key (draft_id, pick_no),
  check ((player_id is null) <> (entrant_id is null))
);
create unique index draft_one_player on draft_picks (draft_id, player_id) where player_id is not null;
create unique index draft_one_entrant on draft_picks (draft_id, entrant_id) where entrant_id is not null;
create table draft_queue (       -- a team's wishlist; autopick uses it first
  draft_id bigint not null references drafts on delete cascade, team_id smallint not null references teams,
  player_id bigint references players, entrant_id bigint references bonus_entrants, rank int not null,
  primary key (draft_id, team_id, rank), check ((player_id is null) <> (entrant_id is null))
);

create function create_draft(p_kind text, p_rounds int, p_order smallint[] default null, p_event bigint default null) returns bigint
language plpgsql security definer set search_path = public as $$
declare d bigint; s league_settings; o smallint[]; r int; i int;
begin
  if auth.uid() is not null and not is_commissioner() then raise exception 'Only the commissioner can create a draft'; end if;
  select * into s from league_settings;
  o := coalesce(p_order, case when p_kind = 'main' then (select array_agg(id order by draft_slot) from teams) else bonus_draft_order() end);
  if array_length(o, 1) <> s.team_count then raise exception 'Draft order must list all % teams', s.team_count; end if;
  insert into drafts (kind, event_id, rounds, pick_seconds) values (p_kind, p_event, p_rounds, case when p_kind = 'main' then s.main_pick_clock else s.bonus_pick_clock end) returning id into d;
  for r in 1..p_rounds loop
    for i in 1..s.team_count loop
      insert into draft_slots values (d, (r - 1) * s.team_count + i, r, case when r % 2 = 1 then o[i] else o[s.team_count + 1 - i] end);
    end loop;
  end loop;
  return d;
end $$;

create function start_draft(p_id bigint) returns void language plpgsql security definer set search_path = public as $$
begin
  if auth.uid() is not null and not is_commissioner() then raise exception 'Only the commissioner can start the draft'; end if;
  update drafts set status = 'live', started_at = app_now(), deadline = app_now() + make_interval(secs => pick_seconds) where id = p_id and status = 'scheduled';
  if not found then raise exception 'Draft % cannot be started', p_id; end if;
end $$;

-- Is this player legal for the team in the main draft? Null when yes.
create function draft_problem(p_draft bigint, p_team smallint, p_player bigint) returns text language plpgsql stable as $$
declare d drafts; s league_settings; n int; f1 int; sp sport;
begin
  select * into d from drafts where id = p_draft; select * into s from league_settings;
  if exists (select 1 from draft_picks where draft_id = p_draft and player_id = p_player) or exists (select 1 from roster_spots where player_id = p_player) then return 'That player has already been drafted'; end if;
  select count(*), count(*) filter (where pl.sport = 'F1') into n, f1 from roster_spots r join players pl on pl.id = r.player_id where r.team_id = p_team;
  if n >= d.rounds then return 'Roster is full for the draft'; end if;
  select sport into sp from players where id = p_player;
  if sp = 'F1' and f1 >= s.f1_cap then return format('At most %s F1 drivers per team', s.f1_cap); end if;
  return null;
end $$;

create function record_pick(p_draft bigint, p_item bigint, p_auto boolean, p_next_deadline timestamptz) returns void language plpgsql as $$
declare d drafts; sl draft_slots;
begin
  select * into d from drafts where id = p_draft for update;
  select * into sl from draft_slots where draft_id = p_draft and pick_no = d.current_pick;
  if d.kind = 'main' then
    insert into roster_spots (team_id, player_id, acquired_at) values (sl.team_id, p_item, app_now());
    insert into draft_picks (draft_id, pick_no, team_id, player_id, auto) values (p_draft, d.current_pick, sl.team_id, p_item, p_auto);
  else
    insert into draft_picks (draft_id, pick_no, team_id, entrant_id, auto) values (p_draft, d.current_pick, sl.team_id, p_item, p_auto);
    insert into bonus_picks (event_id, entrant_id, team_id) values (d.event_id, p_item, sl.team_id);
  end if;
  if d.current_pick >= (select max(pick_no) from draft_slots where draft_id = p_draft) then
    update drafts set status = 'done', deadline = null where id = p_draft;
    if d.kind = 'main' then update league_settings set preseason_open_at = coalesce(preseason_open_at, app_now()); end if;   -- week-1 lineups open
  else
    update drafts set current_pick = current_pick + 1, deadline = p_next_deadline where id = p_draft;
  end if;
end $$;

create function make_pick(p_draft bigint, p_item bigint) returns void language plpgsql security definer set search_path = public as $$
declare d drafts; sl draft_slots; msg text;
begin
  perform advance_if_due(p_draft);                         -- a pick that arrives after the clock ran out loses to the autopick
  select * into d from drafts where id = p_draft for update;
  if d.status <> 'live' then raise exception 'The draft is %', d.status; end if;
  select * into sl from draft_slots where draft_id = p_draft and pick_no = d.current_pick;
  if sl.team_id is distinct from my_team() then raise exception 'It is not your turn'; end if;
  if d.kind = 'main' then
    msg := draft_problem(p_draft, sl.team_id, p_item);
    if msg is not null then raise exception '%', msg; end if;
  elsif not exists (select 1 from bonus_entrants where id = p_item and event_id = d.event_id) or exists (select 1 from draft_picks where draft_id = p_draft and entrant_id = p_item) then
    raise exception 'That entrant is not available';
  end if;
  perform record_pick(p_draft, p_item, false, app_now() + make_interval(secs => d.pick_seconds));
end $$;

create function queue_set(p_draft bigint, p_items bigint[]) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team(); k text;
begin
  if t is null then raise exception 'You do not own a team'; end if;
  if coalesce(array_length(p_items, 1), 0) > 100 then raise exception 'A queue can hold at most 100 players'; end if;
  if (select count(distinct x) from unnest(p_items) x) <> coalesce(array_length(p_items, 1), 0) then raise exception 'The queue lists the same player twice'; end if;
  select kind into k from drafts where id = p_draft;
  delete from draft_queue where draft_id = p_draft and team_id = t;
  if k = 'main' then insert into draft_queue (draft_id, team_id, player_id, rank) select p_draft, t, p, n from unnest(p_items) with ordinality x(p, n);
  else insert into draft_queue (draft_id, team_id, entrant_id, rank) select p_draft, t, p, n from unnest(p_items) with ordinality x(p, n); end if;
end $$;

-- Autopick for anyone whose clock ran out. Idempotent: repeated or concurrent calls make each pick exactly once.
-- After a delay it catches up pick by pick, each deadline chained from the last (a missed hour is not a free hour).
create function run_draft_clock(p_draft bigint, p_now timestamptz) returns int language plpgsql security definer set search_path = public as $$
declare d drafts; sl draft_slots; item bigint; made int := 0;
begin
  loop
    select * into d from drafts where id = p_draft for update;
    exit when not found or d.status <> 'live' or d.deadline > p_now;
    select * into sl from draft_slots where draft_id = p_draft and pick_no = d.current_pick;
    if d.kind = 'main' then
      select q.player_id into item from draft_queue q where q.draft_id = p_draft and q.team_id = sl.team_id and draft_problem(p_draft, sl.team_id, q.player_id) is null order by q.rank limit 1;
      if item is null then
        select pl.id into item from players pl where pl.active and draft_problem(p_draft, sl.team_id, pl.id) is null order by pl.ranking nulls last, pl.id limit 1;
      end if;
    else
      select q.entrant_id into item from draft_queue q where q.draft_id = p_draft and q.team_id = sl.team_id
         and not exists (select 1 from draft_picks x where x.draft_id = p_draft and x.entrant_id = q.entrant_id) order by q.rank limit 1;
      if item is null then
        select n.id into item from bonus_entrants n where n.event_id = d.event_id
           and not exists (select 1 from draft_picks x where x.draft_id = p_draft and x.entrant_id = n.id) order by n.rank, n.id limit 1;
      end if;
    end if;
    if item is null then raise exception 'No legal player is left for team %', sl.team_id; end if;
    perform record_pick(p_draft, item, true, d.deadline + make_interval(secs => d.pick_seconds));
    made := made + 1;
  end loop;
  return made;
end $$;

-- What clients call: always uses the server clock, so nobody can push the draft forward with a made-up time.
create function advance_if_due(p_draft bigint) returns int language sql security definer set search_path = public as $$ select run_draft_clock(p_draft, app_now()) $$;
-- 007: what the lineup and matchup screens need: IR moves, and one read call per screen (security invoker, so row-level security applies).

create function move_to_ir(p_player bigint) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team();
begin
  if t is null then raise exception 'You do not own a team'; end if;
  update roster_spots set slot = 'ir' where team_id = t and player_id = p_player and slot = 'active';
  if not found then raise exception 'That is not an active player on your roster'; end if;
end $$;

create function return_from_ir(p_player bigint) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team();
begin
  if t is null then raise exception 'You do not own a team'; end if;
  update roster_spots set slot = 'active', ir_cleared_at = null where team_id = t and player_id = p_player and slot = 'ir';
  if not found then raise exception 'That player is not on your injured reserve'; end if;
end $$;

-- Everything the lineup screen shows for one team. Lineups are public: anyone can see any team's, at any time.
create function screen_roster(p_team smallint default null) returns jsonb language plpgsql stable as $$
declare t smallint := coalesce(p_team, my_team()); cur int := week_of(app_now()); w int := coalesce(lineup_week(), week_of(app_now())); s league_settings;
  open_ boolean := lineup_open(); opens timestamptz; locks timestamptz;
begin
  select * into s from league_settings;
  locks := week_end_ts(cur);
  opens := ((((locks at time zone s.tz)::date) + s.lineup_open_time) at time zone s.tz);
  return jsonb_build_object(
    'now', app_now(), 'week', cur, 'lineup_week', w, 'open', open_, 'opens_at', opens, 'locks_at', locks,
    'me', my_team(), 'team', (select jsonb_build_object('id', id, 'name', name) from teams where id = t),
    'teams', (select jsonb_agg(jsonb_build_object('id', id, 'name', name) order by id) from teams),
    'limits', jsonb_build_object('active', s.active_limit, 'ir', s.ir_limit, 'starters', s.starter_slots, 'f1', s.f1_cap),
    'mulligan', (select jsonb_build_object('used', m.used_at is not null) from mulligans m where m.team_id = t),
    'players', coalesce((
      select jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position, 'injury', p.injury_status,
               'slot', r.slot, 'starter', exists (select 1 from lineups l where l.team_id = t and l.week = w and l.player_id = p.id),
               'official', is_official_injury(p.injury_status), 'last', (select ps.score from player_scores ps where ps.player_id = p.id and ps.week = w - 1 and ps.played))
             order by p.name)
        from roster_spots r join players p on p.id = r.player_id where r.team_id = t), '[]'::jsonb));
end $$;

-- The matchup for one week for the caller's team, with both sides' starters and scores.
create function screen_matchup(p_week int default null) returns jsonb language plpgsql stable as $$
declare w int := coalesce(p_week, week_of(app_now())); t smallint := my_team(); m matchups; opp smallint;
begin
  select * into m from matchups where week = w and (team_a = t or team_b = t) order by id limit 1;
  if not found then return jsonb_build_object('week', w, 'none', true); end if;
  opp := case when m.team_a = t then m.team_b else m.team_a end;
  return jsonb_build_object('week', w, 'kind', m.kind, 'final', is_final(w), 'winner', m.winner,
    'sides', (select jsonb_agg(side order by ord) from (
      select 1 as ord, screen_side(t, w) as side union all select 2, screen_side(opp, w)) x));
end $$;

create function screen_side(p_team smallint, p_week int) returns jsonb language sql stable as $$
  select jsonb_build_object('team', p_team, 'name', (select name from teams where id = p_team),
    'starter_points', coalesce(tw.starter_points, 0), 'variety', coalesce(tw.variety_bonus, 0), 'bonus', coalesce(tw.bonus_points, 0),
    'total', coalesce(tw.total, 0), 'best', coalesce(tw.best_starter, 0),
    'rows', coalesce((select jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position, 'injury', p.injury_status,
                 'played', coalesce(ps.played, false), 'score', ps.score) order by p.name)
        from lineups l join players p on p.id = l.player_id left join player_scores ps on ps.player_id = p.id and ps.week = p_week
       where l.team_id = p_team and l.week = p_week), '[]'::jsonb))
  from (select 1) d left join team_weeks tw on tw.team_id = p_team and tw.week = p_week $$;

-- ---- players, waivers and trades screens ----
-- Security definer because they read the caller's own re-add blocks and claims; each one only returns league-public data plus the caller's own.

create function screen_market(p_sport text default null, p_search text default null, p_limit int default 60) returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare t smallint := my_team(); now_ timestamptz := app_now(); s league_settings; w int := coalesce(lineup_week(), week_of(app_now()));
begin
  select * into s from league_settings;
  return jsonb_build_object(
    'now', now_, 'me', t, 'open', lineup_open(),
    'limits', jsonb_build_object('active', s.active_limit, 'f1', s.f1_cap),
    'next_run', (select min(ts) from (
        select ((((now_ at time zone s.tz)::date + i) + time '09:00') at time zone s.tz) as ts from generate_series(0, 7) i) x
        where is_waiver_slot(ts) and ts > now_),
    'roster', coalesce((select jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position, 'slot', r.slot, 'injury', p.injury_status,
          'in_lineup', exists (select 1 from lineups l where l.team_id = t and l.week = w and l.player_id = p.id),
          'locked', is_locked_starter(t, p.id, now_)) order by p.name)
        from roster_spots r join players p on p.id = r.player_id where r.team_id = t), '[]'::jsonb),
    'players', coalesce((select jsonb_agg(to_jsonb(q)) from (
        select p.id, p.name, p.sport, p.position, p.injury_status as injury,
               (select ps.score from player_scores ps where ps.player_id = p.id and ps.played order by ps.week desc limit 1) as recent,
               w2.release_at, w2.kind, (w2.player_id is not null) as on_waivers, coalesce(is_waiver_slot(w2.release_at), false) as claim_slot,
               is_free_agent(p.id, now_) as free, rb.until as blocked_until, c.id as claim_id
          from players p
          left join waivers w2 on w2.player_id = p.id
          left join readd_blocks rb on rb.player_id = p.id and rb.team_id = t and rb.until > now_
          left join waiver_claims c on c.player_id = p.id and c.team_id = t and c.status = 'pending'
         where p.active and not exists (select 1 from roster_spots r where r.player_id = p.id)
           and (p_sport is null or p.sport::text = p_sport) and (p_search is null or p.name ilike '%' || p_search || '%')
         order by (select ps.score from player_scores ps where ps.player_id = p.id and ps.played order by ps.week desc limit 1) desc nulls last, p.name
         limit least(greatest(p_limit, 1), 200)) q), '[]'::jsonb),
    'claims', coalesce((select jsonb_agg(jsonb_build_object('id', c.id, 'player_id', c.player_id, 'name', p.name, 'sport', p.sport, 'position', p.position,
          'drop_id', c.drop_player_id, 'drop_name', d.name, 'rank', c.rank, 'run_at', c.run_at, 'status', c.status, 'reason', c.reason)
          order by (c.status = 'pending') desc, c.rank, c.id)
        from waiver_claims c join players p on p.id = c.player_id left join players d on d.id = c.drop_player_id
       where c.team_id = t and (c.status = 'pending' or c.resolved_at > now_ - interval '14 days')), '[]'::jsonb));
end $$;

create function screen_trades() returns jsonb language plpgsql stable security definer set search_path = public as $$
declare t smallint := my_team();
begin
  return jsonb_build_object('now', app_now(), 'me', t, 'deadline', trade_deadline(), 'open', app_now() < trade_deadline(),
    'trades', coalesce((select jsonb_agg(jsonb_build_object('id', x.id, 'status', x.status, 'proposer', x.proposer, 'partner', x.partner,
        'proposer_name', (select name from teams where id = x.proposer), 'partner_name', (select name from teams where id = x.partner),
        'proposed_at', x.proposed_at, 'accepted_at', x.accepted_at, 'veto_until', x.veto_until, 'note', x.note,
        'give', (select coalesce(jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position) order by p.name), '[]') from trade_items i join players p on p.id = i.player_id where i.trade_id = x.id and i.from_team = x.proposer),
        'get', (select coalesce(jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position) order by p.name), '[]') from trade_items i join players p on p.id = i.player_id where i.trade_id = x.id and i.from_team = x.partner),
        'drops', (select coalesce(jsonb_agg(jsonb_build_object('team', d.team_id, 'name', p.name)), '[]') from trade_drops d join players p on p.id = d.player_id where d.trade_id = x.id))
        order by x.id desc)
      from (select * from trades where proposer = t or partner = t or status = 'completed' order by id desc limit 40) x), '[]'::jsonb));
end $$;

create function screen_activity(p_limit int default 50) returns jsonb language sql stable as $$
  select coalesce(jsonb_agg(jsonb_build_object('id', id, 'at', at, 'kind', kind, 'team', team_id, 'message', message, 'commissioner', commissioner) order by id desc), '[]'::jsonb)
  from (select * from activity_log order by id desc limit least(greatest(p_limit, 1), 200)) a $$;

-- League standings, one week's games, the Cup, the playoff bracket and the caller's own schedule.
-- A game that has no final score yet shows the team's live week total.
create function screen_standings(p_week int default null) returns jsonb language plpgsql stable security definer set search_path = public as $$
declare cur int := week_of(app_now()); w int := coalesce(p_week, greatest(1, least(cur, 51))); me smallint := my_team();
begin
  return jsonb_build_object('now', app_now(), 'week', cur, 'shown', w, 'me', me,
    'standings', coalesce((select jsonb_agg(jsonb_build_object('rank', s.rank, 'team_id', s.team_id, 'name', t.name, 'wins', s.wins, 'losses', s.losses,
        'points', s.cumulative, 'cup_bonus', s.cup_bonus, 'seed', s.seed) order by s.rank, s.team_id)
      from (select x.*, case when x.rank <= 4 then x.rank::int
                             when row_number() over (partition by (x.rank <= 4) order by x.cumulative desc, x.wins desc, x.team_id) <= 2 then 4 + (row_number() over (partition by (x.rank <= 4) order by x.cumulative desc, x.wins desc, x.team_id))::int end as seed
              from standings x) s join teams t on t.id = s.team_id), '[]'::jsonb),
    'games', coalesce((select jsonb_agg(g order by g.id) from (
        select m.id, m.week, m.kind, m.team_a, m.team_b, (select name from teams where id = m.team_a) as name_a, (select name from teams where id = m.team_b) as name_b,
               coalesce(m.score_a, (select total from team_weeks where team_id = m.team_a and week = m.week)) as score_a,
               coalesce(m.score_b, (select total from team_weeks where team_id = m.team_b and week = m.week)) as score_b,
               m.winner, is_final(m.week) as final
          from matchups m where m.week = w) g), '[]'::jsonb),
    'mine', coalesce((select jsonb_agg(g order by g.week) from (
        select m.week, m.kind, case when m.team_a = me then m.team_b else m.team_a end as opp,
               (select name from teams where id = case when m.team_a = me then m.team_b else m.team_a end) as opp_name,
               coalesce(case when m.team_a = me then m.score_a else m.score_b end, (select total from team_weeks where team_id = me and week = m.week)) as score,
               coalesce(case when m.team_a = me then m.score_b else m.score_a end, (select total from team_weeks where team_id = case when m.team_a = me then m.team_b else m.team_a end and week = m.week)) as opp_score,
               m.winner, is_final(m.week) as final
          from matchups m where me in (m.team_a, m.team_b)) g), '[]'::jsonb),
    'cup', jsonb_build_object(
      'groups', coalesce((select jsonb_agg(jsonb_build_object('tier', x.tier, 'grp', x.grp, 'teams', x.teams) order by x.tier, x.grp) from (
          select c.tier, c.grp, jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name,
              'wins', (select count(*) from matchups m where m.kind = 'cup-group' and m.winner = t.id),
              'losses', (select count(*) from matchups m where m.kind = 'cup-group' and m.winner is not null and m.winner <> t.id and t.id in (m.team_a, m.team_b)),
              'points', (select coalesce(sum(case when m.team_a = t.id then coalesce(m.score_a, 0) else coalesce(m.score_b, 0) end), 0) from matchups m where m.kind = 'cup-group' and t.id in (m.team_a, m.team_b)))
              order by (select count(*) from matchups m where m.kind = 'cup-group' and m.winner = t.id) desc, t.id) as teams
            from cup_groups c join teams t on t.id = c.team_id group by c.tier, c.grp) x), '[]'::jsonb),
      'knockout', coalesce((select jsonb_agg(jsonb_build_object('id', m.id, 'week', m.week, 'kind', m.kind, 'team_a', m.team_a, 'team_b', m.team_b, 'name_a', (select name from teams where id = m.team_a),
          'name_b', (select name from teams where id = m.team_b), 'winner', m.winner,
          'score_a', coalesce(m.score_a, (select total from team_weeks where team_id = m.team_a and week = m.week)),
          'score_b', coalesce(m.score_b, (select total from team_weeks where team_id = m.team_b and week = m.week))) order by m.week, m.id)
        from matchups m where m.kind in ('cup-semi', 'cup-final')), '[]'::jsonb),
      'champion_bonus', (select team_id from cup_bonus_wins limit 1)),
    'playoffs', coalesce((select jsonb_agg(jsonb_build_object('id', m.id, 'week', m.week, 'kind', m.kind, 'team_a', m.team_a, 'team_b', m.team_b,
          'name_a', (select name from teams where id = m.team_a), 'name_b', (select name from teams where id = m.team_b), 'winner', m.winner,
          'score_a', coalesce(m.score_a, (select total from team_weeks where team_id = m.team_a and week = m.week)),
          'score_b', coalesce(m.score_b, (select total from team_weeks where team_id = m.team_b and week = m.week))) order by m.week, m.id)
        from matchups m where m.kind in ('wildcard', 'playoff-semi', 'playoff-final')), '[]'::jsonb));
end $$;

-- The draft room for one draft (default: the live one, else the next scheduled, else the latest). Opening it also runs the clock,
-- so an expired pick is autopicked even if nobody else is looking. Works for the main draft (players) and bonus drafts (entrants).
create function screen_draft(p_draft bigint default null, p_sport text default null, p_search text default null, p_limit int default 60) returns jsonb
language plpgsql security definer set search_path = public as $$
declare d drafts; me smallint := my_team(); cur smallint; n int;
begin
  select * into d from drafts where id = coalesce(p_draft, (select id from drafts order by (status = 'live') desc, (status = 'scheduled') desc, id desc limit 1));
  if not found then return jsonb_build_object('now', app_now(), 'none', true, 'me', me); end if;
  if d.status = 'live' then perform advance_if_due(d.id); select * into d from drafts where id = d.id; end if;
  select team_id into cur from draft_slots where draft_id = d.id and pick_no = d.current_pick and d.status = 'live';
  select count(*) into n from draft_slots where draft_id = d.id;
  return jsonb_build_object('now', app_now(), 'me', me, 'on_clock', cur,
    'draft', jsonb_build_object('id', d.id, 'kind', d.kind, 'status', d.status, 'rounds', d.rounds, 'pick_seconds', d.pick_seconds,
       'current_pick', d.current_pick, 'deadline', d.deadline, 'total', n, 'teams', (select count(distinct team_id) from draft_slots where draft_id = d.id)),
    'teams', (select jsonb_agg(jsonb_build_object('id', id, 'name', name) order by id) from teams),
    'drafts', (select jsonb_agg(jsonb_build_object('id', x.id, 'label', case when x.kind = 'main' then 'Main draft' else coalesce(e.name, 'Bonus draft') end, 'status', x.status) order by x.id) from drafts x left join bonus_events e on e.id = x.event_id),
    'slots', (select jsonb_agg(jsonb_build_array(pick_no, team_id) order by pick_no) from draft_slots where draft_id = d.id),
    'picks', coalesce((select jsonb_agg(jsonb_build_object('pick_no', k.pick_no, 'team_id', k.team_id, 'auto', k.auto, 'at', k.at,
          'name', coalesce(pl.name, en.name), 'sport', pl.sport, 'position', pl.position) order by k.pick_no)
        from draft_picks k left join players pl on pl.id = k.player_id left join bonus_entrants en on en.id = k.entrant_id where k.draft_id = d.id), '[]'::jsonb),
    'available', coalesce((select jsonb_agg(a) from (
        select pl.id, pl.name, pl.sport, pl.position, pl.ranking as rank, case when me is not null and d.status = 'live' then draft_problem(d.id, me, pl.id) end as problem
          from players pl where d.kind = 'main' and pl.active
           and not exists (select 1 from draft_picks k where k.draft_id = d.id and k.player_id = pl.id) and not exists (select 1 from roster_spots r where r.player_id = pl.id)
           and (p_sport is null or pl.sport::text = p_sport) and (p_search is null or pl.name ilike '%' || p_search || '%')
         order by pl.ranking nulls last, pl.id limit least(greatest(p_limit, 1), 200)) a), '[]'::jsonb)
      || coalesce((select jsonb_agg(a) from (
        select en.id, en.name, null::text as sport, null::text as position, en.rank, null::text as problem
          from bonus_entrants en where d.kind = 'bonus' and en.event_id = d.event_id
           and not exists (select 1 from draft_picks k where k.draft_id = d.id and k.entrant_id = en.id)
           and (p_search is null or en.name ilike '%' || p_search || '%')
         order by en.rank, en.id limit least(greatest(p_limit, 1), 200)) a), '[]'::jsonb),
    'queue', coalesce((select jsonb_agg(jsonb_build_object('id', coalesce(q.player_id, q.entrant_id), 'name', coalesce(pl.name, en.name), 'sport', pl.sport, 'position', pl.position,
          'taken', exists (select 1 from draft_picks k where k.draft_id = d.id and (k.player_id = q.player_id or k.entrant_id = q.entrant_id))) order by q.rank)
        from draft_queue q left join players pl on pl.id = q.player_id left join bonus_entrants en on en.id = q.entrant_id where q.draft_id = d.id and q.team_id = me), '[]'::jsonb));
end $$;


-- Bonus events for everyone. With no argument: the list (state, draft, your points). With an event: its field, results and every team's picks.
create function screen_bonus(p_event bigint default null) returns jsonb language plpgsql stable security definer set search_path = public as $$
declare me smallint := my_team();
begin
  return jsonb_build_object('now', app_now(), 'me', me,
    'events', coalesce((select jsonb_agg(jsonb_build_object('id', e.id, 'name', e.name, 'ends_at', e.ends_at, 'hold', e.hold, 'state', bonus_event_state(e), 'concluded_at', e.concluded_at,
        'draft', (select jsonb_build_object('id', d.id, 'status', d.status, 'rounds', d.rounds) from drafts d where d.event_id = e.id),
        'entrants', (select count(*) from bonus_entrants n where n.event_id = e.id),
        'my_picks', (select count(*) from bonus_picks bp where bp.event_id = e.id and bp.team_id = me),
        'my_points', (select coalesce(sum(s.points), 0) from bonus_scores s where s.event_id = e.id and s.team_id = me)) order by e.ends_at, e.id) from bonus_events e), '[]'::jsonb),
    'detail', case when p_event is null then null else jsonb_build_object('event', p_event,
      'entrants', coalesce((select jsonb_agg(jsonb_build_object('id', n.id, 'name', n.name, 'rank', n.rank, 'status', r.status, 'place', r.place, 'points', coalesce(s.points, 0),
          'team_id', bp.team_id, 'team', (select name from teams where id = bp.team_id)) order by r.place nulls last, n.rank)
        from bonus_entrants n left join bonus_results r on r.entrant_id = n.id left join bonus_picks bp on bp.entrant_id = n.id left join bonus_scores s on s.entrant_id = n.id
        where n.event_id = p_event), '[]'::jsonb),
      'teams', coalesce((select jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name, 'points', coalesce((select sum(s.points) from bonus_scores s where s.event_id = p_event and s.team_id = t.id), 0)) order by 3 desc, t.id) from teams t), '[]'::jsonb)) end);
end $$;
-- Commissioner screens. Each function checks the role itself; the actions (veto_trade, apply_correction,
-- revert_correction, grant_mulligan) were defined earlier and keep their own checks.

create function screen_admin() returns jsonb language plpgsql stable security definer set search_path = public as $$
declare cur int := week_of(app_now());
begin
  if not is_commissioner() then return jsonb_build_object('commissioner', false); end if;
  return jsonb_build_object('commissioner', true, 'now', app_now(), 'week', cur,
    'drafts', coalesce((select jsonb_agg(jsonb_build_object('id', d.id, 'kind', d.kind, 'status', d.status, 'rounds', d.rounds, 'event', e.name) order by d.id)
        from drafts d left join bonus_events e on e.id = d.event_id), '[]'::jsonb),
    'events', coalesce((select jsonb_agg(jsonb_build_object('id', e.id, 'name', e.name, 'ends_at', e.ends_at, 'hold', e.hold, 'state', bonus_event_state(e),
        'entrants', (select count(*) from bonus_entrants n where n.event_id = e.id), 'results', (select count(*) from bonus_results r join bonus_entrants n on n.id = r.entrant_id where n.event_id = e.id),
        'draft', (select jsonb_build_object('id', d.id, 'status', d.status) from drafts d where d.event_id = e.id)) order by e.ends_at, e.id) from bonus_events e), '[]'::jsonb),
    'teams', (select jsonb_agg(jsonb_build_object('id', id, 'name', name, 'mulligan', exists (select 1 from mulligans m where m.team_id = teams.id)) order by id) from teams),
    'veto', coalesce((select jsonb_agg(jsonb_build_object('id', x.id, 'veto_until', x.veto_until, 'accepted_at', x.accepted_at,
        'proposer_name', (select name from teams where id = x.proposer), 'partner_name', (select name from teams where id = x.partner),
        'give', (select coalesce(jsonb_agg(jsonb_build_object('name', p.name, 'sport', p.sport, 'position', p.position) order by p.name), '[]') from trade_items i join players p on p.id = i.player_id where i.trade_id = x.id and i.from_team = x.proposer),
        'get', (select coalesce(jsonb_agg(jsonb_build_object('name', p.name, 'sport', p.sport, 'position', p.position) order by p.name), '[]') from trade_items i join players p on p.id = i.player_id where i.trade_id = x.id and i.from_team = x.partner),
        'drops', (select coalesce(jsonb_agg(jsonb_build_object('team', d.team_id, 'name', p.name)), '[]') from trade_drops d join players p on p.id = d.player_id where d.trade_id = x.id))
        order by x.veto_until)
      from trades x where x.status = 'pending_veto'), '[]'::jsonb),
    'corrections', coalesce((select jsonb_agg(jsonb_build_object('id', c.id, 'at', c.at, 'player', p.name, 'week', c.week, 'stat', c.stat,
        'old', c.old_value, 'new', c.new_value, 'reason', c.reason, 'is_revert', c.reverts_id is not null,
        'reverted', exists (select 1 from stat_corrections r where r.reverts_id = c.id),
        'final', is_final(c.week)) order by c.id desc)
      from (select * from stat_corrections order by id desc limit 40) c join players p on p.id = c.player_id), '[]'::jsonb));
end $$;

-- Find players and show their stat lines for one week, so a correction starts from the number on file.
create function admin_player_stats(p_search text, p_week int) returns jsonb language plpgsql stable security definer set search_path = public as $$
begin
  if not is_commissioner() then raise exception 'Only the commissioner can use this'; end if;
  return jsonb_build_object('week', p_week, 'final', is_final(p_week), 'players', coalesce((
    select jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position,
      'stats', coalesce((select jsonb_object_agg(s.stat, s.value) from stat_lines s where s.player_id = p.id and s.week = p_week), '{}'::jsonb)) order by p.name)
    from (select * from players where name ilike '%' || coalesce(p_search, '') || '%' order by name limit 10) p), '[]'::jsonb));
end $$;

-- ---- bonus events: the commissioner creates one with its field, runs its draft, enters results ----
-- Entrants are listed best-known first; that order is what autopick uses.
create function create_bonus_event(p_name text, p_ends_at timestamptz, p_hold boolean, p_entrants text[]) returns bigint
language plpgsql security definer set search_path = public as $$
declare e bigint; s league_settings; names text[]; n int;
begin
  if not is_commissioner() then raise exception 'Only the commissioner can create a bonus event'; end if;
  select * into s from league_settings;
  select array_agg(trim(x)) filter (where trim(x) <> '') into names from unnest(p_entrants) x;
  n := coalesce(array_length(names, 1), 0);
  if coalesce(trim(p_name), '') = '' then raise exception 'The event needs a name'; end if;
  if n < s.team_count then raise exception 'An event needs at least % entrants (one per team); got %', s.team_count, n; end if;
  if (select count(distinct lower(x)) from unnest(names) x) <> n then raise exception 'Entrant names must be different'; end if;
  insert into bonus_events (name, ends_at, hold) values (trim(p_name), p_ends_at, coalesce(p_hold, false)) returning id into e;
  insert into bonus_entrants (event_id, name, rank) select e, x, o from unnest(names) with ordinality t(x, o);
  perform log_activity('bonus_created', format('Bonus event added: %s (%s entrants)', trim(p_name), n), null, jsonb_build_object('event', e), true);
  return e;
end $$;

create function create_bonus_draft(p_event bigint, p_rounds int default 2) returns bigint language plpgsql security definer set search_path = public as $$
declare s league_settings; n int;
begin
  if not is_commissioner() then raise exception 'Only the commissioner can create a draft'; end if;
  select * into s from league_settings;
  select count(*) into n from bonus_entrants where event_id = p_event;
  if not found or n = 0 then raise exception 'No such event'; end if;
  if exists (select 1 from drafts where event_id = p_event) then raise exception 'This event already has a draft'; end if;
  if p_rounds < 1 or p_rounds * s.team_count > n then raise exception '% rounds need at least % entrants; the field has %', p_rounds, p_rounds * s.team_count, n; end if;
  return create_draft('bonus', p_rounds, null, p_event);
end $$;
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
-- 010: one-time league setup, run by the commissioner in the Supabase SQL editor (never callable from the app).
--
--   select setup_league('[
--     {"email": "commish@example.com", "name": "Commissioner", "commissioner": true},
--     {"email": "ann@example.com",  "name": "Ann",  "team": "Ann''s Aces"},
--     ... eleven more managers ...
--   ]'::jsonb, false);
--
-- Every email must already exist under Authentication > Users. The commissioner may also own a team (give "team" too) or not.
-- Teams are numbered 1..12 in the order listed. Draft order is the same order unless p_shuffle is true (a random order).
-- Safe to run again with the same list; it fixes names, but refuses to change owners once the draft has started.
create function setup_league(p_people jsonb, p_shuffle boolean default false) returns text language plpgsql security definer set search_path = public as $$
declare person jsonb; i int := 0; uid uuid; missing text[] := '{}'; slots int[]; k int; n_teams int := 0; s league_settings;
begin
  if auth.uid() is not null then raise exception 'League setup is run from the database console, not the app'; end if;
  select * into s from league_settings;
  if exists (select 1 from drafts where status <> 'scheduled') then raise exception 'The draft has started; teams can no longer be changed'; end if;
  if (select count(*) from jsonb_array_elements(p_people) e where e ? 'team' and e->>'team' <> '') <> s.team_count then
    raise exception 'Exactly % people must have a team', s.team_count; end if;
  if (select count(*) from jsonb_array_elements(p_people) e where coalesce((e->>'commissioner')::boolean, false)) <> 1 then
    raise exception 'Exactly one person must be the commissioner'; end if;
  for person in select * from jsonb_array_elements(p_people) loop
    if not exists (select 1 from auth.users u where lower(u.email) = lower(person->>'email')) then missing := missing || (person->>'email'); end if;
  end loop;
  if array_length(missing, 1) > 0 then raise exception 'These emails are not under Authentication > Users yet: %', array_to_string(missing, ', '); end if;

  select array_agg(x order by case when p_shuffle then random() else x end) into slots from generate_series(1, s.team_count) x;
  update profiles set is_commissioner = false where is_commissioner;
  for person in select * from jsonb_array_elements(p_people) loop
    select id into uid from auth.users where lower(email) = lower(person->>'email');
    insert into profiles (id, display_name, is_commissioner) values (uid, coalesce(nullif(person->>'name', ''), split_part(person->>'email', '@', 1)), coalesce((person->>'commissioner')::boolean, false))
      on conflict (id) do update set display_name = excluded.display_name, is_commissioner = excluded.is_commissioner;
    if person ? 'team' and person->>'team' <> '' then
      i := i + 1; n_teams := n_teams + 1;
      if exists (select 1 from teams where id = i and owner_id is not null and owner_id <> uid) and not p_shuffle and exists (select 1 from roster_spots) then
        raise exception 'Team % already has a different owner and rosters exist', i; end if;
      insert into teams (id, name, owner_id, draft_slot) values (i, person->>'team', null, null)
        on conflict (id) do update set owner_id = null, draft_slot = null, name = excluded.name;
    end if;
  end loop;
  -- owners and draft slots second, so a re-run that reorders people cannot trip the unique constraints
  i := 0;
  for person in select * from jsonb_array_elements(p_people) loop
    if person ? 'team' and person->>'team' <> '' then
      i := i + 1;
      update teams set owner_id = (select id from auth.users where lower(email) = lower(person->>'email')), draft_slot = slots[i] where id = i;
    end if;
  end loop;
  return format('%s teams set up; draft order: %s', n_teams, (select string_agg(name, ', ' order by draft_slot) from teams));
end $$;
revoke execute on function setup_league(jsonb, boolean) from public, anon, authenticated;
grant execute on function setup_league(jsonb, boolean) to service_role;
-- 011: heartbeats. The scheduled jobs (scoring tick, nightly backup) note each run here, so the app can say whether they are on time.
create table job_runs (
  job text primary key check (job in ('tick', 'backup')),
  last_run_at timestamptz not null,
  last_ok_at timestamptz,
  last_error text,
  detail jsonb not null default '{}'
);
alter table job_runs enable row level security;     -- no policy and no grant: read only through screen_health()

-- Called by the jobs (database console / service role), never by the app.
create function record_job(p_job text, p_ok boolean, p_error text default null, p_detail jsonb default '{}') returns void
language plpgsql security definer set search_path = public as $$
begin
  if auth.uid() is not null then raise exception 'Only the scheduled jobs record their runs'; end if;
  insert into job_runs (job, last_run_at, last_ok_at, last_error, detail)
    values (p_job, app_now(), case when p_ok then app_now() end, case when p_ok then null else left(coalesce(p_error, 'failed'), 600) end, coalesce(p_detail, '{}'))
  on conflict (job) do update set last_run_at = excluded.last_run_at,
    last_ok_at = case when p_ok then excluded.last_ok_at else job_runs.last_ok_at end,
    last_error = excluded.last_error, detail = excluded.detail;
end $$;

-- ok = on time, late = missed a run or two, down = stopped, waiting = has never run (before launch).
create function job_status(p_last_ok timestamptz, p_every interval, p_now timestamptz default app_now()) returns text
language sql immutable as $$
  select case when p_last_ok is null then 'waiting'
              when p_now - p_last_ok <= p_every * 2.5 then 'ok'
              when p_now - p_last_ok <= p_every * 9 then 'late'
              else 'down' end $$;

-- Everyone: are scores current? The commissioner also gets every job, its last error, and a few setup facts.
create function screen_health() returns jsonb language plpgsql stable security definer set search_path = public as $$
declare n timestamptz := app_now(); tk job_runs; bk job_runs; every_tick interval := interval '10 minutes'; every_backup interval := interval '1 day';
        out jsonb;
begin
  select * into tk from job_runs where job = 'tick'; select * into bk from job_runs where job = 'backup';
  out := jsonb_build_object('now', n, 'scores', jsonb_build_object('status', job_status(tk.last_ok_at, every_tick, n),
           'minutes', case when tk.last_ok_at is not null then floor(extract(epoch from (n - tk.last_ok_at)) / 60) end));
  if is_commissioner() then
    out := out || jsonb_build_object('commissioner', true,
      'jobs', jsonb_build_array(
        jsonb_build_object('job', 'tick', 'label', 'Scoring tick', 'expect', 'every 10 minutes', 'status', job_status(tk.last_ok_at, every_tick, n),
          'last_ok_at', tk.last_ok_at, 'last_run_at', tk.last_run_at, 'error', tk.last_error, 'detail', coalesce(tk.detail, '{}')),
        jsonb_build_object('job', 'backup', 'label', 'Nightly backup', 'expect', 'every night', 'status', job_status(bk.last_ok_at, every_backup, n),
          'last_ok_at', bk.last_ok_at, 'last_run_at', bk.last_run_at, 'error', bk.last_error, 'detail', coalesce(bk.detail, '{}'))),
      'facts', jsonb_build_object(
        'teams', (select count(*) from teams), 'teams_with_owner', (select count(*) from teams where owner_id is not null),
        'players', (select count(*) from players where active), 'week', week_of(n),
        'live_draft', exists (select 1 from drafts where status = 'live'),
        'pending_trades', (select count(*) from trades where status in ('proposed', 'pending_veto'))));
  end if;
  return out;
end $$;

revoke execute on function record_job(text, boolean, text, jsonb) from public, anon, authenticated;
grant execute on function record_job(text, boolean, text, jsonb) to service_role;
grant execute on function screen_health() to authenticated;
grant all on job_runs to service_role;
-- 012: where the stat feeds land. Feed jobs call ingest_players and ingest_stats (jobs only; clients cannot).
-- Per-game numbers are kept in game_stats so re-reading a game never double counts; stat_lines holds their weekly sum.
-- A value the commissioner has corrected is never overwritten by a later feed read (until the correction is reverted).
create table game_stats (
  game_key text not null,
  player_id bigint not null references players,
  week smallint not null,
  stat text not null,
  value numeric not null,
  primary key (game_key, player_id, stat)
);
create index game_stats_week on game_stats (player_id, week, stat);
alter table game_stats enable row level security;           -- jobs only

-- Which games the feed has already read, so finished games are not fetched over and over.
create table feed_games (
  game_key text primary key,
  final boolean not null default false,
  fetches int not null default 0,
  last_fetch timestamptz not null default app_now()
);
alter table feed_games enable row level security;

-- Heartbeats for the feeds too (job names feed_mlb, feed_nhl, ...).
alter table job_runs drop constraint job_runs_job_check;
alter table job_runs add constraint job_runs_job_check check (job in ('tick', 'backup') or job like 'feed\_%');

create function has_live_correction(p_player bigint, p_week int, p_stat text) returns boolean language sql stable as $$
  select exists (select 1 from stat_corrections c where c.player_id = p_player and c.week = p_week and c.stat = p_stat and c.reverts_id is null
                   and not exists (select 1 from stat_corrections r where r.reverts_id = c.id)) $$;

-- stat_lines = sum of the games, unless the commissioner has corrected that number.
create function recompute_stat(p_player bigint, p_week int, p_stat text) returns void language plpgsql security definer set search_path = public as $$
declare total numeric; n int;
begin
  if has_live_correction(p_player, p_week, p_stat) then return; end if;
  select sum(value), count(*) into total, n from game_stats where player_id = p_player and week = p_week and stat = p_stat;
  if n = 0 then delete from stat_lines where player_id = p_player and week = p_week and stat = p_stat;
  else insert into stat_lines (player_id, week, stat, value) values (p_player, p_week, p_stat, total)
         on conflict (player_id, week, stat) do update set value = excluded.value where stat_lines.value is distinct from excluded.value;
  end if;
end $$;

-- Which of these games still need reading? p_games = [{"key": "...", "final": true|false}].
-- A game is due while it is live, or if it finished and has been read fewer than 3 times (each at least 90 minutes apart).
create function feed_due(p_games jsonb) returns text[] language sql stable security definer set search_path = public as $$
  select coalesce(array_agg(g->>'key'), '{}') from jsonb_array_elements(p_games) g
   where not exists (select 1 from feed_games f where f.game_key = g->>'key')
      or (select not (f.final and f.fetches >= 3) and (f.last_fetch <= app_now() - interval '90 minutes' or not f.final or (g->>'final')::boolean is distinct from f.final)
            from feed_games f where f.game_key = g->>'key') $$;

-- Players: [{"src": "mlb", "ext": "660271", "name": "...", "sport": "MLB", "position": "Hitter", "ranking": 12}]
-- Known by feed id -> updated (name, ranking). Unknown but one same-name player of that sport and position without a feed id -> linked.
-- Otherwise created. With p_hold, newly created players enter through the 2-day new-player waiver.
create function ingest_players(p jsonb, p_hold boolean default false) returns jsonb language plpgsql security definer set search_path = public as $$
declare e jsonb; pid bigint; made int := 0; updated int := 0; linked int := 0; bad jsonb := '[]'; n int;
begin
  if auth.uid() is not null then raise exception 'Only the feed jobs load players'; end if;
  for e in select * from jsonb_array_elements(p) loop
    if not exists (select 1 from positions where sport = (e->>'sport')::sport and position = e->>'position') then
      bad := bad || to_jsonb(format('%s: unknown position %s %s', e->>'name', e->>'sport', e->>'position')); continue;
    end if;
    select id into pid from players where external_ids->>(e->>'src') = e->>'ext';
    if found then
      update players set name = e->>'name', ranking = coalesce((e->>'ranking')::int, ranking), active = true where id = pid; updated := updated + 1;
    else
      select count(*) into n from players where lower(name) = lower(e->>'name') and sport = (e->>'sport')::sport and position = e->>'position' and not (external_ids ? (e->>'src'));
      if n = 1 then
        update players set external_ids = external_ids || jsonb_build_object(e->>'src', e->>'ext'), ranking = coalesce((e->>'ranking')::int, ranking), active = true
          where lower(name) = lower(e->>'name') and sport = (e->>'sport')::sport and position = e->>'position' and not (external_ids ? (e->>'src')); linked := linked + 1;
      else
        insert into players (name, sport, position, ranking, external_ids) values (e->>'name', (e->>'sport')::sport, e->>'position', (e->>'ranking')::int, jsonb_build_object(e->>'src', e->>'ext'))
          returning id into pid; made := made + 1;
        if p_hold then perform place_on_waivers(pid); end if;
      end if;
    end if;
  end loop;
  return jsonb_build_object('created', made, 'updated', updated, 'linked', linked, 'rejected', bad);
end $$;

-- Game lines: [{"src": "mlb", "ext": "660271", "game": "mlb:745123", "at": "2027-04-02T23:10:00Z", "final": true,
--               "stats": {"Home runs": 1, "Games played": 1}, "create": {"name": "...", "sport": "MLB", "position": "Hitter"}}]
-- Weeks outside the season, final weeks, unknown players and invalid stats are skipped and reported, never fatal.
create function ingest_stats(p jsonb, p_create boolean default false, p_hold boolean default false) returns jsonb
language plpgsql security definer set search_path = public as $$
declare e jsonb; s record; pid bigint; wk int; applied int := 0; unmatched jsonb := '[]'; rejected jsonb := '[]'; out_season int := 0; finals int := 0;
        made int := 0; gm record; v numeric;
begin
  if auth.uid() is not null then raise exception 'Only the feed jobs load stats'; end if;
  for e in select * from jsonb_array_elements(p) loop
    select id into pid from players where external_ids->>(e->>'src') = e->>'ext';
    if not found then
      if p_create and e ? 'create' and exists (select 1 from positions where sport = (e#>>'{create,sport}')::sport and position = e#>>'{create,position}') then
        insert into players (name, sport, position, external_ids) values (e#>>'{create,name}', (e#>>'{create,sport}')::sport, e#>>'{create,position}', jsonb_build_object(e->>'src', e->>'ext'))
          returning id into pid; made := made + 1;
        if p_hold then perform place_on_waivers(pid); end if;
      else
        if jsonb_array_length(unmatched) < 25 and not unmatched ? (e->>'src' || ':' || (e->>'ext')) then unmatched := unmatched || to_jsonb(e->>'src' || ':' || (e->>'ext')); end if;
        continue;
      end if;
    end if;
    wk := week_of((e->>'at')::timestamptz);
    if wk < 1 or wk > 51 then out_season := out_season + 1; continue; end if;
    if is_final(wk) then finals := finals + 1; continue; end if;
    -- stats that were in an earlier read of this game but are gone now
    for s in select stat from game_stats where game_key = e->>'game' and player_id = pid and not (e->'stats' ? stat) loop
      delete from game_stats where game_key = e->>'game' and player_id = pid and stat = s.stat;
      perform recompute_stat(pid, wk, s.stat);
    end loop;
    for s in select key as stat, value from jsonb_each(e->'stats') loop
      v := (s.value #>> '{}')::numeric;
      begin
        if v = 0 and s.stat <> 'Games played' then
          delete from game_stats where game_key = e->>'game' and player_id = pid and stat = s.stat;
        elsif not valid_stat(pid, s.stat) then
          raise exception '% is not a scored stat for this player', s.stat;
        else
          insert into game_stats (game_key, player_id, week, stat, value) values (e->>'game', pid, wk, s.stat, v)
            on conflict (game_key, player_id, stat) do update set value = excluded.value, week = excluded.week;
        end if;
        perform recompute_stat(pid, wk, s.stat);
        applied := applied + 1;
      exception when others then
        if jsonb_array_length(rejected) < 25 then rejected := rejected || to_jsonb(format('%s (%s): %s', (select name from players where id = pid), s.stat, sqlerrm)); end if;
      end;
    end loop;
  end loop;
  for gm in select g->>'game' as k, bool_or(coalesce((g->>'final')::boolean, false)) as f from jsonb_array_elements(p) g group by 1 loop
    insert into feed_games (game_key, final, fetches, last_fetch) values (gm.k, gm.f, 1, app_now())
      on conflict (game_key) do update set final = excluded.final, fetches = feed_games.fetches + 1, last_fetch = app_now();
  end loop;
  return jsonb_build_object('applied', applied, 'created', made, 'unmatched', unmatched, 'rejected', rejected, 'out_of_season', out_season, 'final_weeks', finals);
end $$;

revoke execute on function recompute_stat(bigint, int, text), feed_due(jsonb), ingest_players(jsonb, boolean), ingest_stats(jsonb, boolean, boolean), has_live_correction(bigint, int, text) from public, anon, authenticated;
grant execute on function recompute_stat(bigint, int, text), feed_due(jsonb), ingest_players(jsonb, boolean), ingest_stats(jsonb, boolean, boolean), has_live_correction(bigint, int, text) to service_role;
grant all on game_stats, feed_games to service_role;

-- Health page: the stat-feed jobs (one heartbeat per sport, hourly) join the commissioner's list once they have run.
-- The hand-fed MLS file is not scheduled, so it is not listed.
create or replace function screen_health() returns jsonb language plpgsql stable security definer set search_path = public as $$
declare n timestamptz := app_now(); tk job_runs; bk job_runs; every_tick interval := interval '10 minutes'; every_backup interval := interval '1 day';
        every_feed interval := interval '1 hour'; out jsonb; jobs jsonb; f job_runs;
begin
  select * into tk from job_runs where job = 'tick'; select * into bk from job_runs where job = 'backup';
  out := jsonb_build_object('now', n, 'scores', jsonb_build_object('status', job_status(tk.last_ok_at, every_tick, n),
           'minutes', case when tk.last_ok_at is not null then floor(extract(epoch from (n - tk.last_ok_at)) / 60) end));
  if is_commissioner() then
    jobs := jsonb_build_array(
        jsonb_build_object('job', 'tick', 'label', 'Scoring tick', 'expect', 'every 10 minutes', 'status', job_status(tk.last_ok_at, every_tick, n),
          'last_ok_at', tk.last_ok_at, 'last_run_at', tk.last_run_at, 'error', tk.last_error, 'detail', coalesce(tk.detail, '{}')),
        jsonb_build_object('job', 'backup', 'label', 'Nightly backup', 'expect', 'every night', 'status', job_status(bk.last_ok_at, every_backup, n),
          'last_ok_at', bk.last_ok_at, 'last_run_at', bk.last_run_at, 'error', bk.last_error, 'detail', coalesce(bk.detail, '{}')));
    for f in select * from job_runs where job like 'feed\_%' and job <> 'feed_mls' order by job loop
      jobs := jobs || jsonb_build_array(jsonb_build_object('job', f.job, 'label', upper(substr(f.job, 6)) || ' stats feed', 'expect', 'every hour',
          'status', job_status(f.last_ok_at, every_feed, n), 'last_ok_at', f.last_ok_at, 'last_run_at', f.last_run_at, 'error', f.last_error, 'detail', coalesce(f.detail, '{}')));
    end loop;
    out := out || jsonb_build_object('commissioner', true, 'jobs', jobs,
      'facts', jsonb_build_object(
        'teams', (select count(*) from teams), 'teams_with_owner', (select count(*) from teams where owner_id is not null),
        'players', (select count(*) from players where active), 'week', week_of(n),
        'live_draft', exists (select 1 from drafts where status = 'live'),
        'pending_trades', (select count(*) from trades where status in ('proposed', 'pending_veto'))));
  end if;
  return out;
end $$;
grant execute on function screen_health() to authenticated;
-- 013: the limit of two F1 drivers per team is removed (commissioner's decision). The cap stays a setting rather than code: it is now
-- the size of a roster (20), which can never bind. To bring a limit back, update league_settings set f1_cap = <n>.
update league_settings set f1_cap = 20;
alter table league_settings alter column f1_cap set default 20;
-- 014: injury designations from the feeds.
-- players.injury_source says who set the status: 'feed' (the hourly injury job) or 'manual' (anyone editing the column directly, e.g. the
-- commissioner from the SQL console). A manual status is never overwritten or cleared by the feed until it is cleared by hand.
alter table players add column injury_source text check (injury_source in ('feed', 'manual'));
update players set injury_source = 'manual' where injury_status is not null;

create function players_injury_source() returns trigger language plpgsql as $$
begin
  if new.injury_status is distinct from old.injury_status then
    new.injury_source := case when new.injury_status is null then null
                              when current_setting('app.feed_injury', true) = 'on' then 'feed' else 'manual' end;
  end if;
  return new;
end $$;
create trigger players_injury_source_trg before update of injury_status on players for each row execute function players_injury_source();

-- p = {"src": "espn", "sport": "NBA", "complete": true, "players": [{"ext": "123", "name": "A B", "status": "Out"}]}
-- Sets the status of listed players. With "complete", feed-set statuses of that sport who are no longer listed are cleared (healthy again) --
-- unless that would clear most of them at once (a broken or empty feed), in which case nothing is cleared and the result says so.
-- Matching: known feed id, else a unique same-name player of that sport (the id is then remembered).
create function ingest_injuries(p jsonb) returns jsonb language plpgsql security definer set search_path = public as $$
declare e jsonb; pid bigint; n int; sp sport := (p->>'sport')::sport; src text := p->>'src';
        setn int := 0; clearedn int := 0; same int := 0; kept int := 0; unmatched jsonb := '[]'; listed bigint[] := '{}'; stale bigint[]; had int; suspicious boolean := false;
begin
  if auth.uid() is not null then raise exception 'Only the feed jobs load injuries'; end if;
  perform set_config('app.feed_injury', 'on', true);
  for e in select * from jsonb_array_elements(coalesce(p->'players', '[]')) loop
    select id into pid from players where external_ids->>src = e->>'ext' and sport = sp;
    if not found then
      select count(*) into n from players where lower(name) = lower(e->>'name') and sport = sp;
      if n = 1 then
        select id into pid from players where lower(name) = lower(e->>'name') and sport = sp;
        update players set external_ids = external_ids || jsonb_build_object(src, e->>'ext') where id = pid and not (external_ids ? src);
      else
        unmatched := unmatched || to_jsonb(format('%s (%s)', e->>'name', case when n = 0 then 'not in the pool' else 'name is ambiguous' end)); continue;
      end if;
    end if;
    listed := listed || pid;
    if (select injury_source from players where id = pid) = 'manual' and (select injury_status from players where id = pid) is distinct from e->>'status' then kept := kept + 1; continue; end if;
    if (select injury_status from players where id = pid) is not distinct from e->>'status' then same := same + 1; continue; end if;
    update players set injury_status = e->>'status' where id = pid; setn := setn + 1;
  end loop;
  if coalesce((p->>'complete')::boolean, false) then
    select array_agg(id), count(*) into stale, had from players where sport = sp and injury_source = 'feed' and not (id = any(listed));
    select count(*) into n from players where sport = sp and injury_source = 'feed';
    if had > 0 and had > greatest(5, n * 0.6) then suspicious := true;
    elsif had > 0 then update players set injury_status = null where id = any(stale); clearedn := had; end if;
  end if;
  perform set_config('app.feed_injury', 'off', true);
  return jsonb_build_object('set', setn, 'cleared', clearedn, 'unchanged', same, 'kept_manual', kept, 'unmatched', unmatched, 'suspicious_clear_skipped', suspicious);
end $$;
revoke execute on function ingest_injuries(jsonb), players_injury_source() from public, anon, authenticated;
grant execute on function ingest_injuries(jsonb) to service_role;

-- Health page labels: 'feed_nfl_injuries' reads "NFL injury feed".
create or replace function screen_health() returns jsonb language plpgsql stable security definer set search_path = public as $$
declare n timestamptz := app_now(); tk job_runs; bk job_runs; every_tick interval := interval '10 minutes'; every_backup interval := interval '1 day';
        every_feed interval := interval '1 hour'; out jsonb; jobs jsonb; f job_runs;
begin
  select * into tk from job_runs where job = 'tick'; select * into bk from job_runs where job = 'backup';
  out := jsonb_build_object('now', n, 'scores', jsonb_build_object('status', job_status(tk.last_ok_at, every_tick, n),
           'minutes', case when tk.last_ok_at is not null then floor(extract(epoch from (n - tk.last_ok_at)) / 60) end));
  if is_commissioner() then
    jobs := jsonb_build_array(
        jsonb_build_object('job', 'tick', 'label', 'Scoring tick', 'expect', 'every 10 minutes', 'status', job_status(tk.last_ok_at, every_tick, n),
          'last_ok_at', tk.last_ok_at, 'last_run_at', tk.last_run_at, 'error', tk.last_error, 'detail', coalesce(tk.detail, '{}')),
        jsonb_build_object('job', 'backup', 'label', 'Nightly backup', 'expect', 'every night', 'status', job_status(bk.last_ok_at, every_backup, n),
          'last_ok_at', bk.last_ok_at, 'last_run_at', bk.last_run_at, 'error', bk.last_error, 'detail', coalesce(bk.detail, '{}')));
    for f in select * from job_runs where job like 'feed\_%' and job <> 'feed_mls' order by job loop
      jobs := jobs || jsonb_build_array(jsonb_build_object('job', f.job,
          'label', upper(replace(substr(f.job, 6), '_injuries', '')) || case when f.job like '%\_injuries' then ' injury feed' else ' stats feed' end, 'expect', 'every hour',
          'status', job_status(f.last_ok_at, every_feed, n), 'last_ok_at', f.last_ok_at, 'last_run_at', f.last_run_at, 'error', f.last_error, 'detail', coalesce(f.detail, '{}')));
    end loop;
    out := out || jsonb_build_object('commissioner', true, 'jobs', jobs,
      'facts', jsonb_build_object(
        'teams', (select count(*) from teams), 'teams_with_owner', (select count(*) from teams where owner_id is not null),
        'players', (select count(*) from players where active), 'week', week_of(n),
        'live_draft', exists (select 1 from drafts where status = 'live'),
        'pending_trades', (select count(*) from trades where status in ('proposed', 'pending_veto'))));
  end if;
  return out;
end $$;
grant execute on function screen_health() to authenticated;
-- Generated by tools/export_seed_sql.py from engine/config.json. Do not edit by hand.
begin;
insert into positions (sport, position, profile, min_games, fallback_avg, fallback_sd) values
  ('NFL'::sport, 'QB', 'NFL', 1, 17, 6.5),
  ('NFL'::sport, 'RB', 'NFL', 1, 11, 6),
  ('NFL'::sport, 'WR', 'NFL', 1, 12, 5.5),
  ('NFL'::sport, 'TE', 'NFL', 1, 7, 4),
  ('NBA'::sport, 'Guard', 'NBA', 2, 130, 45),
  ('NBA'::sport, 'Wing', 'NBA', 2, 125, 42),
  ('NBA'::sport, 'Big', 'NBA', 2, 135, 48),
  ('MLB'::sport, 'SP', 'MLB pitcher', 1, 12, 8),
  ('MLB'::sport, 'RP', 'MLB pitcher', 1, 5, 4),
  ('MLB'::sport, 'Hitter', 'MLB hitter', 3, 16, 8),
  ('NHL'::sport, 'Skater', 'NHL skater', 2, 10, 5),
  ('NHL'::sport, 'Goalie', 'NHL goalie', 1, 10, 6),
  ('MLS'::sport, 'Forward', 'MLS', 1, 6, 4.5),
  ('MLS'::sport, 'Midfielder', 'MLS', 1, 5, 4),
  ('MLS'::sport, 'Defender', 'MLS', 1, 4, 3),
  ('MLS'::sport, 'Goalkeeper', 'MLS', 1, 5, 3),
  ('F1'::sport, 'Driver', 'F1', 1, 12, 11)
on conflict (sport, position) do update set profile = excluded.profile, min_games = excluded.min_games, fallback_avg = excluded.fallback_avg, fallback_sd = excluded.fallback_sd;
insert into scoring_weights (profile, stat, weight) values
  ('NFL', 'Passing yards', 0.04),
  ('NFL', 'Passing TD', 4),
  ('NFL', 'Interception thrown', -2),
  ('NFL', 'Rushing yards', 0.1),
  ('NFL', 'Rushing TD', 6),
  ('NFL', 'Receiving yards', 0.1),
  ('NFL', 'Receptions (half-PPR)', 0.5),
  ('NFL', 'Receiving TD', 6),
  ('NFL', 'Fumble lost', -2),
  ('NFL', '2-point conversion', 2),
  ('NBA', 'Points', 1),
  ('NBA', 'Rebounds', 1.2),
  ('NBA', 'Assists', 1.5),
  ('NBA', 'Steals', 2.5),
  ('NBA', 'Blocks', 2.5),
  ('NBA', 'Turnovers', -1),
  ('MLB hitter', 'Singles', 1),
  ('MLB hitter', 'Doubles', 2),
  ('MLB hitter', 'Triples', 3),
  ('MLB hitter', 'Home runs', 4),
  ('MLB hitter', 'Runs', 1),
  ('MLB hitter', 'RBI', 1),
  ('MLB hitter', 'Walks', 1),
  ('MLB hitter', 'Stolen bases', 2),
  ('MLB hitter', 'Strikeouts', -0.5),
  ('MLB pitcher', 'Innings pitched', 3),
  ('MLB pitcher', 'Strikeouts', 1),
  ('MLB pitcher', 'Earned runs', -2),
  ('MLB pitcher', 'Hits allowed', -1),
  ('MLB pitcher', 'Walks allowed', -1),
  ('MLB pitcher', 'Win', 5),
  ('MLB pitcher', 'Save', 5),
  ('NHL skater', 'Goals', 3),
  ('NHL skater', 'Assists', 2),
  ('NHL skater', 'Plus/minus', 0.5),
  ('NHL skater', 'Shots on goal', 0.4),
  ('NHL skater', 'Blocked shots', 0.4),
  ('NHL goalie', 'Wins', 4),
  ('NHL goalie', 'Saves', 0.2),
  ('NHL goalie', 'Goals against', -1),
  ('NHL goalie', 'Shutouts', 3),
  ('MLS', 'Goals', 6),
  ('MLS', 'Assists', 3),
  ('MLS', 'Shots on target', 1),
  ('MLS', 'Key passes', 1),
  ('MLS', 'Tackles won', 0.5),
  ('MLS', 'Appearances (60+ minutes)', 1),
  ('MLS', 'Clean sheet (GK/DEF)', 4),
  ('MLS', 'Saves (GK)', 0.5),
  ('MLS', 'Goals conceded (GK/DEF)', -1),
  ('MLS', 'Yellow cards', -1),
  ('MLS', 'Red cards', -3),
  ('F1', 'Positions gained', 1),
  ('F1', 'Fastest lap', 3),
  ('F1', 'Did not finish', -5)
on conflict (profile, stat) do update set weight = excluded.weight;
insert into bonus_bands (start_place, points) values (1, 30), (2, 24), (3, 20), (4, 16), (6, 10), (11, 5), (21, 2)
on conflict (start_place) do update set points = excluded.points;
insert into f1_points (kind, place, points) values
  ('race', 1, 40),
  ('race', 2, 32),
  ('race', 3, 26),
  ('race', 4, 22),
  ('race', 5, 18),
  ('race', 6, 15),
  ('race', 7, 12),
  ('race', 8, 10),
  ('race', 9, 8),
  ('race', 10, 6),
  ('race', 11, 3),
  ('race', 12, 3),
  ('race', 13, 3),
  ('race', 14, 3),
  ('race', 15, 3),
  ('race', 16, 0),
  ('race', 17, 0),
  ('race', 18, 0),
  ('race', 19, 0),
  ('race', 20, 0),
  ('sprint', 1, 8),
  ('sprint', 2, 7),
  ('sprint', 3, 6),
  ('sprint', 4, 5),
  ('sprint', 5, 4),
  ('sprint', 6, 3),
  ('sprint', 7, 2),
  ('sprint', 8, 1),
  ('pole', 1, 5),
  ('front_row', 2, 3),
  ('front_row', 3, 3)
on conflict (kind, place) do update set points = excluded.points;
commit;
