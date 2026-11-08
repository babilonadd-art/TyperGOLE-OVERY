import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import btts_first_half_won, kickoff_reached_for_settlement, pick_local_date


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
