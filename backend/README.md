# Fantasy league database (Supabase / Postgres 16)

Apply in order: `supabase/migrations/001` through `014`, then `supabase/seed.sql`
(`schema_all.sql` is the same thing in one file).

| File | What it holds |
|---|---|
| 001 | Settings row, test clock, calendar functions (week boundaries, lineup window, waiver slots), all in Eastern wall-clock time |
| 002 | Players, teams, rosters, lineups, activity log; roster/lineup rules as triggers |
| 003 | Stats, append-only corrections, scores, matchups, standings view, injury mulligan |
| 004 | Free agents, waivers, trades |
| 005 | Bonus events, results, tie averaging, held events |
| 006 | Drafts (snake, clock, autopick) |
| 007 | Screen reads and IR moves |
| 008 | Commissioner screens |
| 009 | Row-level security, grants, realtime |
| 010 | One-time league setup (`setup_league`, console only) |
| 011 | Job heartbeats and `screen_health` (the live badge and the commissioner's health panel) |
| 012 | Stat feeds: per-game stat store, `ingest_players`, `ingest_stats`, `feed_due`; feed jobs on the health panel |
| 013 | The two-driver F1 limit is removed (the setting is now 20, a full roster; set it lower to bring a limit back) |
| 014 | Injury designations from the feeds (`ingest_injuries`; `players.injury_source` marks hand-set statuses, which the feed never overwrites or clears) |

Rules live in the database: clients read tables and write only by calling the functions, which run the same checks the Python engine does.

## Tests (real Postgres)
```
backend/tests/run.sh                      # builds fl_test from the migrations
python3 -m unittest discover -s backend/tests -t .
```
100+ tests, including a 40-scenario fuzz that checks the SQL waiver run against `engine/transactions.py`.
`dev_clock` lets tests set "now"; leave it empty in production.

## The app (`app/`)
`app/index.html` + `app.js` is the installable web page: sign in, edit your lineup, see the live matchup. It calls only the database functions (`screen_roster`, `set_lineup`, `move_to_ir`, `return_from_ir`, `use_mulligan`, `screen_matchup`), so every rule is the database's. Put your Supabase URL and anon key in `app/config.js`, then host the folder (GitHub Pages works).
`tools/dev_server.py` is a local stand-in for Supabase (sign-in with any league email and password `test`), used by `backend/tests/test_app_lineup.py`, which drives the page in a real browser against the real rules.

## Scoring job
`tools/scoring_job.py` runs every few minutes. Each run: builds the schedule once, carries forward lineups for teams that didn't reset, freezes the week's baselines (first run only), scores players and lineups live, updates matchup scores, decides winners when a week is final, and advances the season (Cup groups, Cup knockouts, champion bonus win and mulligan, playoff seeds, wildcard, semifinals, final). Safe to re-run. `backend/tests/test_scoring_job.py` plays a whole invented 51-week season on a real database (about 3 minutes).
Production note: the job talks to the database through a small `PsqlDb` class; deploying means swapping in a Supabase service-role version with the same two methods.

## Rule decisions (settled with the commissioner)
1. A player in an accepted trade awaiting the 24-hour veto window can't be started, and starters can't be proposed or accepted in a trade until taken out of the lineup. Lineup carry-over skips players in pending trades. A trade can therefore no longer fail at settlement because of a lineup.
2. Seeds 5 and 6 are ordered by cumulative points.
3. Week-1 lineups open the moment the main draft finishes (`league_settings.preseason_open_at` is set automatically) and lock at the first Tuesday noon.

## App pages (live)
Lineup, Matchup, Players (free agents, waiver claims with ordering and cancel, drops with confirm), Trades (propose, accept, decline, withdraw, veto window, deadline) and Activity. Tests: `tests/test_app_lineup.py`, `tests/test_app_market.py`.

Commissioner tab (visible only to the commissioner): trades in the veto window with a required reason, stat corrections with revert, and mulligan grants. Database side: `008_admin.sql` (`screen_admin`, `admin_player_stats`); the actions keep their own role checks. Test: `tests/test_app_admin.py`. A commissioner who owns no team sees Activity and Commissioner only.

Standings tab: table with playoff seeds (top four by record, then the two best point totals among the rest), games by week with live scores, your season, the Cup groups and knockout, and the playoff bracket. Database side: `screen_standings` in `007_screens.sql`. Tests: `tests/test_app_standings.py`.

Draft tab: live room for the main draft (and bonus drafts): clock, board, available players with legality shown, queue, autopick. Opening the room runs the clock, so expired picks are made even if nobody is watching; the page also refreshes itself every 3 seconds. The commissioner creates and starts the draft from the Commissioner tab. Database side: `screen_draft` in `007_screens.sql`. Tests: `tests/test_app_draft.py`.


## Deploying (GitHub + Supabase)
Three workflows in `.github/workflows/`: **Publish the app** (GitHub Pages, on every change to `app/`), **Update database** (manual; `tools/apply_migrations.py` applies each new migration file once and refreshes the seed), and **Scoring tick** (every 10 minutes; `tools/tick.py` runs the scoring job, `settle_trades()` and `process_waivers()` against the Supabase connection string in the `SUPABASE_DB_URL` secret). The job connects as the database owner, so the functions' commissioner checks (which look at the signed-in user) do not apply to it.
Rules for later changes: never edit a migration that has been applied to the live database; add a new higher-numbered file. Every new function must be granted explicitly (and `revoke execute ... from public, anon, authenticated` first), because Supabase grants new functions to clients by default. Tests: `tests/test_deploy.py`.

Bonus tab: every event with its state (open, points held, scored), draft status, your picks and points; a detail view with the field, results and each team's bonus points. The Commissioner tab adds: create an event (name, end time in Eastern, hold flag, field list), create its draft (2 rounds by default), enter results by pasting `Name: place` / `Name: Missed cut` lines, and conclude a held event. Database side: `create_bonus_event`, `create_bonus_draft` (008_admin.sql), `screen_bonus` (007_screens.sql). The Draft tab has a selector when more than one draft exists. Tests: `tests/test_app_bonus.py`.
Change password: link in the page header, calls Supabase Auth's `PUT /auth/v1/user`.

## Security notes

- Clients can only *select* (row-level security on every table) and call the functions listed in `tests/test_security.py::CLIENT_FUNCTIONS`; every write goes through those functions, which check who is calling.
- Migration 009 closes the default privileges: a function added later is **not callable** by clients until a migration grants it on purpose. `test_security.py` fails if the callable set changes without the allowlist changing.
- All `security definer` functions pin `search_path`; the test clock (`dev_clock`) is unreachable from clients.
- Caps: 24 players per side of a trade, 10 open proposals per team, 100 players in a draft queue, no duplicate queue entries.
- Load check (12 teams, ~3,400-player pool): a full 240-pick catch-up takes ~24 s one time (~0.1 s/pick); the draft screen answers in ~6 ms.

## Backups

`tools/backup.py` (workflow "Nightly backup") saves every league table in the `public` schema as one encrypted, compressed file (AES-256 with the `BACKUP_PASSPHRASE` secret), kept 90 days as a workflow artifact.

    python3 tools/backup.py verify FILE          # decrypt and list row counts per table
    python3 tools/backup.py restore FILE --yes   # replace all league data with the copy

To rebuild from nothing: run "Update database" (empty tables), then restore. Restore switches off the rule triggers for the load (they would re-judge old rows) and carries the id numbering on. Sign-in accounts live in Supabase's auth tables and are not in the copy; if lost, run `setup_league` again. Tests: `tests/test_backup.py`.

## Health

The tick and the nightly backup record each run in `job_runs` (`record_job`, jobs only). `screen_health()` turns the age of the last good run into ok / late / down / waiting: the tick is late after 25 minutes and stopped after 90; the backup is late after 60 hours and stopped after 9 days. Everyone sees the badge in the header (Live, Scores delayed, Scores paused); the commissioner's tab shows each job, its last error, and setup facts. A failed run keeps the last good time, so a stuck job ages into "late" by itself even if it cannot report. `job_runs` is excluded from backups.

## Stat feeds

`tools/feeds/` has one reader per source (MLB statsapi, NHL api-web, NBA cdn, NFL nflverse, F1 Jolpica) and `tools/ingest.py` stores what they read. The Stats workflow runs hourly (`.github/workflows/stats.yml`); each sport is its own step, so one broken feed leaves the others running, and each leaves a heartbeat (`feed_mlb`, ...) on the health panel (late after 2.5 hours, stopped after 9).

- Each game's lines go into `game_stats`; `stat_lines` is the sum of them, so a re-read or a score correction by the source replaces rather than doubles. A stat the commissioner corrected by hand is left alone until the correction is reverted. Final weeks (Wednesday noon after the week) are never touched.
- A game is re-read while live, then up to three times after it finishes (90 minutes apart) to pick up stat corrections.
- Unknown players in a game are created (through the 2-day new-player waiver). The player pool and draft ranking come from `ingest.py pool --sport mlb|nhl|nfl|f1` (last season's totals under our scoring, as a z-score; shows a preview, `--apply` writes). NBA and MLS pools come from a CSV: `ingest.py pool-csv FILE`.
- MLS has no free feed: stats are loaded from a CSV (`ingest.py csv FILE`: `ext,name,position,date,stat,value`).
- Injury designations: `ingest.py injuries --sport nfl|mlb|nba|nhl|mls` (hourly, after the stats). NFL from the nflverse weekly report (Out / Doubtful / Questionable; skipped when the report is more than 10 days old), MLB from roster status codes (injured lists), NBA/NHL/MLS from ESPN's public injury lists (matched to our players by name; an ambiguous name is reported, not guessed). Only Out / Injured Reserve / Injured List / PUP / NFI make a player eligible for IR; the rest show as soft tags. A player who is no longer listed is cleared. A status set by hand (SQL console) is never overwritten or cleared by the feed. If a run would clear most of a sport's injuries at once it clears nothing and reports an error (a broken feed).
- The field names each service uses were written from documentation and have not yet been checked from a live run: `python3 tools/feed_probe.py` does that check (run it once on a normal home connection, and compare with what the readers expect).

## Launch check, player pool, practice draft

Three buttons in the Actions tab (all manual): **Launch check** (`tools/preflight.py`, read-only: secrets, config, migrations, no test clock, 12 owned teams, draft order, enough players per sport, jobs on time, prints READY or NOT READY), **Load players** (`ingest.py pool --sport all`: MLB/NHL/NFL/F1 from the feeds, NBA/MLS from `data/pool_*.csv`; ranked jointly so autopick order is fair across sports; preview by default; refuses to write if a sport failed unless forced) and **Practice draft** (`tools/practice_draft.py`, bots on a throwaway database).
