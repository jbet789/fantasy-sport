"""Feed readers on small saved samples (no network). Field names come from each service's documented/observed shape;
tools/feed_probe.py checks them against the live services."""
import json, unittest
from datetime import datetime, timezone
from tools.feeds import mlb, nhl, nba, nfl, f1, csv_import, pool, injuries
from tools.feeds.common import innings, minutes


def by(lines, ext):
    return next(l for l in lines if str(l["ext"]) == str(ext))


class Helpers(unittest.TestCase):
    def test_numbers(self):
        self.assertAlmostEqual(innings("6.1"), 6 + 1 / 3)
        self.assertAlmostEqual(innings("0.2"), 2 / 3)
        self.assertAlmostEqual(minutes("18:30"), 18.5)
        self.assertAlmostEqual(minutes("PT32M30.00S"), 32.5)
        self.assertEqual(minutes(""), 0)


class MLB(unittest.TestCase):
    SCHED = {"dates": [{"games": [
        {"gamePk": 1, "gameDate": "2027-04-02T23:10:00Z", "status": {"abstractGameState": "Final", "detailedState": "Final"}},
        {"gamePk": 2, "gameDate": "2027-04-02T23:40:00Z", "status": {"abstractGameState": "Live", "detailedState": "In Progress"}},
        {"gamePk": 3, "gameDate": "2027-04-02T23:40:00Z", "status": {"abstractGameState": "Final", "detailedState": "Postponed"}},
        {"gamePk": 4, "gameDate": "2027-04-03T23:40:00Z", "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"}}]}]}
    BOX = {"teams": {"home": {"players": {
        "ID1": {"person": {"id": 11, "fullName": "Slugger One"}, "position": {"abbreviation": "RF"},
                "stats": {"batting": {"atBats": 4, "plateAppearances": 5, "hits": 3, "doubles": 1, "triples": 0, "homeRuns": 1, "runs": 2, "rbi": 3, "baseOnBalls": 1, "stolenBases": 1, "strikeOuts": 1}}},
        "ID2": {"person": {"id": 12, "fullName": "Bench Guy"}, "position": {"abbreviation": "C"}, "stats": {"batting": {}}},
        "ID3": {"person": {"id": 13, "fullName": "Ace Starter"}, "position": {"abbreviation": "P"},
                "stats": {"batting": {}, "pitching": {"inningsPitched": "6.1", "strikeOuts": 8, "earnedRuns": 2, "hits": 5, "baseOnBalls": 1, "wins": 1, "saves": 0, "gamesStarted": 1}}}}},
        "away": {"players": {"ID4": {"person": {"id": 14, "fullName": "Closer Man"}, "position": {"abbreviation": "P"},
                                      "stats": {"pitching": {"inningsPitched": "1.0", "strikeOuts": 2, "earnedRuns": 0, "hits": 0, "baseOnBalls": 0, "wins": 0, "saves": 1, "gamesStarted": 0}}}}}}}

    def test_schedule_keeps_started_games_only(self):
        self.assertEqual([(g["pk"], g["final"]) for g in mlb.parse_schedule(self.SCHED)], [(1, True), (2, False)])

    def test_boxscore(self):
        ls = mlb.parse_boxscore(self.BOX, 1, "2027-04-02T23:10:00Z", True)
        self.assertEqual(sorted(str(l["ext"]) for l in ls), ["11", "13", "14"])   # bench player who did not play is skipped
        h = by(ls, 11)["stats"]
        self.assertEqual((h["Singles"], h["Doubles"], h["Home runs"], h["RBI"], h["Stolen bases"]), (1, 1, 1, 3, 1))
        self.assertNotIn("Innings pitched", h)
        self.assertEqual(by(ls, 11)["create"]["position"], "Hitter")
        sp, rp = by(ls, 13), by(ls, 14)
        self.assertEqual((sp["create"]["position"], rp["create"]["position"]), ("SP", "RP"))
        self.assertAlmostEqual(sp["stats"]["Innings pitched"], 6 + 1 / 3, places=3)
        self.assertEqual((sp["stats"]["Win"], rp["stats"]["Save"]), (1, 1))
        self.assertNotIn("Singles", sp["stats"])
        self.assertEqual(sp["game"], "mlb:1")


