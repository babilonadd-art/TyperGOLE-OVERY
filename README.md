# TyperGOLE-OVERY

Telegram bot for live late-goal alerts, using the free SportScore API.

## Current strategy
- Maximum 4 picks per Warsaw calendar day.
- At 0:0 around minute 30: Over 0.5 goal before half-time (HT).
- At 0:0 around minute 70: Over 0.5 goal before full-time (FT).
- Stake tracking: 50 PLN per pick.
- Automatic settlement from match results and daily summary.
- Odds are optional. SportScore is used for live match data; it does not provide bookmaker odds in the documented free fixtures feed. The bot never invents odds.
- When live stats are available in the fixture payload, a conservative shots signal can filter a match. If stats are absent, the bot can still alert based on the score and minute; this is not a guarantee of goal probability.

## GitHub Secrets
Add these repository secrets:
- `TELEGRAM_BOT_TOKEN` — token from BotFather.
- `TELEGRAM_CHAT_ID` — target channel/group chat ID. The bot must be an administrator with permission to post.

No SportScore API key is required for the free attribution tier.

## Run
GitHub Actions runs every 5 minutes and can also be started manually from Actions → TyperGOLE-OVERY Live Bot → Run workflow.

## Important
- The first version depends on the exact fields returned by SportScore. If their live fixture response changes or omits minute/score/half-time score, the bot logs the issue and skips the affected action rather than guessing.
- A daily summary can report wins/losses and turnover without odds. Profit and yield are only calculated when genuine odds are available for every settled pick.
- SportScore attribution is included in Telegram messages: https://sportscore.com/
