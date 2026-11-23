"""Three-stage live goal-pressure scanner for FotMob."""
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Warsaw")
HEADERS = {
    "User-Agent": "TyperGOLE-OVERY/2.1 (live football monitor)",
    "Accept": "application/json",
}
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BASELINE_FROM = 60
ALERT_FROM = 70
ALERT_TO = 78
LOCK_AFTER = 80
POLL_SECONDS = 60
RUN_SECONDS = 255
MIN_SHOTS_DELTA = 3
MIN_CORNERS_DELTA = 2
REQUIRED_POINTS = 4
STATE_PATH = Path("data/over_live_state.json")
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def now_local():
    return datetime.now(TZ)


def get_json(url, params=None, timeout=18):
    response = SESSION.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "30")
        try:
            delay = max(5, min(120, int(float(retry_after))))
        except (TypeError, ValueError):
            delay = 30
        print(f"HTTP 429; waiting {delay}s before retry", flush=True)
        time.sleep(delay)
        response = SESSION.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def parse_minute(value):
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"(\d{1,3})", str(value or ""))
    return int(match.group(1)) if match else None


def fotmob_minute(match):
    if not isinstance(match, dict):
        return None
    status = match.get("status")
    if not isinstance(status, dict):
        status = {}
    reason = status.get("reason")
    if not isinstance(reason, dict):
        reason = {}
    for value in (
        status.get("liveTime"),
        reason.get("short"),
        status.get("short"),
    ):
        minute = parse_minute(value)
        if minute is not None and 1 <= minute <= 130:
            return minute
    return None


def fotmob_matches():
    payload = get_json(
        "https://www.fotmob.com/api/data/matches",
        {"date": now_local().date().isoformat()},
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"FotMob matches: oczekiwano obiektu JSON, otrzymano {type(payload).__name__}")
    leagues = payload.get("leagues")
    if not isinstance(leagues, list):
        raise RuntimeError("FotMob matches: brak poprawnej listy leagues")
    rows = []
    for league in leagues:
        if not isinstance(league, dict):
            continue
        matches = league.get("matches")
        if not isinstance(matches, list):
            continue
        for match in matches:
            if isinstance(match, dict):
                match["_league_name"] = league.get("name") or league.get("ccode") or "Liga niepodana"
                rows.append(match)
    return rows


WOMENS_MARKERS = (
    "women", "woman", "women's", "womens", "female", "ladies",
    "feminino", "feminina", "feminine", "frauen", "damen",
    "damallsvenskan", "liga f", "nwsl", "wsl", "feminin",
)


def is_womens_match(match):
    """Reject women's competitions using league/team labels and FotMob flags."""
    league = str(match.get("_league_name") or "")
    home = match.get("home") or match.get("homeTeam") or {}
    away = match.get("away") or match.get("awayTeam") or {}
    searchable = " ".join((
        league,
        str(home.get("name") or ""),
        str(away.get("name") or ""),
    )).lower()
    if any(marker in searchable for marker in WOMENS_MARKERS):
        return True

    # Some feed entries expose competition/team gender as explicit metadata.
    def has_womens_flag(node):
        if isinstance(node, dict):
            for key, value in node.items():
                key_norm = re.sub(r"[^a-z]", "", str(key).lower())
                if key_norm in {"iswomen", "iswomens", "isfemale", "womencompetition"} and value is True:
                    return True
                if key_norm in {"gender", "sex"} and str(value).strip().lower() in {"female", "women", "woman"}:
                    return True
                if isinstance(value, (dict, list)) and has_womens_flag(value):
                    return True
        elif isinstance(node, list):
            return any(has_womens_flag(item) for item in node)
        return False

    return has_womens_flag(match)

def normalize_match(match):
    if not isinstance(match, dict):
        raise ValueError("FotMob match nie jest obiektem")
    status = match.get("status")
    home = match.get("home")
    away = match.get("away")
    if not isinstance(status, dict):
        status = {}
    if not isinstance(home, dict):
        home = {}
    if not isinstance(away, dict):
        away = {}
    return {
        "id": str(match.get("id") or match.get("matchId") or ""),
        "home": home.get("name") or (match.get("homeTeam") or {}).get("name") or "Gospodarze",
        "away": away.get("name") or (match.get("awayTeam") or {}).get("name") or "Goście",
        "home_score": score_int(home.get("score", match.get("homeScore"))),
        "away_score": score_int(away.get("score", match.get("awayScore"))),
        "minute": fotmob_minute(match),
        "status": (status.get("reason") or {}).get("short") or status.get("short") or "",
        "league": match.get("_league_name") or "Liga niepodana",
    }


