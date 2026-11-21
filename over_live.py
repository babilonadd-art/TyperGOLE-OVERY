"""Dedicated live full-time Over tip engine.

Uses the repository's public SportScore live fixtures/statistics and the
public Superbet event feed for the actual current FT total-goals price.
Fails closed whenever score, minute, statistics, league or price is missing.
"""
import json
import os
from pathlib import Path
from datetime import datetime

import bot as core

STATE_PATH = Path("data/over_live_state.json")
MIN_ODDS = 1.60
MAX_ODDS = 8.00
ALLOWED_WINDOWS = ((26, 38), (60, 78))
MARKET = "OVER_LIVE_FT"
MAX_EVENT_DETAILS_PER_RUN = 30

# Public ESPN soccer scoreboard (no API key). This supplements SportScore
# where its live board has limited competition coverage.
ESPN_LEAGUES = (
    "eng.1", "eng.2", "eng.3", "eng.fa", "esp.1", "esp.2",
    "ger.1", "ger.2", "ita.1", "ita.2", "fra.1", "fra.2",
    "ned.1", "por.1", "usa.1", "mex.1", "bra.1", "arg.1",
    "uefa.champions", "uefa.europa", "uefa.europa.conf",
    "uefa.nations", "sco.1", "bel.1", "tur.1", "jpn.1", "aus.1",
)
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OVER-LIVE/1.0)", "Accept": "application/json"}

def espn_get(url, params=None, timeout=12):
    import requests
    response = requests.get(url, params=params, headers=ESPN_HEADERS, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}

def espn_live_matches():
    """Collect in-progress fixtures from multiple public ESPN league scoreboards."""
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    found = {}
    errors = 0
    for league in ESPN_LEAGUES:
        try:
            payload = espn_get(f"{ESPN_BASE}/{league}/scoreboard", {"dates": day})
            league_name = next((x.get("name") for x in payload.get("leagues", []) if isinstance(x, dict) and x.get("name")), league)
            for event in payload.get("events", []):
                if not isinstance(event, dict):
                    continue
                competitions = event.get("competitions") or []
                if not competitions:
                    continue
                comp = competitions[0]
                status = event.get("status") or comp.get("status") or {}
                status_type = status.get("type") or {}
                if str(status_type.get("state") or "").lower() != "in":
                    continue
                competitors = comp.get("competitors") or []
                home = next((x for x in competitors if x.get("homeAway") == "home"), {})
                away = next((x for x in competitors if x.get("homeAway") == "away"), {})
                hteam, ateam = home.get("team") or {}, away.get("team") or {}
                home_name, away_name = hteam.get("displayName"), ateam.get("displayName")
                if not home_name or not away_name:
                    continue
                clock = str(status.get("displayClock") or status_type.get("shortDetail") or "")
                import re
                mm = re.search(r"(\d{1,3})", clock)
                minute = int(mm.group(1)) if mm else None
                if minute is None or not 1 <= minute <= 130:
                    continue
                match = {
                    "id": str(event.get("id")), "slug": str(event.get("id")),
                    "home": home_name, "away": away_name,
                    "home_score": home.get("score"), "away_score": away.get("score"),
                    "minute": minute, "status": "in_progress",
                    "status_text": clock, "competition_name": league_name,
                    "_espn_league": league, "_espn_event_id": str(event.get("id")),
                }
                found[str(event.get("id"))] = match
        except Exception as exc:
            errors += 1
            print(f"ESPN {league}: {type(exc).__name__}: {exc}", flush=True)
    result = list(found.values())
    print(f"ESPN: {len(result)} meczów LIVE z {len(ESPN_LEAGUES)} lig; błędy feedów={errors}.", flush=True)
    return result

