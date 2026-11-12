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
