import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import prematch_btts as bot


class PrematchBttsTests(unittest.TestCase):
    def test_normalize_polish_names(self):
        self.assertEqual(bot.normalize("Łódź FC"), "lodz fc")

    def test_beta_smoothing(self):
        self.assertAlmostEqual(bot.rate(0, 5), 1 / 7)
        self.assertAlmostEqual(bot.rate(5, 5), 6 / 7)

    def test_probability_requires_minimum_venue_samples(self):
        with patch.object(bot, "history_rows", return_value=[
            {"home": "Home FC", "away": "Other", "ht_home": 1, "ht_away": 0, "both": False}
        ]):
            self.assertIsNone(bot.estimate_btts_probability("Home FC", "Away FC"))

    def test_probability_is_bounded_and_uses_home_away_split(self):
        home_rows = [
            {"home": "Home FC", "away": f"Opponent {i}", "ht_home": 1, "ht_away": 1 if i % 2 else 0, "both": i % 2 == 1}
            for i in range(8)
        ]
        away_rows = [
            {"home": f"Opponent {i}", "away": "Away FC", "ht_home": 1 if i % 2 else 0, "ht_away": 1, "both": i % 2 == 1}
            for i in range(8)
        ]

        def history(team):
            return home_rows if team == "Home FC" else away_rows

        with patch.object(bot, "history_rows", side_effect=history):
            result = bot.estimate_btts_probability("Home FC", "Away FC")
        self.assertIsNotNone(result)
        self.assertGreater(result["probability"], 0)
        self.assertLess(result["probability"], 1)
        self.assertEqual(result["home_sample"], 8)
        self.assertEqual(result["away_sample"], 8)

    def test_kickoff_rejects_missing_timezone_by_assuming_utc(self):
        parsed = bot.kickoff({"kickoff": "2026-11-06T18:00:00"})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, bot.TZ)


if __name__ == "__main__":
    unittest.main()
