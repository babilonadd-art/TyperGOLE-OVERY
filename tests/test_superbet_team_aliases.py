import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import bot

from bot import (
    normalized_team_name, team_name_aliases, find_superbet_event,
    extract_superbet_live_over_odds, extract_superbet_btts_first_half,
    first_half_btts_probability, same_fixture_pick_exists,
)


class TestSuperbetTeamAliases(unittest.TestCase):
    def test_womens_marker_matches_women_but_not_mens(self):
        self.assertIn("atlante women", team_name_aliases("Atlante W"))
        self.assertIn("atlante women", team_name_aliases("Atlante FC (K)"))
        self.assertNotEqual(normalized_team_name("Tigres Women"), normalized_team_name("Tigres"))

    def test_region_suffix_is_optional(self):
        self.assertIn("londrina", team_name_aliases("Londrina PR"))
        self.assertIn("palmeiras", team_name_aliases("Palmeiras - SP"))

    def test_known_inter_variation(self):
        self.assertIn("inter san carlos", team_name_aliases("International San Carlos"))

    def test_unique_oriented_fixture_required(self):
        events = [
            {"event_id": "1", "text": "Atlante FC (K) · UANL Tigres (K)"},
            {"event_id": "2", "text": "Atlante FC · UANL Tigres"},
        ]
        self.assertEqual(find_superbet_event("Atlante W", "Tigres Women", events)["event_id"], "1")

    def test_standard_superbet_total_goals_market_is_full_match(self):
        event_detail = {
            "markets": [{
                "marketName": "Liczba goli",
                "odds": [
                    {"status": "active", "name": "powyżej 3.5", "price": 1.92},
                    {"status": "active", "name": "poniżej 3.5", "price": 1.88},
                ],
            }]
        }
        quote = extract_superbet_live_over_odds(event_detail, "FT", 3.5)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["line"], 3.5)
        self.assertEqual(quote["odds"], 1.92)

    def test_minute_limited_total_market_is_rejected(self):
        event_detail = {
            "markets": [{
                "marketName": "Liczba goli - do 60 minuty",
                "odds": [{"status": "active", "name": "powyżej 2.5", "price": 1.92}],
            }]
        }
        self.assertIsNone(extract_superbet_live_over_odds(event_detail, "FT", 2.5))

    def test_first_half_total_market_is_rejected_for_ft(self):
        event_detail = {
            "markets": [{
                "marketName": "1. połowa - Liczba goli",
                "odds": [{"status": "active", "name": "powyżej 1.5", "price": 1.92}],
            }]
        }
        self.assertIsNone(extract_superbet_live_over_odds(event_detail, "FT", 1.5))


if __name__ == "__main__":
    unittest.main()


class PreMatchBTTSFirstHalfTests(unittest.TestCase):
    def test_extracts_only_active_yes_from_explicit_first_half_btts_market(self):
        payload = {
            "markets": [{
                "marketName": "Obie drużyny strzelą gola - 1. połowa",
                "odds": [
                    {"status": "active", "name": "Tak", "price": 3.20},
                    {"status": "active", "name": "Nie", "price": 1.30},
                ],
            }]
        }
        quote = extract_superbet_btts_first_half(payload)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["selection"], "Tak")
        self.assertEqual(quote["odds"], 3.20)

    def test_rejects_full_match_btts_market(self):
        payload = {
            "markets": [{
                "marketName": "Obie drużyny strzelą gola - cały mecz",
                "odds": [{"status": "active", "name": "Tak", "price": 1.80}],
            }]
        }
        self.assertIsNone(extract_superbet_btts_first_half(payload))

    def test_rejects_blocked_quote(self):
        payload = {
            "markets": [{
                "marketName": "Obie drużyny strzelą gola - 1. połowa",
                "odds": [{"status": "blocked", "name": "Tak", "price": 3.20}],
            }]
        }
        self.assertIsNone(extract_superbet_btts_first_half(payload))

    def test_probability_uses_both_team_first_half_profiles(self):
        home = {"sample": 10, "scored_avg": 0.8, "conceded_avg": 0.5,
                "scored_rate": 0.7, "conceded_rate": 0.5}
        away = {"sample": 10, "scored_avg": 0.7, "conceded_avg": 0.6,
                "scored_rate": 0.6, "conceded_rate": 0.6}
        result = first_half_btts_probability(home, away)
        self.assertIsNotNone(result)
        self.assertGreater(result["probability"], 0)
        self.assertLess(result["probability"], 1)
        self.assertEqual(result["home_sample"], 10)
        self.assertEqual(result["away_sample"], 10)


if __name__ == "__main__":
    unittest.main()


class BTTSFirstHalfSettlementTests(unittest.TestCase):
    def test_btts_ht_pick_settles_without_live_baseline_score(self):
        state = {
            "picks": [{
                "key": "fixture-1:BTTS_HT",
                "id": "fixture-1",
                "slug": "home-vs-away",
                "home": "Home FC",
                "away": "Away FC",
                "market": "BTTS_HT",
                "delivery_status": "sent",
                "settled": False,
                "odds": 2.10,
                "stake": 50.0,
            }]
        }
        match = {
            "home": "Home FC",
            "away": "Away FC",
            "home_score": 1,
            "away_score": 1,
            "status": "2nd half",
            "minute": 60,
            "half_time_score": "1-1",
        }
        with patch.object(bot, "match_detail", return_value=match), patch.object(bot, "settlement_confirmation_ready", return_value=True), patch.object(bot, "now_local", return_value=datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)), patch.object(bot, "telegram") as send_message, patch.object(bot, "save_state"):
            bot.settle_picks(state)
        self.assertTrue(state["picks"][0]["settled"])
        self.assertFalse(state["picks"][0]["won"])
        send_message.assert_called_once()


class DuplicateGuardRetryTests(unittest.TestCase):
    def test_failed_telegram_delivery_does_not_block_retry(self):
        state = {
            "duplicate_guard": [{
                "id": "fixture-3",
                "key": "fixture-3:BTTS_HT",
                "market": "BTTS_HT",
                "date": "2026-11-06",
                "home": "Home FC",
                "away": "Away FC",
                "slug": "home-vs-away",
                "delivery_status": "failed",
            }]
        }
        self.assertFalse(same_fixture_pick_exists(
            state, "fixture-3", "Home FC", "Away FC",
            "2026-11-06", "BTTS_HT", slug="home-vs-away"
        ))

    def test_successfully_sent_tip_still_blocks_duplicate(self):
        state = {
            "duplicate_guard": [{
                "id": "fixture-4",
                "key": "fixture-4:BTTS_HT",
                "market": "BTTS_HT",
                "date": "2026-11-06",
                "home": "Home FC",
                "away": "Away FC",
                "slug": "home-vs-away",
                "delivery_status": "sent",
            }]
        }
        self.assertTrue(same_fixture_pick_exists(
            state, "fixture-4", "Home FC", "Away FC",
            "2026-11-06", "BTTS_HT", slug="home-vs-away"
        ))
