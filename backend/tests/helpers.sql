-- TEST ONLY helpers. Applied after the migrations and seed; never part of the deployed schema.
create schema t;
grant usage on schema t to public;
create function t.ok(label text, cond boolean) returns void language plpgsql as
  $$ begin if cond is not true then raise exception 'FAIL: %', label; end if; end $$;
create function t.eq(label text, got anyelement, want anyelement) returns void language plpgsql as
  $$ begin if got is distinct from want then raise exception 'FAIL: % (got %, want %)', label, got, want; end if; end $$;
-- Run a statement and require it to fail with a message containing `pattern`.
create function t.err(label text, stmt text, pattern text) returns void language plpgsql as $$
begin
  begin execute stmt; exception when others then
    if sqlerrm ilike '%' || pattern || '%' then return; end if;
    raise exception 'FAIL: % (wrong error: %)', label, sqlerrm;
  end;
  raise exception 'FAIL: % (no error)', label;
end $$;
create function t.at(ts text) returns void language plpgsql as $$
begin
  delete from dev_clock;
  insert into dev_clock (at) values ((ts || ' America/New_York')::timestamptz);
end $$;
create function t.uid(i int) returns uuid language sql immutable as
  $$ select ('00000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid $$;
-- Act as team i's owner (i = 13 is the commissioner, who owns no team). Reset with t.as_service().
create function t.as_user(i int) returns void language plpgsql as $$
begin perform set_config('request.jwt.claim.sub', t.uid(i)::text, true); end $$;
create function t.as_service() returns void language plpgsql as
  $$ begin perform set_config('request.jwt.claim.sub', '', true); end $$;
-- 12 teams with owners, a commissioner (user 13), and a pool of 17 positions x 40 players (F1: 20).
create function t.fixture() returns void language plpgsql as $$
begin
  insert into auth.users (id, email) select t.uid(i), 'u' || i || '@example.test' from generate_series(1, 13) i;
  insert into profiles (id, display_name, is_commissioner) select t.uid(i), 'User ' || i, i = 13 from generate_series(1, 13) i;
  insert into teams (id, name, owner_id, draft_slot) select i, 'Team ' || i, t.uid(i), i from generate_series(1, 12) i;
  insert into players (name, sport, position, ranking)
    select p.sport || ' ' || p.position || ' ' || g, p.sport, p.position, row_number() over (order by p.sport, p.position, g)
      from positions p, generate_series(1, case when p.sport = 'F1' then 20 else 40 end) g;
  perform t.at('2027-03-24 10:00');
end $$;
create function t.pid(nm text) returns bigint language sql stable as $$ select id from players where name = nm $$;
-- put n players of a sport+position on a team's roster
create function t.give(team int, sp sport, pos text, n int, from_idx int default 1) returns void language plpgsql as $$
begin
  insert into roster_spots (team_id, player_id)
    select team, id from players where sport = sp and position = pos and id not in (select player_id from roster_spots)
    order by id offset from_idx - 1 limit n;
end $$;

-- the migration role's default privileges close new functions; the test helpers must stay callable under any role
grant execute on all functions in schema t to public;
