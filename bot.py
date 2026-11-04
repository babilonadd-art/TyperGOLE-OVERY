import difflib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Warsaw")
API = "https://sportscore.com"
MOVIODDS_API = "https://moviodds.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_PATH = Path("data/state.json")
STAKE = 50.0
MAX_DAILY_PICKS = 4
HEADERS = {"User-Agent": "TyperGOLE-OVERY/1.0", "Accept": "application/json"}
ATTRIBUTION = "Powered by SportScore: https://sportscore.com/"


def now_local():
    return datetime.now(TZ)


def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("picks", [])
                data.setdefault("reports_sent", [])
                return data
        except (ValueError, OSError):
            pass
    return {"picks": [], "reports_sent": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def api_get(path, params=None):
    response = requests.get(
        f"{API}/{path.lstrip('/')}",
        params=params,
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {"data": payload}


def moviodds_live_matches():
    """Fetch Bet365's current in-play soccer board. No API key is required."""
    response = requests.get(
        f"{MOVIODDS_API}/odds",
        params={"sport": "soccer", "in_play": 1},
        headers={**HEADERS, "Accept-Encoding": "gzip"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    matches = payload.get("matches", []) if isinstance(payload, dict) else []
    if not isinstance(matches, list):
        raise RuntimeError("MoviOdds returned an unexpected matches payload")
    live = [item for item in matches if isinstance(item, dict) and item.get("in_play") is True]
    print(f"MoviOdds/Bet365: {len(live)} meczów live z kursami", flush=True)
    quote_count = sum(len(item.get("quotes", [])) for item in live if isinstance(item.get("quotes"), list))
    print(f"MoviOdds: {quote_count} ofert na tablicy live", flush=True)
    if live:
        sample_quotes = live[0].get("quotes", [])
        if isinstance(sample_quotes, list):
            relevant = [
                {
                    "group_name": q.get("group_name"), "group_id": q.get("group_id"),
                    "phase": q.get("phase"), "side": q.get("side"),
                    "line": q.get("line"), "odds_dec": q.get("odds_dec"),
                    "suspended": q.get("suspended"),
                }
                for q in sample_quotes[:120] if isinstance(q, dict)
            ]
            print("Przykładowe pola kursów MoviOdds: " + json.dumps(relevant, ensure_ascii=False)[:3500], flush=True)
    return live


def normalized_team_name(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    tokens = re.findall(r"[a-z0-9]+", value)
    ignored = {"fc", "cf", "sc", "afc", "club", "the", "football"}
    return " ".join(token for token in tokens if token not in ignored)


def team_name_similarity(left, right):
    a = normalized_team_name(left)
    b = normalized_team_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    token_ratio = len(at & bt) / max(1, len(at | bt))
    return max(ratio, token_ratio)


def find_moviodds_match(home, away, odds_matches):
    """Match both teams in either orientation; reject weak or ambiguous matches."""
    candidates = []
    for item in odds_matches:
        direct_home = team_name_similarity(home, item.get("home"))
        direct_away = team_name_similarity(away, item.get("away"))
        direct = min(direct_home, direct_away)
        reverse_home = team_name_similarity(home, item.get("away"))
        reverse_away = team_name_similarity(away, item.get("home"))
        reverse = min(reverse_home, reverse_away)
        best = max(direct, reverse)
        if (direct >= 0.78 and direct_home + direct_away >= 1.62) or (
            reverse >= 0.78 and reverse_home + reverse_away >= 1.62
        ):
            candidates.append((best, item))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.025:
        return None
    return candidates[0][1]


def parse_odds_line(quote):
    value = first(quote, "line", "handicap")
    if value is not None:
        match = re.search(r"\d+(?:[.,]\d+)?", str(value))
        if match:
            return float(match.group().replace(",", "."))
    label = str(first(quote, "label", "name", default=""))
    match = re.search(r"(?:over|under)\s*(\d+(?:[.,]\d+)?)", label, re.I)
    return float(match.group(1).replace(",", ".")) if match else None


def extract_live_over_odds(odds_match, market, target_line):
    """Return an active Bet365 in-play Over price for the requested period/line."""
    quotes = odds_match.get("quotes", [])
    if not isinstance(quotes, list):
        return None
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        if str(quote.get("phase", "")).lower() != "inplay":
            continue
        if quote.get("suspended") is True or quote.get("suspended") == 1:
            continue
        group_name = str(first(quote, "group_name", "market", default="")).strip().lower()
        if market == "HT":
            allowed = group_name.startswith(("1st half goal line", "first half goal line"))
        else:
            # Bet365's in-play full-time market is "Goal Line (current score)".
            allowed = group_name.startswith("goal line") and not group_name.startswith("1st half")
        if not allowed:
            continue
        side = str(first(quote, "side", default="")).strip().lower()
        label = str(first(quote, "label", "name", default="")).strip().lower()
        # MoviOdds documents totals sides as O/U (some feeds use Over/Under).
        if side not in {"o", "over"} and not label.startswith("over"):
            continue
        line = parse_odds_line(quote)
        if line is None or abs(line - target_line) > 0.01:
            continue
        group_status = odds_match.get("group_status", {})
        if isinstance(group_status, dict):
            status = group_status.get(str(quote.get("group_id")), {})
            if isinstance(status, dict) and status.get("stale") is True:
                continue
        try:
            odds = float(quote.get("odds_dec"))
        except (TypeError, ValueError):
            continue
        if odds <= 1.0:
            continue
        return {
            "odds": odds,
            "source": "Bet365",
            "market": first(quote, "group_name", default="Goal Line"),
            "line": line,
            "updated_at": quote.get("ts"),
        }
    return None


def first(obj, *keys, default=None):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if value is not None and value != "":
            return value
    return default


def as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        return int(match.group()) if match else None
    return None


def unwrap_match(payload):
    if not isinstance(payload, dict):
        return {}
    for key in ("match", "data", "event"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            # Some envelopes contain another nested match object.
            nested = candidate.get("match")
            return nested if isinstance(nested, dict) else candidate
    return payload


def extract_matches(payload):
    if not isinstance(payload, dict):
        return []
    for key in ("matches", "fixtures", "events", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("matches", "fixtures", "events", "data"):
                if isinstance(value.get(nested), list):
                    return value[nested]
    return []


def teams(match):
    home = first(match, "home", "home_team", "homeTeam", "team_home", "localteam", default="Gospodarze")
    away = first(match, "away", "away_team", "awayTeam", "team_away", "visitorteam", default="Goście")
    if isinstance(home, dict):
        home = first(home, "name", "short_name", "title", default="Gospodarze")
    if isinstance(away, dict):
        away = first(away, "name", "short_name", "title", default="Goście")
    return str(home), str(away)


def parse_minute(match):
    minute = as_int(first(match, "minute", "live_minute", "elapsed", "time_elapsed", "current_minute", "elapsed_minute", "currentMinute"))
    if minute is not None:
        return minute
    status_text = str(first(match, "status_text", "elapsed_time", "status", default=""))
    # SportScore may express the live clock as e.g. 32' or 45+2'.
    match_minute = re.search(r"(\d{1,3})(?:\s*\+\s*\d+)?\s*['′’]", status_text)
    if match_minute:
        return int(match_minute.group(1))
    status = first(match, "status", "state", "match_status", default="")
    if isinstance(status, dict):
        nested_minute = as_int(first(status, "minute", "elapsed"))
        if nested_minute is not None:
            return nested_minute
        status = first(status, "short", "long", "name", default="")
    phase = str(first(match, "status_text", default=status)).strip().lower()
    # The live fixtures feed often gives only "1st half"/"2nd half" and
    # the kickoff timestamp. Estimate the match clock from elapsed wall time.
    kickoff = first(match, "time", "start_time", "kickoff", "date")
    if kickoff and any(x in phase for x in ("half", "1h", "2h", "live", "in play")):
        try:
            start = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 60
            if 0 <= elapsed <= 125:
                if "2nd" in phase or "second" in phase or "2h" in phase:
                    return max(46, min(125, int(elapsed - 15)))
                return max(1, min(60, int(elapsed)))
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def score_and_minute(match):
    score = first(match, "score", "scores", "current_score", default={})
    home_score = first(match, "home_score", "homeScore", "goals_home")
    away_score = first(match, "away_score", "awayScore", "goals_away")
    if isinstance(score, dict):
        home_score = home_score if home_score is not None else first(score, "home", "home_score", "homeScore")
        away_score = away_score if away_score is not None else first(score, "away", "away_score", "awayScore")
    elif isinstance(score, str):
        numbers = re.findall(r"\d+", score)
        if len(numbers) >= 2:
            home_score = home_score if home_score is not None else numbers[0]
            away_score = away_score if away_score is not None else numbers[1]
    status = str(first(match, "status", "state", "match_status", "status_text", default="")).lower()
    if isinstance(match.get("status"), dict):
        status = str(first(match["status"], "short", "long", "name", default=status)).lower()
    return as_int(home_score), as_int(away_score), parse_minute(match), status


def slug_from_url(value):
    if not value:
        return ""
    # Fixture URL format: /football/match/<team-vs-team-slug>/<provider-id>/
    # The match-detail endpoint expects the team-vs-team slug, NOT the final ID.
    parts = [part for part in str(value).split("/") if part]
    if len(parts) >= 4 and parts[-3] == "match":
        return parts[-2]
    return parts[-1] if parts else ""


def id_from_url(value):
    if not value:
        return ""
    parts = [part for part in str(value).split("/") if part]
    if len(parts) >= 4 and parts[-3] == "match":
        return parts[-1]
    return ""


def match_identifier(match):
    keys = (
        "slug", "match_slug", "id", "fixture_id", "fixtureId", "game_id", "gameId",
        "event_id", "eventId", "match_id", "matchId", "uuid", "provider_id", "source_id",
    )
    value = first(match, *keys, default="")
    if value:
        return str(value)
    value = id_from_url(first(match, "url", "match_url", "link", default=""))
    if value:
        return value
    for container_key in ("match", "fixture", "event", "data"):
        nested = match.get(container_key)
        if isinstance(nested, dict):
            value = first(nested, *keys, default="")
            if value:
                return str(value)
            value = id_from_url(first(nested, "url", "match_url", "link", default=""))
            if value:
                return value
    return ""


def match_slug(match):
    value = first(match, "slug", "match_slug", default="")
    if value:
        return str(value)
    value = slug_from_url(first(match, "url", "match_url", "link", default=""))
    if value:
        return value
    for container_key in ("match", "fixture", "event", "data"):
        nested = match.get(container_key)
        if isinstance(nested, dict):
            value = first(nested, "slug", "match_slug", default="")
            if value:
                return str(value)
            value = slug_from_url(first(nested, "url", "match_url", "link", default=""))
            if value:
                return value
    return ""


def stat_number(value):
    if isinstance(value, dict):
        # Home/away values are summed when present.
        vals = [as_int(v) for v in value.values()]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None
    return as_int(value)


def live_match_metrics(match):
    """Extract only live shots and dangerous attacks; xG/corners are deliberately ignored."""
    stats = first(match, "statistics", "stats", "live_statistics", "match_statistics", default=None)
    if isinstance(stats, list):
        flattened = {}
        for item in stats:
            if not isinstance(item, dict):
                continue
            name = str(first(item, "name", "type", "key", "label", default="")).lower()
            value = first(item, "value", "total")
            if value is None and ("home" in item or "away" in item):
                value = {"home": item.get("home"), "away": item.get("away")}
            if name:
                flattened[name] = value
        stats = flattened
    if not isinstance(stats, dict) or not stats:
        return {}

    metrics = {"shots": None, "shots_on_target": None, "shots_off_target": None,
               "attacks": None, "dangerous_attacks": None}
    for key, value in stats.items():
        name = str(key).lower().replace("-", "_").replace(" ", "_")
        if isinstance(value, dict):
            home = as_int(first(value, "home", "home_value", "local"))
            away = as_int(first(value, "away", "away_value", "visitor"))
            total = home + away if home is not None and away is not None else stat_number(value)
        else:
            total = as_int(value)
        if total is None:
            continue

        if "shot" in name and ("on_target" in name or "ontarget" in name):
            metrics["shots_on_target"] = total
        elif "shot" in name and ("off_target" in name or "offtarget" in name):
            metrics["shots_off_target"] = total
        elif "shot" in name and ("total" in name or name in ("shots", "total_shots", "shots_total")):
            metrics["shots"] = total
        elif "dangerous" in name and "attack" in name:
            metrics["dangerous_attacks"] = total
        elif "attack" in name:
            metrics["attacks"] = total

    if metrics["shots"] is None and metrics["shots_on_target"] is not None and metrics["shots_off_target"] is not None:
        metrics["shots"] = metrics["shots_on_target"] + metrics["shots_off_target"]
    return metrics


def same_fixture_pick_exists(state, identifier, home, away, match_date, market):
    """
    Idempotency guard: a fixture + market is one pick, regardless of odds changes
    or small differences in provider IDs. Removing a Telegram message does not
    remove this record from the bot's persistent state.
    """
    wanted_home = normalized_team_name(home)
    wanted_away = normalized_team_name(away)
    wanted_id = str(identifier or "").strip().lower()

    for old in state.get("picks", []):
        if old.get("market") != market:
            continue

        old_id = str(old.get("id") or "").strip().lower()
        old_key = str(old.get("key") or "").strip().lower()
        if wanted_id and (old_id == wanted_id or old_key.startswith(wanted_id + ":")):
            return True

        # Team-name fallback is date-scoped to avoid treating a later rematch
        # between the same clubs as the already-sent fixture.
        if old.get("date") != match_date:
            continue
        old_home = normalized_team_name(old.get("home"))
        old_away = normalized_team_name(old.get("away"))
        same_teams = (
            old_home == wanted_home and old_away == wanted_away
        ) or (
            old_home == wanted_away and old_away == wanted_home
        )
        if wanted_home and wanted_away and same_teams:
            return True

    return False


def match_stats_look_promising(match):
    """Require a combination of live shooting and dangerous-attack indicators."""
    m = live_match_metrics(match)
    if not m:
        return False, m, 0
    score = 0
    if m["shots"] is not None and m["shots"] >= 8:
        score += 1
    if m["shots_on_target"] is not None and m["shots_on_target"] >= 3:
        score += 2
    if m["dangerous_attacks"] is not None and m["dangerous_attacks"] >= 25:
        score += 1
    return score >= 2, m, score


def telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing GitHub Secrets: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def match_detail(slug):
    if not slug:
        return {}
    # SportScore's documented match-detail endpoint accepts the team-pair slug
    # from the penultimate URL segment (not the trailing provider ID).
    return unwrap_match(api_get("/api/widget/match/", {"sport": "football", "slug": slug}))


def current_live_matches():
    # REST v1 fixture feed supports explicit live filtering and up to 200 records.
    payload = api_get("/api/v1/fixtures/", {
        "sport": "football", "status": "live", "limit": 200,
    })
    matches = extract_matches(payload)
    if not matches:
        # Compatibility fallback for the lightweight widget feed.
        payload = api_get("/api/widget/matches/", {"sport": "football", "limit": 50})
        matches = extract_matches(payload)

    live = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        status_value = first(match, "status", "state", "match_status", "fixture_status", default="")
        if isinstance(status_value, dict):
            status_value = first(status_value, "short", "long", "name", "elapsed", default="")
        status_text = str(status_value).strip().lower()
        clock_text = str(first(match, "status_text", "elapsed_time", "elapsed", "minute", "time", default=""))
        finished = any(token in status_text for token in (
            "finished", "full time", "full-time", "ended", "complete", "ft"
        ))
        live_status = any(token in status_text for token in (
            "live", "in play", "in progress", "1h", "2h", "half time", "halftime", "ht", "inplay"
        ))
        has_clock = bool(re.search(r"(?:\d{1,3}(?:\s*\+\s*\d+)?\s*['′’])|(?:^\d{1,3}$)", clock_text.strip()))
        if not finished and (live_status or has_clock):
            live.append(match)

    print(f"Endpoint /api/v1/fixtures/: {len(matches)} rekordów, {len(live)} live", flush=True)
    if matches:
        print("Pola rekordu API: " + ", ".join(sorted(str(k) for k in matches[0].keys())), flush=True)
        print("Przykład rekordu: " + json.dumps(matches[0], ensure_ascii=False)[:1600], flush=True)
    return live

def score_pair(value):
    if isinstance(value, dict):
        return (
            as_int(first(value, "home", "home_score", "homeScore")),
            as_int(first(value, "away", "away_score", "awayScore")),
        )
    if isinstance(value, str):
        nums = re.findall(r"\d+", value)
        if len(nums) >= 2:
            return as_int(nums[0]), as_int(nums[1])
    return None, None


def final_status(match):
    status = str(first(match, "status", "state", "match_status", "status_text", default="")).lower()
    if isinstance(match.get("status"), dict):
        status = str(first(match["status"], "short", "long", "name", default=status)).lower()
    return any(term in status for term in ("finished", "full time", "full-time", "ft", "ended", "complete"))


def settle_picks(state):
    # Goal must remain in the provider's score for 3 minutes before we notify.
    # The workflow runs every 5 minutes, so practical confirmation is on the next scan.
    confirmation_seconds = 180

    for pick in state["picks"]:
        if pick.get("delivery_status", "sent") != "sent":
            continue
        if pick.get("settled") or not pick.get("slug"):
            continue
        try:
            match = match_detail(pick["slug"])
            if not match:
                continue

            baseline_home = as_int(pick.get("score_home"))
            baseline_away = as_int(pick.get("score_away"))
            if baseline_home is None or baseline_away is None:
                print(f"Pomijam rozliczenie typu bez wyniku początkowego: {pick.get('key')}")
                continue
            baseline_total = baseline_home + baseline_away

            home_now, away_now, minute, status = score_and_minute(match)
            current_total = home_now + away_now if home_now is not None and away_now is not None else None
            finished = final_status(match)
            now = now_local()

            target_total = None
            target_score = None
            period_complete = False

            if pick["market"] == "HT":
                ht_home, ht_away = score_pair(first(
                    match, "half_time_score", "halftime_score", "ht_score", "score_ht", "half_time"
                ))
                status_text = str(status or "").lower()
                halftime_reached = (
                    finished
                    or any(term in status_text for term in (
                        "half time", "halftime", "2nd half", "second half", "2h"
                    ))
                    or (minute is not None and minute > 45)
                )
                if ht_home is not None and ht_away is not None and halftime_reached:
                    target_total = ht_home + ht_away
                    target_score = f"{ht_home}-{ht_away}"
                    period_complete = True
                elif not halftime_reached:
                    target_total = current_total
                    target_score = f"{home_now}-{away_now}" if current_total is not None else None
                elif finished:
                    # If provider omits HT score, do not infer it from the FT score.
                    print(f"Brak wyniku HT dla typu {pick.get('key')} — czekam na dane API.")
                    continue
            else:
                target_total = current_total
                target_score = f"{home_now}-{away_now}" if current_total is not None else None
                period_complete = finished

            goal_seen = target_total is not None and target_total > baseline_total
            if goal_seen:
                pending_since = pick.get("goal_pending_since")
                if not pending_since:
                    pick["goal_pending_since"] = now.isoformat()
                    print(f"Wykryto gola dla {pick.get('key')} — uruchamiam 3-minutowe potwierdzenie VAR.")
                    continue
                try:
                    pending_at = datetime.fromisoformat(pending_since)
                    if pending_at.tzinfo is None:
                        pending_at = pending_at.replace(tzinfo=TZ)
                    elapsed = (now - pending_at.astimezone(TZ)).total_seconds()
                except (TypeError, ValueError):
                    pick["goal_pending_since"] = now.isoformat()
                    continue

                if elapsed < confirmation_seconds:
                    continue
                won = True
            else:
                # A VAR cancellation resets the pending goal. A loss is only possible
                # after the relevant period has ended without a qualifying goal.
                if pick.pop("goal_pending_since", None):
                    print(f"Gol anulowany lub wynik cofnięty dla {pick.get('key')} — kasuję oczekujące rozliczenie.")
                if not period_complete:
                    continue
                won = False

            result_text = "WYGRANA" if won else "PRZEGRANA"
            score_text = target_score or "brak"
            telegram(
                f"⚽ ROZLICZENIE: {result_text}\\n{pick['home']} - {pick['away']}\\n"
                f"Typ: Over {float(pick.get('goal_line', 0.5)):.1f} gola {'do HT' if pick['market'] == 'HT' else 'do FT'}\\n"
                f"Stawka: {STAKE:.0f} zł\\nWynik: {score_text}\\n"
                f"Potwierdzenie wyniku: 3 min (bufor VAR)\\n\\n{ATTRIBUTION}"
            )
            pick["settled"] = True
            pick["won"] = won
            pick["settled_score"] = score_text
            pick["settled_at"] = now.isoformat()
            pick.pop("goal_pending_since", None)
            # Persist immediately so a later scan/restart cannot lose the settlement.
            save_state(state)
            print(f"ROZLICZONO: {pick.get('key')} -> {result_text}", flush=True)
        except Exception as exc:
            print(f"Settlement error for {pick.get('key')}: {exc}")


def send_daily_reports(state):
    today = now_local().date()
    sent = set(state.get("reports_sent", []))
    dates = sorted({p.get("date") for p in state["picks"] if p.get("date")})
    for day in dates:
        if day in sent:
            continue
        if today.isoformat() < day:
            continue
        if today.isoformat() == day and now_local().hour < 23:
            continue
        day_picks = [p for p in state["picks"] if p.get("date") == day]
        if not day_picks or not all(p.get("settled") for p in day_picks):
            continue

        wins = sum(1 for p in day_picks if p.get("won"))
        losses = len(day_picks) - wins
        turnover = len(day_picks) * STAKE
        profit = sum(
            (STAKE * (float(p["odds"]) - 1) if p.get("won") else -STAKE)
            for p in day_picks if p.get("odds") is not None
        ) if all(p.get("odds") is not None for p in day_picks) else None

        report = (
            f"📊 PODSUMOWANIE DNIA {day}\n"
            f"Typy: {len(day_picks)}/{MAX_DAILY_PICKS}\n"
            f"Wygrane: {wins}\nPrzegrane: {losses}\n"
            f"Obrót: {turnover:.0f} zł\n"
        )
        if profit is None:
            report += "Zysk/strata i yield: brak kursów — nie da się ich uczciwie obliczyć."
        else:
            yield_pct = profit / turnover * 100 if turnover else 0
            report += f"Zysk/strata: {profit:+.2f} zł\nYield: {yield_pct:+.2f}%"
        telegram(report + f"\n\n{ATTRIBUTION}")
        sent.add(day)
        state["reports_sent"] = sorted(sent)


def main():
    print(f"=== TyperGOLE-OVERY START {now_local().isoformat()} ===", flush=True)
    print(f"Telegram secrets: token={'OK' if TELEGRAM_TOKEN else 'BRAK'}, chat_id={'OK' if CHAT_ID else 'BRAK'}", flush=True)

    state = load_state()
    print(f"Historia typów: {len(state['picks'])}; nierozliczone: {sum(1 for p in state['picks'] if not p.get('settled'))}", flush=True)

    # Settle older picks first; keep history across midnight.
    settle_picks(state)

    today = now_local().date().isoformat()
    todays_picks = [p for p in state["picks"] if p.get("date") == today]
    print(f"Typy wysłane dzisiaj: {len(todays_picks)}/{MAX_DAILY_PICKS}", flush=True)

    if len(todays_picks) < MAX_DAILY_PICKS:
        try:
            matches = current_live_matches()
            print(f"API SportScore: pobrano {len(matches)} meczów live", flush=True)
            if not matches:
                print("UWAGA: API zwróciło 0 meczów. Sprawdź odpowiedź endpointu fixtures/live.", flush=True)

            if matches:
                sample = matches[0]
                print(
                    "Przykładowe pola rekordu SportScore: "
                    + ", ".join(sorted(str(k) for k in sample.keys())),
                    flush=True,
                )
                print(
                    "Przykładowy rekord (bez danych wrażliwych): "
                    + json.dumps(sample, ensure_ascii=False)[:1800],
                    flush=True,
                )

            stats = {
                "bad_item": 0, "detail_error": 0, "missing_id": 0,
"missing_minute": 0, "outside_window": 0,
                "duplicate": 0, "no_promising_stats": 0, "odds_no_match": 0, "odds_missing": 0, "sent": 0, "errors": 0,
            }

            try:
                odds_matches = moviodds_live_matches()
            except Exception as exc:
                odds_matches = []
                print(f"BŁĄD POBIERANIA KURSÓW MOVIODDS: {type(exc).__name__}: {exc}", flush=True)

            for index, raw in enumerate(matches, start=1):
                if len(todays_picks) >= MAX_DAILY_PICKS:
                    print("Osiągnięto dzienny limit typów.", flush=True)
                    break
                if not isinstance(raw, dict):
                    stats["bad_item"] += 1
                    continue

                try:
                    slug = match_slug(raw)
                    match = dict(raw)
                    raw_home, raw_away, raw_minute, raw_status = score_and_minute(raw)
                    if slug:
                        try:
                            detail = match_detail(slug)
                            if detail:
                                match.update(detail)
                                print(
                                    f"[{index}] Statystyki SportScore: minuta={detail.get('live_minute')}, "
                                    f"{live_match_metrics(detail)}",
                                    flush=True,
                                )
                        except Exception as exc:
                            stats["detail_error"] += 1
                            print(f"[{index}] Błąd szczegółów meczu {slug}: {type(exc).__name__}: {exc}", flush=True)

                    home, away = teams(match)
                    identifier = match_identifier(match) or str(slug or "")
                    if not identifier:
                        stats["missing_id"] += 1
                        continue

                    home_score, away_score, minute, status = score_and_minute(match)
                    if home_score is None or away_score is None:
                        print(f"[{index}] Brak aktualnego wyniku — pomijam typ.", flush=True)
                        continue
                    if minute is None:
                        stats["missing_minute"] += 1
                        continue

                    # Skan działa co 5 minut, więc nie ograniczamy alertu do wąskiego okna 29-33/69-73.
                    # HT: od 30. minuty do przerwy; FT: od 70. minuty do końca meczu.
                    market = "HT" if 30 <= minute <= 34 else "FT" if 70 <= minute <= 74 else None
                    if market is None:
                        stats["outside_window"] += 1
                        continue

                    key = f"{identifier}:{market}"
                    if same_fixture_pick_exists(state, identifier, home, away, today, market):
                        stats["duplicate"] += 1
                        print(
                            f"[{index}] DUPLIKAT — {home} - {away}, rynek {market}; "
                            "kurs nie ma wpływu na identyfikację typu.",
                            flush=True,
                        )
                        continue

                    promising, metrics, signal_score = match_stats_look_promising(match)
                    if not promising:
                        stats["no_promising_stats"] += 1
                        print(f"[{index}] Za mało sygnałów ofensywnych: {metrics}, score={signal_score}", flush=True)
                        continue

                    league = first(match, "competition_name", "competition", "league_name", default="Liga nieznana")
                    if isinstance(league, dict):
                        league = first(league, "name", "title", default="Liga nieznana")

                    # Bet365's in-play Goal Line is anchored to the current score.
                    # To back one more goal, request the next half-goal line above the
                    # current total (e.g. 2-1 -> Over 3.5, not Over 0.5).
                    current_total = home_score + away_score
                    goal_line = current_total + 0.5
                    odds_fixture = find_moviodds_match(home, away, odds_matches)
                    if not odds_fixture:
                        stats["odds_no_match"] += 1
                        print(f"[{index}] Brak jednoznacznego meczu w MoviOdds dla {home} - {away}; typ pominięty.", flush=True)
                        continue
                    live_price = extract_live_over_odds(odds_fixture, market, goal_line)
                    if not live_price:
                        stats["odds_missing"] += 1
                        print(f"[{index}] Brak aktywnego kursu Bet365 dla Over {goal_line:.1f} {market}; typ pominięty.", flush=True)
                        continue

                    pick = {
                        "key": key,
                        "id": identifier,
                        "slug": slug,
                        "home": home,
                        "away": away,
                        "league": str(league),
                        "minute": minute,
                        "score_home": home_score,
                        "score_away": away_score,
                        "market": market,
                        "date": today,
                        "stake": STAKE,
                        "odds": live_price["odds"],
                        "odds_source": live_price["source"],
                        "odds_market": live_price["market"],
                        "odds_updated_at": live_price.get("updated_at"),
                        "goal_line": goal_line,
                        "live_metrics": metrics,
                        "signal_score": signal_score,
                        "settled": False,
                    }
                    market_text = f"Over {goal_line:.1f} gola do {'HT' if market == 'HT' else 'FT'}"
                    # Reserve the fixture+market key BEFORE calling Telegram. If Telegram
                    # accepts the message but the HTTP response is lost, the next scan
                    # must not send the same pick again with a changed price.
                    pick["delivery_status"] = "pending"
                    state["picks"].append(pick)
                    todays_picks.append(pick)
                    save_state(state)

                    telegram(
                        f"⚽ TYP LIVE — {market_text}\\n{home} - {away}\\n"
                        f"Liga: {league}\\nMinuta: {minute}' | Wynik: {home_score}:{away_score}\\n"
                        f"Kurs Bet365: {live_price['odds']:.2f}\\n"
                        f"Stawka: {STAKE:.0f} zł\\n\\n{ATTRIBUTION}"
                    )
                    pick["delivery_status"] = "sent"
                    save_state(state)
                    stats["sent"] += 1
                    print(f"WYSŁANO I ZAPISANO TYP: {home} - {away}, {minute}', {market}", flush=True)

                except Exception as exc:
                    stats["errors"] += 1
                    print(f"[{index}] Błąd pojedynczego meczu: {type(exc).__name__}: {exc}", flush=True)
                    continue

            print("PODSUMOWANIE SKANU: " + json.dumps(stats, ensure_ascii=False), flush=True)

        except Exception as exc:
            print(f"BŁĄD SKANOWANIA API: {type(exc).__name__}: {exc}", flush=True)

    try:
        send_daily_reports(state)
    except Exception as exc:
        print(f"Daily report error: {type(exc).__name__}: {exc}", flush=True)

    save_state(state)
    print(f"Stan zapisany. Typy dzisiaj: {len(todays_picks)}/{MAX_DAILY_PICKS}", flush=True)
    print("=== TyperGOLE-OVERY END ===", flush=True)


if __name__ == "__main__":
    main()
