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