def score_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def is_live(match):
    status = str(match.get("status") or "").lower()
    if any(x in status for x in ("finished", "full time", "ft", "ended", "postponed", "cancelled")):
        return False
    return match.get("minute") is not None and 1 <= int(match["minute"]) <= 130


def stat_rows(payload):
    """Extract cumulative match totals; never substitute missing stats with zero."""
    if not isinstance(payload, dict):
        return {}
    content = payload.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    stats = content.get("stats") or payload.get("stats") or {}
    output = {}

    def walk(node, parent=""):
        if isinstance(node, list):
            for item in node:
                walk(item, parent)
        elif isinstance(node, dict):
            label = str(node.get("title") or node.get("name") or node.get("key") or parent).strip().lower()
            values = node.get("stats") or node.get("values")
            if isinstance(values, list) and values:
                numbers = []
                for value in values:
                    raw = value
                    if isinstance(value, dict):
                        raw = value.get("value", value.get("stat", value.get("title")))
                    try:
                        numbers.append(float(str(raw).replace("%", "").replace(",", ".")))
                    except (TypeError, ValueError):
                        continue
                if numbers:
                    output[label] = sum(numbers)
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value, str(key))

    walk(stats)
    return output


def metric(stats, aliases, exclude=()):
    normalized_items = [
        (re.sub(r"[^a-z0-9]+", " ", label.lower()).strip(), value)
        for label, value in stats.items()
    ]
    for alias in aliases:
        for label, value in normalized_items:
            if alias in label and not any(term in label for term in exclude):
                return value
    return None


def extract_pressure(stats):
    shots = metric(stats, ("total shots", "shots"), exclude=("on target", "blocked"))
    corners = metric(stats, ("corners", "corner kicks"))
    shots_on_target = metric(stats, ("shots on target", "on target"))
    return {
        "shots": shots,
        "corners": corners,
        "shots_on_target": shots_on_target,
    }


def match_detail(match_id):
    return get_json(
        "https://www.fotmob.com/api/data/matchDetails",
        {"matchId": match_id},
    )


