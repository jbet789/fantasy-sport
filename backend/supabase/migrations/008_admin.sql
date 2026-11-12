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
