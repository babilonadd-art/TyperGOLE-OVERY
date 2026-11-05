import unittest

from bot import (
    normalized_team_name, team_name_aliases, find_superbet_event,
    extract_superbet_live_over_odds,
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
