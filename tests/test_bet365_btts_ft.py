import unittest

from bot import extract_regulation_time_score, extract_moviodds_btts_ft_quote


class RegulationTimeBttsTests(unittest.TestCase):
    def test_regular_finished_match_uses_final_score(self):
        self.assertEqual(extract_regulation_time_score({"status": "finished", "final_score": "2-1"}), (2, 1))

    def test_penalty_shootout_goals_are_not_added(self):
        match = {
            "status": "finished after penalties", "overtime": True, "final_score": "5-4",
            "periods": [
                {"name": "1st Half", "score": "0-1"},
                {"name": "2nd Half", "score": "1-0"},
            ],
        }
        self.assertEqual(extract_regulation_time_score(match), (1, 1))

    def test_extra_time_without_regulation_score_is_not_settled(self):
        self.assertEqual(
            extract_regulation_time_score({"status": "AET", "overtime": True, "final_score": "2-1"}),
            (None, None),
        )

    def test_penalty_status_without_periods_fails_closed(self):
        self.assertEqual(
            extract_regulation_time_score({"status": "pen", "final_score": "4-3"}),
            (None, None),
        )

    def test_explicit_ninety_minute_score_takes_priority(self):
        self.assertEqual(
            extract_regulation_time_score({
                "status": "after penalties", "overtime": True,
                "regulation_time_score": "1-1", "final_score": "5-4",
            }),
            (1, 1),
        )


class MoviOddsBttsMarketTests(unittest.TestCase):
    def test_only_full_time_yes_in_range_is_selected(self):
        match = {"quotes": [
            {"group_name": "Both Teams to Score", "label": "Yes", "odds_dec": 2.05, "group_id": "btts", "phase": "prematch"},
            {"group_name": "Both Teams to Score", "label": "No", "odds_dec": 1.80, "group_id": "btts", "phase": "prematch"},
            {"group_name": "Both Teams to Score 1st Half", "label": "Yes", "odds_dec": 2.20, "group_id": "btts1h"},
            {"group_name": "Both Teams to Score", "label": "Yes", "odds_dec": 2.60, "group_id": "btts"},
        ]}
        quote = extract_moviodds_btts_ft_quote(match)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["odds"], 2.05)

    def test_suspended_and_out_of_range_quotes_are_rejected(self):
        self.assertIsNone(extract_moviodds_btts_ft_quote({"quotes": [
            {"group_name": "Both Teams to Score", "label": "Yes", "odds_dec": 2.00, "group_id": "btts", "suspended": True}
        ]}))
        self.assertIsNone(extract_moviodds_btts_ft_quote({"quotes": [
            {"group_name": "Both Teams to Score", "label": "Yes", "odds_dec": 2.55, "group_id": "btts"}
        ]}))


if __name__ == "__main__":
    unittest.main()
