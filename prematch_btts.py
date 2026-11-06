"""Pre-match BTTS 1H strategy. Fails closed when fixture, history or Superbet data is incomplete."""
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Warsaw")
SPORTSCORE = "https://sportscore.com"
SUPERBET = "https://production-superbet-offer-pl.freetls.fastly.net"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_PATH = Path("data/state.json")
MAX_DAILY_PICKS = 4
MIN_EDGE = 0.05
MIN_VENUE_SAMPLE = 5
HISTORY_LIMIT = 20
HEADERS = {"User-Agent": "TyperGOLE-OVERY/2.0", "Accept": "application/json"}


def now_local():
    return datetime.now(TZ)


def normalize(value):
    value = str(value or "").replace("ł", "l").replace("Ł", "L")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def slugify(value):
    return normalize(value).replace(" ", "-")


def get_json(url, params=None, headers=None, timeout=20):
    response = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def unwrap_list(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "fixtures", "matches", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = unwrap_list(value)
            if nested:
                return nested
    return []


def first(obj, *keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return None


def team_names(match):
    home = first(match, "home", "home_team", "homeTeam", "team_home")
    away = first(match, "away", "away_team", "awayTeam", "team_away")
    if isinstance(home, dict):
        home = first(home, "name", "short_name", "title")
    if isinstance(away, dict):
        away = first(away, "name", "short_name", "title")
    return str(home or ""), str(away or "")


def kickoff(match):
    value = first(match, "kickoff", "kickoff_time", "start_time", "date", "datetime", "starting_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TZ)
    except ValueError:
        return None


def fixture_id(match):
    return str(first(match, "id", "fixture_id", "match_id", "slug") or "")


def upcoming_fixtures():
    """Only accept records explicitly marked scheduled/upcoming and with a future kickoff."""
    today = now_local().date().isoformat()
    errors = []
    for path in ("/api/v1/fixtures/", "/api/v1/fixtures"):
        try:
            payload = get_json(SPORTSCORE + path, {
                "sport": "football", "date": today, "status": "upcoming", "limit": 200
            })
            rows = unwrap_list(payload)
            accepted = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                status = normalize(first(row, "status", "state", "match_status", "fixture_status") or "")
                start = kickoff(row)
                home, away = team_names(row)
                if not (home and away and start and start > now_local() + timedelta(minutes=10)):
                    continue
                if status and not any(word in status for word in ("scheduled", "upcoming", "not started", "notstarted", "fixture")):
                    continue
                if first(row, "home_score", "homeScore", "score_home") is not None:
                    continue
                row["_kickoff_local"] = start.isoformat()
                accepted.append(row)
            if accepted:
                print(f"SportScore: {len(accepted)} nadchodzących meczów", flush=True)
                return accepted
            print(f"SportScore {path}: brak potwierdzonych nadchodzących rekordów; próbuję kolejny endpoint", flush=True)
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    print("Nie udało się pobrać terminarza: " + " | ".join(errors), flush=True)
    return []


def history_rows(team):
    slug = slugify(team)
    payload = get_json(SPORTSCORE + "/api/widget/team/", {
        "sport": "football", "slug": slug, "limit": HISTORY_LIMIT
    })
    rows = unwrap_list(payload)
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        home, away = team_names(row)
        if normalize(team) not in (normalize(home), normalize(away)):
            continue
        ht_home = first(row, "half_time_home_score", "halftime_home_score", "ht_home", "home_ht_score")
        ht_away = first(row, "half_time_away_score", "halftime_away_score", "ht_away", "away_ht_score")
        if ht_home is None or ht_away is None:
            slug_match = first(row, "slug", "match_slug")
            if not slug_match:
                continue
            detail = get_json(SPORTSCORE + "/api/widget/match/", {
                "sport": "football", "slug": slug_match
            })
            detail = detail.get("data", detail) if isinstance(detail, dict) else {}
            if isinstance(detail, list):
                detail = detail[0] if detail else {}
            ht_home = first(detail, "half_time_home_score", "halftime_home_score", "ht_home", "home_ht_score")
            ht_away = first(detail, "half_time_away_score", "halftime_away_score", "ht_away", "away_ht_score")
            if ht_home is None or ht_away is None:
                score = first(detail, "half_time_score", "halftime_score", "ht_score")
                if isinstance(score, str):
                    found = re.match(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", score)
                    if found:
                        ht_home, ht_away = found.groups()
        try:
            ht_home, ht_away = int(ht_home), int(ht_away)
        except (TypeError, ValueError):
            continue
        output.append({
            "home": home, "away": away, "ht_home": ht_home, "ht_away": ht_away,
            "both": ht_home > 0 and ht_away > 0
        })
    return output


def rate(successes, total):
    # Light Beta(1,1) smoothing avoids extreme 0%/100% estimates from small samples.
    return (successes + 1.0) / (total + 2.0) if total >= 1 else None


def estimate_btts_probability(home, away):
    h_rows = history_rows(home)
    a_rows = history_rows(away)
    h_home = [r for r in h_rows if normalize(r["home"]) == normalize(home)]
    a_away = [r for r in a_rows if normalize(r["away"]) == normalize(away)]
    if len(h_home) < MIN_VENUE_SAMPLE or len(a_away) < MIN_VENUE_SAMPLE:
        return None
    h_score = rate(sum(r["ht_home"] > 0 for r in h_home), len(h_home))
    h_concede = rate(sum(r["ht_away"] > 0 for r in h_home), len(h_home))
    a_score = rate(sum(r["ht_away"] > 0 for r in a_away), len(a_away))
    a_concede = rate(sum(r["ht_home"] > 0 for r in a_away), len(a_away))
    # Each side's scoring chance is averaged with the opponent's conceding rate.
    p_home_scores = (h_score + a_concede) / 2
    p_away_scores = (a_score + h_concede) / 2
    direct = rate(sum(r["both"] for r in (h_home + a_away)), len(h_home + a_away))
    probability = 0.8 * p_home_scores * p_away_scores + 0.2 * direct
    return {
        "probability": max(0.01, min(0.95, probability)),
        "home_sample": len(h_home), "away_sample": len(a_away),
        "home_scoring_rate": h_score, "away_scoring_rate": a_score,
        "home_conceding_rate": h_concede, "away_conceding_rate": a_concede,
        "direct_btts_rate": direct,
    }


def superbet_events():
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    payload = get_json(SUPERBET + "/v3/pl-PL/events", {
        "startDate": start.isoformat().replace("+00:00", "Z"),
        "endDate": end.isoformat().replace("+00:00", "Z"),
    }, headers={**HEADERS, "Origin": "https://superbet.pl", "Referer": "https://superbet.pl/"}, timeout=25)
    return payload.get("events", []) if isinstance(payload, dict) else []


def event_teams(event):
    fixture = event.get("fixture") or {}
    name = str(first(fixture, "event_name", "eventName") or "")
    parts = name.split("·", 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", "")


def match_superbet_event(home, away, events):
    matches = []
    for event in events:
        eh, ea = event_teams(event)
        if normalize(eh) == normalize(home) and normalize(ea) == normalize(away):
            event_id = first(event, "event_id", "id", default=None)
            if event_id is not None:
                matches.append(str(event_id))
    return matches[0] if len(matches) == 1 else None


def superbet_btts_1h(event_id):
    detail_payload = get_json(SUPERBET + f"/v2/pl-PL/events/{event_id}", {
        "_": str(datetime.now(timezone.utc).timestamp())
    }, headers={**HEADERS, "Origin": "https://superbet.pl", "Referer": "https://superbet.pl/"})
    records = detail_payload.get("data", []) if isinstance(detail_payload, dict) else []
    if not records:
        return None
    found = []

    def walk(node, market_context=""):
        if isinstance(node, list):
            for item in node:
                walk(item, market_context)
        elif isinstance(node, dict):
            labels = [str(node.get(k)) for k in (
                "marketName", "market_name", "marketGroupName", "market_group_name",
                "marketTitle", "market_title", "market", "title"
            ) if node.get(k) not in (None, "") and not isinstance(node.get(k), (dict, list))]
            context = " ".join([market_context] + labels).strip()
            selection = str(first(node, "name", "selectionName", "selection_name", "oddName", "outcomeName", "label", "displayName") or "")
            price = first(node, "price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec")
            if price is not None and selection:
                text = normalize(context)
                pick = normalize(selection)
                status = normalize(first(node, "status", "oddStatus", "state") or "")
                is_btts = ("both teams" in text or "obie druzyny" in text or "obie druzyny" in pick or "both teams" in pick)
                is_first_half = ("first half" in text or "1st half" in text or "pierwsza polowa" in text or "1 polowa" in text)
                is_yes = pick in ("yes", "tak", "obie druzyny strzela", "obie druzyny strzela gola")
                if is_btts and is_first_half and is_yes and status in ("active", "open", "opened", "1", "true"):
                    try:
                        odds = float(str(price).replace(",", "."))
                        if 1 < odds < 1000:
                            found.append(odds)
                    except ValueError:
                        pass
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, context)

    walk(records[0])
    return found[0] if len(found) == 1 else None


def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("picks", [])
                return data
        except (ValueError, OSError):
            pass
    return {"picks": []}


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID")
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def main():
    print(f"=== PREMATCH BTTS 1H {now_local().isoformat()} ===", flush=True)
    state = load_state()
    today = now_local().date().isoformat()
    sent = [p for p in state["picks"] if p.get("strategy") == "BTTS_1H_PREMATCH" and p.get("date") == today]
    if len(sent) >= MAX_DAILY_PICKS:
        print(f"Dzienny limit {MAX_DAILY_PICKS} osiągnięty.", flush=True)
        return
    fixtures = upcoming_fixtures()
    if not fixtures:
        print("Brak potwierdzonego terminarza — nie wysyłam typów.", flush=True)
        return
    events = superbet_events()
    candidates = []
    seen = {p.get("fixture_id") for p in sent}
    for fixture in fixtures:
        home, away = team_names(fixture)
        fid = fixture_id(fixture)
        if not fid or fid in seen:
            continue
        start = kickoff(fixture)
        if not start or start <= now_local() + timedelta(minutes=10):
            continue
        try:
            stats = estimate_btts_probability(home, away)
            if not stats:
                print(f"Pominięto {home} - {away}: za mało danych HT (min. {MIN_VENUE_SAMPLE} meczów na venue).", flush=True)
                continue
            event_id = match_superbet_event(home, away, events)
            if not event_id:
                print(f"Pominięto {home} - {away}: brak jednoznacznego eventu Superbet.", flush=True)
                continue
            odds = superbet_btts_1h(event_id)
            if not odds:
                print(f"Pominięto {home} - {away}: brak aktywnego kursu BTTS 1H YES.", flush=True)
                continue
            implied = 1.0 / odds
            edge = stats["probability"] - implied
            if edge < MIN_EDGE:
                print(f"Pominięto {home} - {away}: edge {edge*100:.1f} pp poniżej {MIN_EDGE*100:.0f} pp.", flush=True)
                continue
            candidates.append({
                "fixture_id": fid, "home": home, "away": away, "kickoff": start,
                "odds": odds, "stats": stats, "edge": edge
            })
        except Exception as exc:
            print(f"Błąd analizy {home} - {away}: {type(exc).__name__}: {exc}", flush=True)
    candidates.sort(key=lambda x: (x["edge"], x["stats"]["probability"]), reverse=True)
    slots = MAX_DAILY_PICKS - len(sent)
    for item in candidates[:slots]:
        # Final safety check: the kickoff must still be in the future immediately before delivery.
        if item["kickoff"] <= now_local() + timedelta(minutes=5):
            continue
        prob = item["stats"]["probability"]
        message = (
            "⚽ BTTS – PIERWSZA POŁOWA: TAK\n"
            f"{item['home']} - {item['away']}\n"
            f"Rozgrywki: {first(item, 'league', 'competition') or '—'}\n"
            f"Start: {item['kickoff'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Kurs Superbet: {item['odds']:.2f}\n"
            f"Szacowane prawdopodobieństwo: {prob*100:.1f}%\n"
            f"Prawdopodobieństwo z kursu: {100/item['odds']:.1f}%\n"
            f"Przewaga modelu: {item['edge']*100:.1f} pp\n"
            f"Próba statystyczna: {item['stats']['home_sample']} meczów u siebie / "
            f"{item['stats']['away_sample']} meczów na wyjeździe\n"
            "Model: wyniki do przerwy, forma strzelania i tracenia goli w 1. połowie.\n"
            "Powered by SportScore: https://sportscore.com/"
        )
        send_telegram(message)
        state["picks"].append({
            "strategy": "BTTS_1H_PREMATCH", "market": "BTTS_1H", "fixture_id": item["fixture_id"],
            "home": item["home"], "away": item["away"], "date": today,
            "kickoff_time": item["kickoff"].isoformat(), "odds": item["odds"],
            "probability": prob, "edge": item["edge"], "sent_at": now_local().isoformat()
        })
        print(f"WYSŁANO {item['home']} - {item['away']} | BTTS 1H | {item['odds']:.2f} | {prob*100:.1f}%", flush=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Koniec. Kandydaci: {len(candidates)}; wysłano teraz: {min(slots, len(candidates))}; dzienny limit: {MAX_DAILY_PICKS}.", flush=True)


if __name__ == "__main__":
    main()
