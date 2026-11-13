import unittest
from unittest.mock import patch

import requests
import bot
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import btts_first_half_won, kickoff_reached_for_settlement, pick_local_date



class BttsTelegramMessageTests(unittest.TestCase):
    def test_ft_pick_message_is_short_and_shows_model_probability(self):
        pick = {
            "home": "Home FC",
            "away": "Away FC",
            "league": "Test League",
            "kickoff_time": "2026-11-12T18:30:00+01:00",
            "odds": 1.85,
            "stake": 10,
            "bet_url": "https://example.com/match",
            "analysis": {
                "metrics": {
                    "model_probability": 0.60,
                    "home_btts": 0.80,
                    "away_btts": 0.75,
                    "home_recent_btts": 4,
                    "away_recent_btts": 3,
                }
            },
        }
        message = bot.format_btts_ft_pick_message(pick, 3)
        self.assertIn("Szacowana szansa BTTS do końca meczu: 60%", message)
        self.assertIn("Home FC – Away FC", message)
        self.assertIn("Kurs: 1.85", message)
        self.assertNotIn("BTTS sezon", message)
        self.assertNotIn("Ostatnie mecze", message)
        self.assertNotIn("gospodarz", message)
        self.assertNotIn("H2H", message)

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

    def test_telegram_reply_falls_back_when_original_post_was_deleted(self):
        with patch.object(
            bot, "telegram",
            side_effect=[
                RuntimeError('Telegram API HTTP 400: {"ok":false,"description":"Bad Request: message to be replied not found"}'),
                {"ok": True, "result": {"message_id": 456}},
            ],
        ) as send:
            result = bot.telegram_reply("Rozliczenie", 123)
        self.assertEqual(result["result"]["message_id"], 456)
        self.assertEqual(send.call_args_list[0].kwargs, {"reply_to_message_id": 123})
        self.assertEqual(send.call_args_list[1].args, ("Rozliczenie",))
        self.assertEqual(send.call_args_list[1].kwargs, {})

    def test_telegram_reply_does_not_fallback_for_other_errors(self):
        error = RuntimeError("Telegram API HTTP 403: bot was blocked")
        with patch.object(bot, "telegram", side_effect=error) as send:
            with self.assertRaisesRegex(RuntimeError, "bot was blocked"):
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


class BttsQualityGateTests(unittest.TestCase):
    def test_rejects_negative_value_over_market(self):
        self.assertFalse(bot.btts_value_gate(0.55, 1.70))

    def test_rejects_probability_below_relaxed_minimum(self):
        self.assertFalse(bot.btts_value_gate(0.49, 2.40))

    def test_accepts_probability_and_nonnegative_market_edge(self):
        self.assertTrue(bot.btts_value_gate(0.62, 1.70))

    def test_rejects_invalid_odds_or_probability(self):
        self.assertFalse(bot.btts_value_gate(0.70, 1.0))
        self.assertFalse(bot.btts_value_gate(None, 2.0))
        self.assertFalse(bot.btts_value_gate(1.2, 2.0))


    def test_missing_optional_attack_stats_do_not_change_model(self):
        base = {
            "btts": 0.60, "last5": 3, "last5_played": 5, "played": 8,
            "goals_for_avg": 1.5, "goals_against_avg": 1.2,
            "shots_sample": 0, "shots_avg": None,
            "shots_on_target_sample": 0, "shots_on_target_avg": None,
            "corners_sample": 0, "corners_avg": None,
        }
        venue = dict(base, played=5)
        estimate = bot.btts_probability_estimate(base, base, venue, venue)
        self.assertEqual(estimate["activity_adjustment"], 0.0)

    def test_small_samples_are_shrunk_toward_neutral(self):
        high = {
            "btts": 1.0, "last5": 5, "last5_played": 5, "played": 5,
            "goals_for_avg": 2.0, "goals_against_avg": 2.0,
            "shots_sample": 0, "shots_avg": None,
            "shots_on_target_sample": 0, "shots_on_target_avg": None,
            "corners_sample": 0, "corners_avg": None,
        }
        venue = dict(high, played=3)
        estimate = bot.btts_probability_estimate(high, high, venue, venue)
        self.assertLess(estimate["probability"], estimate["raw_probability"])
        self.assertGreater(estimate["probability"], 0.50)


if __name__ == "__main__":
    unittest.main()
