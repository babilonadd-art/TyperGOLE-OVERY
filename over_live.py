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


def in_send_window(minute):
    return minute is not None and any(start <= minute <= end for start, end in ALLOWED_WINDOWS)


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
    if h is None or a is None or not in_send_window(minute):
        return False

    # Require at least one actual attacking signal from the live stats payload.
    promising, metrics, signal_count = core.match_stats_look_promising(match, "FT")
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
    quote = core.extract_superbet_live_over_odds(detail, "FT", line_floor)
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
        "minute": minute, "score": f"{h}:{a}", "line": quote["line"],
        "odds": odds, "league": league, "sent_at": core.now_local().isoformat(),
        "delivery_status": "pending",
    }
    # Persist the fixture claim before Telegram delivery to prevent duplicate posts
    # if the runner is interrupted after Telegram accepted the message.
    state["sent"].append(record)
    save_live_state(state)

    url = core.superbet_goal_market_url(event.get("url"))
    message = (
        "⚽ OVER LIVE — CAŁY MECZ (FT)\n"
        f"🏆 {league}\n"
        f"{home} – {away}\n"
        f"⏱️ {minute}' | Wynik: {h}:{a}\n"
        f"🎯 Over {quote['line']:.1f} FT\n"
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
    live = core.current_live_matches()
    candidates = []
    for match in live:
        h, a, minute, _ = core.score_and_minute(match)
        home, away = core.teams(match)
        if h is None or a is None or not in_send_window(minute):
            continue
        if already_sent(state, match, home, away):
            continue
        promising, metrics, signals = core.match_stats_look_promising(match, "FT")
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
