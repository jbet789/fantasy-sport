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
