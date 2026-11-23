import unittest
from unittest.mock import patch

import over_live


class FotMobNullFeedTests(unittest.TestCase):
    def test_skips_null_leagues_and_matches(self):
        payload = {
            "leagues": [
                None,
                {"name": "Liga testowa", "matches": [None, {"id": "123"}]},
                "invalid",
                {"name": "Bez meczów", "matches": None},
            ]
        }
        with patch("over_live.get_json", return_value=payload):
            rows = over_live.fotmob_matches()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "123")
        self.assertEqual(rows[0]["_league_name"], "Liga testowa")

    def test_null_match_status_is_safe(self):
        self.assertIsNone(over_live.fotmob_minute({"status": None}))

    def test_null_stats_payload_is_safe(self):
        self.assertEqual(over_live.stat_rows(None), {})

    def test_missing_team_objects_do_not_crash_normalization(self):
        normalized = over_live.normalize_match({
            "id": "123", "status": None, "home": None, "away": None,
            "homeScore": 0, "awayScore": 0,
        })
        self.assertEqual(normalized["home"], "Gospodarze")
        self.assertEqual(normalized["away"], "Goście")
        self.assertIsNone(normalized["minute"])


if __name__ == "__main__":
    unittest.main()