class NHL(unittest.TestCase):
    SCHED = {"gameWeek": [{"games": [
        {"id": 100, "gameType": 2, "gameState": "FINAL", "startTimeUTC": "2027-01-05T00:00:00Z"},
        {"id": 101, "gameType": 2, "gameState": "LIVE", "startTimeUTC": "2027-01-05T02:00:00Z"},
        {"id": 102, "gameType": 1, "gameState": "FINAL", "startTimeUTC": "2027-01-05T02:00:00Z"},
        {"id": 103, "gameType": 2, "gameState": "FUT", "startTimeUTC": "2027-01-06T02:00:00Z"}]}]}
    BOX = {"playerByGameStats": {"homeTeam": {
        "forwards": [{"playerId": 1, "name": {"default": "A. Winger"}, "toi": "18:33", "goals": 2, "assists": 1, "plusMinus": -1, "sog": 5, "blockedShots": 0},
                     {"playerId": 2, "name": {"default": "Scratched"}, "toi": "00:00", "goals": 0}],
        "defense": [{"playerId": 3, "name": {"default": "B. Blueliner"}, "toi": "22:10", "goals": 0, "assists": 2, "plusMinus": 2, "sog": 1, "blockedShots": 4}],
        "goalies": [{"playerId": 4, "name": {"default": "G. Keeper"}, "toi": "60:00", "saves": 30, "goalsAgainst": 0, "decision": "W"}]},
        "awayTeam": {"forwards": [], "defense": [], "goalies": [{"playerId": 5, "name": {"default": "H. Netminder"}, "toi": "57:00", "saves": 20, "goalsAgainst": 3, "decision": "L"}]}}}

    def test_schedule(self):
        self.assertEqual([(g["id"], g["final"]) for g in nhl.parse_schedule(self.SCHED)], [(100, True), (101, False)])

    def test_boxscore(self):
        ls = nhl.parse_boxscore(self.BOX, 100, "2027-01-05T00:00:00Z", True)
        self.assertEqual(sorted(str(l["ext"]) for l in ls), ["1", "3", "4", "5"])
        w = by(ls, 1)["stats"]
        self.assertEqual((w["Goals"], w["Assists"], w["Plus/minus"], w["Shots on goal"]), (2, 1, -1, 5))
        self.assertEqual(by(ls, 3)["stats"]["Blocked shots"], 4)
        g, g2 = by(ls, 4), by(ls, 5)
        self.assertEqual((g["create"]["position"], g["stats"]["Wins"], g["stats"]["Shutouts"], g["stats"]["Saves"]), ("Goalie", 1, 1, 30))
        self.assertEqual((g2["stats"].get("Wins", 0), g2["stats"].get("Shutouts", 0), g2["stats"]["Goals against"]), (0, 0, 3))


class NBA(unittest.TestCase):
    SCHED = {"leagueSchedule": {"gameDates": [{"games": [
        {"gameId": "0022600001", "gameStatus": 3, "gameDateTimeUTC": "2027-01-05T00:00:00+00:00"},
        {"gameId": "0022600002", "gameStatus": 2, "gameDateTimeUTC": "2027-01-05T02:30:00+00:00"},
        {"gameId": "0022600003", "gameStatus": 1, "gameDateTimeUTC": "2027-01-06T00:00:00+00:00"}]}]}}
    BOX = {"game": {"homeTeam": {"players": [
        {"personId": 7, "statistics": {"minutes": "PT32M10.00S", "points": 28, "reboundsTotal": 9, "assists": 7, "steals": 2, "blocks": 1, "turnovers": 4}},
        {"personId": 8, "statistics": {"minutes": "PT00M00.00S", "points": 0}}]}, "awayTeam": {"players": []}}}

    def test_schedule_and_box(self):
        self.assertEqual([(g["id"], g["final"], g["at"]) for g in nba.parse_schedule(self.SCHED)],
                         [("0022600001", True, "2027-01-05T00:00:00Z"), ("0022600002", False, "2027-01-05T02:30:00Z")])
        ls = nba.parse_boxscore(self.BOX, "0022600001", "2027-01-05T00:00:00Z", True)
        self.assertEqual([str(l["ext"]) for l in ls], ["7"])
        self.assertEqual((ls[0]["stats"]["Points"], ls[0]["stats"]["Rebounds"], ls[0]["stats"]["Turnovers"]), (28, 9, 4))
        self.assertNotIn("create", ls[0])


