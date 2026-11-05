import unittest

from bot import parse_minute, over_result_is_win, same_fixture_pick_exists


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



class SuperbetMarketScopeRegressionTests(unittest.TestCase):
    def test_rejects_goals_market_limited_to_x_minute(self):
        payload = {
            "markets": [{
                "marketName": "Liczba goli - do X minuty",
                "odds": [{
                    "name": "Powyżej 2.5",
                    "price": 2.45,
                    "status": "active",
                }],
            }]
        }
        from bot import extract_superbet_live_over_odds
        self.assertIsNone(extract_superbet_live_over_odds(payload, "FT", 0.5))

    def test_accepts_standard_superbet_match_total_goals_market(self):
        payload = {
            "markets": [{
                "marketName": "Liczba goli",
                "odds": [{
                    "name": "Powyżej 2.5",
                    "price": 2.45,
                    "status": "active",
                }],
            }]
        }
        from bot import extract_superbet_live_over_odds
        result = extract_superbet_live_over_odds(payload, "FT", 2.5)
        self.assertIsNotNone(result)
        self.assertEqual(result["line"], 2.5)
        self.assertEqual(result["odds"], 2.45)

    def test_accepts_explicit_whole_match_goals_market(self):
        payload = {
            "markets": [{
                "marketName": "Liczba goli - cały mecz",
                "odds": [{
                    "name": "Powyżej 2.5",
                    "price": 2.45,
                    "status": "active",
                }],
            }]
        }
        from bot import extract_superbet_live_over_odds
        result = extract_superbet_live_over_odds(payload, "FT", 0.5)
        self.assertIsNotNone(result)
        self.assertEqual(result["line"], 2.5)
        self.assertEqual(result["odds"], 2.45)


class OverSettlementRegressionTests(unittest.TestCase):
    def test_over_3_5_is_not_win_at_one_one(self):
        self.assertFalse(over_result_is_win(3.5, 2, True))

    def test_over_3_5_is_not_win_at_zero_one(self):
        self.assertFalse(over_result_is_win(3.5, 1, True))

    def test_over_3_5_is_win_at_two_two(self):
        self.assertTrue(over_result_is_win(3.5, 4, True))

    def test_live_match_is_never_settled(self):
        self.assertIsNone(over_result_is_win(3.5, 5, False))

    def test_same_fixture_blocked_when_names_have_minor_variants(self):
        state = {"picks": [{
            "id": "old-id", "key": "old-id:FT", "market": "FT",
            "home": "Sint-Truidense", "away": "Brann",
            "slug": "sint-truidense-vs-brann", "date": "2026-11-05",
        }]}
        self.assertTrue(same_fixture_pick_exists(
            state, "new-id", "Sint Truiden", "SK Brann",
            "2026-11-05", "FT", slug="sint-truidense-vs-brann"
        ))

    def test_different_fixture_is_not_blocked(self):
        state = {"picks": [{
            "id": "old-id", "key": "old-id:FT", "market": "FT",
            "home": "Celtic FC", "away": "Besiktas JK",
            "slug": "celtic-vs-besiktas", "date": "2026-11-05",
        }]}
        self.assertFalse(same_fixture_pick_exists(
            state, "new-id", "Getafe", "Brighton",
            "2026-11-05", "FT", slug="getafe-vs-brighton"
        ))


class SuperbetTeamNameMatchingTests(unittest.TestCase):
    def test_matches_club_prefix_variants(self):
        from unittest.mock import patch
        from bot import find_superbet_event
        events = [
            {"event_id": 14539435, "text": "Gimnasia La Plata·CA Belgrano"},
            {"event_id": 15610530, "text": "Universidad de Chile·Deportes Antofagasta"},
        ]
        with patch("bot.log_superbet_diagnostic"):
            result = find_superbet_event("Gimnasia La Plata", "Belgrano", events)
            result2 = find_superbet_event("Universidad de Chile", "CSD Antofagasta", events)
        self.assertEqual(result["event_id"], 14539435)
        self.assertEqual(result2["event_id"], 15610530)

    def test_matches_brazilian_state_suffix_only_when_unique(self):
        from unittest.mock import patch
        from bot import find_superbet_event
        events = [{"event_id": 14395784, "text": "Palmeiras·Remo"}]
        with patch("bot.log_superbet_diagnostic"):
            result = find_superbet_event("Palmeiras - SP", "Remo - PA", events)
        self.assertEqual(result["event_id"], 14395784)

    def test_rejects_ambiguous_state_suffix_collision(self):
        from unittest.mock import patch
        from bot import find_superbet_event
        events = [
            {"event_id": 1, "text": "Botafogo·Londrina"},
            {"event_id": 2, "text": "Botafogo - RJ·Londrina"},
        ]
        with patch("bot.log_superbet_diagnostic"):
            result = find_superbet_event("Botafogo - SP", "Londrina PR", events)
        self.assertIsNone(result)

    def test_never_reverses_home_and_away(self):
        from unittest.mock import patch
        from bot import find_superbet_event
        events = [{"event_id": 9, "text": "Belgrano·Gimnasia La Plata"}]
        with patch("bot.log_superbet_diagnostic"):
            result = find_superbet_event("Gimnasia La Plata", "Belgrano", events)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
