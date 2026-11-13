import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import bot


class BttsNewDayScanGateTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Warsaw")

    def test_blocks_new_day_before_0800(self):
        now = datetime(2026, 11, 13, 7, 59, tzinfo=self.tz)
        allowed, reason = bot.btts_new_day_scan_gate({"picks": []}, now)
        self.assertFalse(allowed)
        self.assertIn("08:00", reason)

    def test_blocks_until_previous_day_picks_are_settled(self):
        now = datetime(2026, 11, 13, 8, 0, tzinfo=self.tz)
        state = {
            "picks": [{
                "market": bot.BTTS_FT_MARKET,
                "delivery_status": "sent",
                "date": "2026-11-12",
                "settled": False,
                "telegram_message_id": 123,
            }],
            "daily_btts_closers_sent": ["2026-11-12"],
        }
        allowed, reason = bot.btts_new_day_scan_gate(state, now)
        self.assertFalse(allowed)
        self.assertIn("nierozliczonych", reason)

    def test_blocks_until_previous_day_closer_is_sent(self):
        now = datetime(2026, 11, 13, 8, 0, tzinfo=self.tz)
        state = {
            "picks": [{
                "market": bot.BTTS_FT_MARKET,
                "delivery_status": "sent",
                "date": "2026-11-12",
                "settled": True,
                "telegram_message_id": 123,
            }],
            "daily_btts_closers_sent": [],
        }
        allowed, reason = bot.btts_new_day_scan_gate(state, now)
        self.assertFalse(allowed)
        self.assertIn("nie ma jeszcze wysłanego zamknięcia", reason)

    def test_allows_new_day_after_previous_day_is_settled_and_closed(self):
        now = datetime(2026, 11, 13, 8, 0, tzinfo=self.tz)
        state = {
            "picks": [{
                "market": bot.BTTS_FT_MARKET,
                "delivery_status": "sent",
                "date": "2026-11-12",
                "settled": True,
                "telegram_message_id": 123,
                "settlement_notification_retry": False,
            }],
            "daily_btts_closers_sent": ["2026-11-12"],
        }
        allowed, reason = bot.btts_new_day_scan_gate(state, now)
        self.assertTrue(allowed)
        self.assertIn("rozliczony i zamknięty", reason)

    def test_allows_first_day_when_there_were_no_previous_picks(self):
        now = datetime(2026, 11, 13, 8, 0, tzinfo=self.tz)
        allowed, _ = bot.btts_new_day_scan_gate({"picks": []}, now)
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
