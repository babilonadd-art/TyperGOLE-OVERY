"""Live football goal scanner (FotMob with ESPN fallback).

The scanner deliberately does not invent bookmaker prices. It publishes only
verified live candidates and labels unavailable market prices as unavailable.
"""
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Warsaw")
HEADERS = {
    "User-Agent": "TyperGOLE-OVERY/2.0 (live football monitor)",
    "Accept": "application/json",
}
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MINUTE_MIN = 68
MINUTE_MAX = 82
MAX_POSTS_PER_RUN = 5
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_json(url, params=None, timeout=18):
    response = SESSION.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "30")
        try:
            delay = max(5, min(120, int(float(retry_after))))
        except (TypeError, ValueError):
            delay = 30
        print(f"HTTP 429 from {url}; respecting Retry-After for {delay}s", flush=True)
        time.sleep(delay)
        response = SESSION.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fotmob_matches():
    day = datetime.now(TZ).date().isoformat()
    payload = get_json("https://www.fotmob.com/api/data/matches", {"date": day})
    rows = []
    for league in payload.get(" leagues", payload.get("leagues", [])):
        for match in league.get("matches", []):
            if isinstance(match, dict):
                match["_league_name"] = league.get("name") or league.get("ccode") or "Liga niepodana"
                rows.append(match)
    return rows


def espn_matches():
    # ESPN fallback scoreboard. This endpoint may cover fewer competitions.
    payload = get_json("https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard")
    rows = []
    for event in payload.get("events", []):
        competitions = event.get("competitions") or []
        comp = competitions[0] if competitions else {}
        competitors = comp.get("competitors") or []
        home = next((x for x in competitors if x.get("homeAway") == "home"), {})
        away = next((x for x in competitors if x.get("homeAway") == "away"), {})
        status = (comp.get("status") or {}).get("type") or {}
        clock = (comp.get("status") or {}).get("displayClock") or ""
        minute = parse_minute(clock)
        rows.append({
            "id": event.get("id"),
            "home": (home.get("team") or {}).get("displayName"),
            "away": (away.get("team") or {}).get("displayName"),
            "home_score": home.get("score"),
            "away_score": away.get("score"),
            "minute": minute,
            "status": status.get("name") or status.get("description"),
            "_league_name": (event.get("season") or {}).get("name") or "ESPN Soccer",
            "_source": "ESPN",
        })
    return rows


def parse_minute(value):
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"(\d{1,3})", str(value or ""))
    return int(match.group(1)) if match else None


def fotmob_minute(match):
    status = match.get("status") or {}
    candidates = [
        status.get("liveTime"), status.get("reason", {}).get("short"),
        match.get("status", {}).get("short"),
    ]
    for value in candidates:
        minute = parse_minute(value)
        if minute is not None and 1 <= minute <= 130:
            return minute
    return None


def normalize_match(match):
    if match.get("_source") == "ESPN":
        return match
    status = match.get("status") or {}
    score = match.get("status") or {}
    home = match.get("home") or {}
    away = match.get("away") or {}
    return {
        "id": str(match.get("id") or match.get("matchId") or ""),
        "home": home.get("name") or match.get("homeTeam", {}).get("name"),
        "away": away.get("name") or match.get("awayTeam", {}).get("name"),
        "home_score": home.get("score", match.get("homeScore")),
        "away_score": away.get("score", match.get("awayScore")),
        "minute": fotmob_minute(match),
        "status": status.get("reason", {}).get("short") or status.get("short") or "",
        "_league_name": match.get("_league_name") or "Liga niepodana",
        "_source": "FotMob",
        "_raw": match,
    }


def is_live(match):
    status = str(match.get("status") or "").lower()
    if any(x in status for x in ("finished", "full time", "ft", "ended", "postponed", "cancelled")):
        return False
    minute = match.get("minute")
    return minute is not None and 1 <= int(minute) <= 130


def stat_rows(payload):
    # FotMob matchDetails stats vary by competition. Flatten only numeric values.
    content = payload.get("content") or {}
    stats = content.get("stats") or payload.get("stats") or {}
    output = {}
    def walk(node, path=""):
        if isinstance(node, list):
            for item in node:
                walk(item, path)
        elif isinstance(node, dict):
            label = str(node.get("title") or node.get("name") or node.get("key") or path).lower()
            values = node.get("stats") or node.get("values")
            if isinstance(values, list) and values:
                nums = []
                for v in values:
                    raw = v.get(" stat") if isinstance(v, dict) else v
                    if isinstance(v, dict):
                        raw = v.get("value") or v.get("stat") or v.get("title")
                    try:
                        nums.append(float(str(raw).replace("%", "").replace(",", ".")))
                    except (TypeError, ValueError):
                        pass
                if nums:
                    output[label] = sum(nums)
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value, str(key))
    walk(stats)
    return output


