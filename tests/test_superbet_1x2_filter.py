import unittest
from unittest.mock import Mock, patch

import requests

from bot import extract_superbet_1x2_full_time, btts_prematch_odds_gate, btts_1x2_risk_gate, telegram_reply


class SuperbetFullTime1X2Tests(unittest.TestCase):
    def test_reads_active_home_draw_away_prices_from_polish_match_market(self):
        payload = {"markets": [{
            "marketName": "Mecz",
            "marketGroupId": 547,
            "outcomes": [
                {"name": "1", "price": "2.87", "status": "active"},
                {"name": "X", "price": "3.40", "status": "active"},
                {"name": "2", "price": "2.10", "status": "active"},
            ],
        }]}
        self.assertEqual(
            extract_superbet_1x2_full_time(payload),
            {"home": 2.87, "draw": 3.4, "away": 2.1, "market_group_id": 547},
        )

    def test_rejects_incomplete_or_inactive_market(self):
        payload = {"markets": [{
            "marketName": "Mecz", "marketGroupId": 547,
            "outcomes": [
                {"name": "1", "price": "2.87", "status": "active"},
                {"name": "X", "price": "3.40", "status": "active"},
                {"name": "2", "price": "2.10", "status": "blocked"},
            ],
        }]}
        self.assertIsNone(extract_superbet_1x2_full_time(payload))

    def test_ignores_other_market_groups_and_correct_score_prices(self):
        payload = {"markets": [
            {"marketName": "Mecz", "marketGroupId": 537,
             "outcomes": [{"name": "1", "price": "45", "status": "active"}]},
            {"marketName": "Dokładny wynik", "marketGroupId": 547,
             "outcomes": [
                 {"name": "1", "price": "45", "status": "active"},
                 {"name": "X", "price": "3.4", "status": "active"},
                 {"name": "2", "price": "2.1", "status": "active"},
             ]},
        ]}
        self.assertIsNone(extract_superbet_1x2_full_time(payload))

    def test_keeps_complete_prices_even_when_home_or_away_is_above_ten_for_caller_filter(self):
        payload = {"markets": [{
            "marketName": "Mecz", "marketGroupId": 547,
            "outcomes": [
                {"name": "1", "price": "45", "status": "active"},
                {"name": "X", "price": "8.0", "status": "active"},
                {"name": "2", "price": "1.05", "status": "active"},
            ],
        }]}
        quote = extract_superbet_1x2_full_time(payload)
        self.assertIsNotNone(quote)
        self.assertGreater(quote["home"], 10.0)


class BttsOnlyPrematchOddsGateTests(unittest.TestCase):
    def test_accepts_btts_odds_at_both_range_boundaries(self):
        self.assertTrue(btts_prematch_odds_gate({"odds": 1.55}))
        self.assertTrue(btts_prematch_odds_gate({"odds": 2.40}))

    def test_accepts_btts_odds_inside_range(self):
        self.assertTrue(btts_prematch_odds_gate({"odds": 1.90}))

    def test_rejects_btts_odds_outside_range(self):
        self.assertFalse(btts_prematch_odds_gate({"odds": 1.54}))
        self.assertFalse(btts_prematch_odds_gate({"odds": 2.41}))

    def test_rejects_missing_or_invalid_btts_quote(self):
        self.assertFalse(btts_prematch_odds_gate(None))
        self.assertFalse(btts_prematch_odds_gate({"odds": "not-a-price"}))



class BttsUnderdogRiskGateTests(unittest.TestCase):
    def setUp(self):
        self.metrics = {
            "home_scored_rate": 0.80,
            "away_scored_rate": 0.72,
            "home_venue_scored_rate": 0.82,
            "away_venue_scored_rate": 0.70,
            "home_venue_goals_for_avg": 1.35,
            "away_venue_goals_for_avg": 0.95,
        }

    def test_rejects_underdog_at_eight_or_higher_even_if_scoring_numbers_are_good(self):
        ok, reason = btts_1x2_risk_gate(
            {"home": 1.30, "draw": 5.0, "away": 9.00}, self.metrics
        )
        self.assertFalse(ok)
        self.assertIn("9.00", reason)

    def test_requires_strong_underdog_attack_between_six_and_eight(self):
        weak = dict(self.metrics, away_scored_rate=0.50, away_venue_scored_rate=0.45)
        ok, reason = btts_1x2_risk_gate(
            {"home": 1.35, "draw": 4.8, "away": 6.50}, weak
        )
        self.assertFalse(ok)
        self.assertIn("słaba ofensywa", reason)

    def test_accepts_six_to_eight_only_with_strong_scoring_evidence(self):
        ok, reason = btts_1x2_risk_gate(
            {"home": 1.35, "draw": 4.8, "away": 6.50}, self.metrics
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_does_not_add_extra_restriction_to_balanced_1x2_market(self):
        ok, reason = btts_1x2_risk_gate(
            {"home": 2.20, "draw": 3.2, "away": 3.10}, self.metrics
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_missing_1x2_market_preserves_existing_behavior(self):
        self.assertEqual(btts_1x2_risk_gate(None, self.metrics), (True, ""))


class TelegramSettlementDeliveryTests(unittest.TestCase):
    def test_http_400_reply_failure_does_not_publish_standalone_settlement(self):
        response = Mock()
        response.status_code = 400
        response.json.return_value = {"ok": False, "description": "Bad Request: message to be replied not found"}
        error = requests.HTTPError("400 Client Error", response=response)
        with patch("bot.telegram", side_effect=error) as send:
            with self.assertRaises(requests.HTTPError):
                telegram_reply("WYGRANA\\nDrużyna A - Drużyna B\\nWynik: 1-1", 123)
        send.assert_called_once_with(
            "WYGRANA\\nDrużyna A - Drużyna B\\nWynik: 1-1",
            reply_to_message_id=123,
        )

    def test_non_400_delivery_errors_are_not_retried(self):
        response = Mock()
        response.status_code = 500
        error = requests.HTTPError("500 Server Error", response=response)
        with patch("bot.telegram", side_effect=error) as send:
            with self.assertRaises(requests.HTTPError):
                telegram_reply("WYGRANA", 123)
        send.assert_called_once_with("WYGRANA", reply_to_message_id=123)


if __name__ == "__main__":
    unittest.main()
