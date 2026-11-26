import unittest
from unittest.mock import Mock

import bot
import btts_ft


class ConfirmedCompetitionGuardTests(unittest.TestCase):
    def setUp(self):
        self.original = bot.btts_recent_form_analysis
        self.delegate = Mock(return_value={"analysis": "ok"})
        bot.btts_recent_form_analysis = self.delegate

    def tearDown(self):
        bot.btts_recent_form_analysis = self.original

    def test_rejects_empty_competition(self):
        btts_ft.install_confirmed_competition_guard()
        result = bot.btts_recent_form_analysis(
            "Home FC", "Away FC", None, league="", odds=1.85
        )
        self.assertIsNone(result)
        self.delegate.assert_not_called()

    def test_rejects_unconfirmed_competition_label(self):
        btts_ft.install_confirmed_competition_guard()
        result = bot.btts_recent_form_analysis(
            "Home FC", "Away FC", None,
            league="Rozgrywki niepotwierdzone", odds=1.85
        )
        self.assertIsNone(result)
        self.delegate.assert_not_called()

    def test_keeps_confirmed_competition_analysis_unchanged(self):
        btts_ft.install_confirmed_competition_guard()
        result = bot.btts_recent_form_analysis(
            "Home FC", "Away FC", None,
            league="EFL Championship", odds=1.85
        )
        self.assertEqual(result, {"analysis": "ok"})
        self.delegate.assert_called_once_with(
            "Home FC", "Away FC", None,
            league="EFL Championship", odds=1.85, one_x_two=None
        )


if __name__ == "__main__":
    unittest.main()
