import unittest

from bot import extract_superbet_1x2_full_time, btts_prematch_odds_gate


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


    def test_prematch_gate_accepts_over_25_below_185_and_btts_at_190(self):
        self.assertTrue(btts_prematch_odds_gate(
            {"line": 2.5, "odds": 1.84},
            {"odds": 1.90},
        ))

    def test_prematch_gate_rejects_over_25_at_185_or_higher(self):
        self.assertFalse(btts_prematch_odds_gate(
            {"line": 2.5, "odds": 1.85},
            {"odds": 1.80},
        ))

    def test_prematch_gate_rejects_btts_above_190(self):
        self.assertFalse(btts_prematch_odds_gate(
            {"line": 2.5, "odds": 1.70},
            {"odds": 1.91},
        ))

    def test_prematch_gate_requires_exact_over_25_line(self):
        self.assertFalse(btts_prematch_odds_gate(
            {"line": 3.5, "odds": 1.50},
            {"odds": 1.80},
        ))

    def test_prematch_gate_rejects_missing_or_invalid_quotes(self):
        self.assertFalse(btts_prematch_odds_gate(None, {"odds": 1.80}))
        self.assertFalse(btts_prematch_odds_gate(
            {"line": 2.5, "odds": "not-a-price"},
            {"odds": 1.80},
        ))



if __name__ == "__main__":
    unittest.main()
