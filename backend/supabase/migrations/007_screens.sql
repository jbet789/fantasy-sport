-- 007: what the lineup and matchup screens need: IR moves, and one read call per screen (security invoker, so row-level security applies).

create function move_to_ir(p_player bigint) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team();
begin
  if t is null then raise exception 'You do not own a team'; end if;
  update roster_spots set slot = 'ir' where team_id = t and player_id = p_player and slot = 'active';
  if not found then raise exception 'That is not an active player on your roster'; end if;
end $$;

create function return_from_ir(p_player bigint) returns void language plpgsql security definer set search_path = public as $$
declare t smallint := my_team();
begin
  if t is null then raise exception 'You do not own a team'; end if;
  update roster_spots set slot = 'active', ir_cleared_at = null where team_id = t and player_id = p_player and slot = 'ir';
  if not found then raise exception 'That player is not on your injured reserve'; end if;
end $$;

-- Everything the lineup screen shows for one team. Lineups are public: anyone can see any team's, at any time.
create function screen_roster(p_team smallint default null) returns jsonb language plpgsql stable as $$
declare t smallint := coalesce(p_team, my_team()); cur int := week_of(app_now()); w int := coalesce(lineup_week(), week_of(app_now())); s league_settings;
  open_ boolean := lineup_open(); opens timestamptz; locks timestamptz;
begin
  select * into s from league_settings;
  locks := week_end_ts(cur);
  opens := ((((locks at time zone s.tz)::date) + s.lineup_open_time) at time zone s.tz);
  return jsonb_build_object(
    'now', app_now(), 'week', cur, 'lineup_week', w, 'open', open_, 'opens_at', opens, 'locks_at', locks,
    'me', my_team(), 'team', (select jsonb_build_object('id', id, 'name', name) from teams where id = t),
    'teams', (select jsonb_agg(jsonb_build_object('id', id, 'name', name) order by id) from teams),
    'limits', jsonb_build_object('active', s.active_limit, 'ir', s.ir_limit, 'starters', s.starter_slots, 'f1', s.f1_cap),
    'mulligan', (select jsonb_build_object('used', m.used_at is not null) from mulligans m where m.team_id = t),
    'players', coalesce((
      select jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position, 'injury', p.injury_status,
               'slot', r.slot, 'starter', exists (select 1 from lineups l where l.team_id = t and l.week = w and l.player_id = p.id),
               'official', is_official_injury(p.injury_status), 'last', (select ps.score from player_scores ps where ps.player_id = p.id and ps.week = w - 1 and ps.played))
             order by p.name)
        from roster_spots r join players p on p.id = r.player_id where r.team_id = t), '[]'::jsonb));
end $$;

-- The matchup for one week for the caller's team, with both sides' starters and scores.
create function screen_matchup(p_week int default null) returns jsonb language plpgsql stable as $$
declare w int := coalesce(p_week, week_of(app_now())); t smallint := my_team(); m matchups; opp smallint;
begin
  select * into m from matchups where week = w and (team_a = t or team_b = t) order by id limit 1;
  if not found then return jsonb_build_object('week', w, 'none', true); end if;
  opp := case when m.team_a = t then m.team_b else m.team_a end;
  return jsonb_build_object('week', w, 'kind', m.kind, 'final', is_final(w), 'winner', m.winner,
    'sides', (select jsonb_agg(side order by ord) from (
      select 1 as ord, screen_side(t, w) as side union all select 2, screen_side(opp, w)) x));
end $$;

create function screen_side(p_team smallint, p_week int) returns jsonb language sql stable as $$
  select jsonb_build_object('team', p_team, 'name', (select name from teams where id = p_team),
    'starter_points', coalesce(tw.starter_points, 0), 'variety', coalesce(tw.variety_bonus, 0), 'bonus', coalesce(tw.bonus_points, 0),
    'total', coalesce(tw.total, 0), 'best', coalesce(tw.best_starter, 0),
    'rows', coalesce((select jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position, 'injury', p.injury_status,
                 'played', coalesce(ps.played, false), 'score', ps.score) order by p.name)
        from lineups l join players p on p.id = l.player_id left join player_scores ps on ps.player_id = p.id and ps.week = p_week
       where l.team_id = p_team and l.week = p_week), '[]'::jsonb))
  from (select 1) d left join team_weeks tw on tw.team_id = p_team and tw.week = p_week $$;

