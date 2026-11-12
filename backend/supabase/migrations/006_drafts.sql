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
