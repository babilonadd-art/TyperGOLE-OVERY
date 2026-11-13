import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import bot


class NationsLeagueBttsTests(unittest.TestCase):
    def test_recognizes_common_nations_league_labels(self):
        for label in (
            "UEFA Nations League",
            "Nations League",
            "Liga Narodów UEFA",
        ):
            with self.subTest(label=label):
                self.assertTrue(bot.is_uefa_nations_league_competition(label))

    def test_does_not_use_club_history_for_nations_league(self):
        kickoff = datetime(2026, 11, 13, 20, 45, tzinfo=ZoneInfo("Europe/Warsaw"))
        # Even if a similarly named club fixture exists in Open Matchday,
        # it must not be treated as national-team history.
        data = {
            "fixtures": [],
            "results": [{
                "date": "2026-10-01",
                "home": "Netherlands",
                "away": "Greece",
                "ft_home": "2",
                "ft_away": "1",
                "_league": "Club Friendly",
                "_folder": "club_friendly",
            }],
        }
        with patch.object(bot, "open_matchday_data", return_value=data):
            analysis = bot.btts_recent_form_analysis(
                "Netherlands",
                "Greece",
                kickoff,
                "UEFA Nations League",
                1.95,
                {"home": 2.10, "away": 3.40},
            )

        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["method"], "market_only_no_open_matchday_history")
        self.assertEqual(analysis["samples"]["home"], 0)
        self.assertEqual(analysis["samples"]["away"], 0)


if __name__ == "__main__":
    unittest.main()