def load_state():
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value.setdefault("matches", {})
            return value
    except (OSError, ValueError):
        pass
    return {"matches": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID w GitHub Secrets.")
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def process_match(match, state, today):
    minute = int(match["minute"])
    match_id = match["id"]
    if not match_id or match["home_score"] is None or match["away_score"] is None:
        return
    if abs(match["home_score"] - match["away_score"]) > 1:
        return

    key = f"{today}:{match_id}"
    record = state["matches"].setdefault(key, {
        "alert_wyslany": False,
        "locked": False,
        "baseline": None,
    })

    if minute >= LOCK_AFTER:
        record["locked"] = True
        return
    if record.get("locked") or record.get("alert_wyslany"):
        return
    total_goals = match["home_score"] + match["away_score"]
    if total_goals >= 2:
        record["locked"] = True
        return

    # Baseline is captured once on the first observation in minutes 60–69.
    if BASELINE_FROM <= minute < ALERT_FROM and record.get("baseline") is None:
        detail = match_detail(match_id)
        pressure = extract_pressure(stat_rows(detail))
        if pressure["shots"] is None or pressure["corners"] is None:
            print(f"{match_id}: baseline skipped; shots/corners unavailable", flush=True)
            return
        record["baseline"] = {
            "minute": minute,
            "captured_at": now_local().isoformat(),
            **pressure,
        }
        print(f"{match_id}: quiet baseline saved at {minute}'", flush=True)
        return

    # No baseline means no signal: never compare against fabricated zeros.
    if not (ALERT_FROM <= minute <= ALERT_TO) or record.get("baseline") is None:
        return

    detail = match_detail(match_id)
    current = extract_pressure(stat_rows(detail))
    baseline = record["baseline"]
    if any(current[name] is None or baseline.get(name) is None for name in ("shots", "corners")):
        print(f"{match_id}: comparison skipped; incomplete stats", flush=True)
        return

    shots_delta = max(0, current["shots"] - baseline["shots"])
    corners_delta = max(0, current["corners"] - baseline["corners"])
    sot_delta = None
    if current["shots_on_target"] is not None and baseline.get("shots_on_target") is not None:
        sot_delta = max(0, current["shots_on_target"] - baseline["shots_on_target"])

    points = 0
    if shots_delta >= MIN_SHOTS_DELTA:
        points += 2
    if corners_delta >= MIN_CORNERS_DELTA:
        points += 2
    if sot_delta is not None and sot_delta >= 1:
        points += 1

    print(
        f"{match_id}: {minute}' shots +{shots_delta}, corners +{corners_delta}, "
        f"SOT +{sot_delta if sot_delta is not None else 'n/a'}, points={points}",
        flush=True,
    )
    # The primary pressure pattern is mandatory; points alone cannot bypass it.
    if shots_delta < MIN_SHOTS_DELTA or corners_delta < MIN_CORNERS_DELTA or points < REQUIRED_POINTS:
        return

    market = "Over 0.5 FT" if total_goals == 0 else "Over 1.5 FT"
    message = (
        "⚡ OVER LIVE — SYGNAŁ PRESJI\n"
        f"{match['home']} — {match['away']}\n"
        f"Liga: {match['league']}\n"
        f"Minuta: {minute}' | Wynik: {match['home_score']}:{match['away_score']}\n"
        f"Rynek: {market}\n"
        f"Od zapisu z {baseline['minute']}': +{shots_delta} strzały, "
        f"+{corners_delta} rożne"
        + (f", +{sot_delta} celne strzały" if sot_delta is not None else "")
        + f"\nPunkty presji: {points}/{REQUIRED_POINTS}\n"
        "Kurs: sprawdź aktualny rynek u bukmachera przed decyzją."
    )
    send_telegram(message)
    record["alert_wyslany"] = True
    record["alert_minute"] = minute
    record["alert_market"] = market
    record["alert_sent_at"] = now_local().isoformat()
    print(f"{match_id}: alert sent; alert_wyslany=True", flush=True)


def run():
    started = time.monotonic()
    deadline = started + RUN_SECONDS
    state = load_state()
    today = now_local().date().isoformat()
    print(
        f"=== OVER LIVE 3-STAGE START {now_local().isoformat()} "
        f"(60' baseline / 70-78' alert / >80' lock) ===",
        flush=True,
    )

    cycle = 0
    while time.monotonic() < deadline:
        cycle += 1
        cycle_started = time.monotonic()
        try:
            raw_matches = fotmob_matches()
            excluded_womens = sum(1 for item in raw_matches if is_womens_match(item))
            matches = [normalize_match(item) for item in raw_matches if not is_womens_match(item)]
            print(f"Women's matches excluded: {excluded_womens}", flush=True)
            live = [
                item for item in matches
                if is_live(item)
                and item["minute"] is not None
                and BASELINE_FROM <= int(item["minute"]) <= 130
                and item["home_score"] is not None
                and item["away_score"] is not None
                and abs(item["home_score"] - item["away_score"]) <= 1
            ]
            print(f"Cycle {cycle}: live candidates={len(live)}", flush=True)
            for match in live:
                try:
                    process_match(match, state, today)
                except Exception as exc:
                    print(
                        f"Match {match.get('id')} processing error: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                save_state(state)
                if time.monotonic() >= deadline:
                    break
                time.sleep(1.5)
        except Exception as exc:
            print(f"Live feed error: {type(exc).__name__}: {exc}", flush=True)

        save_state(state)
        elapsed = time.monotonic() - cycle_started
        remaining = min(POLL_SECONDS - elapsed, deadline - time.monotonic())
        if remaining > 0:
            time.sleep(remaining)

    save_state(state)
    print(
        f"=== OVER LIVE END {now_local().isoformat()} "
        f"cycles={cycle}; state saved ===",
        flush=True,
    )


if __name__ == "__main__":
    run()