-- ---- players, waivers and trades screens ----
-- Security definer because they read the caller's own re-add blocks and claims; each one only returns league-public data plus the caller's own.

create function screen_market(p_sport text default null, p_search text default null, p_limit int default 60) returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare t smallint := my_team(); now_ timestamptz := app_now(); s league_settings; w int := coalesce(lineup_week(), week_of(app_now()));
begin
  select * into s from league_settings;
  return jsonb_build_object(
    'now', now_, 'me', t, 'open', lineup_open(),
    'limits', jsonb_build_object('active', s.active_limit, 'f1', s.f1_cap),
    'next_run', (select min(ts) from (
        select ((((now_ at time zone s.tz)::date + i) + time '09:00') at time zone s.tz) as ts from generate_series(0, 7) i) x
        where is_waiver_slot(ts) and ts > now_),
    'roster', coalesce((select jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position, 'slot', r.slot, 'injury', p.injury_status,
          'in_lineup', exists (select 1 from lineups l where l.team_id = t and l.week = w and l.player_id = p.id),
          'locked', is_locked_starter(t, p.id, now_)) order by p.name)
        from roster_spots r join players p on p.id = r.player_id where r.team_id = t), '[]'::jsonb),
    'players', coalesce((select jsonb_agg(to_jsonb(q)) from (
        select p.id, p.name, p.sport, p.position, p.injury_status as injury,
               (select ps.score from player_scores ps where ps.player_id = p.id and ps.played order by ps.week desc limit 1) as recent,
               w2.release_at, w2.kind, (w2.player_id is not null) as on_waivers, coalesce(is_waiver_slot(w2.release_at), false) as claim_slot,
               is_free_agent(p.id, now_) as free, rb.until as blocked_until, c.id as claim_id
          from players p
          left join waivers w2 on w2.player_id = p.id
          left join readd_blocks rb on rb.player_id = p.id and rb.team_id = t and rb.until > now_
          left join waiver_claims c on c.player_id = p.id and c.team_id = t and c.status = 'pending'
         where p.active and not exists (select 1 from roster_spots r where r.player_id = p.id)
           and (p_sport is null or p.sport::text = p_sport) and (p_search is null or p.name ilike '%' || p_search || '%')
         order by (select ps.score from player_scores ps where ps.player_id = p.id and ps.played order by ps.week desc limit 1) desc nulls last, p.name
         limit least(greatest(p_limit, 1), 200)) q), '[]'::jsonb),
    'claims', coalesce((select jsonb_agg(jsonb_build_object('id', c.id, 'player_id', c.player_id, 'name', p.name, 'sport', p.sport, 'position', p.position,
          'drop_id', c.drop_player_id, 'drop_name', d.name, 'rank', c.rank, 'run_at', c.run_at, 'status', c.status, 'reason', c.reason)
          order by (c.status = 'pending') desc, c.rank, c.id)
        from waiver_claims c join players p on p.id = c.player_id left join players d on d.id = c.drop_player_id
       where c.team_id = t and (c.status = 'pending' or c.resolved_at > now_ - interval '14 days')), '[]'::jsonb));
end $$;

create function screen_trades() returns jsonb language plpgsql stable security definer set search_path = public as $$
declare t smallint := my_team();
begin
  return jsonb_build_object('now', app_now(), 'me', t, 'deadline', trade_deadline(), 'open', app_now() < trade_deadline(),
    'trades', coalesce((select jsonb_agg(jsonb_build_object('id', x.id, 'status', x.status, 'proposer', x.proposer, 'partner', x.partner,
        'proposer_name', (select name from teams where id = x.proposer), 'partner_name', (select name from teams where id = x.partner),
        'proposed_at', x.proposed_at, 'accepted_at', x.accepted_at, 'veto_until', x.veto_until, 'note', x.note,
        'give', (select coalesce(jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position) order by p.name), '[]') from trade_items i join players p on p.id = i.player_id where i.trade_id = x.id and i.from_team = x.proposer),
        'get', (select coalesce(jsonb_agg(jsonb_build_object('id', p.id, 'name', p.name, 'sport', p.sport, 'position', p.position) order by p.name), '[]') from trade_items i join players p on p.id = i.player_id where i.trade_id = x.id and i.from_team = x.partner),
        'drops', (select coalesce(jsonb_agg(jsonb_build_object('team', d.team_id, 'name', p.name)), '[]') from trade_drops d join players p on p.id = d.player_id where d.trade_id = x.id))
        order by x.id desc)
      from (select * from trades where proposer = t or partner = t or status = 'completed' order by id desc limit 40) x), '[]'::jsonb));
