import unittest
from .test_db import DbCase


class Corrections(DbCase):
    SETUP = """
        select t.give(1,'MLB','Hitter',1);
        create temp table h as select player_id as id from roster_spots where team_id=1;
        grant all on h to authenticated;
        insert into stat_lines values ((select id from h), 12, 'Home runs', 2);
        select t.at('2027-03-30 20:00');
    """

    def test_only_the_commissioner_can_correct(self):
        self.sql(self.SETUP + """
            select t.as_user(1);
            select t.err('manager', $q$ select apply_correction((select id from h), 12, 'Home runs', 3, 'x') $q$, 'Only the commissioner');
            select t.as_user(13);
            select apply_correction((select id from h), 12, 'Home runs', 3, 'Scorer changed a double to a homer');
            select t.eq('value updated', (select value from stat_lines where week=12 and stat='Home runs')::int, 3);
        """)

    def test_log_records_old_and_new_and_cannot_be_edited(self):
        self.sql(self.SETUP + """
            select t.as_user(13);
            select apply_correction((select id from h), 12, 'Home runs', 3, ' Scorer error ');
            select t.eq('old', (select old_value from stat_corrections)::int, 2);
            select t.eq('new', (select new_value from stat_corrections)::int, 3);
            select t.eq('reason trimmed', (select reason from stat_corrections), 'Scorer error');
            select t.eq('logged for the league', (select count(*) from activity_log where kind='stat_correction' and commissioner), 1::bigint);
            select t.as_service();
            select t.err('edit log', 'update stat_corrections set reason = ''x''', 'append-only');
            select t.err('delete log', 'delete from stat_corrections', 'append-only');
            select t.err('edit activity', 'delete from activity_log', 'append-only');
        """)

    def test_week_is_final_wednesday_noon_for_everyone(self):
        self.sql(self.SETUP + """
            select t.as_user(13);
            select t.at('2027-03-31 11:59');
            select apply_correction((select id from h), 12, 'Home runs', 3, 'in time');
            select t.at('2027-03-31 12:00');
            select t.err('too late', $q$ select apply_correction((select id from h), 12, 'Home runs', 4, 'late') $q$, 'became final');
            select t.as_service();
            select t.err('jobs cannot change it either', $q$ update stat_lines set value = 9 where week = 12 $q$, 'became final');
            select t.err('or add rows', $q$ insert into stat_lines values ((select id from h), 12, 'Runs', 1) $q$, 'became final');
            select t.err('or remove rows', $q$ delete from stat_lines where week = 12 $q$, 'became final');
            select t.eq('value unchanged', (select value from stat_lines where week=12 and stat='Home runs')::int, 3);
        """)

    def test_validation(self):
        self.sql(self.SETUP + """
            select t.as_user(13);
            select t.err('blank reason', $q$ select apply_correction((select id from h), 12, 'Home runs', 3, '   ') $q$, 'reason is required');
            select t.err('not a hitter stat', $q$ select apply_correction((select id from h), 12, 'Goals', 3, 'x') $q$, 'not a scored stat');
            select t.err('negative', $q$ select apply_correction((select id from h), 12, 'Home runs', -1, 'x') $q$, 'cannot be negative');
            select t.give(2,'NHL','Skater',1);
            select apply_correction((select player_id from roster_spots where team_id=2), 12, 'Plus/minus', -2, 'plus/minus can be negative');
            select t.give(3,'F1','Driver',1);
            select apply_correction((select player_id from roster_spots where team_id=3), 12, 'Race finish position', 3, 'f1 stat');
            select apply_correction((select id from h), 12, 'Games played', 6, 'games played always allowed');
        """)

    def test_revert_restores_the_old_value_and_works_once(self):
        self.sql(self.SETUP + """
            select t.as_user(13);
            select apply_correction((select id from h), 12, 'Home runs', 3, 'fix');
            select apply_correction((select id from h), 12, 'Runs', 2, 'added a stat that did not exist');
            select revert_correction((select min(id) from stat_corrections));
            select revert_correction((select id from stat_corrections where stat='Runs' and reverts_id is null));
            select t.eq('old value back', (select value from stat_lines where week=12 and stat='Home runs')::int, 2);
            select t.eq('added stat removed', (select count(*) from stat_lines where stat='Runs'), 0::bigint);
            select t.eq('log keeps all four', (select count(*) from stat_corrections), 4::bigint);
            select t.err('revert twice', $q$ select revert_correction((select min(id) from stat_corrections)) $q$, 'already reverted');
        """)