class NFL(unittest.TestCase):
    GAMES = "season,week,game_type,gameday\n2026,1,REG,2026-09-10\n2026,1,REG,2026-09-14\n2026,1,POST,2027-01-20\n2026,2,REG,2026-09-21\n"
    STATS = ("season,week,season_type,player_id,player_display_name,position,passing_yards,passing_tds,passing_interceptions,rushing_yards,rushing_tds,"
             "receiving_yards,receptions,receiving_tds,rushing_fumbles_lost,receiving_fumbles_lost,sack_fumbles_lost,passing_2pt_conversions,rushing_2pt_conversions,receiving_2pt_conversions\n"
             "2026,1,REG,00-1,Quinn Back,QB,301,3,1,12,0,0,0,0,0,0,1,1,0,0\n"
             "2026,1,REG,00-2,Rudy Runner,RB,0,0,0,88,1,25,4,0,1,0,0,0,1,0\n"
             "2026,1,REG,00-3,Kicker Kim,K,0,0,0,0,0,0,0,0,0,0,0,0,0,0\n"
             "2026,2,REG,00-1,Quinn Back,QB,250,2,0,5,0,0,0,0,0,0,0,0,0,0\n"
             "2026,1,POST,00-1,Quinn Back,QB,999,9,0,0,0,0,0,0,0,0,0,0,0,0\n")

    def test_games_use_last_game_day_at_noon_eastern(self):
        w = nfl.parse_games(self.GAMES)
        self.assertEqual(set(w), {(2026, 1), (2026, 2)})
        self.assertEqual(w[(2026, 1)], "2026-09-14T16:00:00Z")      # noon EDT

    def test_week_stats(self):
        w = nfl.parse_games(self.GAMES)
        ls = nfl.parse_week_stats(self.STATS, w)
        self.assertEqual(len(ls), 3)                                  # kicker and playoff row dropped
        q = next(l for l in ls if l["ext"] == "00-1" and l["game"] == "nfl:2026:1")
        self.assertEqual((q["stats"]["Passing yards"], q["stats"]["Passing TD"], q["stats"]["Interception thrown"], q["stats"]["Fumble lost"], q["stats"]["2-point conversion"]), (301, 3, 1, 1, 1))
        r = by(ls, "00-2")["stats"]
        self.assertEqual((r["Rushing yards"], r["Receptions (half-PPR)"], r["Fumble lost"], r["2-point conversion"]), (88, 4, 1, 1))
        self.assertEqual(by(ls, "00-2")["create"], {"name": "Rudy Runner", "sport": "NFL", "position": "RB"})

    def test_window(self):
        w = nfl.parse_games(self.GAMES)
        ls = nfl.parse_week_stats(self.STATS, w, since="2026-09-20T00:00:00Z", until="2026-09-30T00:00:00Z")
        self.assertEqual({l["game"] for l in ls}, {"nfl:2026:2"})


class F1(unittest.TestCase):
    RACE = {"MRData": {"RaceTable": {"Races": [{"season": "2027", "round": "1", "date": "2027-03-14", "time": "04:00:00Z", "Results": [
        {"position": "1", "grid": "3", "status": "Finished", "Driver": {"driverId": "max", "givenName": "Max", "familyName": "V"}, "FastestLap": {"rank": "1"}},
        {"position": "2", "grid": "1", "status": "+5.2 s", "Driver": {"driverId": "lando", "givenName": "Lando", "familyName": "N"}, "FastestLap": {"rank": "3"}},
        {"position": "3", "grid": "0", "status": "Lapped", "Driver": {"driverId": "pitlane", "givenName": "Pit", "familyName": "Lane"}},
        {"position": "4", "grid": "2", "status": "Engine", "Driver": {"driverId": "retired", "givenName": "Re", "familyName": "Tired"}}]}]}}}
    QUALI = {"MRData": {"RaceTable": {"Races": [{"season": "2027", "round": "1", "date": "2027-03-13", "time": "05:00:00Z",
                                                 "QualifyingResults": [{"position": "1", "Driver": {"driverId": "lando", "givenName": "Lando", "familyName": "N"}}]}]}}}

    def test_race(self):
        ls = f1.parse_race(self.RACE)
        m = by(ls, "max")["stats"]
        self.assertEqual((m["Race finish position"], m["Positions gained"], m.get("Did not finish", 0), m["Fastest lap"]), (1, 2, 0, 1))
        l = by(ls, "lando")["stats"]
        self.assertEqual((l["Positions gained"], l.get("Fastest lap", 0)), (-1, 0))
        self.assertEqual(by(ls, "pitlane")["stats"]["Positions gained"], 4 - 3)           # pit-lane start counts as last (4th)
        self.assertEqual((by(ls, "retired")["stats"]["Did not finish"]), 1)
        self.assertEqual(by(ls, "max")["at"], "2027-03-14T04:00:00Z")

    def test_qualifying(self):
        ls = f1.parse_qualifying(self.QUALI)
        self.assertEqual((ls[0]["stats"], ls[0]["game"]), ({"Qualifying position": 1}, "f1:2027:1:quali"))


