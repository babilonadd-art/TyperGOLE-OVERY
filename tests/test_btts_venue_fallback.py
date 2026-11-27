import unittest

import bot


class BttsVenueFallbackTests(unittest.TestCase):
    def test_sparse_venue_uses_overall_form_but_keeps_real_sample(self):
        overall = {"played": 8, "btts": 0.75, "goals_for_avg": 1.8}
        venue = {"played": 1, "btts": 0.0, "goals_for_avg": 0.5}
        selected, used_fallback = bot.btts_venue_fallback(overall, venue)
        self.assertTrue(used_fallback)
        self.assertEqual(selected["played"], 1)
        self.assertEqual(selected["btts"], 0.75)
        self.assertEqual(selected["goals_for_avg"], 1.8)

    def test_zero_venue_matches_does_not_discard_overall_form(self):
        overall = {"played": 6, "btts": 0.5}
        venue = {"played": 0, "btts": 0.0}
        selected, used_fallback = bot.btts_venue_fallback(overall, venue)
        self.assertTrue(used_fallback)
        self.assertEqual(selected["played"], 0)
        self.assertEqual(selected["btts"], 0.5)

    def test_sufficient_venue_sample_is_kept(self):
        overall = {"played": 9, "btts": 0.6}
        venue = {"played": 2, "btts": 1.0}
        selected, used_fallback = bot.btts_venue_fallback(overall, venue)
        self.assertFalse(used_fallback)
        self.assertEqual(selected, venue)


class BttsOpponentStrengthTests(unittest.TestCase):
    def test_uses_only_current_competition_and_bounds_adjustment(self):
        from datetime import date

        results = [
            {"date": "2026-08-01", "home": "Alpha", "away": "Beta", "ft_home": 2, "ft_away": 0, "_league": "Test League"},
            {"date": "2026-08-08", "home": "Gamma", "away": "Alpha", "ft_home": 1, "ft_away": 1, "_league": "Test League"},
            {"date": "2026-08-15", "home": "Alpha", "away": "Delta", "ft_home": 1, "ft_away": 0, "_league": "Test League"},
            {"date": "2026-08-22", "home": "Beta", "away": "Gamma", "ft_home": 0, "ft_away": 2, "_league": "Test League"},
            {"date": "2026-08-29", "home": "Delta", "away": "Beta", "ft_home": 1, "ft_away": 1, "_league": "Test League"},
            {"date": "2026-09-05", "home": "Gamma", "away": "Delta", "ft_home": 3, "ft_away": 1, "_league": "Test League"},
            {"date": "2026-09-12", "home": "Alpha", "away": "Gamma", "ft_home": 1, "ft_away": 2, "_league": "Test League"},
            {"date": "2026-09-19", "home": "Delta", "away": "Alpha", "ft_home": 0, "ft_away": 1, "_league": "Test League"},
            {"date": "2026-09-26", "home": "Beta", "away": "Delta", "ft_home": 2, "ft_away": 2, "_league": "Test League"},
            {"date": "2026-10-03", "home": "Gamma", "away": "Beta", "ft_home": 1, "ft_away": 0, "_league": "Test League"},
        ]
        result = bot.btts_opponent_strength_adjustment(
            results, "Alpha", "Beta", date(2026, 10, 10), date(2026, 7, 1), "Test League"
        )
        self.assertGreaterEqual(result["adjustment"], -0.015)
        self.assertLessEqual(result["adjustment"], 0.015)
        self.assertGreaterEqual(result["sample_home"], 3)
        self.assertGreaterEqual(result["sample_away"], 3)

    def test_missing_competition_data_is_neutral(self):
        from datetime import date

        result = bot.btts_opponent_strength_adjustment(
            [], "Alpha", "Beta", date(2026, 10, 10), date(2026, 7, 1), "Test League"
        )
        self.assertEqual(result["adjustment"], 0.0)
        self.assertIsNone(result["league_average_ppg"])


if __name__ == "__main__":
    unittest.main()
