import unittest

import bot


class BttsListFormatTests(unittest.TestCase):
    def test_league_is_first_then_time_and_teams(self):
        message = bot.format_btts_ft_pick_list([{
            "pick_number": 1,
            "league": "Premier League",
            "home": "Arsenal",
            "away": "Chelsea",
            "kickoff_time": "2026-11-27T18:30:00+01:00",
            "odds": 1.85,
        }], "2026-11-27")

        league_pos = message.index("<b>1.  Premier League</b>")
        match_pos = message.index("<b>🕒 18:30  |  Arsenal – Chelsea</b>")
        market_pos = message.index("BTTS TAK (CAŁY MECZ)")
        odds_pos = message.index("1.85")
        self.assertLess(league_pos, match_pos)
        self.assertLess(match_pos, market_pos)
        self.assertLess(market_pos, odds_pos)


if __name__ == "__main__":
    unittest.main()
