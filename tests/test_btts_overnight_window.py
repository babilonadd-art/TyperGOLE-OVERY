import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import bot


class BttsOvernightWindowTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Warsaw")

    def test_early_today_scans_today(self):
        now = datetime(2026, 11, 21, 0, 15, tzinfo=self.tz)
        self.assertEqual(bot.btts_overnight_scan_date(now).isoformat(), "2026-11-21")

    def test_evening_scans_remaining_fixtures_today(self):
        now = datetime(2026, 11, 21, 18, 30, tzinfo=self.tz)
        self.assertEqual(bot.btts_overnight_scan_date(now).isoformat(), "2026-11-21")

    def test_two_am_is_a_night_match(self):
        kickoff = datetime(2026, 11, 22, 2, 0, tzinfo=self.tz)
        self.assertTrue(bot.btts_is_overnight_kickoff(kickoff))

    def test_six_thirty_pm_is_not_a_night_match(self):
        kickoff = datetime(2026, 11, 22, 18, 30, tzinfo=self.tz)
        self.assertFalse(bot.btts_is_overnight_kickoff(kickoff))

    def test_six_am_is_outside_the_night_window(self):
        kickoff = datetime(2026, 11, 22, 6, 0, tzinfo=self.tz)
        self.assertFalse(bot.btts_is_overnight_kickoff(kickoff))


if __name__ == "__main__":
    unittest.main()
