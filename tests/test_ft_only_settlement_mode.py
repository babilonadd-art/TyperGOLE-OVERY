import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import bot
from bot import BTTS_FT_MARKET, settle_picks


class SettlementMarketIsolationTests(unittest.TestCase):
    def test_ft_only_mode_does_not_touch_legacy_first_half_picks(self):
        state = {"picks": [{
            "key": "legacy:BTTS_HT",
            "market": bot.BTTS_HT_MARKET,
            "delivery_status": "sent",
            "settled": False,
            "home": "Home",
            "away": "Away",
            "kickoff_time": "2026-11-07T10:00:00+01:00",
            "slug": "home-vs-away",
        }]}
        with patch.object(bot, "match_detail") as match_detail, patch.object(bot, "save_state"):
            settle_picks(state, allowed_markets={BTTS_FT_MARKET})
        match_detail.assert_not_called()
        self.assertFalse(state["picks"][0]["settled"])


class PreviousDayBttsFtSettlementTests(unittest.TestCase):
    def test_previous_day_btts_ft_pick_is_retried_and_posted(self):
        tz = ZoneInfo("Europe/Warsaw")
        now = datetime(2026, 11, 10, 23, 30, tzinfo=tz)
        pick = {
            "key": "previous-day:BTTS_FT",
            "market": BTTS_FT_MARKET,
            "delivery_status": "sent",
            "settled": False,
            "home": "Home",
            "away": "Away",
            "league": "League",
            "date": "2026-11-09",
            "kickoff_time": "2026-11-09T20:00:00+01:00",
            "odds": 1.90,
            "stake": 10,
            "telegram_message_id": 456,
        }
        match = {
            "home": "Home",
            "away": "Away",
            "status": "finished",
            "home_score": 1,
            "away_score": 1,
        }
        state = {"picks": [pick]}
        with patch.object(bot, "now_local", return_value=now), \\
             patch.object(bot, "match_detail", return_value=match), \\
             patch.object(bot, "telegram_reply") as reply, \\
             patch.object(bot, "save_state"):
            settle_picks(state, allowed_markets={BTTS_FT_MARKET})
        self.assertTrue(pick["settled"])
        self.assertTrue(pick["won"])
        self.assertEqual(pick["settled_score"], "1-1")
        reply.assert_called_once()


if __name__ == "__main__":
    unittest.main()