class Standings(DbCase):
    def test_ranking_is_wins_then_cumulative_points(self):
        self.sql("""
            insert into matchups (week, kind, team_a, team_b, score_a, score_b, winner) values
              (1,'regular',1,2,100,90,1), (1,'regular',3,4,100,90,3), (2,'regular',1,3,100,90,1), (2,'regular',2,4,100,90,2);
            insert into team_weeks (team_id, week, total) values (1,1,100),(1,2,100),(3,1,100),(3,2,90),(2,1,90),(2,2,100),(4,1,90),(4,2,90);
            select t.eq('leader', (select team_id from standings order by rank limit 1)::int, 1);
            select t.eq('team 2 beats team 3 on points', (select team_id from standings where rank=2)::int, 2);
            insert into cup_bonus_wins (team_id) values (4);
            select t.eq('cup bonus win counts', (select wins from standings where team_id=4)::int, 1);
        """)


class Mulligan(DbCase):
    SETUP = """
        select t.give(1,'NFL','RB',12);
        create temp table m as select player_id as id from roster_spots where team_id=1 order by player_id;
        grant all on m to authenticated;
        select t.as_user(1);
        select t.at('2027-03-30 04:00');
        select set_lineup((select array_agg(id) from (select id from m order by id limit 10) s));
        select t.as_service();
        select grant_mulligan(1::smallint);
        update players set injury_status='Questionable' where id = (select id from m order by id limit 1);
    """

    def test_swap_after_lock_for_an_injured_starter(self):
        self.sql(self.SETUP + """
            select t.at('2027-03-30 12:01');
            select t.as_user(1);
            select t.err('Questionable is not an official designation', $q$ select use_mulligan((select id from m order by id limit 1), (select id from m order by id desc limit 1)) $q$, 'official injury');
            select t.as_service();
            update players set injury_status='Out' where id = (select id from m order by id limit 1);
            select t.as_user(1);
            select t.err('replacement must be bench', $q$ select use_mulligan((select id from m order by id limit 1), (select id from m order by id offset 1 limit 1)) $q$, 'bench');
            select use_mulligan((select id from m order by id limit 1), (select id from m order by id desc limit 1));
            select t.ok('swapped in', exists (select 1 from lineups where team_id=1 and week=13 and player_id=(select id from m order by id desc limit 1)));
            select t.eq('still 10 starters', (select count(*) from lineups where team_id=1 and week=13), 10::bigint);
            select t.err('only once', $q$ select use_mulligan((select id from m order by id offset 1 limit 1), (select id from m order by id offset 10 limit 1)) $q$, 'already been used');
        """)

    def test_not_before_the_lock_and_not_without_holding_one(self):
        self.sql(self.SETUP + """
            update players set injury_status='Out' where id = (select id from m order by id limit 1);
            select t.at('2027-03-30 11:00');
            select t.as_user(1);
            select t.err('before lock', $q$ select use_mulligan((select id from m order by id limit 1), (select id from m order by id desc limit 1)) $q$, 'after the lineup lock');
            select t.as_user(2);
            select t.at('2027-03-30 12:01');
            select t.err('does not hold one', $q$ select use_mulligan(1, 2) $q$, 'does not hold');
        """)


if __name__ == "__main__":
    unittest.main()
