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
