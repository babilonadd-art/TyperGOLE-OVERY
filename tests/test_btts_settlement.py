import unittest

from bot import btts_first_half_won


class BttsFirstHalfSettlementTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
