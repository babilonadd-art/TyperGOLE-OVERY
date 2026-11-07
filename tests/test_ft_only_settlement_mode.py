import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
