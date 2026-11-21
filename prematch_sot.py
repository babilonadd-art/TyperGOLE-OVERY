"""Pre-match team shots-on-target scanner; fails closed on missing data."""
import json
import re
from datetime import datetime
from pathlib import Path
import bot as core

STATE_PATH = Path("data/prematch_sot_state.json")
MIN_ODDS, MAX_ODDS, MAX_DAILY_PICKS = 1.60, 8.00, 4
LINES = (1.5, 2.5, 3.5)
SAMPLE_SIZE, MIN_SAMPLE, MIN_HIT_RATE = 10, 5, 0.70
SOT_MARKERS = ("celne strzaly", "strzaly celne", "shots on target", "shots on goal")
HALF_MARKERS = ("1 pol", "pierwsza polowa", "first half", "1st half", "2nd half", "druga polowa", "half time", "ht")
EXTRA_MARKERS = ("dogryw", "extra time", "overtime", "penalty shoot", "rzuty karne")

def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                state.setdefault("sent", [])
                return state
        except (OSError, ValueError):
            pass
    return {"sent": []}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def norm_text(value):
    value = str(value or "").replace("ł", "l").replace("Ł", "L")
    value = core.unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.,+-]+", " ", value)).strip()

def recent_venue_rows(results, team, venue, folder):
    found = []
    for row in results:
        if row.get("_folder") != folder:
            continue
        side = "home" if core.team_name_similarity(team, str(row.get("home") or "")) >= 0.90 else (
            "away" if core.team_name_similarity(team, str(row.get("away") or "")) >= 0.90 else None
        )
        if side != venue:
            continue
        own = core.as_int(row.get("home_target" if side == "home" else "away_target"))
        opponent = core.as_int(row.get("away_target" if side == "home" else "home_target"))
        if own is None or opponent is None:
            continue
        date = str(row.get("date") or "")
        if date:
            found.append((date, own, opponent, row))
    found.sort(key=lambda item: item[0], reverse=True)
    return found[:SAMPLE_SIZE]

def choose_stats_folder(results, event):
    home, away = event["home"], event["away"]
    league = norm_text(event.get("competition_name") or "")
    folders = sorted({str(row.get("_folder") or "") for row in results if row.get("_folder")})
    candidates = []
    for folder in folders:
        hr = recent_venue_rows(results, home, "home", folder)
        ar = recent_venue_rows(results, away, "away", folder)
        if min(len(hr), len(ar)) < MIN_SAMPLE:
            continue
        league_name = next((str(r.get("_league") or "") for r in results if r.get("_folder") == folder), "")
        similarity = core.team_name_similarity(league, norm_text(league_name)) if league else 0.0
        candidates.append((similarity, min(len(hr), len(ar)), folder, league_name, hr, ar))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if league and candidates[0][0] < 0.55 and len(candidates) > 1:
        return None
    if len(candidates) > 1 and abs(candidates[0][0] - candidates[1][0]) < 0.04 and candidates[0][1] == candidates[1][1]:
        return None
    return candidates[0]

def analyze_team(home_rows, away_rows):
    home_for, home_allowed = [x[1] for x in home_rows], [x[2] for x in home_rows]
    away_for, away_allowed = [x[1] for x in away_rows], [x[2] for x in away_rows]
    picks = []
    for side, own, allowed in (("home", home_for, [x[2] for x in away_rows]), ("away", away_for, [x[2] for x in home_rows])):
        if len(own) < MIN_SAMPLE or len(allowed) < MIN_SAMPLE:
            continue
        expected = 0.60 * (sum(own) / len(own)) + 0.40 * (sum(allowed) / len(allowed))
        rates = {line: sum(v > line for v in own) / len(own) for line in LINES}
        valid = [line for line in LINES if rates[line] >= MIN_HIT_RATE and expected >= line + 0.25]
        if valid:
            line = max(valid)
            picks.append({"team_side": side, "line": line, "expected": expected,
                          "hit_rate": rates[line], "sample": len(own),
                          "avg_for": sum(own) / len(own), "avg_allowed": sum(allowed) / len(allowed)})
    return picks