class HandFed(unittest.TestCase):
    def test_stats_csv(self):
        txt = ("ext,name,position,date,stat,value\n9,Jo Striker,Forward,2027-03-06,Goals,2\n9,Jo Striker,Forward,2027-03-06,Shots on target,4\n"
               "9,Jo Striker,Forward,2027-03-13,Assists,1\n")
        ls = csv_import.parse_stats(txt)
        self.assertEqual(len(ls), 2)
        d1 = next(l for l in ls if "03-06" in l["game"])
        self.assertEqual((d1["stats"]["Goals"], d1["stats"]["Games played"], d1["create"]["position"]), (2, 1, "Forward"))

    def test_pool_csv(self):
        es = pool.csv_pool("src,ext,name,sport,position,ranking\nnba,1,Top Guard,NBA,Guard,1\nnba,2,Bottom Big,NBA,Big,2\n")
        self.assertGreater(es[0]["score"], es[1]["score"])
        self.assertEqual(csv_import.parse_pool("src,ext,name,sport,position,ranking\nmls,5,X Y,MLS,Defender,7\n")[0]["ranking"], 7)


class Ranking(unittest.TestCase):
    W = {"nba": {"Points": 1.0}, "mlbh": {"Home runs": 4.0}}
    S = {("NBA", "Guard"): ("nba", 20.0, 5.0), ("MLB", "Hitter"): ("mlbh", 1.0, 1.0)}

    def test_z_across_sports(self):
        a = {"src": "x", "ext": "a", "name": "A", "sport": "NBA", "position": "Guard", "totals": {"Points": 25 * 25 + 0}}     # 25/wk = z 1.0
        b = {"src": "x", "ext": "b", "name": "B", "sport": "MLB", "position": "Hitter", "totals": {"Home runs": 130}}           # 130*4/26 = 20/wk -> z 19
        c = {"src": "x", "ext": "c", "name": "C", "sport": "NBA", "position": "Guard", "totals": {"Points": 15 * 25}}
        r = pool.rank_pool([a, b, c], self.W, self.S)
        self.assertEqual([x["name"] for x in r], ["B", "A", "C"])
        self.assertEqual([x["ranking"] for x in r], [1, 2, 3])

    def test_seasons_for_a_january_draft(self):
        from datetime import datetime, timezone
        n = datetime(2027, 1, 1, tzinfo=timezone.utc)
        self.assertEqual({sp: pool.default_season(sp, n) for sp in ("MLB", "NHL", "NFL", "F1")}, {"MLB": 2026, "NHL": 2025, "NFL": 2026, "F1": 2027})

    def test_a_short_sample_counts_as_part_season_not_a_hot_streak(self):
        full = {"src": "x", "ext": "a", "name": "A", "sport": "NBA", "position": "Guard", "totals": {"Points": 25 * 25}, "games": 25}
        short = {"src": "x", "ext": "b", "name": "B", "sport": "NBA", "position": "Guard", "totals": {"Points": 3 * 40}, "games": 3}      # 40 a game for 3 games
        r = pool.rank_pool([full, short], self.W, self.S)
        self.assertEqual([x["name"] for x in r], ["A", "B"])

    def test_f1_standings(self):
        doc = {"MRData": {"StandingsTable": {"StandingsLists": [{"DriverStandings": [
            {"position": "1", "Driver": {"driverId": "a", "givenName": "A", "familyName": "One"}},
            {"position": "2", "Driver": {"driverId": "b", "givenName": "B", "familyName": "Two"}}]}]}}}
        drivers = {"MRData": {"DriverTable": {"Drivers": [{"driverId": "n", "givenName": "New", "familyName": "Rookie"}]}}}
        es = pool.f1_pool(doc, drivers)
        self.assertEqual([e["ext"] for e in sorted(es, key=lambda e: -e["score"])], ["a", "b", "n"])

    def test_mlb_pool(self):
        people = {"people": [{"id": 1, "primaryPosition": {"abbreviation": "RF"}}, {"id": 2, "primaryPosition": {"abbreviation": "P"}}]}
        hit = {"stats": [{"splits": [{"player": {"id": 1, "fullName": "Hit Guy"}, "stat": {"hits": 10, "doubles": 2, "triples": 0, "homeRuns": 3, "runs": 5, "rbi": 6, "baseOnBalls": 4, "stolenBases": 1, "strikeOuts": 9}},
                                     {"player": {"id": 2, "fullName": "Pitcher Bat"}, "stat": {"hits": 1}}]}]}
        pit = {"stats": [{"splits": [{"player": {"id": 2, "fullName": "Pitch Guy"}, "stat": {"gamesStarted": 30, "gamesPitched": 30, "inningsPitched": "180.1", "strikeOuts": 200, "earnedRuns": 60, "hits": 150, "baseOnBalls": 40, "wins": 14, "saves": 0}}]}]}
        es = pool.mlb_pool(people, hit, pit)
        self.assertEqual(sorted((e["name"], e["position"]) for e in es), [("Hit Guy", "Hitter"), ("Pitch Guy", "SP")])
        self.assertEqual(es[0]["totals"]["Singles"], 5)

    def test_nhl_pool(self):
        es = pool.nhl_pool({"data": [{"playerId": 1, "skaterFullName": "S K", "goals": 30, "assists": 40, "plusMinus": 5, "shots": 200}]},
                           {"data": [{"playerId": 1, "blockedShots": 50}]},
                           {"data": [{"playerId": 2, "goalieFullName": "G K", "saves": 1500, "goalsAgainst": 120, "wins": 30, "shutouts": 4}]})
        self.assertEqual({(e["name"], e["position"]) for e in es}, {("S K", "Skater"), ("G K", "Goalie")})
        self.assertEqual(es[0]["totals"]["Blocked shots"], 50)


