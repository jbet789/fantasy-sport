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
