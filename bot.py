import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Warsaw")
API = "https://sportscore.com"
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
    home = first(match, "home", "home_team", "homeTeam", "team_home", default="Gospodarze")
    away = first(match, "away", "away_team", "awayTeam", "team_away", default="Goście")
    if isinstance(home, dict):
        home = first(home, "name", "short_name", "title", default="Gospodarze")
    if isinstance(away, dict):
        away = first(away, "name", "short_name", "title", default="Goście")
    return str(home), str(away)


def parse_minute(match):
    minute = as_int(first(match, "minute", "elapsed", "time_elapsed", "current_minute"))
    if minute is not None:
        return minute
    status_text = str(first(match, "status_text", "time", "elapsed_time", "status", default=""))
    # SportScore may express the live clock as e.g. 32' or 45+2'.
    match_minute = re.search(r"(\d{1,3})(?:\s*\+\s*\d+)?\s*['′’]", status_text)
    if match_minute:
        return int(match_minute.group(1))
    status = first(match, "status", "state", "match_status", default="")
    if isinstance(status, dict):
        return as_int(first(status, "minute", "elapsed"))
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


def match_identifier(match):
    # SportScore list records do not always expose the same identifier key.
    value = first(
        match, "id", "fixture_id", "fixtureId", "event_id", "eventId",
        "match_id", "matchId", "slug", "match_slug", "uuid", default=""
    )
    if value:
        return str(value)
    for container_key in ("match", "fixture", "event", "data"):
        nested = match.get(container_key)
        if isinstance(nested, dict):
            value = first(
                nested, "id", "fixture_id", "fixtureId", "event_id",
                "eventId", "match_id", "matchId", "slug", "match_slug",
                "uuid", default=""
            )
            if value:
                return str(value)
    return ""


def stat_number(value):
    if isinstance(value, dict):
        # Home/away values are summed when present.
        vals = [as_int(v) for v in value.values()]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None
    return as_int(value)


def match_stats_look_promising(match):
    """Require real live shot data before alerting; missing stats are not treated as a green light."""
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
        return False

    total_shots = None
    shots_on_target = None
    for key, value in stats.items():
        name = str(key).lower().replace("-", "_")
        number = stat_number(value)
        if number is None:
            continue
        if "shot" in name and ("on_target" in name or "on target" in name or "ontarget" in name):
            shots_on_target = number if shots_on_target is None else max(shots_on_target, number)
        elif "shot" in name and ("total" in name or name in ("shots", "total_shots", "shots_total")):
            total_shots = number if total_shots is None else max(total_shots, number)

    # A positive signal requires at least 8 total shots or 3 shots on target combined.
    return (total_shots is not None and total_shots >= 8) or (
        shots_on_target is not None and shots_on_target >= 3
    )


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
    return unwrap_match(api_get("/api/widget/match/", {"sport": "football", "slug": slug}))