def espn_enrich_live_match(match):
    """Fetch in-play match summary and convert ESPN team stats to common metrics."""
    league, event_id = match.get("_espn_league"), match.get("_espn_event_id")
    if not league or not event_id:
        return match
    try:
        payload = espn_get(f"{ESPN_BASE}/{league}/summary", {"event": event_id})
        box = payload.get("boxscore") or {}
        teams = box.get("teams") or []
        stats = {}
        for side in teams:
            team = side.get("team") or {}
            side_name = team.get("displayName") or team.get("name")
            key = "home" if core.normalized_team_name(side_name) == core.normalized_team_name(match["home"]) else "away"
            for item in side.get("statistics") or []:
                if not isinstance(item, dict):
                    continue
                import re
                raw_name = str(item.get("name") or item.get("label") or "").lower()
                name = re.sub(r"[^a-z0-9]+", "_", raw_name).strip("_")
                value = item.get("displayValue", item.get("value"))
                try:
                    number = float(str(value).replace("%", ""))
                except (TypeError, ValueError):
                    continue
                if "shot" in name and ("target" in name or "on_goal" in name):
                    name = "shots_on_target"
                elif "shot" in name and ("total" in name or name in ("shots", "shot")):
                    name = "total_shots"
                elif "dangerous" in name and "attack" in name:
                    name = "dangerous_attacks"
                stats.setdefault(name, {})[key] = number
        if stats:
            match["statistics"] = stats
        match["_espn_summary_received"] = True
    except Exception as exc:
        print(f"ESPN summary {event_id}: {type(exc).__name__}: {exc}", flush=True)
    return match


def market_for_minute(minute):
    if minute is None:
        return None
    if 26 <= minute <= 38:
        return "HT"
    if 60 <= minute <= 78:
        return "FT"
    return None


def target_over_line(total_goals):
    # The next winning whole-match total line must require at least one more goal.
    return round(int(total_goals) + 0.5, 1)


def load_live_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("sent", [])
                return data
        except (OSError, ValueError):
            pass
    return {"sent": []}


def save_live_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def already_sent(state, match, home, away):
    identity = core.match_identifier(match) or core.match_slug(match)
    for item in state["sent"]:
        if item.get("delivery_status") not in {"sent", "pending"}:
            continue
        if identity and str(item.get("id") or "") == str(identity):
            return True
        if (core.normalized_team_name(item.get("home")) == core.normalized_team_name(home)
                and core.normalized_team_name(item.get("away")) == core.normalized_team_name(away)):
            old_date = str(item.get("date") or "")[:10]
            new_date = core.now_local().date().isoformat()
            if old_date == new_date:
                return True
    return False


def publish_candidate(state, match, superbet_events):
    home, away = core.teams(match)
    if home in ("Gospodarze", "") or away in ("Goście", ""):
        return False
    h, a, minute, status = core.score_and_minute(match)
    market = market_for_minute(minute)
    if h is None or a is None or market is None:
        return False

    # Require at least one actual attacking signal from the live stats payload.
    promising, metrics, signal_count = core.match_stats_look_promising(match, market)
    if not promising:
        return False

    league = core.fixture_competition_label(match)
    if not league or str(league).strip().casefold() in {"unknown", "unknown league", "liga nieznana"}:
        return False

    event = core.find_superbet_event(home, away, superbet_events)
    if not event:
        return False
    detail = core.superbet_event_detail(event["event_id"])
    if not detail:
        return False

    line_floor = target_over_line(h + a)
    quote = core.extract_superbet_live_over_odds(detail, market, line_floor, min_odds=MIN_ODDS)
    if not quote:
        return False
    odds = float(quote["odds"])
    if not MIN_ODDS <= odds <= MAX_ODDS:
        return False

    identity = core.match_identifier(match) or core.match_slug(match) or (
        f"{core.normalized_team_name(home)}:{core.normalized_team_name(away)}"
    )
    record = {
        "id": str(identity), "home": home, "away": away,
        "date": core.now_local().date().isoformat(),
        "minute": minute, "score": f"{h}:{a}", "market": market,
        "line": quote["line"], "odds": odds, "league": league,
        "sent_at": core.now_local().isoformat(),
        "delivery_status": "pending",
    }
    # Persist the fixture claim before Telegram delivery to prevent duplicate posts
    # if the runner is interrupted after Telegram accepted the message.
    state["sent"].append(record)
    save_live_state(state)

    url = core.superbet_goal_market_url(event.get("url"))
    market_label = "DO KOŃCA 1. POŁOWY (HT)" if market == "HT" else "DO KOŃCA MECZU (FT)"
    message = (
        f"⚽ OVER LIVE — {market_label}\n"
        f"🏆 {league}\n"
        f"{home} – {away}\n"
        f"⏱️ {minute}' | Wynik: {h}:{a}\n"
        f"🎯 Over {quote['line']:.1f} {market}\n"
        f"📈 Kurs Superbet: {odds:.2f}\n"
        f"📊 Sygnały ofensywne LIVE: {signal_count}\n"
        + (f"🔗 {url}" if url else "")
    )
    try:
        response = core.telegram(message)
        result = response.get("result", {}) if isinstance(response, dict) else {}
        record["telegram_message_id"] = result.get("message_id") if isinstance(result, dict) else None
        record["delivery_status"] = "sent"
        record["sent_at"] = core.now_local().isoformat()
        save_live_state(state)
        print(f"OVER LIVE WYSŁANY: {home} - {away}, {minute}', {h}:{a}, Over {quote['line']}, {odds:.2f}", flush=True)
        return True
    except Exception as exc:
        record["delivery_status"] = "failed"
        record["delivery_error"] = f"{type(exc).__name__}: {exc}"
        save_live_state(state)
        print(f"Telegram send failed for {home} - {away}: {exc}", flush=True)
        return False


