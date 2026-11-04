# TyperGOLE-OVERY

Live Telegram alerts for late-goal opportunities, using the free SportScore API.

## Strategy
- Up to 4 alerts per Warsaw calendar day.
- If the match is 0:0 around minute 30 (29–33): Over 0.5 goal before half-time.
- If the match is 0:0 around minute 70 (69–73): Over 0.5 goal before full-time.
- Stake recorded: 50 PLN per pick.
- The bot requires positive live shot statistics before sending an alert. If SportScore does not return usable statistics for a match, the bot skips it rather than treating missing data as a positive signal.
- Picks are settled automatically when the API provides the required half-time or full-time score.
- Odds are optional. No odds are fabricated. If no real bookmaker odds are available, the message contains the match and pick only; profit/yield are not calculated without genuine odds.

## GitHub Secrets
Add these two **repository secrets in this repository** (secrets from another repository are not shared):
- `TELEGRAM_BOT_TOKEN` — token from BotFather.
- `TELEGRAM_CHAT_ID` — ID of the Telegram channel/group where this new bot should post. The bot must be an administrator with permission to post.

SportScore requires no API key on its free attribution tier. It allows approximately 10,000 requests per 24 hours per IP. Its terms require visible attribution; Telegram messages include a "Powered by SportScore" link.

## Run
GitHub Actions runs every 5 minutes. You can also start it manually from Actions → TyperGOLE-OVERY Live Bot → Run workflow.

## Notes
- State is stored in `data/state.json` and committed back to the repository so daily limits and unsettled picks survive workflow runs and midnight.
- Daily reports are sent after all picks from that day have been settled; if a match finishes late, the report waits for its result.
- SportScore's fixtures endpoint uses UTC calendar dates. The bot queries the current UTC day.
- API fields may vary. If the API omits minute, score, shot statistics, or HT/FT scores, the bot skips the alert or delays settlement rather than inventing data.
