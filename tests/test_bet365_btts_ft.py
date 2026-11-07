import unittest

from bot import extract_regulation_time_score, extract_moviodds_btts_ft_quote


class RegulationTimeBttsTests(unittest.TestCase):
    def test_regular_finished_match_uses_final_score(self):
        self.assertEqual(
            extract_regulation_time_score({"status": "finished", "final_score": "2-1"}),
            (2, 1),
        )

    def test_penalty_shootout_score_is_not_added_to_regulation_score(self):
        match = {
            "status": "finished after penalties",
            "overtime": True,
            "final_score": "5-4",
            "periods": [
                {"name": "1st Half", "score": "0-1"},
                {"name": "2nd Half", "score": "1-0"},
            ],
        }
        self.assertEqual(extract_regulation_time_score(match), (1, 1))

    def test_extra_time_score_is_not_used_when_period_scores_missing(self):
        match = {"status": "AET", "overtime": True, "final_score": "2-1"}
        self.assertEqual(extract_regulation_time_score(match), (None, None))

    def test_penalty_shootout_status_without_regulation_periods_fails_closed(self):
        match = {"status": "pen", "final_score": "4-3"}
        self.assertEqual(extract_regulation_time_score(match), (None, None))

    def test_explicit_ninety_minute_score_has_priority(self):
        match = {
            "status": "after penalties",
            "overtime": True,
            "regulation_time_score": "1-1",
            "final_score": "5-4",
        }
        self.assertEqual(extract_regulation_time_score(match), (1, 1))


class MoviOddsBttsMarketTests(unittest.TestCase):
    def test_extracts_only_full_match_yes_within_requested_odds(self):
        match = {"quotes": [
            {"group_name": "Both Teams to Score", "label": "Yes", "odds_dec": 2.05, "group_id": "btts", "phase": "prematch"},
            {"group_name": "Both Teams to Score", "label": "No", "odds_dec": 1.80, "group_id": "btts", "phase": "prematch"},
            {"group_name": "Both Teams to Score 1st Half", "label": "Yes", "odds_dec": 2.20, "group_id": "btts1h"},
            {"group_name": "Both Teams to Score", "label": "Yes", "odds_dec": 2.60, "group_id": "btts"},
        ]}
        result = extract_moviodds_btts_ft_quote(match)
        self.assertIsNotNone(result)
        self.assertEqual(result["odds"], 2.05)

    def test_rejects_suspended_or_out_of_range_quotes(self):
        self.assertIsNone(extract_moviodds_btts_ft_quote({"quotes": [
            {"group_name": "Both Teams to Score", "label": "Yes", "odds_dec": 2.00, "group_id": "btts", "suspended": True}
        ]}))
        self.assertIsNone(extract_moviodds_btts_ft_quote({"quotes": [
            {"group_name": "Both Teams to Score", "label": "Yes", "odds_dec": 2.55, "group_id": "btts"}
        ]}))


if __name__ == "__main__":
    unittest.main()
