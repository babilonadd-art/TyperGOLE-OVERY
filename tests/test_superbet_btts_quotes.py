import unittest

from bot import extract_superbet_btts_full_time


class SuperbetFullTimeBttsQuoteTests(unittest.TestCase):
    def test_reads_yes_price_from_full_time_btts_group_only(self):
        payload = {
            "markets": [
                {
                    "marketName": "Obie drużyny strzelą",
                    "marketGroupId": 102,
                    "outcomes": [
                        {"name": "Tak", "price": "2.22", "status": "active"},
                        {"name": "Nie", "price": "1.58", "status": "active"},
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


if __name__ == "__main__":
    unittest.main()
