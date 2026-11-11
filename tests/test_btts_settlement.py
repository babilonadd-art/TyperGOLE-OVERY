import unittest
from unittest.mock import patch

import requests
import bot
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import btts_first_half_won, kickoff_reached_for_settlement, pick_local_date


class BttsFirstHalfSettlementTests(unittest.TestCase):
    def test_pick_local_date_uses_warsaw_kickoff_date(self):
        pick = {"kickoff_time": "2026-11-08T00:30:00+01:00", "date": "2026-11-07"}
        self.assertEqual(str(pick_local_date(pick)), "2026-11-08")

    def test_pick_local_date_falls_back_to_date_field(self):
        self.assertEqual(str(pick_local_date({"date": "2026-11-08"})), "2026-11-08")

    def test_pick_without_any_date_is_not_eligible(self):
        self.assertIsNone(pick_local_date({}))

    def test_future_kickoff_cannot_be_settled(self):
        pick = {"kickoff_time": "2026-11-07T21:00:00+01:00"}
        now = datetime.fromisoformat("2026-11-07T17:45:00+01:00")
        self.assertFalse(kickoff_reached_for_settlement(pick, now))

    def test_settlement_allowed_after_kickoff(self):
        pick = {"kickoff_time": "2026-11-07T21:00:00+01:00"}
        now = datetime.fromisoformat("2026-11-07T21:01:00+01:00")
        self.assertTrue(kickoff_reached_for_settlement(pick, now))

    def test_missing_kickoff_blocks_automatic_settlement(self):
        self.assertFalse(kickoff_reached_for_settlement({}))


    def test_sportscore_archive_can_reconcile_picks_from_previous_days(self):
        fixture = {
            "id": "historic-fixture-1",
            "home": "Home FC",
            "away": "Away FC",
            "time": "2026-11-08T12:30:00+01:00",
            "status": "finished",
            "home_score": 2,
            "away_score": 1,
        }
        pick = {
            "home": "Home FC",
            "away": "Away FC",
            "date": "2026-11-08",
            "kickoff_time": "2026-11-08T12:30:00+01:00",
            "league": "Test League",
        }
        with (
            patch.object(bot, "api_get", return_value={"data": [fixture]}) as fetch,
            patch.object(bot, "extract_matches", return_value=[fixture]),
            patch.object(bot, "settlement_fixture_matches_pick", return_value=True),
            patch.object(bot, "confirmed_final_fixture", return_value=True),
        ):
            result = bot.sportscore_result_for_pick(pick)
        self.assertIs(result, fixture)
        self.assertTrue(fetch.called)

    def test_both_teams_scored_before_break_is_win(self):
        self.assertTrue(btts_first_half_won(1, 1))
        self.assertTrue(btts_first_half_won(2, 1))

    def test_either_team_without_goal_is_loss(self):
        self.assertFalse(btts_first_half_won(1, 0))
        self.assertFalse(btts_first_half_won(0, 2))
        self.assertFalse(btts_first_half_won(0, 0))

    def test_missing_or_invalid_ht_score_is_not_settled(self):
        self.assertIsNone(btts_first_half_won(None, 1))
        self.assertIsNone(btts_first_half_won(1, None))
        self.assertIsNone(btts_first_half_won(-1, 1))



class BttsDailyCloseTests(unittest.TestCase):
    def test_daily_net_profit_includes_tax_and_losses(self):
        picks = [
            {"stake": 10, "odds": 2.00, "won": True},
            {"stake": 10, "odds": 1.80, "won": False},
        ]
        self.assertEqual(bot.daily_btts_net_profit(picks), -2.40)

    def test_void_pick_has_zero_profit(self):
        self.assertEqual(bot.daily_btts_net_profit([{
            "stake": 10, "odds": 2.00, "won": None, "settlement_outcome": "void",
        }]), 0.00)

    def test_telegram_reply_never_falls_back_to_standalone_post(self):
        response = requests.Response()
        response.status_code = 400
        error = requests.HTTPError("reply rejected", response=response)
        with patch.object(bot, "telegram", side_effect=error) as send:
            with self.assertRaises(requests.HTTPError):
                bot.telegram_reply("Rozliczenie", 123)
        send.assert_called_once_with("Rozliczenie", reply_to_message_id=123)

    def test_daily_close_waits_until_every_pick_is_settled(self):
        state = {"picks": [{
            "market": bot.BTTS_FT_MARKET, "delivery_status": "sent",
            "date": "2026-11-11", "settled": False, "telegram_message_id": 123,
        }]}
        now = datetime(2026, 11, 12, 0, 5, tzinfo=ZoneInfo("Europe/Warsaw"))
        with patch.object(bot, "now_local", return_value=now), patch.object(bot, "telegram_daily_btts_closer") as closer, patch.object(bot, "save_state"):
            bot.send_daily_btts_closers(state)
        closer.assert_not_called()
        self.assertNotIn("2026-11-11", state.get("daily_btts_closers_sent", []))

    def test_daily_close_sends_profit_and_marks_day_closed(self):
        state = {"picks": [{
            "market": bot.BTTS_FT_MARKET, "delivery_status": "sent",
            "date": "2026-11-11", "settled": True, "won": True,
            "odds": 2.00, "stake": 10, "telegram_message_id": 123,
        }]}
        now = datetime(2026, 11, 12, 0, 5, tzinfo=ZoneInfo("Europe/Warsaw"))
        response = {"result": {"message_id": 456}}
        with patch.object(bot, "now_local", return_value=now), patch.object(bot, "telegram_daily_btts_closer", return_value=response) as closer, patch.object(bot, "save_state"):
            bot.send_daily_btts_closers(state)
        closer.assert_called_once()
        self.assertIn("2026-11-11", state["daily_btts_closers_sent"])
        self.assertEqual(state["daily_btts_daily_profit"]["2026-11-11"], 7.60)

if __name__ == "__main__":
    unittest.main()
