import unittest
from engine import load_config, frozen_baselines, compute_baselines
from engine.scoring import PlayerWeek

CFG = load_config()


def wk(key, natives, games=None):
    sport, pos = key.split("|")
    g = games if games is not None else CFG["min_games"][key]
    return {f"{key}{i}": PlayerWeek(sport, pos, v, g) for i, v in enumerate(natives)}


class Frozen(unittest.TestCase):
    def test_pools_only_the_last_four_weeks_before_this_one(self):
        hist = {w: wk("NFL|QB", [10 + w, 14 + w, 18 + w, 12 + w, 16 + w, 20 + w]) for w in range(1, 8)}
        b = frozen_baselines(CFG, hist, 8)["NFL|QB"]
        pooled = {}
        for w in (4, 5, 6, 7):
            pooled.update({(w, p): pw for p, pw in hist[w].items()})
        want = compute_baselines(CFG, pooled)["NFL|QB"]
        self.assertEqual((b.avg, b.sd, b.fallback), (want.avg, want.sd, False))

    def test_never_looks_at_the_week_being_scored_or_later(self):
        hist = {1: wk("NFL|QB", [10, 12, 14, 16, 18, 20]), 2: wk("NFL|QB", [99, 98, 97, 96, 95, 94])}
        self.assertEqual(frozen_baselines(CFG, hist, 2)["NFL|QB"].avg, 15)

    def test_skips_weeks_when_a_sport_did_not_play(self):
        qb = wk("NFL|QB", [10, 12, 14, 16, 18, 20])
        hist = {1: qb, 2: wk("MLB|SP", [5, 6, 7, 8, 9, 10]), 3: wk("MLB|SP", [5, 6, 7, 8, 9, 10]),
                4: wk("MLB|SP", [5, 6, 7, 8, 9, 10]), 5: wk("MLB|SP", [5, 6, 7, 8, 9, 10])}
        b = frozen_baselines(CFG, hist, 6)["NFL|QB"]
        self.assertFalse(b.fallback)                    # week 1 still counts: the four later weeks had no NFL
        self.assertEqual(b.avg, 15)

    def test_week_with_only_cameos_does_not_use_up_a_slot(self):
        cameo = wk("MLB|Hitter", [30, 31, 32, 33, 34, 35], games=1)       # below the 3-game minimum
        full = wk("MLB|Hitter", [10, 14, 18, 12, 16, 20])
        hist = {1: full, 2: cameo}
        self.assertEqual(frozen_baselines(CFG, hist, 3)["MLB|Hitter"].avg, 15)

    def test_falls_back_when_the_pool_is_too_small(self):
        hist = {1: wk("NHL|Goalie", [8, 12, 10])}                          # 3 < min_pool_size (5)
        b = frozen_baselines(CFG, hist, 2)["NHL|Goalie"]
        self.assertTrue(b.fallback)
        self.assertEqual((b.avg, b.sd), tuple(CFG["baselines_fallback"]["NHL|Goalie"]))

    def test_first_week_has_no_history_so_every_sport_uses_fallbacks(self):
        b = frozen_baselines(CFG, {}, 1)
        self.assertEqual(set(b), set(CFG["baselines_fallback"]))
        self.assertTrue(all(x.fallback for x in b.values()))


if __name__ == "__main__":
    unittest.main()
