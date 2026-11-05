import unittest

from bot import parse_minute


class ParseMinuteRegressionTests(unittest.TestCase):
    def test_halftime_overrides_stale_numeric_60(self):
        self.assertEqual(parse_minute({
            "minute": 60,
            "status": "HT",
            "status_text": "Half Time",
        }), 45)

    def test_halftime_with_stoppage_time(self):
        self.assertEqual(parse_minute({
            "minute": 60,
            "status": "break",
            "status_text": "45+2'",
        }), 47)

    def test_second_half_keeps_real_numeric_clock(self):
        self.assertEqual(parse_minute({
            "minute": 60,
            "status": "live",
            "status_text": "2nd half",
        }), 60)

    def test_first_half_keeps_real_numeric_clock(self):
        self.assertEqual(parse_minute({
            "minute": 32,
            "status": "live",
            "status_text": "1st half",
        }), 32)

    def test_missing_clock_is_not_guessed_from_kickoff(self):
        self.assertIsNone(parse_minute({
            "status": "live",
            "status_text": "2nd half",
            "time": "2026-11-05T20:30:00+00:00",
        }))

    def test_halftime_marker_is_not_matched_inside_other_words(self):
        self.assertEqual(parse_minute({
            "minute": 68,
            "status": "live",
            "status_text": "2nd half",
            "description": "shirt sponsor",
        }), 68)


if __name__ == "__main__":
    unittest.main()
