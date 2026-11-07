import unittest

from bot import extract_superbet_btts_full_time


class SuperbetFullTimeBttsQuoteTests(unittest.TestCase):
    def test_reads_yes_price_from_full_time_btts_group_only(self):
        payload = {
            "markets": [
                {
                    "marketName": "Mecz",
                    "marketGroupId": 102,
                    "outcomes": [
                        {"info": "Obie drużyny strzelą", "name": "Tak", "price": "2.22", "status": "active"},
                        {"info": "Obie drużyny strzelą", "name": "Nie", "price": "1.58", "status": "active"},
                    ],
                },
                {
                    "marketName": "Zwycięzca meczu",
                    "marketGroupId": 1,
                    "outcomes": [
                        {"name": "Tak", "price": "1.30", "status": "active"},
                    ],
                },
            ]
        }
        quote = extract_superbet_btts_full_time(payload)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["odds"], 2.22)
        self.assertEqual(quote["market_group_id"], 102)


    def test_ignores_btts_combo_markets_and_keeps_standalone_full_time(self):
        payload = {
            "markets": [
                {
                    "marketName": "Mecz",
                    "marketGroupId": 539,
                    "outcomes": [
                        {"info": "Obie drużyny strzelą gola w meczu", "name": "Tak", "price": "1.90", "status": "active"},
                    ],
                },
                {
                    "marketName": "Obie drużyny strzelą gola lub powyżej 2,5 gola",
                    "marketGroupId": 201501,
                    "outcomes": [
                        {"name": "Tak", "price": "1.55", "status": "active"},
                    ],
                },
                {
                    "marketName": "Obie drużyny strzelą w przynajmniej jednej połowie meczu",
                    "marketGroupId": 200751,
                    "outcomes": [
                        {"name": "Tak", "price": "2.05", "status": "active"},
                    ],
                },
            ]
        }
        quote = extract_superbet_btts_full_time(payload)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["odds"], 1.90)
        self.assertEqual(quote["market_group_id"], 539)


    def test_rejects_first_half_btts(self):
        payload = {
            "markets": [{
                "marketName": "BTTS 1. połowa",
                "marketGroupId": 78,
                "outcomes": [{"name": "Tak", "price": "2.22", "status": "active"}],
            }]
        }
        self.assertIsNone(extract_superbet_btts_full_time(payload))

    def test_rejects_ambiguous_duplicate_btts_groups(self):
        payload = {
            "markets": [
                {
                    "marketName": "Both Teams To Score",
                    "marketGroupId": 102,
                    "outcomes": [{"name": "Yes", "price": "2.22", "status": "active"}],
                },
                {
                    "marketName": "Both Teams To Score",
                    "marketGroupId": 103,
                    "outcomes": [{"name": "Yes", "price": "2.35", "status": "active"}],
                },
            ]
        }
        self.assertIsNone(extract_superbet_btts_full_time(payload))


    def test_over_15_ht_selects_only_aggregate_match_goals_not_team_totals_corners_or_cards(self):
        from bot import extract_superbet_over_15_ht

        payload = {
            "markets": [
                {
                    "marketName": "1. połowa Liczba goli",
                    "marketGroupId": 501,
                    "outcomes": [
                        {"name": "Powyżej 1.5", "price": "2.20", "status": "active"},
                        {"name": "Poniżej 1.5", "price": "1.60", "status": "active"},
                    ],
                },
                {
                    "marketName": "1. połowa St Mirren Liczba goli",
                    "marketGroupId": 502,
                    "outcomes": [
                        {"name": "Powyżej 1.5", "price": "3.00", "status": "active"},
                    ],
                },
                {
                    "marketName": "1. połowa St Mirren Liczba rzutów rożnych",
                    "marketGroupId": 503,
                    "outcomes": [
                        {"name": "Powyżej 1.5", "price": "1.92", "status": "active"},
                    ],
                },
                {
                    "marketName": "1. połowa Liczba kartek",
                    "marketGroupId": 504,
                    "outcomes": [
                        {"name": "Powyżej 1.5", "price": "2.30", "status": "active"},
                    ],
                },
            ]
        }

        quote = extract_superbet_over_15_ht(payload)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["odds"], 2.20)
        self.assertIn("1 polowa liczba goli", quote["market"])



if __name__ == "__main__":
    unittest.main()