def collect_price_rows(node, inherited=""):
    rows = []
    if isinstance(node, list):
        for child in node:
            rows.extend(collect_price_rows(child, inherited))
        return rows
    if not isinstance(node, dict):
        return rows
    keys = ("marketName", "market_name", "marketGroupName", "market_group_name",
            "marketTitle", "market_title", "market", "marketGroup", "market_group", "title", "name")
    local = " ".join(str(node[k]) for k in keys if node.get(k) not in (None, "") and not isinstance(node.get(k), (dict, list)))
    context = " ".join(x for x in (inherited, local) if x).strip()
    has_price = any(node.get(k) not in (None, "") for k in ("price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec"))
    has_sel = any(node.get(k) not in (None, "") for k in ("name", "selectionName", "selection_name", "oddName", "outcomeName", "label", "displayName"))
    if has_price and has_sel:
        row = dict(node)
        row["_market_context"] = context
        rows.append(row)
    for value in node.values():
        if isinstance(value, (dict, list)):
            rows.extend(collect_price_rows(value, context))
    return rows

def parse_superbet_sot_quotes(detail, event):
    quotes = []
    for row in collect_price_rows(detail):
        market = norm_text(row.get("_market_context") or "")
        if not any(marker in market for marker in SOT_MARKERS):
            continue
        if any(marker in market for marker in HALF_MARKERS + EXTRA_MARKERS):
            continue
        status = str(core.first(row, "status", "oddStatus", "state", default="")).strip().lower()
        if status not in {"active", "opened", "open", "1", "true"}:
            continue
        selection = str(core.first(row, "name", "selectionName", "selection_name", "oddName",
                                   "outcomeName", "label", "displayName", default="")).strip()
        combined = norm_text(market + " " + selection)
        over = re.search(r"\b(?:powyzej|over)\s*(\d+(?:[.,]\d+)?)\b", combined)
        if not over:
            continue
        line = float(over.group(1).replace(",", "."))
        if line not in LINES:
            continue
        side = None
        for candidate, team in (("home", event["home"]), ("away", event["away"])):
            if norm_text(team) and norm_text(team) in combined:
                side = candidate
                break
        if side is None:
            if re.search(r"\b(?:gospodarze|home team|home)\b", combined):
                side = "home"
            elif re.search(r"\b(?:goscie|away team|away)\b", combined):
                side = "away"
        if side is None:
            continue
        prices, bad = [], False
        for key in ("price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec"):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                price = float(str(raw).replace(",", "."))
            except (TypeError, ValueError):
                bad = True
                break
            if not 1.0 < price <= 1000:
                bad = True
                break
            prices.append(price)
        if bad or not prices or max(prices) - min(prices) > 0.005:
            continue
        odds = prices[0]
        if MIN_ODDS <= odds <= MAX_ODDS:
            quotes.append({"team_side": side, "line": line, "odds": odds,
                           "market": market, "selection": selection})
    unique = {}
    for quote in quotes:
        unique.setdefault((quote["team_side"], quote["line"]), set()).add(quote["odds"])
    return [q for q in quotes if len(unique[(q["team_side"], q["line"])]) == 1]

def already_sent(state, day, event_id, side):
    key = f"{day}:{event_id}:{side}"
    return any(item.get("key") == key and item.get("delivery_status") in {"pending", "sent"}
               for item in state["sent"])

