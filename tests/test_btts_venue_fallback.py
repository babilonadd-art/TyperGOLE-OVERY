import unittest

import bot


class BttsVenueFallbackTests(unittest.TestCase):
    def test_sparse_venue_uses_overall_form_but_keeps_real_sample(self):
        overall = {"played": 8, "btts": 0.75, "goals_for_avg": 1.8}
        venue = {"played": 1, "btts": 0.0, "goals_for_avg": 0.5}
        selected, used_fallback = bot.btts_venue_fallback(overall, venue)
        self.assertTrue(used_fallback)
        self.assertEqual(selected["played"], 1)
        self.assertEqual(selected["btts"], 0.75)
        self.assertEqual(selected["goals_for_avg"], 1.8)

    def test_zero_venue_matches_does_not_discard_overall_form(self):
        overall = {"played": 6, "btts": 0.5}
        venue = {"played": 0, "btts": 0.0}
        selected, used_fallback = bot.btts_venue_fallback(overall, venue)
        self.assertTrue(used_fallback)
        self.assertEqual(selected["played"], 0)
        self.assertEqual(selected["btts"], 0.5)

    def test_sufficient_venue_sample_is_kept(self):
        overall = {"played": 9, "btts": 0.6}
        venue = {"played": 2, "btts": 1.0}
        selected, used_fallback = bot.btts_venue_fallback(overall, venue)
        self.assertFalse(used_fallback)
        self.assertEqual(selected, venue)


if __name__ == "__main__":
    unittest.main()
