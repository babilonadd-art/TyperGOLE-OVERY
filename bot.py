import json
import os
import re
import sys
from datetime import datetime, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo(os.getenv("TZ", "Europe/Warsaw"))
API = "https://sportscore.com/api/v1"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_PATH = Path("data/state.json")
STAKE = 50.0
MAX_DAILY_PICKS = 4
HEADERS = {"User-Agent": "TyperGOLE-OVERY/1.0 (SportScore attribution: https://sportscore.com/)"}


def now_local():
    return datetime.now(TZ)


def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"date": now_local().date().isoformat(), "picks": [], "settled": [], "daily_reported": None}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def api_get(path, params=None):
    response = requests.get(f"{API}/{path.lstrip('/')}", params=params, headers=HEADERS, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        return payload
    return {"data": payload}


def first(obj, *keys, default=None):
    for key in keys:
        value = obj.get(key) if isinstance(obj, dict) else None
        if value is not None and value != "":
            return value
    return default


def as_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"\d+", value)
        return int(m.group()) if m else None
    return None


def extract_matches(payload):
    for key in ("data", "matches", "fixtures", "events", "results"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("matches", "fixtures", "events", "data"):
                if isinstance(value.get(nested), list):
                    return value[nested]
    return []


def teams(match):
    home = first(match, "home_team", "homeTeam", "home", "team_home", default={})
    away = first(match, "away_team", "awayTeam", "away", "team_away", default={})
    if isinstance(home, dict):
        home = first(home, "name", "short_name", "title", default="Gospodarze")
    if isinstance(away, dict):
        away = first(away, "name", "short_name", "title", default="Goście")
    return str(home), str(away)


def score_and_minute(match):
    score = first(match, "score", "scores", "current_score", default={})
    home_score = first(match, "home_score", "homeScore", "goals_home")
    away_score = first(match, "away_score", "awayScore", "goals_away")
    if isinstance(score, dict):
        home_score = home_score if home_score is not None else first(score, "home", "home_score", "homeScore")
        away_score = away_score if away_score is not None else first(score, "away", "away_score", "awayScore")
    elif isinstance(score, str):
        nums = re.findall(r"\d+", score)
        if len(nums) >= 2:
            home_score = home_score if home_score is not None else nums[0]
            away_score = away_score if away_score is not None else nums[1]
    minute = as_int(first(match, "minute", "elapsed", "time_elapsed", "current_minute"))
    status = first(match, "status", "state", "match_status", default="")
    if isinstance(status, dict):
        minute = minute if minute is not None else as_int(first(status, "minute", "elapsed"))
        status = first(status, "short", "long", "name", default="")
    return as_int(home_score), as_int(away_score), minute, str(status).lower()


def match_id(match):
    return str(first(match, "id", "fixture_id", "event_id", "match_id", "slug", default=""))


def match_stats_look_promising(match):
    """Conservative optional live-stat filter. Missing stats never create fake values."""
    stats = first(match, "statistics", "stats", "live_statistics", default={})
    if isinstance(stats, list):
        flat = {}
        for item in stats:
            if not isinstance(item, dict):
                continue
            name = str(first(item, "name", "type", "key", default="")).lower()
            value = first(item, "value", "total")
            if name:
                flat[name] = value
        stats = flat
    if not isinstance(stats, dict) or not stats:
        return None
    shots = 0
    for key, value in stats.items():
        k = str(key).lower()
        if "shot" in k and ("total" in k or "on target" in k or "on_target" in k):
            if isinstance(value, dict):
                value = first(value, "home", "total", default=0)
            shots += as_int(value) or 0
    # Return unknown rather than reject if provider's schema does not expose a known shots field.
    return shots >= 8 if shots else None


def telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secret")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(url, json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True}, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def fixture_detail(match):
    slug = first(match, "slug", "match_slug")
    if not slug:
        return match
    try:
        payload = api_get("match/", {"sport": "football", "slug": slug})
        for key in ("match", "data", "event"):
            candidate = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(candidate, dict):
                merged = dict(match)
                merged.update(candidate)
                return merged
    except Exception as exc:
        print(f"Detail fetch skipped for {slug}: {exc}")
    return match


def current_live_matches():
    today = now_local().date().isoformat()
    payload = api_get("fixtures/", {"sport": "football", "date": today, "status": "live", "limit": 200})
    return extract_matches(payload)


def settle_picks(state):
    changed = False
    for pick in state["picks"]:
        if pick.get("settled"):
            continue
        try:
            detail = api_get("match/", {"sport": "football", "slug": pick["slug"]}) if pick.get("slug") else None
            match = {}
            if isinstance(detail, dict):
                match = first(detail, "match", "data", "event", default=detail)
            if not isinstance(match, dict):
                continue
            status = str(first(match, "status", "state", "match_status", default="")).lower()
            if not any(word in status for word in ("finished", "full time", "ft", "ended", "complete")):
                continue
            ht = first(match, "half_time_score", "halftime_score", "ht_score", "score_ht", default={})
            final = first(match, "score", "scores", "full_time_score", "ft_score", default={})
            ht_home = ht_away = None
            if isinstance(ht, dict):
                ht_home = as_int(first(ht, "home", "home_score"))
                ht_away = as_int(first(ht, "away", "away_score"))
            if isinstance(final, dict):
                f_home = as_int(first(final, "home", "home_score"))
                f_away = as_int(first(final, "away", "away_score"))
            else:
                f_home = as_int(first(match, "home_score", "homeScore"))
                f_away = as_int(first(match, "away_score", "awayScore"))
            if pick["market"] == "HT":
                if ht_home is None or ht_away is None:
                    continue
                won = ht_home + ht_away >= 1
            else:
                if f_home is None or f_away is None:
                    continue
                won = f_home + f_away >= 1
            pick["settled"] = True
            pick["won"] = won
            pick["settled_score"] = f"{f_home}-{f_away}" if f_home is not None else "?"
            pick["settled_at"] = now_local().isoformat()
            changed = True
            outcome = "WYGRANA" if won else "PRZEGRANA"
            telegram(f"\u2705 ROZLICZENIE: {outcome}\n{pick['home']} - {pick['away']}\nTyp: Over 0.5 gola {'do przerwy' if pick['market']=='HT' else 'do końca'}\nStawka: {STAKE:.0f} zł\nWynik FT: {pick['settled_score']}\n\nPowered by SportScore: https://sportscore.com/")
        except Exception as exc:
            print(f"Settlement error: {exc}")
    return changed


def send_daily_report(state):
    today = now_local().date().isoformat()
    if now_local().hour < 23 or state.get("daily_reported") == today:
        return
    picks = [p for p in state["picks"] if p.get("date") == today]
    settled = [p for p in picks if p.get("settled")]
    wins = sum(1 for p in settled if p.get("won"))
    losses = len(settled) - wins
    turnover = len(picks) * STAKE
    odds_count = sum(1 for p in settled if p.get("odds"))
    msg = (f"\U0001f4ca PODSUMOWANIE DNIA {today}\n"
           f"Typy: {len(picks)}/{MAX_DAILY_PICKS}\nWygrane: {wins}\nPrzegrane: {losses}\n"
           f"Obrót: {turnover:.0f} zł\n")
    if odds_count == len(settled) and settled:
        profit = sum((STAKE * (float(p["odds"]) - 1) if p["won"] else -STAKE) for p in settled)
        yield_pct = (profit / turnover * 100) if turnover else 0
        msg += f"Zysk/strata: {profit:+.2f} zł\nYield: {yield_pct:+.2f}%"
    else:
        msg += "Zysk/yield: brak pełnych kursów — nie da się uczciwie wyliczyć."
    msg += "\n\nPowered by SportScore: https://sportscore.com/"
    telegram(msg)
    state["daily_reported"] = today


def main():
    state = load_state()
    today = now_local().date().isoformat()
    if state.get("date") != today:
        state = {"date": today, "picks": [], "settled": [], "daily_reported": None}
    try:
        settle_picks(state)
    except Exception as exc:
        print(f"Settlement cycle error: {exc}")

    todays_picks = [p for p in state["picks"] if p.get("date") == today]
    if len(todays_picks) < MAX_DAILY_PICKS:
        try:
            matches = current_live_matches()
            for raw in matches:
                if len(todays_picks) >= MAX_DAILY_PICKS:
                    break
                match = fixture_detail(raw)
                home, away = teams(match)
                mid = match_id(match)
                slug = first(match, "slug", "match_slug")
                hs, aws, minute, status = score_and_minute(match)
                if not mid and not slug:
                    continue
                if hs != 0 or aws != 0 or minute is None:
                    continue
                market = "HT" if 29 <= minute <= 33 else "FT" if 69 <= minute <= 73 else None
                if not market:
                    continue
                key = f"{mid or slug}:{market}"
                if any(p.get("key") == key for p in state["picks"]):
                    continue
                if any(p.get("key") == key for p in todays_picks):
                    continue
                # Never fabricate odds. SportScore score feed is used for live match state only.
                stats_signal = match_stats_look_promising(match)
                if stats_signal is False:
                    continue
                league = first(match, "competition_name", "competition", "league_name", default="Liga nieznana")
                kickoff = first(match, "kickoff", "kickoff_utc", "start_time", default="")
                pick = {
                    "key": key, "id": mid, "slug": slug, "home": home, "away": away,
                    "league": str(league), "minute": minute, "market": market,
                    "date": today, "stake": STAKE, "odds": None, "settled": False,
                    "kickoff": str(kickoff)
                }
                state["picks"].append(pick)
                todays_picks.append(pick)
                market_text = "Over 0.5 gola do HT" if market == "HT" else "Over 0.5 gola do FT"
                telegram(
                    f"\u26bd TYP LIVE — {market_text}\n{home} - {away}\n"
                    f"Liga: {league}\nMinuta: {minute}' | Wynik: 0:0\n"
                    f"Stawka: {STAKE:.0f} zł\nKurs: brak wiarygodnego kursu w SportScore\n"
                    f"\nPowered by SportScore: https://sportscore.com/"
                )
        except Exception as exc:
            print(f"Live scan error: {exc}")
    try:
        send_daily_report(state)
    except Exception as exc:
        print(f"Daily report error: {exc}")
    save_state(state)


if __name__ == "__main__":
    main()