class Injuries(unittest.TestCase):
    def test_words(self):
        n = injuries.norm_status
        self.assertEqual([n("Out"), n("Injured Reserve"), n("10-Day-IL"), n("D60"), n("Day-To-Day"), n("Questionable"), n("Doubtful"), n("PUP-R"), n("Out For Season")],
                         ["Out", "Injured Reserve", "Injured List", "Injured List", "Day-to-day", "Questionable", "Doubtful", "PUP", "Injured Reserve"])
        for dropped in ("Suspension", "Personal", "", None, "Probable", "Healthy"):
            self.assertIsNone(n(dropped))

    NFL = ("season,game_type,team,week,gsis_id,position,full_name,report_status,date_modified\n"
           "2026,REG,KC,3,00-1,QB,Quinn Back,Questionable,2026-09-25\n"
           "2026,REG,KC,4,00-1,QB,Quinn Back,Out,2026-10-02\n"
           "2026,REG,KC,4,00-2,RB,Rudy Runner,Doubtful,2026-10-02\n"
           "2026,REG,KC,4,00-3,K,Kicker Kim,Out,2026-10-02\n"
           "2026,REG,KC,4,00-4,WR,Healthy Joe,,2026-10-02\n")

    def test_nfl_latest_week_only_and_skaters_of_our_positions(self):
        r = injuries.parse_nfl(self.NFL, now=datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.assertEqual({(p["ext"], p["status"]) for p in r["players"]}, {("00-1", "Out"), ("00-2", "Doubtful")})
        self.assertTrue(r["complete"])

    def test_nfl_stale_report_changes_nothing(self):
        self.assertIsNone(injuries.parse_nfl(self.NFL, now=datetime(2027, 2, 20, tzinfo=timezone.utc)))

    def test_mlb_injured_list_codes(self):
        doc = {"roster": [{"person": {"id": 1, "fullName": "A"}, "status": {"code": "A"}}, {"person": {"id": 2, "fullName": "B"}, "status": {"code": "D15"}},
                          {"person": {"id": 3, "fullName": "C"}, "status": {"code": "D60"}}, {"person": {"id": 4, "fullName": "D"}, "status": {"code": "SU"}},
                          {"person": {"id": 5, "fullName": "E"}, "status": {"code": "RM"}}]}
        self.assertEqual([(p["ext"], p["status"]) for p in injuries.parse_mlb_roster(doc)], [("2", "Injured List"), ("3", "Injured List")])

    def test_espn(self):
        doc = {"injuries": [{"displayName": "Team", "injuries": [
            {"athlete": {"id": "9", "displayName": "Hurt Guard"}, "status": "Out"}, {"athlete": {"id": "10", "displayName": "Sore Wing"}, "status": "Day-To-Day"},
            {"athlete": {"id": "11", "displayName": "Away Big"}, "status": "Suspension"}]}]}
        r = injuries.parse_espn(doc, "NBA")
        self.assertEqual([(p["name"], p["status"]) for p in r["players"]], [("Hurt Guard", "Out"), ("Sore Wing", "Day-to-day")])
        self.assertEqual((r["src"], r["sport"]), ("espn", "NBA"))


if __name__ == "__main__":
    unittest.main()
