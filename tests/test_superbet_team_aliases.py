import unittest

from bot import normalized_team_name, team_name_aliases, find_superbet_event


class TestSuperbetTeamAliases(unittest.TestCase):
    def test_womens_marker_matches_women_but_not_mens(self):
        self.assertIn("atlante women", team_name_aliases("Atlante W"))
        self.assertIn("atlante women", team_name_aliases("Atlante FC (K)"))
        self.assertNotEqual(normalized_team_name("Tigres Women"), normalized_team_name("Tigres"))

    def test_region_suffix_is_optional(self):
        self.assertIn("londrina", team_name_aliases("Londrina PR"))
        self.assertIn("palmeiras", team_name_aliases("Palmeiras - SP"))

    def test_known_inter_variation(self):
        self.assertIn("inter san carlos", team_name_aliases("International San Carlos"))

    def test_unique_oriented_fixture_required(self):
        events = [
            {"event_id": "1", "text": "Atlante FC (K) · UANL Tigres (K)"},
            {"event_id": "2", "text": "Atlante FC · UANL Tigres"},
        ]
        self.assertEqual(find_superbet_event("Atlante W", "Tigres Women", events)["event_id"], "1")


if __name__ == "__main__":
    unittest.main()