end $$;

create function screen_activity(p_limit int default 50) returns jsonb language sql stable as $$
  select coalesce(jsonb_agg(jsonb_build_object('id', id, 'at', at, 'kind', kind, 'team', team_id, 'message', message, 'commissioner', commissioner) order by id desc), '[]'::jsonb)
  from (select * from activity_log order by id desc limit least(greatest(p_limit, 1), 200)) a $$;

-- League standings, one week's games, the Cup, the playoff bracket and the caller's own schedule.
-- A game that has no final score yet shows the team's live week total.
create function screen_standings(p_week int default null) returns jsonb language plpgsql stable security definer set search_path = public as $$
declare cur int := week_of(app_now()); w int := coalesce(p_week, greatest(1, least(cur, 51))); me smallint := my_team();
begin
  return jsonb_build_object('now', app_now(), 'week', cur, 'shown', w, 'me', me,
    'standings', coalesce((select jsonb_agg(jsonb_build_object('rank', s.rank, 'team_id', s.team_id, 'name', t.name, 'wins', s.wins, 'losses', s.losses,
        'points', s.cumulative, 'cup_bonus', s.cup_bonus, 'seed', s.seed) order by s.rank, s.team_id)
      from (select x.*, case when x.rank <= 4 then x.rank::int
                             when row_number() over (partition by (x.rank <= 4) order by x.cumulative desc, x.wins desc, x.team_id) <= 2 then 4 + (row_number() over (partition by (x.rank <= 4) order by x.cumulative desc, x.wins desc, x.team_id))::int end as seed
              from standings x) s join teams t on t.id = s.team_id), '[]'::jsonb),
    'games', coalesce((select jsonb_agg(g order by g.id) from (
        select m.id, m.week, m.kind, m.team_a, m.team_b, (select name from teams where id = m.team_a) as name_a, (select name from teams where id = m.team_b) as name_b,
               coalesce(m.score_a, (select total from team_weeks where team_id = m.team_a and week = m.week)) as score_a,
               coalesce(m.score_b, (select total from team_weeks where team_id = m.team_b and week = m.week)) as score_b,
               m.winner, is_final(m.week) as final
          from matchups m where m.week = w) g), '[]'::jsonb),
    'mine', coalesce((select jsonb_agg(g order by g.week) from (
        select m.week, m.kind, case when m.team_a = me then m.team_b else m.team_a end as opp,
               (select name from teams where id = case when m.team_a = me then m.team_b else m.team_a end) as opp_name,
               coalesce(case when m.team_a = me then m.score_a else m.score_b end, (select total from team_weeks where team_id = me and week = m.week)) as score,
               coalesce(case when m.team_a = me then m.score_b else m.score_a end, (select total from team_weeks where team_id = case when m.team_a = me then m.team_b else m.team_a end and week = m.week)) as opp_score,
               m.winner, is_final(m.week) as final
          from matchups m where me in (m.team_a, m.team_b)) g), '[]'::jsonb),
    'cup', jsonb_build_object(
      'groups', coalesce((select jsonb_agg(jsonb_build_object('tier', x.tier, 'grp', x.grp, 'teams', x.teams) order by x.tier, x.grp) from (
          select c.tier, c.grp, jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name,
              'wins', (select count(*) from matchups m where m.kind = 'cup-group' and m.winner = t.id),
              'losses', (select count(*) from matchups m where m.kind = 'cup-group' and m.winner is not null and m.winner <> t.id and t.id in (m.team_a, m.team_b)),
              'points', (select coalesce(sum(case when m.team_a = t.id then coalesce(m.score_a, 0) else coalesce(m.score_b, 0) end), 0) from matchups m where m.kind = 'cup-group' and t.id in (m.team_a, m.team_b)))
              order by (select count(*) from matchups m where m.kind = 'cup-group' and m.winner = t.id) desc, t.id) as teams
            from cup_groups c join teams t on t.id = c.team_id group by c.tier, c.grp) x), '[]'::jsonb),
      'knockout', coalesce((select jsonb_agg(jsonb_build_object('id', m.id, 'week', m.week, 'kind', m.kind, 'team_a', m.team_a, 'team_b', m.team_b, 'name_a', (select name from teams where id = m.team_a),
          'name_b', (select name from teams where id = m.team_b), 'winner', m.winner,
          'score_a', coalesce(m.score_a, (select total from team_weeks where team_id = m.team_a and week = m.week)),
          'score_b', coalesce(m.score_b, (select total from team_weeks where team_id = m.team_b and week = m.week))) order by m.week, m.id)
        from matchups m where m.kind in ('cup-semi', 'cup-final')), '[]'::jsonb),
      'champion_bonus', (select team_id from cup_bonus_wins limit 1)),
    'playoffs', coalesce((select jsonb_agg(jsonb_build_object('id', m.id, 'week', m.week, 'kind', m.kind, 'team_a', m.team_a, 'team_b', m.team_b,
          'name_a', (select name from teams where id = m.team_a), 'name_b', (select name from teams where id = m.team_b), 'winner', m.winner,
          'score_a', coalesce(m.score_a, (select total from team_weeks where team_id = m.team_a and week = m.week)),
          'score_b', coalesce(m.score_b, (select total from team_weeks where team_id = m.team_b and week = m.week))) order by m.week, m.id)
        from matchups m where m.kind in ('wildcard', 'playoff-semi', 'playoff-final')), '[]'::jsonb));