def score_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def candidate_score(match, stats):
    home_goals = score_int(match.get("home_score"))
    away_goals = score_int(match.get("away_score"))
    if home_goals is None or away_goals is None:
        return None, "brak potwierdzonego wyniku"
    total = home_goals + away_goals
    # Do not recommend an already-settled total-goals line.
    if total >= 2:
        return None, "Over 1.5 FT jest już rozstrzygnięty"
    if total == 1:
        market = "OVER_1_5_FT"
    else:
        market = "OVER_0_5_FT"

    # Cumulative stats are not misrepresented as last-15-minute momentum.
    signals = []
    for key, threshold, label in (
        ("total shots", 8, "strzały"),
        ("shots", 8, "strzały"),
        ("shots on target", 3, "celne strzały"),
        ("corners", 5, "rożne"),
        ("dangerous attacks", 25, "groźne ataki"),
        ("expected goals", 1.0, "xG"),
        ("xg", 1.0, "xG"),
    ):
        value = next((v for k, v in stats.items() if key in k), None)
        if value is not None and value >= threshold and label not in signals:
            signals.append(label)
    if len(signals) < 2:
        return None, "za mało potwierdzonych sygnałów statystycznych"
    return market, ", ".join(signals)


def fetch_fotmob_detail(match_id):
    return get_json("https://www.fotmob.com/api/data/matchDetails", {"matchId": match_id})


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID w GitHub Secrets.")
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload


def run():
    print(f"=== OVER LIVE START {datetime.now(TZ).isoformat()} ===", flush=True)
    source = "FotMob"
    try:
        raw_matches = fotmob_matches()
        matches = [normalize_match(x) for x in raw_matches]
        print(f"FotMob: {len(matches)} fixtures fetched", flush=True)
    except Exception as exc:
        print(f"FotMob unavailable: {type(exc).__name__}: {exc}; trying ESPN fallback", flush=True)
        source = "ESPN fallback"
        matches = espn_matches()

    live = [m for m in matches if is_live(m)]
    filtered = [
        m for m in live
        if m.get("minute") is not None
        and MINUTE_MIN <= int(m["minute"]) <= MINUTE_MAX
        and score_int(m.get("home_score")) is not None
        and score_int(m.get("away_score")) is not None
        and abs(score_int(m.get("home_score")) - score_int(m.get("away_score"))) <= 1
    ]
    print(f"{source}: live={len(live)}, minute/score candidates={len(filtered)}", flush=True)

    picks = []
    for match in filtered:
        stats = {}
        if match.get("_source") == "ESPN":
            # ESPN fallback scoreboard has no reliable detailed live-stat feed here.
            continue
        try:
            detail = fetch_fotmob_detail(match.get("id"))
            stats = stat_rows(detail)
        except Exception as exc:
            print(f"FotMob detail {match.get('id')}: {type(exc).__name__}: {exc}", flush=True)
            continue
        market, reason = candidate_score(match, stats)
        if not market:
            continue
        # Odds are intentionally not fabricated: this data feed does not supply
        # a verified bookmaker quote for either requested market.
        picks.append((match, market, reason))
        if len(picks) >= MAX_POSTS_PER_RUN:
            break
        time.sleep(3)

    if not picks:
        print("OVER LIVE: brak kandydatów spełniających kryteria; nie wysyłam pustej ani fikcyjnej listy.", flush=True)
        return

    lines = ["⚽ OVER LIVE — KANDYDACI", f"🕒 {datetime.now(TZ).strftime('%d.%m.%Y %H:%M')} (Europe/Warsaw)", ""]
    for match, market, reason in picks:
        market_label = "Over 0.5 FT" if market == "OVER_0_5_FT" else "Over 1.5 FT"
        lines.extend([
            f"{match.get('home')} — {match.get('away')}",
            f"Liga: {match.get('_league_name') or 'niepodana'}",
            f"Minuta: {match.get('minute')} | Wynik: {match.get('home_score')}:{match.get('away_score')}",
            f"Rynek: {market_label}",
            f"Sygnały (statystyki meczowe łącznie): {reason}",
            "Kurs: niedostępny w użytym źródle — sprawdź przed zagraniem.",
            "",
        ])
    send_telegram("\n".join(lines))
    print(f"OVER LIVE: sent {len(picks)} candidate(s)", flush=True)


if __name__ == "__main__":
    run()
