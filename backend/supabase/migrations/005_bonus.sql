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
