import unittest

from bot import (
    fixture_category_signature,
    fixture_categories_compatible,
    format_fixture_competition,
    normalized_team_name,
    same_fixture_pick_exists,
    settlement_fixture_matches_pick,
)


class FixtureCategorySafetyTests(unittest.TestCase):
    def test_all_requested_youth_age_groups_are_detected(self):
        for age in range(16, 24):
            with self.subTest(age=age):
                result = fixture_category_signature(
                    "Academy Home", "Academy Away", f"National League U{age}"
                )
                self.assertEqual(result["age_group"], f"U{age}")
                self.assertEqual(result["gender"], "MEN")

    def test_under_wording_and_hyphenated_u_age_are_detected(self):
        self.assertEqual(
            fixture_category_signature("Home", "Away", "Under 19 League")["age_group"],
            "U19",
        )
        self.assertEqual(
            fixture_category_signature("Home U-17", "Away", "Youth League")["age_group"],
            "U17",
        )

    def test_womens_k_and_w_markers_are_distinguished_from_mens(self):
        women_k = fixture_category_signature("Club A (K)", "Club B (K)", "Liga")
        women_w = fixture_category_signature("Club A (W)", "Club B (W)", "League")
        women_comp = fixture_category_signature("Club A", "Club B", "Women's League")
        men = fixture_category_signature("Club A", "Club B", "Premier League")
        self.assertEqual(women_k["gender"], "WOMEN")
        self.assertEqual(women_w["gender"], "WOMEN")
        self.assertEqual(women_comp["gender"], "WOMEN")
        self.assertEqual(men["gender"], "MEN")
        self.assertNotEqual(women_k, men)

    def test_youth_and_womens_categories_can_coexist(self):
        category = fixture_category_signature(
            "Club A (K) U19", "Club B (K) U19", "Women's U19 League"
        )
        self.assertEqual(category, {"gender": "WOMEN", "age_group": "U19"})

    def test_normalized_names_keep_exact_age_and_gender_identity(self):
        self.assertNotEqual(normalized_team_name("Academy FC U16"), normalized_team_name("Academy FC U18"))
        self.assertNotEqual(normalized_team_name("Club A (W)"), normalized_team_name("Club A"))
        self.assertNotEqual(normalized_team_name("Club A (K)"), normalized_team_name("Club A (W)") if False else normalized_team_name("Club A"))

    def test_category_mismatch_is_never_considered_same_fixture(self):
        self.assertFalse(fixture_categories_compatible(
            "Club A", "Club B", "U17 League",
            "Club A", "Club B", "U19 League",
        ))
        self.assertFalse(fixture_categories_compatible(
            "Club A (K)", "Club B (K)", "League",
            "Club A", "Club B", "League",
        ))
        self.assertTrue(fixture_categories_compatible(
            "Club A (W)", "Club B (W)", "Women's U21",
            "Club A", "Club B", "U21 Women",
        ))

    def test_telegram_competition_label_preserves_u_and_adds_k_marker(self):
        self.assertEqual(
            format_fixture_competition("National League", "Club A U19", "Club B U19"),
            "National League U19",
        )
        self.assertEqual(
            format_fixture_competition("Women's League", "Club A", "Club B"),
            "Women's League",
        )
        self.assertEqual(
            format_fixture_competition("League", "Club A (K)", "Club B (K)"),
            "League (K)",
        )

    def test_duplicate_guard_does_not_mix_senior_and_youth(self):
        state = {"duplicate_guard": [{
            "id": "senior-1", "home": "Club A", "away": "Club B",
            "date": "2026-11-07", "market": "BTTS_FT",
            "league": "Senior League", "delivery_status": "sent",
        }]}
        self.assertFalse(same_fixture_pick_exists(
            state, "youth-1", "Club A U19", "Club B U19",
            "2026-11-07", "BTTS_FT", competition="U19 League",
        ))

    def test_duplicate_guard_blocks_same_youth_fixture_even_if_provider_id_changes(self):
        state = {"duplicate_guard": [{
            "id": "youth-old", "home": "Club A U19", "away": "Club B U19",
            "date": "2026-11-07", "market": "BTTS_FT",
            "league": "U19 League", "delivery_status": "sent",
        }]}
        self.assertTrue(same_fixture_pick_exists(
            state, "youth-new", "Club A U19", "Club B U19",
            "2026-11-07", "BTTS_FT", competition="National U19 League",
        ))

    def test_settlement_rejects_same_teams_from_wrong_age_group(self):
        match = {
            "home": "Club A U17", "away": "Club B U17",
            "competition_name": "U17 League",
            "time": "2026-11-07T18:00:00+01:00",
        }
        pick = {
            "home": "Club A U19", "away": "Club B U19",
            "league": "U19 League",
            "kickoff_time": "2026-11-07T18:00:00+01:00",
        }
        self.assertFalse(settlement_fixture_matches_pick(match, pick))


if __name__ == "__main__":
    unittest.main()
