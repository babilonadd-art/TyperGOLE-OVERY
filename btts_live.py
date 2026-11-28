"""Conservative LIVE BTTS scanner using public FotMob match statistics.

This module is deliberately separate from prematch BTTS and the existing OVER LIVE
scanner. It never fabricates bookmaker odds. It sends at most four alerts per
Warsaw-local day and requires evidence of attacking threat from both sides.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Warsaw")
HEADERS = {
    "User-Agent": "TyperGOLE-OVERY/1.0 (live BTTS monitor)",
    "Accept": "application/json",
}
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STATE_PATH = Path("data/btts_live_state.json")
MAX_ALERTS_PER_DAY = 4

MOVIODDS_API = "https://moviodds.com"
MOVIODDS_API_KEY = os.getenv("MOVIODDS_API_KEY", "").strip()
_LIVE_ODDS_BOARD = None

def normalize_team(value):
    value = str(value or "").lower()
    value = re.sub(r"\b(fc|cf|sc|afc|club|the)\b", " ", value)
    return " ".join(re.findall(r"[a-z0-9]+", value))

def live_odds_board():
    global _LIVE_ODDS_BOARD
    if _LIVE_ODDS_BOARD is not None:
        return _LIVE_ODDS_BOARD
    if not MOVIODDS_API_KEY:
        raise RuntimeError("Brak MOVIODDS_API_KEY w GitHub Secrets — bez klucza nie ma weryfikacji kursu LIVE.")
    response = SESSION.get(
        f"{MOVIODDS_API}/odds",
        params={"sport": "soccer", "in_play": 1},
        headers={"X-API-Key": MOVIODDS_API_KEY, "Accept-Encoding": "gzip"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("matches") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("MoviOdds: nieprawidłowa odpowiedź tablicy LIVE.")
    _LIVE_ODDS_BOARD = [row for row in rows if isinstance(row, dict) and row.get("in_play") is True]
    print(f"MoviOdds/Bet365: {len(_LIVE_ODDS_BOARD)} meczów LIVE do weryfikacji kursu.", flush=True)
    return _LIVE_ODDS_BOARD

def find_bet365_live_btts_quote(match):
    home_key, away_key = normalize_team(match["home"]), normalize_team(match["away"])
    fixture_matches = []
    for row in live_odds_board():
        row_home, row_away = normalize_team(row.get("home")), normalize_team(row.get("away"))
        if not home_key or not away_key:
            continue
        home_ratio = difflib.SequenceMatcher(None, home_key, row_home).ratio()
        away_ratio = difflib.SequenceMatcher(None, away_key, row_away).ratio()
        if home_key == row_home and away_key == row_away:
            fixture_matches.append((1.0, row))
        elif home_ratio >= 0.92 and away_ratio >= 0.92:
            fixture_matches.append((min(home_ratio, away_ratio), row))
    if len(fixture_matches) != 1:
        if len(fixture_matches) > 1:
            print(f"MoviOdds: niejednoznaczne dopasowanie {match['home']} - {match['away']}; odrzucam kurs.", flush=True)
        return None
    fixture = fixture_matches[0][1]
    group_status = fixture.get("group_status") if isinstance(fixture.get("group_status"), dict) else {}
    candidates = []
    for quote in fixture.get("quotes", []) if isinstance(fixture.get("quotes"), list) else []:
        if not isinstance(quote, dict):
            continue
        market = re.sub(r"[^a-z0-9]+", " ", str(quote.get("group_name") or "").lower()).strip()
        if market != "both teams to score":
            continue
        phase = str(quote.get("phase") or "").strip().lower().replace("-", "_")
        if phase and phase not in {"live", "inplay", "in_play"}:
            continue
        group_id = str(quote.get("group_id") or "")
        status = group_status.get(group_id, {}) if group_id else {}
        if not isinstance(status, dict) or status.get("stale") is True:
            continue
        if quote.get("suspended") is True or quote.get("stale") is True:
            continue
        if str(quote.get("label") or "").strip().lower() not in {"yes", "tak"}:
            continue
        try:
            odds = float(quote.get("odds_dec"))
        except (TypeError, ValueError):
            continue
        if not 1.01 <= odds <= 1000:
            continue
        candidates.append({"odds": odds, "source": "Bet365 / MoviOdds",
            "market": "Both Teams to Score — Yes (LIVE FT)", "updated_at": quote.get("ts"),
            "fixture_id": str(fixture.get("id") or ""), "group_id": group_id,
            "sel_id": str(quote.get("sel_id") or "")})
    unique = {(item["group_id"], round(item["odds"], 3)) for item in candidates}
    if len(unique) != 1:
        return None
    chosen = candidates[0]
    print(f"Bet365 LIVE BTTS potwierdzony: {match['home']} - {match['away']} @ {chosen['odds']:.2f}; ts={chosen['updated_at']}", flush=True)
    return chosen


MIN_MINUTE = 18
MAX_MINUTE = 78
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def now_local():
    return datetime.now(TZ)


def get_json(url, params=None):
    response = SESSION.get(url, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("FotMob zwrócił nieprawidłowy format danych")
    return payload


def parse_minute(match):
    status = match.get("status") or {}
    reason = status.get("reason") or {}
    for value in (status.get("liveTime"), reason.get("short"), status.get("short")):
        found = re.search(r"(\d{1,3})", str(value or ""))
        if found:
            minute = int(found.group(1))
            if 1 <= minute <= 130:
                return minute
    return None


def is_womens(match, league):
    home = match.get("home") or match.get("homeTeam") or {}
    away = match.get("away") or match.get("awayTeam") or {}
    text = " ".join((str(league), str(home.get("name") or ""), str(away.get("name") or ""))).lower()
    markers = ("women", "woman", "female", "ladies", "feminino", "feminina",
               "frauen", "damen", "nwsl", "wsl", "damallsvenskan", "liga f")
    if any(marker in text for marker in markers):
        return True

    def flagged(node):
        if isinstance(node, dict):
            for key, value in node.items():
                key = re.sub(r"[^a-z]", "", str(key).lower())
                if key in {"iswomen", "iswomens", "isfemale", "womencompetition"} and value is True:
                    return True
                if key in {"gender", "sex"} and str(value).lower() in {"female", "women", "woman"}:
                    return True
                if isinstance(value, (dict, list)) and flagged(value):
                    return True
        elif isinstance(node, list):
            return any(flagged(x) for x in node)
        return False
    return flagged(match)


def to_number(value):
    if isinstance(value, dict):
        value = value.get("value", value.get("stat", value.get("title")))
    try:
        cleaned = str(value).replace("%", "").replace(",", ".").strip()
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def collect_team_stats(payload):
    """Return {normalized label: (home_value, away_value)}; never sum the sides."""
    content = payload.get("content") or {}
    root = content.get("stats") or payload.get("stats") or {}
    found = {}

    def visit(node, parent=""):
        if isinstance(node, list):
            for item in node:
                visit(item, parent)
            return
        if not isinstance(node, dict):
            return

        label = str(node.get("title") or node.get("name") or node.get("key") or parent).strip().lower()
        values = node.get("stats", node.get("values"))
        if isinstance(values, list) and len(values) >= 2:
            pair = (to_number(values[0]), to_number(values[1]))
            if pair[0] is not None or pair[1] is not None:
                found[re.sub(r"[^a-z0-9]+", " ", label).strip()] = pair
        elif isinstance(values, dict):
            home = to_number(values.get("home", values.get("homeValue")))
            away = to_number(values.get("away", values.get("awayValue")))
            if home is not None or away is not None:
                found[re.sub(r"[^a-z0-9]+", " ", label).strip()] = (home, away)

        # Some FotMob responses expose home/away values directly on the stat row.
        home = to_number(node.get("home"))
        away = to_number(node.get("away"))
        if (home is not None or away is not None) and label:
            found[re.sub(r"[^a-z0-9]+", " ", label).strip()] = (home, away)

        for key, value in node.items():
            if isinstance(value, (dict, list)):
                visit(value, str(key))
    visit(root)
    return found


def find_stat(stats, aliases, exclude=()):
    for alias in aliases:
        for label, pair in stats.items():
            if alias in label and not any(term in label for term in exclude):
                return pair
    return (None, None)


def team_metrics(stats):
    shots = find_stat(stats, ("total shots", "shots"), ("on target", "blocked"))
    sot = find_stat(stats, ("shots on target", "on target"))
    xg = find_stat(stats, ("expected goals", "xg"))
    big = find_stat(stats, ("big chances",))
    corners = find_stat(stats, ("corners", "corner kicks"))
    return {
        "shots": shots, "sot": sot, "xg": xg, "big": big, "corners": corners,
    }


def num(pair, side):
    return pair[side] if pair and len(pair) > side else None


def team_has_threat(metrics, side, scoreless_mode=False):
    shots = num(metrics["shots"], side)
    sot = num(metrics["sot"], side)
    xg = num(metrics["xg"], side)
    big = num(metrics["big"], side)
    # Missing stats are not treated as zero and cannot create an alert.
    if scoreless_mode:
        return (
            (shots is not None and shots >= 4 and sot is not None and sot >= 1)
            or (xg is not None and xg >= 0.35 and shots is not None and shots >= 3)
            or (big is not None and big >= 1 and shots is not None and shots >= 3)
        )
    return (
        (shots is not None and shots >= 3 and sot is not None and sot >= 1)
        or (xg is not None and xg >= 0.25 and shots is not None and shots >= 2)
        or (big is not None and big >= 1)
    )


def format_stat(value):
    if value is None:
        return "brak danych"
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def load_state():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            state.setdefault("alerts", {})
            return state
    except (OSError, ValueError):
        pass
    return {"alerts": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID w GitHub Secrets")
    response = SESSION.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result["result"]["message_id"]


def live_matches():
    payload = get_json(
        "https://www.fotmob.com/api/data/matches",
        {"date": now_local().date().isoformat()},
    )
    leagues = payload.get("leagues")
    if not isinstance(leagues, list):
        raise RuntimeError("FotMob: brak listy rozgrywek")
    output = []
    for league in leagues:
        if not isinstance(league, dict):
            continue
        league_name = league.get("name") or league.get("ccode") or ""
        for match in league.get("matches", []) if isinstance(league.get("matches"), list) else []:
            if not isinstance(match, dict) or is_womens(match, league_name):
                continue
            status = match.get("status") or {}
            reason = status.get("reason") or {}
            status_text = " ".join((str(status.get("short") or ""), str(reason.get("short") or ""))).lower()
            if any(word in status_text for word in ("finished", "full time", "ft", "ended", "postponed", "cancelled")):
                continue
            minute = parse_minute(match)
            home = match.get("home") or match.get("homeTeam") or {}
            away = match.get("away") or match.get("awayTeam") or {}
            try:
                hs = int(home.get("score", match.get("homeScore")))
                aws = int(away.get("score", match.get("awayScore")))
            except (TypeError, ValueError):
                continue
            if minute is None or not MIN_MINUTE <= minute <= MAX_MINUTE:
                continue
            # BTTS is still open only at 0-0, 1-0 or 0-1.
            if (hs, aws) not in {(0, 0), (1, 0), (0, 1)}:
                continue
            match_id = str(match.get("id") or match.get("matchId") or "")
            if not match_id:
                continue
            output.append({
                "id": match_id, "home": home.get("name") or "Gospodarze",
                "away": away.get("name") or "Goście", "home_score": hs,
                "away_score": aws, "minute": minute, "league": league_name or "Liga niepodana",
            })
    return output


def evaluate(match):
    detail = get_json(
        "https://www.fotmob.com/api/data/matchDetails",
        {"matchId": match["id"]},
    )
    metrics = team_metrics(collect_team_stats(detail))
    scoreless = match["home_score"] == 0 and match["away_score"] == 0
    if scoreless:
        home_ok = team_has_threat(metrics, 0, True)
        away_ok = team_has_threat(metrics, 1, True)
        if not (home_ok and away_ok):
            return None
        reason = "obie drużyny tworzą sytuacje (strzały/celne/xG)"
    elif match["home_score"] == 1:
        if not team_has_threat(metrics, 1, False):
            return None
        reason = "gospodarze już strzelili; goście pokazują realną presję"
    else:
        if not team_has_threat(metrics, 0, False):
            return None
        reason = "goście już strzelili; gospodarze pokazują realną presję"

    quote = find_bet365_live_btts_quote(match)
    if not quote:
        print("LIVE BTTS: brak aktywnego, jednoznacznego kursu BTTS TAK FT — pomijam mecz.", flush=True)
        return None

    return {
        "reason": reason,
        "home": metrics,
        "away": metrics,
        "quote": quote,
    }


def run():
    now = now_local()
    day = now.date().isoformat()
    state = load_state()
    alerts = state["alerts"]
    sent_today = sum(1 for key, item in alerts.items() if key.startswith(day + ":") and item.get("sent"))
    print(f"=== LIVE BTTS START {now.isoformat()} | wysłano dziś: {sent_today}/{MAX_ALERTS_PER_DAY} ===", flush=True)
    if sent_today >= MAX_ALERTS_PER_DAY:
        print("Dzienny limit LIVE BTTS osiągnięty.", flush=True)
        return

    candidates = live_matches()
    print(f"FotMob: kandydaci LIVE BTTS={len(candidates)}", flush=True)
    for match in candidates:
        key = f"{day}:{match['id']}"
        if key in alerts or sent_today >= MAX_ALERTS_PER_DAY:
            continue
        try:
            signal = evaluate(match)
            if not signal:
                continue
            home_m = signal["home"]
            away_m = signal["away"]
            message = (
                "⚽ BTTS LIVE — SYGNAŁ DO FT\n"
                f"{match['league']} | {match['minute']}'\n"
                f"{match['home']} {match['home_score']}:{match['away_score']} {match['away']}\n"
                "Rynek: OBA ZESPOŁY STRZELĄ — TAK (FT)\n"
                f"Kurs LIVE Bet365: {signal['quote']['odds']:.2f}\n"
                f"Źródło: {signal['quote']['source']} | aktualizacja: {signal['quote'].get('updated_at') or 'brak znacznika czasu'}\n"
                f"Ocena: {signal['reason']}\n"
                f"Statystyki H/A — strzały: {format_stat(num(home_m['shots'], 0))}/{format_stat(num(away_m['shots'], 1))}, "
                f"celne: {format_stat(num(home_m['sot'], 0))}/{format_stat(num(away_m['sot'], 1))}, "
                f"xG: {format_stat(num(home_m['xg'], 0))}/{format_stat(num(away_m['xg'], 1))}\n"
                "Kurs został sprawdzony przed wysłaniem alertu.\n"
                "To sygnał statystyczny, nie gwarancja wyniku."
            )
            message_id = send_telegram(message)
            alerts[key] = {
                "sent": True, "message_id": message_id,
                "sent_at": now_local().isoformat(), "minute": match["minute"],
                "home": match["home"], "away": match["away"],
                "score": [match["home_score"], match["away_score"]],
            }
            save_state(state)
            sent_today += 1
            print(f"LIVE BTTS wysłany: {match['home']} - {match['away']} ({message_id})", flush=True)
        except Exception as exc:
            print(f"LIVE BTTS {match.get('id')} error: {type(exc).__name__}: {exc}", flush=True)
    save_state(state)
    print(f"=== LIVE BTTS END; wysłano dziś: {sent_today}/{MAX_ALERTS_PER_DAY} ===", flush=True)


if __name__ == "__main__":
    run()