def current_live_matches():
    # Official SportScore endpoint: GET /api/widget/matches/
    # The API base is https://sportscore.com; do not use the obsolete /api/v1 path.
    payload = api_get("/api/widget/matches/", {
        "sport": "football",
        "limit": 50,
    })
    matches = extract_matches(payload)
    live = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        status_value = first(match, "status", "state", "match_status", default="")
        if isinstance(status_value, dict):
            status_value = first(status_value, "short", "long", "name", default="")
        status_text = str(status_value).strip().lower()
        clock_text = str(first(match, "status_text", "elapsed_time", "time", default=""))
        is_live_status = any(token in status_text for token in (
            "live", "in play", "in progress", "1h", "2h", "half time", "halftime", "ht"
        ))
        has_live_clock = bool(re.search(r"\d{1,3}(?:\s*\+\s*\d+)?\s*['′’]", clock_text))
        is_finished = any(token in status_text for token in (
            "finished", "full time", "full-time", "ended", "complete", "ft"
        ))
        if not is_finished and (is_live_status or has_live_clock):
            live.append(match)
    print(f"Endpoint /matches/: {len(matches)} rekordów, {len(live)} oznaczonych jako live", flush=True)
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
    for pick in state["picks"]:
        if pick.get("settled") or not pick.get("slug"):
            continue
        try:
            match = match_detail(pick["slug"])
            if not match or not final_status(match):
                continue
            ht_home, ht_away = score_pair(first(
                match, "half_time_score", "halftime_score", "ht_score", "score_ht", "half_time"
            ))
            ft_home, ft_away = score_pair(first(
                match, "score", "scores", "full_time_score", "ft_score"
            ))
            if ft_home is None or ft_away is None:
                ft_home = as_int(first(match, "home_score", "homeScore"))
                ft_away = as_int(first(match, "away_score", "awayScore"))

            if pick["market"] == "HT":
                if ht_home is None or ht_away is None:
                    continue
                won = ht_home + ht_away >= 1
            else:
                if ft_home is None or ft_away is None:
                    continue
                won = ft_home + ft_away >= 1

            # Only mark settled after Telegram confirms the notification.
            result_text = "WYGRANA" if won else "PRZEGRANA"
            score_text = f"{ft_home}-{ft_away}" if ft_home is not None else "brak"
            telegram(
                f"⚽ ROZLICZENIE: {result_text}\n{pick['home']} - {pick['away']}\n"
                f"Typ: Over 0.5 gola {'do HT' if pick['market'] == 'HT' else 'do FT'}\n"
                f"Stawka: {STAKE:.0f} zł\nWynik FT: {score_text}\n\n{ATTRIBUTION}"
            )
            pick["settled"] = True
            pick["won"] = won
            pick["settled_score"] = score_text
            pick["settled_at"] = now_local().isoformat()
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
                "not_0_0": 0, "missing_minute": 0, "outside_window": 0,
                "duplicate": 0, "no_shot_stats": 0, "sent": 0, "errors": 0,
            }

            for index, raw in enumerate(matches, start=1):
                if len(todays_picks) >= MAX_DAILY_PICKS:
                    print("Osiągnięto dzienny limit typów.", flush=True)
                    break
                if not isinstance(raw, dict):
                    stats["bad_item"] += 1
                    continue

                try:
                    slug = first(raw, "slug", "match_slug")
                    match = dict(raw)
                    if slug:
                        try:
                            detail = match_detail(slug)
                            if detail:
                                match.update(detail)
                        except Exception as exc:
                            stats["detail_error"] += 1
                            print(f"[{index}] Błąd szczegółów meczu {slug}: {type(exc).__name__}: {exc}", flush=True)

                    home, away = teams(match)
                    identifier = match_identifier(match) or str(slug or "")
                    if not identifier:
                        stats["missing_id"] += 1
                        continue

                    home_score, away_score, minute, status = score_and_minute(match)
                    if home_score != 0 or away_score != 0:
                        stats["not_0_0"] += 1
                        continue
                    if minute is None:
                        stats["missing_minute"] += 1
                        continue

                    market = "HT" if 29 <= minute <= 33 else "FT" if 69 <= minute <= 73 else None
                    if market is None:
                        stats["outside_window"] += 1
                        continue

                    key = f"{identifier}:{market}"
                    if any(p.get("key") == key for p in state["picks"]):
                        stats["duplicate"] += 1
                        continue

                    if not match_stats_look_promising(match):
                        stats["no_shot_stats"] += 1
                        continue

                    league = first(match, "competition_name", "competition", "league_name", default="Liga nieznana")
                    if isinstance(league, dict):
                        league = first(league, "name", "title", default="Liga nieznana")

                    pick = {
                        "key": key,
                        "id": identifier,
                        "slug": slug,
                        "home": home,
                        "away": away,
                        "league": str(league),
                        "minute": minute,
                        "market": market,
                        "date": today,
                        "stake": STAKE,
                        "odds": None,
                        "settled": False,
                    }
                    market_text = "Over 0.5 gola do HT" if market == "HT" else "Over 0.5 gola do FT"
                    telegram(
                        f"⚽ TYP LIVE — {market_text}\n{home} - {away}\n"
                        f"Liga: {league}\nMinuta: {minute}' | Wynik: 0:0\n"
                        f"Stawka: {STAKE:.0f} zł\nKurs: niedostępny\n\n{ATTRIBUTION}"
                    )
                    state["picks"].append(pick)
                    todays_picks.append(pick)
                    stats["sent"] += 1
                    print(f"WYSŁANO TYP: {home} - {away}, {minute}', {market}", flush=True)

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
