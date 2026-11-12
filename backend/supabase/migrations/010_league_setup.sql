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