def publish(state, event, analysis, quote, league):
    side = quote["team_side"]
    stats = next((x for x in analysis if x["team_side"] == side and x["line"] == quote["line"]), None)
    if not stats:
        return False
    kickoff = core.kickoff_datetime({"kickoff_time": event.get("time")})
    if not kickoff or kickoff <= core.now_local():
        return False
    day = kickoff.astimezone(core.TZ).date().isoformat()
    event_id = str(event.get("event_id") or "")
    if not event_id or already_sent(state, day, event_id, side):
        return False
    daily = [x for x in state["sent"] if x.get("date") == day and x.get("delivery_status") == "sent"]
    if len(daily) >= MAX_DAILY_PICKS:
        return False
    url = str(event.get("url") or "")
    if not url.startswith("https://superbet.pl/"):
        return False
    team = event["home"] if side == "home" else event["away"]
    message = (
        "🎯 CELNE STRZAŁY DRUŻYNY — FT\n"
        f"🏆 {league}\n⚽ {event['home']} – {event['away']}\n"
        f"📌 Typ: {team} — over {quote['line']:.1f} celnych strzałów (FT)\n"
        f"📈 Kurs Superbet: {quote['odds']:.2f}\n"
        f"📊 Ostatnie {stats['sample']} meczów: trafione {round(stats['hit_rate'] * stats['sample'])}/{stats['sample']}; "
        f"średnia własna {stats['avg_for']:.2f}; średnia dopuszczona przez rywali {stats['avg_allowed']:.2f}\n"
        f"🕒 {kickoff.astimezone(core.TZ).strftime('%d.%m.%Y %H:%M')} (Polska)\n"
        f"🔗 Mecz Superbet: {url}\nŹródło statystyk: Open Matchday."
    )
    record = {"key": f"{day}:{event_id}:{side}", "date": day, "event_id": event_id,
              "home": event["home"], "away": event["away"], "team": team,
              "team_side": side, "line": quote["line"], "odds": quote["odds"],
              "delivery_status": "pending", "sent_at": core.now_local().isoformat()}
    state["sent"].append(record)
    save_state(state)
    try:
        result = core.telegram(message)
        payload = result.get("result", {}) if isinstance(result, dict) else {}
        record["telegram_message_id"] = payload.get("message_id") if isinstance(payload, dict) else None
        record["delivery_status"] = "sent"
        record["sent_at"] = core.now_local().isoformat()
        save_state(state)
        print(f"SOT WYSŁANY: {team} over {quote['line']}, kurs {quote['odds']:.2f}", flush=True)
        return True
    except Exception as exc:
        record["delivery_status"] = "failed"
        record["delivery_error"] = f"{type(exc).__name__}: {exc}"
        save_state(state)
        print(f"SOT Telegram error: {type(exc).__name__}: {exc}", flush=True)
        return False

def main():
    now = core.now_local()
    print(f"=== PREMATCH SOT START {now.isoformat()} ===", flush=True)
    if not core.TELEGRAM_TOKEN or not core.CHAT_ID:
        raise RuntimeError("Brak sekretów TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID")
    state = load_state()
    results = core.open_matchday_data().get("results", [])
    print(f"Open Matchday: {len(results)} wyników; statystyki celnych strzałów.", flush=True)
    events = core.superbet_prematch_event_links(target_date=now.date())
    future = []
    for event in events:
        kickoff = core.kickoff_datetime({"kickoff_time": event.get("time")})
        if kickoff and kickoff > now:
            future.append(event)
    print(f"Superbet: {len(future)} przyszłych meczów na dziś.", flush=True)
    sent = examined = no_stats = no_market = 0
    for event in future:
        if sent >= MAX_DAILY_PICKS:
            break
        examined += 1
        selected = choose_stats_folder(results, event)
        if not selected:
            no_stats += 1
            continue
        _, _, folder, league, home_rows, away_rows = selected
        analysis = analyze_team(home_rows, away_rows)
        if not analysis:
            no_stats += 1
            continue
        try:
            detail = core.superbet_event_detail(event["event_id"])
        except Exception as exc:
            print(f"Superbet detail {event['home']} - {event['away']}: {type(exc).__name__}: {exc}", flush=True)
            no_market += 1
            continue
        quotes = parse_superbet_sot_quotes(detail, event)
        if not quotes:
            no_market += 1
            continue
        for quote in quotes:
            if sent >= MAX_DAILY_PICKS:
                break
            if publish(state, event, analysis, quote, league):
                sent += 1
    print(f"PREMATCH SOT: sprawdzono={examined}, bez_statystyk={no_stats}, bez_potwierdzonego_rynku={no_market}, wysłano={sent}.", flush=True)

if __name__ == "__main__":
    main()