end $$;

-- The draft room for one draft (default: the live one, else the next scheduled, else the latest). Opening it also runs the clock,
-- so an expired pick is autopicked even if nobody else is looking. Works for the main draft (players) and bonus drafts (entrants).
create function screen_draft(p_draft bigint default null, p_sport text default null, p_search text default null, p_limit int default 60) returns jsonb
language plpgsql security definer set search_path = public as $$
declare d drafts; me smallint := my_team(); cur smallint; n int;
begin
  select * into d from drafts where id = coalesce(p_draft, (select id from drafts order by (status = 'live') desc, (status = 'scheduled') desc, id desc limit 1));
  if not found then return jsonb_build_object('now', app_now(), 'none', true, 'me', me); end if;
  if d.status = 'live' then perform advance_if_due(d.id); select * into d from drafts where id = d.id; end if;
  select team_id into cur from draft_slots where draft_id = d.id and pick_no = d.current_pick and d.status = 'live';
  select count(*) into n from draft_slots where draft_id = d.id;
  return jsonb_build_object('now', app_now(), 'me', me, 'on_clock', cur,
    'draft', jsonb_build_object('id', d.id, 'kind', d.kind, 'status', d.status, 'rounds', d.rounds, 'pick_seconds', d.pick_seconds,
       'current_pick', d.current_pick, 'deadline', d.deadline, 'total', n, 'teams', (select count(distinct team_id) from draft_slots where draft_id = d.id)),
    'teams', (select jsonb_agg(jsonb_build_object('id', id, 'name', name) order by id) from teams),
    'drafts', (select jsonb_agg(jsonb_build_object('id', x.id, 'label', case when x.kind = 'main' then 'Main draft' else coalesce(e.name, 'Bonus draft') end, 'status', x.status) order by x.id) from drafts x left join bonus_events e on e.id = x.event_id),
    'slots', (select jsonb_agg(jsonb_build_array(pick_no, team_id) order by pick_no) from draft_slots where draft_id = d.id),
    'picks', coalesce((select jsonb_agg(jsonb_build_object('pick_no', k.pick_no, 'team_id', k.team_id, 'auto', k.auto, 'at', k.at,
          'name', coalesce(pl.name, en.name), 'sport', pl.sport, 'position', pl.position) order by k.pick_no)
        from draft_picks k left join players pl on pl.id = k.player_id left join bonus_entrants en on en.id = k.entrant_id where k.draft_id = d.id), '[]'::jsonb),
    'available', coalesce((select jsonb_agg(a) from (
        select pl.id, pl.name, pl.sport, pl.position, pl.ranking as rank, case when me is not null and d.status = 'live' then draft_problem(d.id, me, pl.id) end as problem
          from players pl where d.kind = 'main' and pl.active
           and not exists (select 1 from draft_picks k where k.draft_id = d.id and k.player_id = pl.id) and not exists (select 1 from roster_spots r where r.player_id = pl.id)
           and (p_sport is null or pl.sport::text = p_sport) and (p_search is null or pl.name ilike '%' || p_search || '%')
         order by pl.ranking nulls last, pl.id limit least(greatest(p_limit, 1), 200)) a), '[]'::jsonb)
      || coalesce((select jsonb_agg(a) from (
        select en.id, en.name, null::text as sport, null::text as position, en.rank, null::text as problem
          from bonus_entrants en where d.kind = 'bonus' and en.event_id = d.event_id
           and not exists (select 1 from draft_picks k where k.draft_id = d.id and k.entrant_id = en.id)
           and (p_search is null or en.name ilike '%' || p_search || '%')
         order by en.rank, en.id limit least(greatest(p_limit, 1), 200)) a), '[]'::jsonb),
    'queue', coalesce((select jsonb_agg(jsonb_build_object('id', coalesce(q.player_id, q.entrant_id), 'name', coalesce(pl.name, en.name), 'sport', pl.sport, 'position', pl.position,
          'taken', exists (select 1 from draft_picks k where k.draft_id = d.id and (k.player_id = q.player_id or k.entrant_id = q.entrant_id))) order by q.rank)
        from draft_queue q left join players pl on pl.id = q.player_id left join bonus_entrants en on en.id = q.entrant_id where q.draft_id = d.id and q.team_id = me), '[]'::jsonb));