def main():
    now = core.now_local()
    print(f"=== OVER LIVE START {now.isoformat()} ===", flush=True)
    if not core.TELEGRAM_TOKEN or not core.CHAT_ID:
        raise RuntimeError("Brak sekretów TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID")
    state = load_live_state()
    save_live_state(state)  # ensure the workflow can persist an empty first-run state
    live = core.current_live_matches()
    espn_live = espn_live_matches()
    known = {str(core.match_identifier(m) or core.match_slug(m)) for m in live}
    known_pairs = {(core.normalized_team_name(core.teams(m)[0]), core.normalized_team_name(core.teams(m)[1])) for m in live}
    for item in espn_live:
        pair = (core.normalized_team_name(item.get("home")), core.normalized_team_name(item.get("away")))
        if str(item.get("id")) not in known and pair not in known_pairs:
            live.append(item)
            known.add(str(item.get("id")))
            known_pairs.add(pair)
    candidates = []
    for match in live:
        h, a, minute, _ = core.score_and_minute(match)
        home, away = core.teams(match)
        market = market_for_minute(minute)
        if h is None or a is None or market is None:
            continue
        if already_sent(state, match, home, away):
            continue
        if match.get("_espn_event_id"):
            match = espn_enrich_live_match(match)
        promising, metrics, signals = core.match_stats_look_promising(match, market)
        if not metrics:
            # The live board often omits stats; request the match's own live detail.
            slug = core.match_slug(match)
            if slug:
                try:
                    detail = core.match_detail(slug, None)
                    if detail:
                        enriched = dict(match)
                        enriched.update({k: v for k, v in detail.items() if v not in (None, "", [], {})})
                        match = enriched
                        promising, metrics, signals = core.match_stats_look_promising(match, market)
                except Exception as exc:
                    print(f"Nie udało się pobrać statystyk LIVE dla {home} - {away}: {exc}", flush=True)
        if promising:
            candidates.append((signals, minute, match))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    print(f"OVER LIVE: {len(live)} meczów LIVE; {len(candidates)} kandydatów w oknach wysyłki.", flush=True)
    if not candidates:
        print("Brak meczu spełniającego warunki; niczego nie wysyłam.", flush=True)
        return
    events = core.superbet_prematch_event_links(now.date())
    sent = 0
    for _, _, match in candidates[:MAX_EVENT_DETAILS_PER_RUN]:
        home, away = core.teams(match)
        if already_sent(state, match, home, away):
            continue
        try:
            if publish_candidate(state, match, events):
                sent += 1
        except Exception as exc:
            print(f"Pominięto {home} - {away}: {type(exc).__name__}: {exc}", flush=True)
    save_live_state(state)
    print(f"OVER LIVE END: wysłano {sent} typów.", flush=True)


if __name__ == "__main__":
    main()
