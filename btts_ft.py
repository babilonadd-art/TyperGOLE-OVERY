#!/usr/bin/env python3
"""Independent scheduled pre-match BTTS FT scanner.

Only analyze/publish fixtures with a confirmed competition label. The
probability model, BTTS market, and odds thresholds remain in bot.py unchanged.
"""
import bot


def install_confirmed_competition_guard():
    """Reject BTTS candidates when Superbet/Open Matchday cannot confirm a league."""
    original = bot.btts_recent_form_analysis

    def guarded_analysis(home, away, kickoff, league="", odds=None, one_x_two=None):
        competition = str(league or "").strip().casefold()
        if competition in {"", "unknown", "unknown league", "liga nieznana", "rozgrywki niepotwierdzone"}:
            print(
                f"BTTS FT: pomijam {home} - {away}: brak potwierdzonej nazwy rozgrywek.",
                flush=True,
            )
            return None
        return original(
            home, away, kickoff, league=league, odds=odds, one_x_two=one_x_two
        )

    bot.btts_recent_form_analysis = guarded_analysis


if __name__ == "__main__":
    install_confirmed_competition_guard()
    bot.run_btts_ft()