end $$;


-- Bonus events for everyone. With no argument: the list (state, draft, your points). With an event: its field, results and every team's picks.
create function screen_bonus(p_event bigint default null) returns jsonb language plpgsql stable security definer set search_path = public as $$
declare me smallint := my_team();
begin
  return jsonb_build_object('now', app_now(), 'me', me,
    'events', coalesce((select jsonb_agg(jsonb_build_object('id', e.id, 'name', e.name, 'ends_at', e.ends_at, 'hold', e.hold, 'state', bonus_event_state(e), 'concluded_at', e.concluded_at,
        'draft', (select jsonb_build_object('id', d.id, 'status', d.status, 'rounds', d.rounds) from drafts d where d.event_id = e.id),
        'entrants', (select count(*) from bonus_entrants n where n.event_id = e.id),
        'my_picks', (select count(*) from bonus_picks bp where bp.event_id = e.id and bp.team_id = me),
        'my_points', (select coalesce(sum(s.points), 0) from bonus_scores s where s.event_id = e.id and s.team_id = me)) order by e.ends_at, e.id) from bonus_events e), '[]'::jsonb),
    'detail', case when p_event is null then null else jsonb_build_object('event', p_event,
      'entrants', coalesce((select jsonb_agg(jsonb_build_object('id', n.id, 'name', n.name, 'rank', n.rank, 'status', r.status, 'place', r.place, 'points', coalesce(s.points, 0),
          'team_id', bp.team_id, 'team', (select name from teams where id = bp.team_id)) order by r.place nulls last, n.rank)
        from bonus_entrants n left join bonus_results r on r.entrant_id = n.id left join bonus_picks bp on bp.entrant_id = n.id left join bonus_scores s on s.entrant_id = n.id
        where n.event_id = p_event), '[]'::jsonb),
      'teams', coalesce((select jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name, 'points', coalesce((select sum(s.points) from bonus_scores s where s.event_id = p_event and s.team_id = t.id), 0)) order by 3 desc, t.id) from teams t), '[]'::jsonb)) end);
end $$;
