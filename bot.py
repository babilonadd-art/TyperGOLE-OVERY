from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import difflib
import io
from html.parser import HTMLParser
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Warsaw")
API = "https://sportscore.com"  # legacy fallback for older pick types
OPEN_MATCHDAY_ZIP = "https://github.com/oladeeayo/open-matchday/archive/refs/heads/master.zip"
OPEN_MATCHDAY_SEASON = "2627"
_OPEN_MATCHDAY_CACHE = None
_SPORTSCORE_SETTLEMENT_CACHE = {}
MOVIODDS_API = "https://moviodds.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_PATH = Path("data/state.json")
STAKE = 10.0
BET_TAX_RATE = 0.12
# Maksymalnie 10 typów BTTS FT dziennie.

def calculate_bet_result(stake, odds):
    """Calculate Polish 12% stake tax, payout and net profit."""
    stake = float(stake)
    odds = float(odds)
    tax = round(stake * BET_TAX_RATE, 2)
    effective_stake = round(stake - tax, 2)
    payout = round(effective_stake * odds, 2)
    profit = round(payout - stake, 2)
    return {
        "stake": stake,
        "tax": tax,
        "effective_stake": effective_stake,
        "payout": payout,
        "profit": profit,
    }
HEADERS = {"User-Agent": "TyperGOLE-OVERY/1.0", "Accept": "application/json"}
ATTRIBUTION = "Dane meczowe: Open Matchday (https://github.com/oladeeayo/open-matchday)"


def now_local():
    return datetime.now(TZ)


# Limit 8 typów BTTS FT na każdy dzień kalendarzowy (Europe/Warsaw).
# Licznik resetuje się automatycznie po zmianie daty; historia pozostaje zachowana.
MAX_DAILY_BTTS_FT_PICKS = 10


def daily_sent_match_count(state, day):
    """Count distinct BTTS FT fixtures that count toward the daily cap."""
    fixtures = set()
    for pick in state.get("picks", []):
        if pick.get("date") != day or pick.get("delivery_status", "sent") != "sent":
            continue
        if pick.get("market") != OVER_15_HT_MARKET or pick.get("counts_toward_daily_limit") is False:
            continue
        fixture_id = pick.get("id") or pick.get("slug") or pick.get("key")
        if fixture_id:
            fixtures.add(str(fixture_id))
    return len(fixtures)


def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("picks", [])
                data.setdefault("reports_sent", [])
                data.setdefault("duplicate_guard", [])
                return data
        except (ValueError, OSError):
            pass
    return {"picks": [], "reports_sent": [], "duplicate_guard": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")



def open_matchday_data(force_refresh=False):
    """Load public Open Matchday CSV data; no API key or signup required."""
    global _OPEN_MATCHDAY_CACHE
    if _OPEN_MATCHDAY_CACHE is not None and not force_refresh:
        return _OPEN_MATCHDAY_CACHE
    response = requests.get(OPEN_MATCHDAY_ZIP, headers=HEADERS, timeout=90)
    response.raise_for_status()
    fixtures, results, league_names = [], [], {}
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        try:
            index_text = archive.read("open-matchday-master/LEAGUE_INDEX.md").decode("utf-8-sig")
            for line in index_text.splitlines():
                match = re.match(r"\| `([^ `]+)` \| ([^|]+) \|", line)
                if match:
                    league_names[match.group(1)] = match.group(2).strip()
        except KeyError:
            pass
        for filename in archive.namelist():
            if not filename.endswith((f"fixtures_{OPEN_MATCHDAY_SEASON}.csv",
                                      f"results_{OPEN_MATCHDAY_SEASON}.csv")):
                continue
            folder = filename.split("/")[-2] if "/" in filename else ""
            league = league_names.get(folder, folder.replace("_", " ").strip())
            raw = archive.read(filename).decode("utf-8-sig")
            target = fixtures if "/fixtures_" in filename else results
            for row in csv.DictReader(io.StringIO(raw)):
                row["_league"] = league
                row["_folder"] = folder
                target.append(row)
    if not fixtures and not results:
        raise RuntimeError("Open Matchday ZIP nie zawiera CSV sezonu 2627")
    _OPEN_MATCHDAY_CACHE = {"fixtures": fixtures, "results": results}
    print(f"Open Matchday: {len(fixtures)} terminarzowych, {len(results)} wyników; CSV 2627, bez klucza API.", flush=True)
    return _OPEN_MATCHDAY_CACHE


def open_matchday_kickoff(row):
    """CSV date/time uses fixed CET (UTC+1) convention."""
    day, clock = str(row.get("date") or "").strip(), str(row.get("time") or "").strip()
    try:
        if not day:
            return None
        naive = datetime.fromisoformat(f"{day}T{clock or '12:00'}")
        return naive.replace(tzinfo=timezone(timedelta(hours=1))).astimezone(TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def open_matchday_fixture(row):
    home, away = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
    kickoff = open_matchday_kickoff(row)
    if not home or not away or not kickoff:
        return None
    def slugify(value):
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value)).strip("-")
    slug = f"{slugify(home)}-vs-{slugify(away)}"
    identity = f"openmatchday:{row.get('_folder','')}:{row.get('date')}:{slug}"
    return {
        "id": identity, "slug": slug, "_bot_slug": slug, "home": home, "away": away,
        "time": kickoff.isoformat(), "date": row.get("date"),
        "competition_name": row.get("_league", "Liga nieznana"),
        "_bot_kickoff": kickoff.isoformat(), "_source": "Open Matchday",
    }


def open_matchday_result_for_pick(pick):
    """Find a completed result by both teams, allowing timezone/date drift in public CSV."""
    try:
        expected = datetime.fromisoformat(
            str(pick.get("kickoff_time") or pick.get("date")).replace("Z", "+00:00")
        )
        if expected.tzinfo is None:
            expected = expected.replace(tzinfo=TZ)
        expected = expected.astimezone(TZ)
    except (TypeError, ValueError):
        return None

    candidates = []
    for row in open_matchday_data()["results"]:
        home = str(row.get("home") or "").strip()
        away = str(row.get("away") or "").strip()
        if not home or not away:
            continue
        if not fixture_categories_compatible(
            home, away, row.get("_league", ""),
            pick.get("home"), pick.get("away"),
            pick.get("league") or pick.get("competition") or "",
        ):
            continue

        direct = (
            team_name_similarity(home, pick.get("home")) >= 0.88
            and team_name_similarity(away, pick.get("away")) >= 0.88
        )
        reverse = (
            team_name_similarity(home, pick.get("away")) >= 0.88
            and team_name_similarity(away, pick.get("home")) >= 0.88
        )
        if not (direct or reverse):
            continue

        kickoff = open_matchday_kickoff(row)
        if not kickoff:
            continue
        kickoff = kickoff.astimezone(TZ)

        # Public football feeds sometimes store the UTC match date while the
        # pick stores Warsaw-local date. Accept an adjacent calendar date only
        # when both teams match and the kickoff is still close to the pick.
        if abs((kickoff.date() - expected.date()).days) > 1:
            continue
        time_delta = abs((kickoff - expected).total_seconds())
        if time_delta > 8 * 60 * 60:
            continue

        fh, fa = as_int(row.get("ft_home")), as_int(row.get("ft_away"))
        if fh is None or fa is None:
            continue
        candidates.append((time_delta, {
            "home": home, "away": away, "time": kickoff.isoformat(),
            "status": "finished", "home_score": fh, "away_score": fa,
            "ft_home": fh, "ft_away": fa,
            "ht_home": as_int(row.get("ht_home")),
            "ht_away": as_int(row.get("ht_away")),
            "half_time_score": {
                "home": as_int(row.get("ht_home")),
                "away": as_int(row.get("ht_away")),
            },
            "competition_name": row.get("_league", "Liga nieznana"),
            "_source": "Open Matchday",
        }))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    # If the two closest records are effectively tied, fail closed rather than
    # risk settling against a different meeting of the same clubs.
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 30 * 60:
        print(
            f"Open Matchday: niejednoznaczne wyniki dla {pick.get('home')} - "
            f"{pick.get('away')}; nie rozliczam automatycznie.",
            flush=True,
        )
        return None
    return candidates[0][1]


def api_get(path, params=None, timeout=20):
    response = requests.get(
        f"{API}/{path.lstrip('/')}",
        params=params,
        headers=HEADERS,
        timeout=timeout,
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
    """Normalize names without erasing women's or U16-U23 identity."""
    raw = str(value or "").strip()
    is_womens = is_womens_team_name(raw) if "is_womens_team_name" in globals() else bool(
        re.search(r"\(\s*[kw]\s*\)|\b(?:women|woman|female|feminine|femenil|feminino|femenino)\b", raw, re.I)
    )
    age_groups = sorted({
        next(group for group in match.groups() if group)
        for match in re.finditer(
            r"\b(?:u\s*[- ]?\s*(1[6-9]|2[0-3])|under\s*(1[6-9]|2[0-3]))\b",
            raw, re.I
        )
    })
    raw = re.sub(r"\(\s*[kw]\s*\)", " ", raw, flags=re.I)
    raw = re.sub(r"\b(?:u\s*[- ]?\s*(1[6-9]|2[0-3])|under\s*(1[6-9]|2[0-3]))\b", " ", raw, flags=re.I)
    value = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().lower()
    tokens = re.findall(r"[a-z0-9]+", value)
    ignored = {"fc", "cf", "sc", "afc", "club", "the", "football", "women", "woman", "female", "feminine", "femenil", "w", "k", "feminino", "femenino"}
    tokens = [token for token in tokens if token not in ignored]
    tokens.extend(f"u{age}" for age in age_groups)
    if is_womens:
        tokens.append("women")
    return " ".join(tokens)


def team_name_aliases(value):
    """Safe aliases; final event selection still requires one unique oriented fixture."""
    raw = str(value or "").strip()
    base = normalized_team_name(raw)
    aliases = {base} if base else set()

    # Regional suffixes (Brazilian state abbreviations) are optional.
    shortened_raw = re.sub(r"(?:\s+-\s+|\s+)[A-Z]{2,3}\s*$", "", raw)
    shortened = normalized_team_name(shortened_raw)
    if shortened and shortened != base:
        aliases.add(shortened)

    # Some feeds abbreviate International as Inter, and vice versa.
    for name in list(aliases):
        if re.search(r"\binternational\b", name):
            aliases.add(re.sub(r"\binternational\b", "inter", name))
        if re.search(r"\binter\b", name):
            aliases.add(re.sub(r"\binter\b", "international", name))

    # Known provider naming variants. These remain aliases only; both teams
    # must still match in the correct home/away order.
    for name in list(aliases):
        if name == "san lorenzo almagro":
            aliases.add("san lorenzo")
        elif name == "san lorenzo":
            aliases.add("san lorenzo almagro")
        if name == "operario ferroviario pr":
            aliases.add("operario pr")
        elif name == "operario pr":
            aliases.add("operario ferroviario pr")

    # Remove common club/legal prefixes without changing the core name.
    prefix_patterns = (
        r"^(?:club\s+social\s+y\s+deportivo|club\s+social\s+deportivo|club\s+atletico|club\s+deportivo)\s+",
        r"^(?:csd|cs|ca|cd|uanl)\s+",
        r"^deportes\s+",
    )
    pending = list(aliases)
    for _ in range(4):
        additions = set()
        for name in pending:
            for pattern in prefix_patterns:
                stripped = re.sub(pattern, "", name).strip()
                if stripped and stripped != name:
                    additions.add(stripped)
        additions -= aliases
        if not additions:
            break
        aliases.update(additions)
        pending = list(additions)
    return aliases

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


class SuperbetEventParser(HTMLParser):
    """Collect official Superbet event links and their visible anchor text."""
    def __init__(self):
        super().__init__()
        self.events = []
        self.current_href = None
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.current_href = dict(attrs).get("href")
            self.current_text = []

    def handle_data(self, data):
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current_href:
            self.events.append({
                "url": self.current_href,
                "text": " ".join(self.current_text).strip(),
            })
            self.current_href = None
            self.current_text = []


def superbet_event_kickoff(event):
    """Return the event's exact Warsaw-local kickoff from Superbet metadata."""
    fixture = event.get("fixture") or {}
    millis = first(fixture, "unix_date_millis", default=first(event, "unix_date_millis"))
    if millis not in (None, ""):
        try:
            return datetime.fromtimestamp(float(millis) / 1000, timezone.utc).astimezone(TZ)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    raw = first(
        fixture, "utc_date", "event_date", "eventDate", "start_time",
        "startTime", "start_date", "startDate",
        default=first(event, "utc_date", "event_date", "eventDate", "start_time", "startTime"),
    )
    if raw in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def superbet_event_local_date(event):
    """Return the event's Warsaw-local calendar date, or None when missing."""
    kickoff = superbet_event_kickoff(event)
    return kickoff.date() if kickoff else None


# Pula prawdziwych fotografii piłkarskich z Wikimedia Commons.
# Nie generujemy ilustracji: jeśli pobranie zdjęcia się nie powiedzie, wysyłka
# grafiki jest pomijana (telegram_btts_ft zgłosi błąd zamiast wysłać rysunek).
BTTS_IMAGE_POOL = tuple(range(50))
_BTTS_PHOTO_CACHE = {}

def make_btts_graphic(image_index):
    """Fetch a real editorial-style football photograph; never draw synthetic art."""
    from PIL import Image
    import random

    index = int(image_index) % len(BTTS_IMAGE_POOL)
    if index in _BTTS_PHOTO_CACHE:
        cached = _BTTS_PHOTO_CACHE[index]
        cached.seek(0)
        return cached

    api = "https://commons.wikimedia.org/w/api.php"
    headers = {
        **HEADERS,
        "User-Agent": "TyperGOLE-OVERY/1.0 (football photo preview; contact: bot operator)",
    }
    search_terms = (
        "association football match player action",
        "footballer celebrating goal match",
        "professional football match action",
        "soccer player kicking ball stadium",
    )
    rng = random.Random(index + 20261107)
    query = search_terms[index % len(search_terms)]
    response = requests.get(
        api,
        params={
            "action": "query", "format": "json",
            "generator": "search", "gsrsearch": query,
            "gsrnamespace": 6, "gsrlimit": 50,
            "prop": "imageinfo", "iiprop": "url|mime",
            "iiurlwidth": 1400,
        },
        headers=headers, timeout=25,
    )
    response.raise_for_status()
    pages = list((response.json().get("query") or {}).get("pages", {}).values())
    candidates = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        image_url = info.get("thumburl") or info.get("url")
        mime = str(info.get("mime") or "")
        title = str(page.get("title") or "").lower()
        football_terms = ("football", "soccer", "futbol", "match", "stadium", "player", "goal", "league", "cup", "fc_", "f.c.")
        if (image_url and mime.startswith("image/")
                and any(word in title for word in football_terms)
                and not any(word in title for word in ("logo", "flag", "emblem", "icon", "diagram", "kit"))):
            candidates.append(image_url)
    if not candidates:
        raise RuntimeError("Wikimedia Commons nie zwróciło prawdziwych zdjęć piłkarskich")

    # Deterministic rotation by cursor; skip inaccessible files until one works.
    offset = (index * 7) % len(candidates)
    ordered = candidates[offset:] + candidates[:offset]
    last_error = None
    for image_url in ordered[:12]:
        try:
            photo_response = requests.get(image_url, headers=headers, timeout=30)
            photo_response.raise_for_status()
            if not photo_response.headers.get("Content-Type", "").lower().startswith("image/"):
                continue
            photo = Image.open(io.BytesIO(photo_response.content)).convert("RGB")
            if photo.width < 500 or photo.height < 300:
                continue
            photo.thumbnail((1400, 900), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            photo.save(output, format="JPEG", quality=90, optimize=True)
            output.seek(0)
            _BTTS_PHOTO_CACHE[index] = output
            return output
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Nie udało się pobrać prawdziwego zdjęcia piłkarskiego: {last_error}")

def telegram_btts_ft(message, image_index=None):
    """Send a real football photograph above the BTTS pick caption."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    payload = {"chat_id": CHAT_ID, "caption": message}
    if image_index is not None:
        try:
            photo = make_btts_graphic(image_index)
            response = requests.post(
                url, data=payload,
                files={"photo": (f"btts-photo-{int(image_index):02d}.jpg", photo, "image/jpeg")},
                timeout=45,
            )
            if response.status_code == 429:
                try:
                    retry_after = response.json().get("parameters", {}).get("retry_after", 300)
                except Exception:
                    retry_after = 300
                raise RuntimeError(f"Telegram rate limit 429; retry_after={retry_after}s")
            response.raise_for_status()
            result = response.json()
            if result.get("ok"):
                return result
            raise RuntimeError(f"Telegram sendPhoto error: {result}")
        except Exception as exc:
            if "429" in str(exc):
                raise
            print(f"Grafika piłkarska niedostępna — wysyłam typ bez zdjęcia: {exc}", flush=True)
    return telegram(message)


def telegram_edit_btts_pick(message, message_id):
    """Edit the original BTTS post when correcting its daily pick number."""
    if not TELEGRAM_TOKEN or not CHAT_ID or not message_id:
        raise RuntimeError("Brak danych do edycji oryginalnego typu BTTS.")
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
    common = {"chat_id": CHAT_ID, "message_id": int(message_id)}
    caption_response = requests.post(
        base + "editMessageCaption",
        json={**common, "caption": message},
        timeout=20,
    )
    try:
        caption_response.raise_for_status()
        payload = caption_response.json()
        if payload.get("ok"):
            return payload
        caption_error = payload
    except Exception as exc:
        caption_error = exc
    # Text-only fallback is needed when the original post was sent without a photo.
    text_response = requests.post(
        base + "editMessageText",
        json={**common, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    text_response.raise_for_status()
    payload = text_response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram edit failed: caption={caption_error}; text={payload}")
    return payload


def format_btts_ft_pick_message(pick, pick_number):
    """Create a short, user-friendly BTTS FT pick caption."""
    kickoff = kickoff_datetime({"kickoff_time": pick.get("kickoff_time")}) or now_local()
    analysis = pick.get("analysis") or {}
    metrics = analysis.get("metrics") or {}
    odds = float(pick.get("odds") or 0)
    stake = float(pick.get("stake") or STAKE)
    payout = calculate_bet_result(stake, odds)
    home = pick.get("home") or "Gospodarze"
    away = pick.get("away") or "Goście"
    league = str(pick.get("league") or pick.get("competition") or "").strip()
    if not league or league.casefold() in {"unknown", "unknown league", "liga nieznana"}:
        raise ValueError(
            f"Nie wysyłam BTTS bez potwierdzonej nazwy ligi: {home} - {away}"
        )

    probability = metrics.get("model_probability")
    if probability is None:
        probability = analysis.get("probability")
    try:
        probability_text = f"{float(probability):.0%}" if probability is not None else "—"
    except (TypeError, ValueError):
        probability_text = "—"

    market_only = analysis.get("method") == "market_only_no_open_matchday_history"
    probability_line = (
        f"📊 Prawdopodobieństwo wynikające z kursu BTTS: {probability_text} (bez historii drużyn)\n"
        if market_only else
        f"📊 Szacowana szansa BTTS do końca meczu: {probability_text}\n"
    )
    confidence_line = (
        "⚠️ Analiza awaryjna — brak historii w Open Matchday; niższa pewność, decyzja oparta na rynku.\n"
        if market_only else ""
    )
    message = (
        f"🎯 𝗧𝗬𝗣 𝗡𝗥. {pick_number} — BTTS TAK (FT)\n\n"
        f"⚽ {home} – {away}\n"
        f"📅 {kickoff.strftime('%d.%m.%Y')} | 🕒 {kickoff.strftime('%H:%M')} (Polska)\n"
        + f"🏆 Liga: {league}\n"
        + probability_line
        + confidence_line
        + f"💰 Kurs: {odds:.2f}\n"
        + f"💵 Stawka: {stake:.2f} zł | Możliwa wypłata po podatku: {payout['payout']:.2f} zł\n"
        + f"🔗 Mecz: {pick.get('bet_url') or ''}\n\n"
        + "Typ dotyczy obu drużyn ze strzelonym golem po 90 minutach + doliczony czas. Bez dogrywki."
    )
    return message


def btts_ft_sent_picks_on_day(state, day):
    """Return BTTS FT picks delivered after the most recent daily-cap reset."""
    sent = []
    reset_at = state.get("btts_daily_limit_reset_at")
    reset_stamp = None
    if reset_at:
        try:
            reset_stamp = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00"))
            if reset_stamp.tzinfo is None:
                reset_stamp = reset_stamp.replace(tzinfo=TZ)
        except (TypeError, ValueError):
            reset_stamp = None
    for pick in state.get("picks", []):
        if pick.get("market") != BTTS_FT_MARKET or pick.get("delivery_status") != "sent":
            continue
        sent_at = pick.get("sent_at")
        if not sent_at:
            continue
        try:
            stamp = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=TZ)
            stamp = stamp.astimezone(TZ)
            if stamp.date().isoformat() != str(day):
                continue
            if reset_stamp is not None and stamp <= reset_stamp.astimezone(TZ):
                continue
            sent.append(pick)
        except (TypeError, ValueError):
            continue
    sent.sort(key=lambda item: str(item.get("sent_at") or ""))
    return sent

def superbet_prematch_event_links(target_date=None):
    """Get pre-match football event links for one Warsaw-local calendar date."""
    found = {}
    base = "https://production-superbet-offer-pl.freetls.fastly.net"
    local_now = now_local()
    target_day = target_date or local_now.date()
    if isinstance(target_day, str):
        target_day = datetime.fromisoformat(target_day).date()
    local_start = datetime(target_day.year, target_day.month, target_day.day, tzinfo=TZ)
    local_end = local_start + timedelta(days=1)
    start = local_start.astimezone(timezone.utc)
    end = local_end.astimezone(timezone.utc)
    try:
        response = requests.get(
            f"{base}/v3/pl-PL/events",
            params={
                "startDate": start.isoformat().replace("+00:00", "Z"),
                "endDate": end.isoformat().replace("+00:00", "Z"),
            },
            headers={
                **HEADERS, "Origin": "https://superbet.pl",
                "Referer": "https://superbet.pl/",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        api_events = payload.get("events", []) if isinstance(payload, dict) else []
        rejected_date = 0
        rejected_sport = 0
        for event in api_events:
            if not isinstance(event, dict):
                continue
            fixture = event.get("fixture") or {}
            event_date = superbet_event_local_date(event)
            if event_date != target_day:
                rejected_date += 1
                continue
            sport_id = first(fixture, "sport_id", "sportId", default=first(event, "sport_id", "sportId"))
            if sport_id not in (None, "") and str(sport_id) != "5":
                rejected_sport += 1
                continue
            name = str(first(fixture, "event_name", "eventName", default=""))
            parts = name.split("·", 1)
            event_id = first(event, "event_id", "id", default=first(fixture, "event_id", "eventId"))
            if len(parts) != 2 or not event_id:
                continue
            home, away = (part.strip() for part in parts)
            href = first(fixture, "url", "event_url", "eventUrl", default=first(event, "url", "event_url"))
            if href:
                href = str(href)
                full_url = href if href.startswith("http") else f"https://superbet.pl{href}"
            else:
                def slug_part(value):
                    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
                    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value)).strip("-")
                slug = f"{slug_part(home)}-vs-{slug_part(away)}-{event_id}"
                full_url = f"https://superbet.pl/kursy/pilka-nozna/{slug}"
            kickoff = superbet_event_kickoff(event)
            found[full_url] = {
                "url": full_url, "text": name, "event_id": event_id,
                "home": home, "away": away,
                "time": kickoff.isoformat() if kickoff else None,
                # Preserve competition metadata from the original Superbet event.
                # It was previously discarded here, causing every later pick to
                # fall back to "Liga nieznana".
                "competition_name": extract_superbet_competition_name(event) or "",
            }
        print(
            f"Superbet API: {len(api_events)} rekordów pobranych; "
            f"odrzucono {rejected_date} spoza dzisiejszej daty/brak daty i "
            f"{rejected_sport} spoza piłki nożnej; "
            f"pozostało {len(found)} meczów piłkarskich na {target_day.isoformat()}.",
            flush=True,
        )
    except Exception as exc:
        print(f"Superbet API: błąd pobierania wydarzeń: {type(exc).__name__}: {exc}", flush=True)

    # No HTML fallback: it can contain several days of the bookmaker's offer.
    # If the date-scoped API fails, do not scan an unbounded board.
    if not found:
        print("Superbet: brak potwierdzonych wydarzeń na dzisiejszą datę; pomijam ofertę HTML.", flush=True)

    events = list(found.values())
    print(f"Superbet: znaleziono {len(events)} linków do wydarzeń", flush=True)
    return events




def update_existing_btts_superbet_links(state):
    """Replace Bet365 links in already-sent, upcoming BTTS FT Telegram photo captions."""
    now = now_local()
    pending = []
    for pick in state.get("picks", []):
        if pick.get("market") != BTTS_FT_MARKET:
            continue
        if pick.get("delivery_status", "sent") != "sent" or pick.get("settled"):
            continue
        if not pick.get("telegram_message_id"):
            continue
        if "superbet.pl/" in str(pick.get("bet_url") or "") and pick.get("link_caption_version") == 2:
            continue
        kickoff = kickoff_datetime(pick)
        if not kickoff or kickoff <= now:
            continue
        day = str(pick.get("date") or kickoff.date().isoformat())
        pending.append((day, pick))

    if not pending:
        print("Superbet link updater: brak przyszłych typów do poprawienia.", flush=True)
        return

    by_day = {}
    for day, pick in pending:
        by_day.setdefault(day, []).append(pick)

    updated = 0
    for day, day_picks in sorted(by_day.items()):
        events = superbet_prematch_event_links(target_date=day)
        for pick in day_picks:
            matches = []
            for event in events:
                home_score = team_name_similarity(pick.get("home", ""), event.get("home", ""))
                away_score = team_name_similarity(pick.get("away", ""), event.get("away", ""))
                if home_score >= 0.84 and away_score >= 0.84:
                    url = superbet_btts_full_time_url(event.get("url"))
                    if url and url.startswith("https://superbet.pl/kursy/pilka-nozna/"):
                        matches.append((min(home_score, away_score), url))
            matches.sort(key=lambda item: item[0], reverse=True)
            if not matches or (len(matches) > 1 and matches[0][0] - matches[1][0] < 0.04):
                print(f"Superbet link updater: brak jednoznacznego meczu dla {pick.get('home')} - {pick.get('away')} ({day}); pomijam.", flush=True)
                continue
            new_url = matches[0][1]
            try:
                kickoff = kickoff_datetime(pick)
                kickoff = kickoff.astimezone(TZ) if kickoff.tzinfo else kickoff.replace(tzinfo=TZ)
                odds = float(pick.get("odds"))
                stake = float(pick.get("stake", STAKE) or STAKE)
                payout = calculate_bet_result(stake, odds)
                caption = (
                    f"📅 DATA: {kickoff.strftime('%d.%m.%Y')}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"🕒 GODZINA: {kickoff.strftime('%H:%M')} — CZAS POLSKI\n"
                    f"🎯 BTTS TAK — CAŁY MECZ (FT, 90 MIN + DOLICZONY CZAS)\n"
                    f"⚽ {pick.get('home')} – {pick.get('away')}\n"
                    f"🏆 Liga: {pick.get('league') or 'Liga nieznana'}\n"
                    f"📈 Superbet: {odds:.2f}\n"
                    f"💵 Stawka: {stake:.2f} zł | Potencjalna wypłata po podatku: {payout['payout']:.2f} zł\n"
                    f"🔗 Mecz Superbet: {new_url}\n"
                    "Rozliczenie: wynik po 90 minutach; bez dogrywki i serii karnych."
                )
                response = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageCaption",
                    json={"chat_id": CHAT_ID, "message_id": int(pick["telegram_message_id"]), "caption": caption},
                    timeout=25,
                )
                response.raise_for_status()
                result = response.json()
                if not result.get("ok"):
                    raise RuntimeError(f"Telegram editMessageCaption error: {result}")
                pick["bet_url"] = new_url
                pick["link_updated_at"] = now_local().isoformat()
                pick["link_update_status"] = "updated"
                pick["link_caption_version"] = 2
                save_state(state)
                updated += 1
                print(f"ZAKTUALIZOWANO LINK SUPERBET: {pick.get('home')} - {pick.get('away')} (wiadomość {pick.get('telegram_message_id')})", flush=True)
            except Exception as exc:
                pick["link_update_status"] = "failed"
                pick["link_update_error"] = f"{type(exc).__name__}: {exc}"
                save_state(state)
                print(f"Superbet link updater: edycja nieudana dla {pick.get('home')} - {pick.get('away')}: {type(exc).__name__}: {exc}", flush=True)
    print(f"Superbet link updater: zaktualizowano {updated}/{len(pending)} przyszłych wiadomości.", flush=True)


def log_superbet_diagnostic(record):
    """Print and persist a structured Superbet audit record for this workflow run."""
    line = json.dumps(record, ensure_ascii=False, default=str)
    print("SUPERBET_DIAGNOSTIC " + line, flush=True)
    audit_path = Path("data/superbet_diagnostics.jsonl")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as audit_file:
        audit_file.write(line + "\n")


def is_womens_team_name(value):
    """Recognize Superbet's Polish women's marker (K) and common feed labels."""
    raw = str(value or "")
    return bool(re.search(
        r"\(\s*k\s*\)|\b(?:women|woman|female|feminine|femenil|feminino|femenino)\b|\bW\b",
        raw, re.I
    ))


def gender_neutral_team_aliases(value):
    """Aliases without gender tokens, used only for a guarded (K) fallback."""
    raw = str(value or "")
    raw = re.sub(r"\(\s*k\s*\)", " ", raw, flags=re.I)
    raw = re.sub(
        r"\b(?:women|woman|female|feminine|femenil|feminino|femenino|w)\b",
        " ", raw, flags=re.I
    )
    return team_name_aliases(raw)


def fixture_is_womens(match):
    """Read women's/female markers from Matchday team and competition metadata."""
    if not isinstance(match, dict):
        return None
    markers = []
    relevant_keys = {
        "gender", "sex", "competition", "competition_name", "competitionName",
        "league", "league_name", "leagueName", "tournament", "tournament_name",
        "category", "name", "short_name", "slug", "title",
    }

    def inspect(node, depth=0):
        if depth > 3:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in relevant_keys:
                    if isinstance(value, (str, int, float)):
                        markers.append(str(value))
                    elif isinstance(value, (dict, list)):
                        inspect(value, depth + 1)
                elif isinstance(value, (dict, list)) and depth < 3:
                    inspect(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:20]:
                inspect(value, depth + 1)

    inspect(match)
    if any(is_womens_team_name(value) or re.search(
        r"\b(?:women|woman|female|feminine|femenil|feminino|femenino|kobiet|kobieca)\b",
        value, re.I
    ) for value in markers):
        return True
    return None


def fixture_competition_label(value):
    """Extract a competition label without guessing from unrelated payload text."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in (
        "competition_name", "competitionName", "league_name", "leagueName",
        "tournament_name", "tournamentName", "championship_name",
        "championshipName", "competition", "tournament", "league",
    ):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            name = first(item, "name", "title", "short_name", "display_name", default="")
            if name:
                return str(name).strip()
    return ""


def fixture_category_signature(home="", away="", competition=""):
    """Return a strict gender + age-group identity for a fixture."""
    values = [str(home or ""), str(away or ""), str(competition or "")]
    youth_groups = set()
    youth_pattern = re.compile(
        r"\b(?:u\s*[- ]?\s*(1[6-9]|2[0-3])|under\s*(1[6-9]|2[0-3]))\b",
        re.I,
    )
    for value in values:
        for match in youth_pattern.finditer(value):
            youth_groups.add(next(group for group in match.groups() if group))
    if len(youth_groups) > 1:
        age_group = "ambiguous"
    elif youth_groups:
        age_group = f"U{next(iter(youth_groups))}"
    else:
        age_group = "SENIOR"

    women = any(is_womens_team_name(value) or re.search(
        r"\b(?:women|woman|female|feminine|femenil|feminino|femenino|ladies|kobiet|kobieca)\b",
        value, re.I
    ) for value in values)
    return {
        "gender": "WOMEN" if women else "MEN",
        "age_group": age_group,
    }


def fixture_categories_compatible(home_a, away_a, competition_a, home_b, away_b, competition_b):
    """Fail closed if the women's/senior/youth category differs or is ambiguous."""
    left = fixture_category_signature(home_a, away_a, competition_a)
    right = fixture_category_signature(home_b, away_b, competition_b)
    if "ambiguous" in (left["age_group"], right["age_group"]):
        return False
    return left == right


def resolve_btts_competition(home, away, kickoff, preferred=""):
    """Resolve a missing BTTS league from Open Matchday's global fixtures/results.
    Never return a fabricated or generic league label.
    """
    preferred = str(preferred or "").strip()
    if preferred and preferred.casefold() not in {"unknown", "unknown league", "liga nieznana"}:
        return format_fixture_competition(preferred, home, away), "Superbet"

    try:
        data = open_matchday_data()
    except Exception as exc:
        print(f"League resolver: Open Matchday unavailable: {type(exc).__name__}: {exc}", flush=True)
        return "", ""

    target_date = kickoff.date() if isinstance(kickoff, datetime) else None
    candidates = []
    # First choice: the same fixture in the public worldwide match schedule.
    for row in data.get("fixtures", []):
        row_home, row_away = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
        if not row_home or not row_away:
            continue
        row_kickoff = open_matchday_kickoff(row)
        if target_date and row_kickoff and abs((row_kickoff.date() - target_date).days) > 1:
            continue
        direct = (team_name_similarity(row_home, home), team_name_similarity(row_away, away))
        reverse = (team_name_similarity(row_home, away), team_name_similarity(row_away, home))
        score = max(min(direct), min(reverse))
        league = str(row.get("_league") or "").strip()
        if score >= 0.88 and league and league.casefold() not in {"unknown", "liga nieznana"}:
            candidates.append((score, 2 if target_date and row_kickoff and row_kickoff.date() == target_date else 1, league))
    if candidates:
        candidates.sort(reverse=True)
        best = candidates[0]
        return format_fixture_competition(best[2], home, away), "Open Matchday — terminarz"

    # Second choice: identify a competition shared by both teams in recent results.
    team_leagues = []
    for team in (home, away):
        counts = {}
        latest = {}
        for row in data.get("results", []):
            row_home, row_away = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
            if max(team_name_similarity(team, row_home), team_name_similarity(team, row_away)) < 0.88:
                continue
            league = str(row.get("_league") or "").strip()
            if not league or league.casefold() in {"unknown", "liga nieznana"}:
                continue
            counts[league] = counts.get(league, 0) + 1
            row_date = str(row.get("date") or "")
            latest[league] = max(latest.get(league, ""), row_date)
        team_leagues.append((counts, latest))
    common = set(team_leagues[0][0]) & set(team_leagues[1][0])
    if common:
        league = max(common, key=lambda name: (
            min(team_leagues[0][0][name], team_leagues[1][0][name]),
            min(team_leagues[0][1].get(name, ""), team_leagues[1][1].get(name, "")),
        ))
        return format_fixture_competition(league, home, away), "Open Matchday — wspólna liga w historii"
    return "", ""


def format_fixture_competition(competition, home="", away=""):
    """Keep youth age and explicitly mark women's competitions in Telegram."""
    label = str(competition or "Liga nieznana").strip()
    category = fixture_category_signature(home, away, label)
    if category["age_group"].startswith("U") and not re.search(
        r"\b(?:u\s*[- ]?\s*(1[6-9]|2[0-3])|under\s*(1[6-9]|2[0-3]))\b",
        label, re.I
    ):
        label = f"{label} {category['age_group']}"
    if category["gender"] == "WOMEN" and not re.search(
        r"\(\s*[kw]\s*\)|\b(?:women|woman|female|feminine|femenil|feminino|femenino|ladies|kobiet|kobieca)\b",
        label, re.I
    ):
        label = f"{label} (K)"
    return label


def find_superbet_event(home, away, events, womens=None):
    """Match exact oriented teams; support Superbet's (K) marker without mixing sexes."""
    if womens is True:
        home = home if is_womens_team_name(home) else f"{home} W"
        away = away if is_womens_team_name(away) else f"{away} W"
    wanted_home = team_name_aliases(home)
    wanted_away = team_name_aliases(away)
    neutral_home = gender_neutral_team_aliases(home)
    neutral_away = gender_neutral_team_aliases(away)
    exact = []
    womens_fallback = []
    near = []

    for event in events:
        parts = str(event.get("text") or "").split("·", 1)
        if len(parts) != 2:
            continue
        left, right = (part.strip() for part in parts)
        found_home = team_name_aliases(left)
        found_away = team_name_aliases(right)

        # Same home/away orientation only. Never use fuzzy names to choose odds.
        if wanted_home & found_home and wanted_away & found_away:
            if event.get("event_id"):
                exact.append(event)
            continue

        # Matchday may omit the women's suffix while Superbet appends (K).
        # This fallback is restricted to explicitly women's bookmaker events.
        if womens is not False and (
            is_womens_team_name(left) or is_womens_team_name(right)
        ):
            if (neutral_home & gender_neutral_team_aliases(left)
                    and neutral_away & gender_neutral_team_aliases(right)
                    and event.get("event_id")):
                womens_fallback.append(event)
                continue

        direct_home = team_name_similarity(home, left)
        direct_away = team_name_similarity(away, right)
        if min(direct_home, direct_away) >= 0.55:
            near.append({
                "event_id": event.get("event_id"),
                "event_name": event.get("text"),
                "home_similarity": round(direct_home, 3),
                "away_similarity": round(direct_away, 3),
            })

    # Prefer explicit gender-aware exact matches; use the guarded (K) fallback
    # only when no exact match exists. Deduplicate repeated API representations.
    selected_pool = exact if exact else womens_fallback
    unique = {str(event["event_id"]): event for event in selected_pool}
    exact = list(unique.values())

    diagnostic = {
        "type": "SUPERBET_EVENT_MATCH",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sportscore_home": home,
        "sportscore_away": away,
        "normalized_home": normalized_team_name(home),
        "normalized_away": normalized_team_name(away),
        "home_aliases": sorted(wanted_home),
        "away_aliases": sorted(wanted_away),
        "exact_matches": len(exact),
        "womens_marker_fallback_matches": len(womens_fallback),
        "near_candidates": near[:5],
    }
    if len(exact) != 1:
        diagnostic["result"] = "REJECTED_NO_EXACT_MATCH" if not exact else "REJECTED_AMBIGUOUS"
        log_superbet_diagnostic(diagnostic)
        return None

    selected = exact[0]
    diagnostic.update({
        "result": "EXACT_MATCH_WITH_SAFE_ALIASES",
        "superbet_event_id": str(selected.get("event_id")),
        "superbet_event_name": selected.get("text"),
        "superbet_url": selected.get("url"),
    })
    log_superbet_diagnostic(diagnostic)
    return selected



def superbet_event_detail(event_id):
    """Fetch current Superbet prices for one event from its public event endpoint."""
    base = "https://production-superbet-offer-pl.freetls.fastly.net"
    response = requests.get(
        f"{base}/v2/pl-PL/events/{event_id}",
        params={"_": str(datetime.now(timezone.utc).timestamp())},
        headers={
            **HEADERS,
            "Origin": "https://superbet.pl",
            "Referer": "https://superbet.pl/",
            "Accept": "application/json, text/plain, */*",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    records = payload.get("data", []) if isinstance(payload, dict) else []
    if not records or not isinstance(records[0], dict):
        return {}
    return records[0]


def extract_superbet_live_over_odds(event_detail, market, min_line):
    """Choose the lowest currently open match-total Over line in the accepted odds band."""
    def normalize_market_text(value):
        # Polish ł/Ł does not decompose under NFKD; transliterate it before
        # ASCII normalization so "cały mecz" is recognized as "caly mecz".
        value = str(value or "").replace("ł", "l").replace("Ł", "L")
        value = unicodedata.normalize("NFKD", value)
        value = value.encode("ascii", "ignore").decode("ascii").lower()
        value = re.sub(r"(?<=\d),(?=\d)", ".", value)
        value = re.sub(r"[^a-z0-9.]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    odds_list = []

    def collect_price_rows(node, inherited_market=""):
        if isinstance(node, list):
            for child in node:
                collect_price_rows(child, inherited_market)
            return
        if not isinstance(node, dict):
            return
        market_keys = (
            "marketName", "market_name", "marketGroupName", "market_group_name",
            "marketTitle", "market_title", "market", "marketGroup", "market_group", "title",
        )
        local_parts = [
            node.get(key) for key in market_keys
            if node.get(key) not in (None, "") and not isinstance(node.get(key), (dict, list))
        ]
        local_market = " ".join(str(x) for x in local_parts)
        combined_market = " ".join(x for x in (inherited_market, local_market) if x).strip()
        has_price = any(node.get(key) not in (None, "") for key in (
            "price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec"
        ))
        has_selection = any(node.get(key) not in (None, "") for key in (
            "name", "selectionName", "selection_name", "oddName",
            "outcomeName", "label", "displayName"
        ))
        if has_price and has_selection:
            row = dict(node)
            row["_parentMarketText"] = inherited_market
            odds_list.append(row)
        for child in node.values():
            if isinstance(child, (dict, list)):
                collect_price_rows(child, combined_market)

    collect_price_rows(event_detail)
    if not odds_list:
        return None

    total_market_names = {
        "liczba goli", "suma goli", "total goals", "goals total",
        "total goal", "goal line", "gole", "suma bramek",
    }
    first_half_markers = (
        "1 pol", "pierwsza polowa", "1st half", "first half", "half time", "ht",
    )
    second_half_markers = ("2 pol", "druga polowa", "2nd half", "second half")
    extra_time_markers = ("dogryw", "extra time", "overtime", "penalty shoot")
    phase_patterns = (
        r"\b(?:first half|1st half|pierwsza polowa|1 pol(?:owa)?)\b",
        r"\b(?:second half|2nd half|druga polowa|2 pol(?:owa)?)\b",
        r"\b(?:half time|ht|full time|ft|caly mecz|full match|whole match)\b",
    )
    candidates = []

    for odd in odds_list:
        status_value = first(odd, "status", "oddStatus", "state", default=None)
        status = str(status_value).strip().lower() if status_value is not None else ""
        # Superbet status "block" means the outcome is blocked, even if a
        # numeric price remains in the payload. Never publish blocked prices.
        if status not in {"active", "opened", "open", "1", "true"}:
            continue

        market_parts = [
            first(odd, key)
            for key in (
                "marketName", "market_name", "marketGroupName", "market_group_name",
                "marketTitle", "market_title", "market", "marketGroup", "market_group",
            )
            if first(odd, key) not in (None, "")
        ]
        if odd.get("_parentMarketText"):
            market_parts.append(odd["_parentMarketText"])
        market_text = normalize_market_text(" ".join(str(x) for x in market_parts))
        if not market_text or any(token in market_text for token in extra_time_markers):
            continue

        # Reject markets whose settlement is limited to a specific minute.
        # Example seen in Superbet: "Liczba goli - do X minuty".
        # Such a quote is not a full-time total, even if it appears in the
        # generic goals market group and the caller requests FT.
        time_limited_market = re.search(
            r"\b(?:do\s+(?:x|\d+)\s*(?:minuta|minuty|minut|min)|"
            r"(?:until|up to|by)\s+(?:the\s+)?(?:\d+|x)\s*(?:st|nd|rd|th)?\s*minute)\b",
            market_text,
        )
        if time_limited_market:
            continue

        is_first_half = any(token in market_text for token in first_half_markers)
        is_second_half = any(token in market_text for token in second_half_markers)
        if market == "HT":
            if not is_first_half or is_second_half:
                continue
        elif market == "FT":
            # Superbet's live event endpoint exposes the standard full-match
            # total-goals market simply as "Liczba goli" (without an FT suffix).
            # Accept only an exact standard total-market label OR an explicit
            # full-match label. Minute-limited markets are rejected above;
            # half-time/second-half and extra-time markets remain rejected.
            explicit_full_match = re.search(
                r"\b(?:caly mecz|pelny mecz|full match|whole match|full time|ft)\b",
                market_text,
            )
            standard_match_total = market_text in total_market_names
            if is_first_half or is_second_half or not (explicit_full_match or standard_match_total):
                continue
        else:
            continue

        normalized_parts = []
        for part in market_parts:
            part_text = normalize_market_text(part)
            for pattern in phase_patterns:
                part_text = re.sub(pattern, " ", part_text)
            normalized_parts.append(re.sub(r"\s+", " ", part_text).strip(" -_:"))
        base_market_text = market_text
        for pattern in phase_patterns:
            base_market_text = re.sub(pattern, " ", base_market_text)
        base_market_text = re.sub(r"\s+", " ", base_market_text).strip(" -_:")
        recognized_total_market = (
            base_market_text in total_market_names
            or any(
                part in total_market_names
                or any(name in part for name in total_market_names)
                for part in normalized_parts
            )
        )
        if not recognized_total_market:
            continue

        selection_name = str(first(
            odd, "name", "selectionName", "selection_name", "oddName",
            "outcomeName", "label", "displayName", default=""
        )).strip()
        selection_text = normalize_market_text(selection_name)
        selection_match = re.search(
            r"\b(?:powyzej|over)\s*(\d+(?:[.,]\d+)?)\b", selection_text
        )
        if not selection_match:
            continue
        line = float(selection_match.group(1).replace(",", "."))
        if line < float(min_line) - 0.001:
            continue

        line_value = first(odd, "specialBetValue", "special_bet_value")
        specifiers = first(odd, "specifiers", "specifier")
        if line_value is None and isinstance(specifiers, dict):
            line_value = first(specifiers, "total", "goals", "line", "hcp")
        if line_value is not None:
            line_match = re.search(r"(\d+(?:[.,]\d+)?)", str(line_value))
            if not line_match:
                continue
            structured_line = float(line_match.group(1).replace(",", "."))
            if abs(structured_line - line) > 0.001:
                continue

        # Do not silently choose one price field if the Superbet payload
        # exposes conflicting aliases. A disagreement makes this quote unsafe.
        price_values = []
        invalid_price_field = False
        for price_key in ("price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec"):
            raw_value = odd.get(price_key)
            if raw_value in (None, ""):
                continue
            try:
                parsed_value = float(str(raw_value).replace(",", "."))
            except (TypeError, ValueError):
                invalid_price_field = True
                break
            if parsed_value <= 1.0 or parsed_value > 1000:
                invalid_price_field = True
                break
            price_values.append(parsed_value)
        if invalid_price_field or not price_values:
            continue
        if max(price_values) - min(price_values) > 0.005:
            print(
                "Superbet: sprzeczne pola kursu dla "
                f"Over {line:.1f} {market}: {price_values}; odrzucam ofertę.",
                flush=True,
            )
            continue
        price = price_values[0]
        # No minimum-odds threshold: publish the actual open Superbet quote.

        candidates.append({
            "odds": price,
            "source": "Superbet",
            "market": " ".join(str(x) for x in market_parts),
            "line": line,
            "updated_at": first(odd.get("extra", {}), "feedPlatformTimestamp", "feedOriginTimestamp"),
        })

    # Collapse exact duplicate rows. If the feed reports conflicting prices
    # for one line, discard that line instead of guessing which quote is live.
    by_line = {}
    for item in candidates:
        by_line.setdefault(item["line"], set()).add(item["odds"])
    valid_lines = [
        line for line, prices in by_line.items()
        if len(prices) == 1
    ]
    if not valid_lines:
        status_counts = {}
        for item in odds_list:
            status = str(first(item, "status", "oddStatus", "state", default="missing")).lower()
            status_counts[status] = status_counts.get(status, 0) + 1
        sample = [
            {
                "status": first(item, "status", "oddStatus", "state"),
                "market": first(item, "marketName", "market_name", "marketGroupName", "market_group_name", "market", "marketGroup"),
                "name": first(item, "name", "selectionName", "selection_name", "oddName"),
                "price": first(item, "price", "odds", "oddValue", "odds_dec"),
            }
            for item in odds_list[:16] if isinstance(item, dict)
        ]
        print(
            f"Superbet: brak otwartego Over {market} od linii {float(min_line):.1f}; "
            f"statusy={status_counts}; próbka={json.dumps(sample, ensure_ascii=False)}",
            flush=True,
        )
        return None

    chosen_line = min(valid_lines)
    chosen_price = next(
        item for item in candidates
        if item["line"] == chosen_line
    )
    print(
        f"Superbet: wybrano rzeczywisty rynek {market} Over {chosen_line:.1f} "
        f"po kursie {chosen_price['odds']:.2f} (dostępne linie: {sorted(valid_lines)})",
        flush=True,
    )
    return chosen_price

def superbet_goal_market_url(url):
    """Open the event with Superbet's goal-count market group selected."""
    if not url:
        return None
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    # marketGroup=103 comes from the Superbet event URL supplied by the user.
    # It opens the "Liczba goli" market group, not a preselected bet.
    query["marketGroup"] = "103"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def superbet_btts_first_half_url(url):
    """Open the event directly in Superbet's BTTS first-half market group."""
    if not url:
        return None
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    # Parameters copied from the user's working Superbet BTTS 1st-half link.
    query["t"] = "offer-prematch-102"
    query["mdt"] = "o"
    query["marketGroup"] = "78"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def superbet_btts_full_time_url(url, quote=None):
    """Build a clean Superbet event link for BTTS FT without forcing a market tab.

    Superbet's event page exposes the available full-match BTTS prices when
    opened without a marketGroup override. IDs copied from the odds payload
    are not reliable URL market-group IDs and can lead to an empty "no odds"
    tab, so deliberately remove any inherited marketGroup parameter.
    """
    if not url:
        return None
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["t"] = "offer-prematch-102"
    query["mdt"] = "o"
    query.pop("marketGroup", None)
    result = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    print("Superbet BTTS FT: link do wydarzenia bez wymuszonego marketGroup.", flush=True)
    return result


def find_superbet_match_url(home, away, events):
    """Return only an unambiguous official event URL matching both teams."""
    candidates = []
    wanted_home = normalized_team_name(home)
    wanted_away = normalized_team_name(away)
    for event in events:
        href = str(event.get("url") or "")
        text_value = str(event.get("text") or "")
        if "/kursy/pilka-nozna/" not in href:
            continue
        slug = href.split("/kursy/pilka-nozna/", 1)[-1].strip("/").rsplit("-", 1)[0]
        if "-vs-" in slug:
            left, right = slug.split("-vs-", 1)
            left = left.replace("-", " ")
            right = right.replace("-", " ")
            direct = min(team_name_similarity(home, left), team_name_similarity(away, right))
            reverse = min(team_name_similarity(home, right), team_name_similarity(away, left))
            score = max(direct, reverse)
        else:
            # Fallback to visible event text only when both team names are clearly present.
            normalized_text = normalized_team_name(text_value)
            home_hit = wanted_home and wanted_home in normalized_text
            away_hit = wanted_away and wanted_away in normalized_text
            score = 0.90 if home_hit and away_hit else 0.0
        if score >= 0.78:
            candidates.append((score, f"https://superbet.pl{href}" if href.startswith("/") else href))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.05:
        print(f"Superbet: niejednoznaczny link dla {home} - {away}; nie wysyłam linku", flush=True)
        return None
    return superbet_goal_market_url(candidates[0][1])


def find_moviodds_match(home, away, odds_matches):
    """Match both teams in either orientation, allowing common provider name variants."""
    candidates = []
    diagnostics = []
    for item in odds_matches:
        odds_home = first(item, "home", "home_team", "home_name", default="")
        odds_away = first(item, "away", "away_team", "away_name", default="")
        direct_home = team_name_similarity(home, odds_home)
        direct_away = team_name_similarity(away, odds_away)
        reverse_home = team_name_similarity(home, odds_away)
        reverse_away = team_name_similarity(away, odds_home)
        direct = min(direct_home, direct_away)
        reverse = min(reverse_home, reverse_away)
        best = max(direct, reverse)
        total = max(direct_home + direct_away, reverse_home + reverse_away)
        diagnostics.append((best, total, str(odds_home), str(odds_away)))
        # Providers often shorten club names or omit city/club prefixes.
        # Require both teams to match reasonably well to avoid wrong fixtures.
        if (direct >= 0.65 and direct_home + direct_away >= 1.55) or (
            reverse >= 0.65 and reverse_home + reverse_away >= 1.55
        ):
            candidates.append((best + total * 0.05, item))
    if not candidates:
        diagnostics.sort(reverse=True)
        if diagnostics:
            print(
                f"MoviOdds: brak dopasowania dla {home} - {away}; "
                f"najbliższe: {diagnostics[:3]}",
                flush=True,
            )
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.035:
        print(
            f"MoviOdds: niejednoznaczne dopasowanie dla {home} - {away}; "
            f"wyniki={[(round(score, 3), first(item, 'home'), first(item, 'away')) for score, item in candidates[:3]]}",
            flush=True,
        )
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
        match = re.search(r"-?\d+", value)
        return int(match.group()) if match else None
    return None


def as_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:[.,]\d+)?", value)
        return float(match.group().replace(",", ".")) if match else None
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
    """
    Zwraca minutę tylko wtedy, gdy feed podaje wiarygodny zegar lub
    jednoznaczny status przerwy. Nie szacuje minuty z kickoffu:
    taki szacunek potrafi zamienić przerwę w błędne 60'.
    """
    if not isinstance(match, dict):
        return None

    phase_keys = (
        "status", "state", "match_status", "status_text",
        "fixture_status", "period", "phase", "elapsed_time",
        "current_period", "period_name", "match_period",
        "periodName", "currentPeriod",
    )
    phase_parts = []
    for key in phase_keys:
        value = match.get(key)
        if isinstance(value, dict):
            value = first(
                value, "short", "long", "name", "description",
                "label", "status", "period", "value", default=""
            )
        if value is not None:
            phase_parts.append(str(value))

    normalized_phase = re.sub(
        r"[_-]+", " ", " ".join(phase_parts).strip().lower()
    )
    normalized_phase = re.sub(r"\s+", " ", normalized_phase)

    # Status przerwy ma bezwarunkowe pierwszeństwo przed polami liczbowymi.
    halftime_patterns = (
        r"\bhalf\s*time\b",
        r"\bhalftime\b",
        r"\binterval\b",
        r"\bprzerwa\b",
        r"\bbreak\b",
        r"\bht\b",
    )
    if any(re.search(pattern, normalized_phase) for pattern in halftime_patterns):
        stoppage = re.search(r"45\s*\+\s*(\d+)", normalized_phase)
        return 45 + int(stoppage.group(1)) if stoppage else 45

    # Jawne pole zegara ma pierwszeństwo przed tekstem statusu.
    minute = as_int(first(
        match, "minute", "live_minute", "elapsed", "time_elapsed",
        "current_minute", "elapsed_minute", "currentMinute"
    ))
    if minute is not None and 0 <= minute <= 130:
        return minute

    # Tekst zegara, np. 32', 45+2' albo 67′.
    clock_values = [
        first(match, "status_text", "elapsed_time", "clock", "time", default=""),
        first(match, "status", "state", "match_status", default=""),
    ]
    for value in clock_values:
        if isinstance(value, dict):
            value = first(value, "short", "long", "name", "description", default="")
        match_minute = re.search(
            r"(\d{1,3})(?:\s*\+\s*(\d+))?\s*['′’]",
            str(value)
        )
        if match_minute:
            base = int(match_minute.group(1))
            added = int(match_minute.group(2) or 0)
            return base + added

    # Brak wiarygodnego zegara = brak typu. Nie wyliczamy minuty z kickoffu,
    # bo opóźnienia, przerwa i doliczony czas powodują fałszywe wartości.
    return None


def score_from_value(value):
    """Parse a home-away score pair, ignoring penalty suffixes."""
    if isinstance(value, dict):
        h = as_int(first(value, "home", "home_score", "homeScore"))
        a = as_int(first(value, "away", "away_score", "awayScore"))
        return (h, a) if h is not None and a is not None else (None, None)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return as_int(value[0]), as_int(value[1])
    if isinstance(value, str):
        found = re.search(r"(?<!\d)(\d+)\s*[-:]\s*(\d+)(?!\d)", value)
        if found:
            return int(found.group(1)), int(found.group(2))
    return None, None


def extract_regulation_time_score(match):
    """Return 90-minute score only; never count extra-time or shootout goals."""
    for key in (
        "regulation_time_score", "regular_time_score", "ninety_minute_score",
        "score_90", "ft_90_score", "full_time_90_score", "regulation_score",
        "score_at_90", "score90",
    ):
        pair = score_from_value(match.get(key)) if isinstance(match, dict) else (None, None)
        if pair[0] is not None and pair[1] is not None:
            return pair

    status_values = []
    for key in ("status", "state", "match_status", "fixture_status", "status_text"):
        value = match.get(key) if isinstance(match, dict) else None
        if isinstance(value, dict):
            value = first(value, "short", "long", "name", "description", default="")
        if value is not None:
            status_values.append(str(value).lower())
    status = " ".join(status_values)
    beyond_regulation = bool(
        match.get("overtime") is True
        or re.search(r"\b(?:aet|pen|pens|penalties|shootout|shoot out|after extra time|after penalties|extra time|overtime)\b", status)
    )
    if beyond_regulation:
        first_half = second_half = None
        periods = match.get("periods") if isinstance(match, dict) else None
        for period in periods if isinstance(periods, list) else []:
            if not isinstance(period, dict):
                continue
            name = re.sub(r"[^a-z0-9]+", " ", str(period.get("name") or "").lower()).strip()
            pair = score_from_value(period.get("score") or period)
            if pair[0] is None or pair[1] is None:
                continue
            if name in {"1st half", "first half", "half 1", "1 half"}:
                first_half = pair
            elif name in {"2nd half", "second half", "half 2", "2 half"}:
                second_half = pair
        if first_half and second_half:
            return first_half[0] + second_half[0], first_half[1] + second_half[1]
        return None, None

    for key in ("final_score", "full_time_score", "ft_score", "score_ft", "score"):
        pair = score_from_value(match.get(key)) if isinstance(match, dict) else (None, None)
        if pair[0] is not None and pair[1] is not None:
            return pair
    home = as_int(first(match, "home_score", "homeScore", "goals_home"))
    away = as_int(first(match, "away_score", "awayScore", "goals_away"))
    return (home, away) if home is not None and away is not None else (None, None)


def score_and_minute(match):
    score = first(match, "score", "scores", "current_score", "final_score", default={})
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
    status_parts = []
    for key in ("status", "state", "match_status", "status_text", "fixture_status"):
        value = match.get(key)
        if isinstance(value, dict):
            value = first(value, "short", "long", "name", "description", default="")
        if value is not None:
            status_parts.append(str(value))
    status = " ".join(status_parts).lower()
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
    """Extract confirmed live metrics and optional advanced attacking indicators."""
    stats = first(match, "statistics", "stats", "live_statistics", "match_statistics", default=None)
    if isinstance(stats, list):
        flattened = {}
        for item in stats:
            if not isinstance(item, dict):
                continue
            name = str(first(item, "name", "type", "key", "label", "stat_name", default="")).lower()
            value = first(item, "value", "total")
            if value is None and ("home" in item or "away" in item):
                value = {"home": item.get("home"), "away": item.get("away")}
            if name:
                flattened[name] = value
        stats = flattened
    if not isinstance(stats, dict) or not stats:
        return {}

    metrics = {
        "shots": None, "shots_on_target": None, "shots_off_target": None,
        "attacks": None, "dangerous_attacks": None, "xg": None,
        "big_chances": None, "shots_inside_box": None,
        "touches_in_box": None, "saves": None,
    }
    for key, value in stats.items():
        name = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
        if isinstance(value, dict):
            home = as_float(first(value, "home", "home_value", "local"))
            away = as_float(first(value, "away", "away_value", "visitor"))
            total = home + away if home is not None and away is not None else as_float(first(value, "total", "value"))
        else:
            total = as_float(value)
        if total is None:
            continue

        if ("expected_goals" in name or name in ("xg", "expected_goal")):
            metrics["xg"] = total
        elif "big_chance" in name:
            metrics["big_chances"] = total
        elif ("shot" in name and ("inside_box" in name or "inside_penalty_area" in name or "in_box" in name)):
            metrics["shots_inside_box"] = total
        elif "touch" in name and ("opposition_box" in name or "penalty_area" in name or "in_box" in name):
            metrics["touches_in_box"] = total
        elif "save" in name:
            metrics["saves"] = total
        elif "shot" in name and ("on_target" in name or "ontarget" in name):
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


def match_stats_look_promising(match, market):
    """
    Broad live-goal filter:
    - no mandatory shots-on-target requirement;
    - one independent attacking signal is enough;
    - use lower thresholds to avoid rejecting most live fixtures.
    Missing optional metrics are ignored because SportScore does not expose
    every statistic consistently for every competition.
    """
    m = live_match_metrics(match)
    if not m:
        return False, m, 0

    if market == "HT":
        thresholds = {
            "shots": 2,
            "dangerous_attacks": 8,
            "xg": 0.15,
            "big_chances": 1,
            "shots_inside_box": 1,
            "touches_in_box": 2,
        }
    else:
        thresholds = {
            "shots": 3,
            "dangerous_attacks": 12,
            "xg": 0.25,
            "big_chances": 1,
            "shots_inside_box": 1,
            "touches_in_box": 4,
        }

    signals = sum(
        1 for key, threshold in thresholds.items()
        if m.get(key) is not None and m[key] >= threshold
    )
    return signals >= 1, m, signals

def same_fixture_pick_exists(state, identifier, home, away, match_date, market, slug=None, competition=None):
    """
    Idempotency guard: a fixture + market is one pick, regardless of odds changes
    or small differences in provider IDs. Removing a Telegram message does not
    remove this record from the bot's persistent state.
    """
    wanted_home = normalized_team_name(home)
    wanted_away = normalized_team_name(away)
    wanted_id = str(identifier or "").strip().lower()

    for old in state.get("duplicate_guard", []):
        # A persisted pending record means the send was started. Treat it as
        # already claimed so a rerun cannot send the same fixture twice if the
        # process stopped after Telegram accepted the message but before the
        # final "sent" state was saved. Explicit failures remain retryable.
        if old.get("delivery_status", "sent") not in {"sent", "pending"}:
            continue
        if old.get("market") != market:
            continue
        old_id = str(old.get("id") or "").strip().lower()
        old_key = str(old.get("key") or "").strip().lower()
        # An identical provider fixture ID is always already claimed, even if
        # its metadata conflicts: fail closed instead of sending it twice.
        if wanted_id and (old_id == wanted_id or old_key.startswith(wanted_id + ":")):
            return True

        # For fuzzy team/date matching, require the same exact gender and age group.
        old_competition = old.get("league") or old.get("competition") or ""
        if not fixture_categories_compatible(
            old.get("home"), old.get("away"), old_competition,
            home, away, competition,
        ):
            continue

        # Provider IDs can change between list/detail scans. Match the fixture
        # by both team names as well, allowing a one-day timezone boundary.
        old_date = str(old.get("date") or "")
        try:
            day_gap = abs((datetime.fromisoformat(old_date).date() - datetime.fromisoformat(match_date).date()).days)
        except (TypeError, ValueError):
            day_gap = 99
        if day_gap > 1:
            continue
        old_slug = re.sub(r"[^a-z0-9]", "", str(old.get("slug") or "").lower())
        new_slug = re.sub(r"[^a-z0-9]", "", str(slug or "").lower())
        if old_slug and new_slug and old_slug == new_slug:
            return True

        old_home = old.get("home")
        old_away = old.get("away")
        direct_similarity = (
            team_name_similarity(old_home, home),
            team_name_similarity(old_away, away),
        )
        reverse_similarity = (
            team_name_similarity(old_home, away),
            team_name_similarity(old_away, home),
        )
        same_teams = (
            min(direct_similarity) >= 0.86
            or min(reverse_similarity) >= 0.86
        )
        if wanted_home and wanted_away and same_teams:
            return True

    return False



def telegram(message, reply_to_message_id=None):
    """Send Telegram messages; a requested reply must never fall back to a new post."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing GitHub Secrets: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True}
    if reply_to_message_id is not None:
        try:
            reply_id = int(reply_to_message_id)
        except (TypeError, ValueError):
            raise RuntimeError("Brak prawidłowego ID oryginalnego typu; wynik nie zostanie wysłany osobno.")
        if reply_id <= 0:
            raise RuntimeError("Nieprawidłowe ID oryginalnego typu; wynik nie zostanie wysłany osobno.")
        payload["reply_to_message_id"] = reply_id
    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def telegram_reply(message, reply_to_message_id):
    """Send settlement only as a reply to its original pick; never publish standalone."""
    if reply_to_message_id in (None, ""):
        raise RuntimeError("Brak ID wiadomości typu; nie można bezpiecznie powiązać rozliczenia.")
    try:
        reply_id = int(reply_to_message_id)
    except (TypeError, ValueError):
        raise RuntimeError("Nieprawidłowe ID wiadomości typu; rozliczenie pozostaje oczekujące.")
    if reply_id <= 0:
        raise RuntimeError("Nieprawidłowe ID wiadomości typu; rozliczenie pozostaje oczekujące.")
    # Any Telegram rejection/network error is allowed to propagate. The pick
    # stays unsettled and the next scheduled scan retries the same reply.
    return telegram(message, reply_to_message_id=reply_id)


# Settlement lookup supports both live detail and the finished-fixture archive.
def settlement_fixture_matches_pick(match, pick):
    """Reject stale/recycled slugs: team names AND scheduled calendar date must match."""
    if not isinstance(match, dict) or not isinstance(pick, dict):
        return False
    home, away = teams(match)
    result_competition = fixture_competition_label(match)
    pick_competition = pick.get("league") or pick.get("competition") or ""
    if not fixture_categories_compatible(
        home, away, result_competition,
        pick.get("home"), pick.get("away"), pick_competition,
    ):
        print(
            f"Settlement guard: niezgodna kategoria rozgrywek "
            f"{fixture_category_signature(home, away, result_competition)} != "
            f"{fixture_category_signature(pick.get('home'), pick.get('away'), pick_competition)}",
            flush=True,
        )
        return False
    direct = team_name_similarity(home, pick.get("home")) >= 0.86 and team_name_similarity(away, pick.get("away")) >= 0.86
    reverse = team_name_similarity(home, pick.get("away")) >= 0.86 and team_name_similarity(away, pick.get("home")) >= 0.86
    if not (direct or reverse):
        return False
    try:
        expected = datetime.fromisoformat(str(pick.get("kickoff_time") or pick.get("date")).replace("Z", "+00:00"))
        if expected.tzinfo is None:
            expected = expected.replace(tzinfo=TZ)
        expected_day = expected.astimezone(TZ).date()
    except (TypeError, ValueError):
        return False
    actual = kickoff_datetime(match)
    if actual is None:
        return False
    # Settlement must refer to this exact scheduled fixture, not another
    # meeting between the same teams on the previous/next day.
    if actual.astimezone(TZ).date() != expected_day:
        print(
            f"Settlement guard: odrzucono inną datę ({actual.astimezone(TZ).date()} != {expected_day})",
            flush=True,
        )
        return False
    if abs((actual - expected.astimezone(actual.tzinfo or TZ)).total_seconds()) > 3 * 60 * 60:
        print(
            f"Settlement guard: odrzucono inną godzinę ({actual} != {expected})",
            flush=True,
        )
        return False
    return True

def moviodds_get(path, params=None, timeout=90, max_attempts=5):
    """Read MoviOdds with bounded retries for temporary HTTP and network failures.

    Settlement lookups pass a smaller timeout/retry budget so one slow fixture
    cannot hold up the entire batch of pending picks.
    """
    retryable_statuses = {429, 500, 502, 503, 504}
    last_error = None
    max_attempts = max(1, int(max_attempts))
    for attempt in range(max_attempts):
        response = None
        try:
            response = requests.get(
                f"{MOVIODDS_API}{path}", params=params or {},
                headers={**HEADERS, "Accept-Encoding": "gzip"}, timeout=timeout,
            )
            if response.status_code not in retryable_statuses:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError(f"MoviOdds returned unexpected JSON for {path}")
                return payload
            last_error = requests.HTTPError(
                f"MoviOdds temporary HTTP {response.status_code} for {path}",
                response=response,
            )
            retry_after = response.headers.get("Retry-After")
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            retry_after = None
        if attempt < max_attempts - 1:
            try:
                requested_delay = float(retry_after) if retry_after else 0
            except (TypeError, ValueError):
                requested_delay = 0
            # Exponential backoff: 3, 6, 12, then 20 seconds (bounded).
            delay = min(20, 3 * (2 ** attempt))
            if requested_delay > 0:
                delay = min(30, max(delay, requested_delay))
            print(
                f"MoviOdds: próba {attempt + 1}/{max_attempts} nieudana "
                f"({type(last_error).__name__}); ponawiam za {delay}s.",
                flush=True,
            )
            time.sleep(delay)
    if last_error:
        raise last_error
    raise RuntimeError(f"MoviOdds unavailable for {path}")


def moviodds_finished_result(pick):
    """Fetch result for the exact Bet365 fixture ID stored with the pick."""
    fixture_id = str(pick.get("id") or "").strip()
    if not fixture_id:
        return {}
    # Keep each settlement lookup bounded: 2 attempts × 8s plus short backoff.
    # The scheduled workflow runs every 5 minutes, so long retries across several
    # pending fixtures previously made settlement appear frozen.
    payload = moviodds_get(
        "/results", {"sport": "soccer", "fi": fixture_id},
        timeout=8, max_attempts=2,
    )
    rows = payload.get("results", [])
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("fixture_id") or row.get("id") or "") != fixture_id:
            continue
        result = dict(row)
        result["id"] = fixture_id
        result["time"] = pick.get("kickoff_time")
        result["competition_name"] = row.get("league") or pick.get("league") or ""
        result["score"] = row.get("final_score") or ""
        return result
    return {}


def moviodds_prematch_board():
    """Fetch all Bet365 pre-match soccer fixtures and prices."""
    payload = moviodds_get("/odds", {"sport": "soccer", "links": "1"}, timeout=90)
    matches = payload.get("matches", [])
    if not isinstance(matches, list):
        raise RuntimeError("MoviOdds /odds returned no matches list")
    return matches


def extract_moviodds_btts_ft_quote(match):
    """Select only standalone full-time Both Teams to Score — Yes."""
    candidates = []
    group_status = match.get("group_status") if isinstance(match.get("group_status"), dict) else {}
    for quote in match.get("quotes", []) or []:
        market = re.sub(r"[^a-z0-9]+", " ", str(quote.get("group_name") or "").lower()).strip()
        if market != "both teams to score":
            continue
        if quote.get("phase") not in (None, "", "prematch"):
            continue
        if quote.get("suspended") is True or quote.get("stale") is True:
            continue
        group_id = str(quote.get("group_id") or "")
        if group_id and isinstance(group_status.get(group_id), dict) and group_status[group_id].get("stale"):
            continue
        if str(quote.get("label") or "").strip().lower() not in {"yes", "tak"}:
            continue
        try:
            odds = float(quote.get("odds_dec"))
        except (TypeError, ValueError):
            continue
        if not 1.70 <= odds <= 2.50:
            continue
        candidates.append({
            "odds": odds, "market": "Both Teams to Score — Yes",
            "group_id": group_id, "sel_id": str(quote.get("sel_id") or ""),
            "updated_at": quote.get("ts"),
        })
    if not candidates:
        return None
    if len({round(item["odds"], 3) for item in candidates}) != 1:
        return None
    if len({item["group_id"] for item in candidates}) != 1:
        return None
    return candidates[0]


def moviodds_btts_candidates(state, now=None, board=None):
    """Build unique, future-only Bet365 BTTS FT candidates."""
    current = now or now_local()
    candidates = []
    for match in (board if board is not None else moviodds_prematch_board()):
        if match.get("in_play") is True:
            continue
        if str(match.get("sport") or "soccer").lower() not in {"soccer", "football"}:
            continue
        kickoff = kickoff_datetime({"kickoff_time": match.get("kickoff_utc")})
        if kickoff is None or kickoff <= current or kickoff.date() != current.date():
            continue
        home, away = str(match.get("home") or "").strip(), str(match.get("away") or "").strip()
        fixture_id = str(match.get("id") or "").strip()
        if not home or not away or not fixture_id:
            continue
        league = format_fixture_competition(str(match.get("league") or "Liga nieznana"), home, away)
        quote = extract_moviodds_btts_ft_quote(match)
        if not quote:
            continue
        if same_fixture_pick_exists(
            state, fixture_id, home, away, kickoff.date().isoformat(),
            BTTS_FT_MARKET, competition=league,
        ):
            continue
        links = match.get("links") if isinstance(match.get("links"), dict) else {}
        match_url = links.get("prematch") or links.get("match") or ""
        if not str(match_url).startswith("https://"):
            match_url = ""
        candidates.append({
            "id": fixture_id, "home": home, "away": away, "league": league,
            "kickoff": kickoff, "quote": quote, "url": match_url,
            "competition_category": fixture_category_signature(home, away, league),
        })
    candidates.sort(key=lambda item: (item["kickoff"], item["home"], item["away"]))
    print(f"MoviOdds/Bet365: kandydaci BTTS FT w kursie 1.70–2.50: {len(candidates)}", flush=True)
    return candidates


def sportscore_result_for_pick(pick):
    """Find one exact, confirmed finished fixture in SportScore's results archive.

    Search the pick's own fixture date even after midnight. This lets older
    unresolved BTTS FT picks be reconciled instead of being limited to today's
    fixtures; exact team/category/date/kickoff checks remain mandatory.
    """
    if not isinstance(pick, dict) or not pick_local_date(pick):
        return None

    kickoff = kickoff_datetime(pick)
    if kickoff is None:
        return None

    local_day = pick_local_date(pick)
    query_days = {
        local_day - timedelta(days=1),
        local_day,
        local_day + timedelta(days=1),
        kickoff.astimezone(timezone.utc).date(),
    }
    candidates = {}
    for day in sorted(query_days):
        try:
            cache_key = ("date", day.isoformat())
            if cache_key not in _SPORTSCORE_SETTLEMENT_CACHE:
                payload = api_get(
                    "/api/v1/fixtures/",
                    {"sport": "football", "date": day.isoformat(),
                     "status": "finished", "limit": 200},
                    timeout=15,
                )
                _SPORTSCORE_SETTLEMENT_CACHE[cache_key] = extract_matches(payload)
            for fixture in _SPORTSCORE_SETTLEMENT_CACHE[cache_key]:
                if not isinstance(fixture, dict):
                    continue
                if not settlement_fixture_matches_pick(fixture, pick):
                    continue
                if not confirmed_final_fixture(fixture):
                    continue
                identity = match_identifier(fixture) or (
                    f"{teams(fixture)[0]}:{teams(fixture)[1]}:{kickoff_datetime(fixture)}"
                )
                candidates[str(identity)] = fixture
        except Exception as exc:
            print(f"SportScore archive {day}: {type(exc).__name__}: {exc}", flush=True)

    # The daily archive is capped; query each team schedule if it did not
    # return an exact, unique fixture. The same date/team/final checks apply.
    if not candidates:
        def slugify(value):
            value = unicodedata.normalize("NFKD", str(value or "")).encode(
                "ascii", "ignore"
            ).decode().lower()
            return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value)).strip("-")

        for team_slug in dict.fromkeys((slugify(pick.get("home")), slugify(pick.get("away")))):
            if not team_slug:
                continue
            try:
                cache_key = ("team", team_slug)
                if cache_key not in _SPORTSCORE_SETTLEMENT_CACHE:
                    payload = api_get(
                        "/api/v1/team/",
                        {"sport": "football", "slug": team_slug, "limit": 50},
                        timeout=15,
                    )
                    _SPORTSCORE_SETTLEMENT_CACHE[cache_key] = extract_matches(payload)
                for fixture in _SPORTSCORE_SETTLEMENT_CACHE[cache_key]:
                    if not isinstance(fixture, dict):
                        continue
                    if not settlement_fixture_matches_pick(fixture, pick):
                        continue
                    if not confirmed_final_fixture(fixture):
                        continue
                    identity = match_identifier(fixture) or (
                        f"{teams(fixture)[0]}:{teams(fixture)[1]}:{kickoff_datetime(fixture)}"
                    )
                    candidates[str(identity)] = fixture
            except Exception as exc:
                print(f"SportScore team archive {team_slug}: {type(exc).__name__}: {exc}", flush=True)

    if len(candidates) == 1:
        fixture = next(iter(candidates.values()))
        home, away = teams(fixture)
        print(f"SportScore: dokładny, zakończony mecz {home} - {away}; weryfikuję wynik po 90 minutach.", flush=True)
        return fixture
    if len(candidates) > 1:
        print(f"SportScore: kilka pasujących rekordów dla {pick.get('home')} - {pick.get('away')}; nie rozliczam automatycznie.", flush=True)
    return None


def match_detail(slug, pick=None):
    """
    Fetch match detail and search finished fixtures. The daily archive is
    capped at 200 rows, so also query the documented team filter to find
    fixtures that fall outside the first 200 results.
    """
    live_detail = {}
    if pick and pick.get("odds_source") == "Bet365 / MoviOdds":
        try:
            result = moviodds_finished_result(pick)
            # Accept provider final-state variants (FT/ended/completed), but
            # require a complete score and exact fixture match before settlement.
            if result and confirmed_final_fixture(result):
                if settlement_fixture_matches_pick(result, pick):
                    return result
                print(f"MoviOdds: wynik odrzucony — niezgodny mecz/kategoria {pick.get('key')}.", flush=True)
            else:
                print(
                    f"MoviOdds: brak potwierdzonego FT dla {pick.get('key')}; "
                    "sprawdzam dalej Open Matchday/live detail zamiast kończyć próbę.",
                    flush=True,
                )
        except Exception as exc:
            print(f"MoviOdds result error {pick.get('id')}: {type(exc).__name__}: {exc}; próbuję źródła zapasowego.", flush=True)
        # Do not return here: use the same exact-match Open Matchday fallback
        # below so one temporarily missing provider result cannot block settlement.
    if slug:
        # BTTS FT settlement needs one fast, exact live lookup; the legacy
        # widget fallback and broad archive search must not hold up new picks.
        endpoints = ("/api/v1/match/",) if pick and pick.get("market") == BTTS_FT_MARKET else (
            "/api/v1/match/", "/api/widget/match/"
        )
        for endpoint in endpoints:
            try:
                detail = unwrap_match(api_get(
                    endpoint, {"sport": "football", "slug": slug},
                    timeout=8 if pick and pick.get("market") == BTTS_FT_MARKET else 20,
                ))
                if detail and first(detail, "home", "away", "status", "home_score", "score") is not None:
                    if pick and not settlement_fixture_matches_pick(detail, pick):
                        dh, da = teams(detail)
                        print(f"ODRZUCAM STARY/NIEZGODNY DETAIL slug={slug}: {dh} - {da}, data={kickoff_datetime(detail)}; oczekiwano {pick.get('home')} - {pick.get('away')} {pick.get('kickoff_time')}", flush=True)
                        continue
                    if confirmed_final_fixture(detail):
                        return detail
                    if not live_detail or (
                        score_and_minute(detail)[0] is not None
                        and score_and_minute(detail)[1] is not None
                    ):
                        live_detail = detail
            except Exception as exc:
                print(f"Match detail {endpoint} niedostępny dla {slug}: {type(exc).__name__}: {exc}", flush=True)

    if pick and pick.get("market") == BTTS_FT_MARKET:
        # Primary source: SportScore. This path is strictly limited to today's
        # Warsaw-local fixtures; settle_picks also enforces the same date.
        result = sportscore_result_for_pick(pick)
        if result:
            return result

        # Fallback: Open Matchday, only when SportScore has no unique exact FT.
        result = open_matchday_result_for_pick(pick)
        if result:
            print(f"Open Matchday fallback: wynik FT {result['home']} - {result['away']} {result['home_score']}-{result['away_score']}", flush=True)
            return result
        if live_detail:
            print(
                f"BTTS FT: mecz {pick.get('home')} - {pick.get('away')} nadal bez potwierdzonego FT; "
                "zostawiam nierozliczony i ponowię sprawdzenie.",
                flush=True,
            )
            return live_detail
        print(
            f"BTTS FT: brak potwierdzonego wyniku FT dla {pick.get('home')} - {pick.get('away')}; "
            "ponowię sprawdzenie przy następnym skanie.",
            flush=True,
        )
        return {}

    if pick:
        dates = []
        kickoff = pick.get("kickoff_time")
        if kickoff:
            try:
                kickoff_dt = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
                dates.append(kickoff_dt.astimezone(timezone.utc).date())
            except (TypeError, ValueError):
                pass
        try:
            local_day = datetime.fromisoformat(str(pick.get("date"))).date()
            dates.extend([local_day - timedelta(days=1), local_day, local_day + timedelta(days=1)])
        except (TypeError, ValueError):
            pass

        # Slug contains both team slugs: home-vs-away. Query each team separately
        # because a full day's archive may contain more than the 200-row cap.
        team_slugs = []
        if slug and "-vs-" in slug:
            team_slugs = [part for part in slug.split("-vs-", 1) if part]
        queries = [None] + team_slugs

        # Team schedule is another documented route to recent finished
        # fixtures and is not constrained by the daily 200-row archive cap.
        for team_slug in team_slugs:
            try:
                payload = api_get("/api/v1/team/", {
                    "sport": "football", "slug": team_slug, "limit": 50,
                })
                fixtures = extract_matches(payload)
                print(
                    f"Harmonogram drużyny {team_slug}: {len(fixtures)} rekordów",
                    flush=True,
                )
                for fixture in fixtures:
                    if not isinstance(fixture, dict):
                        continue
                    fixture_id = match_identifier(fixture) or id_from_url(
                        first(fixture, "url", "match_url", "link", default="")
                    )
                    if pick.get("id") and fixture_id and str(pick["id"]).lower() == fixture_id.lower():
                        if not settlement_fixture_matches_pick(fixture, pick):
                            fh, fa = teams(fixture)
                            print(f"ID pasuje, ale data/drużyny nie: {fh} - {fa}, data={kickoff_datetime(fixture)}; oczekiwano {pick.get('home')} - {pick.get('away')} {pick.get('kickoff_time')}", flush=True)
                            continue
                        if confirmed_final_fixture(fixture):
                            print(f"Harmonogram: potwierdzony zakończony mecz po ID {fixture_id}", flush=True)
                            return fixture
                        print(f"Harmonogram: ID {fixture_id} pasuje, ale mecz nie ma potwierdzonego statusu końcowego — pomijam.", flush=True)
                        continue
                    fh, fa = teams(fixture)
                    if (
                        normalized_team_name(fh) == normalized_team_name(pick.get("home"))
                        and normalized_team_name(fa) == normalized_team_name(pick.get("away"))
                    ) or (
                        normalized_team_name(fh) == normalized_team_name(pick.get("away"))
                        and normalized_team_name(fa) == normalized_team_name(pick.get("home"))
                    ):
                        if not settlement_fixture_matches_pick(fixture, pick):
                            print(
                                f"Harmonogram: odrzucam podobne drużyny z innej daty/godziny "
                                f"({kickoff_datetime(fixture)}; oczekiwano {pick.get('kickoff_time')}).",
                                flush=True,
                            )
                            continue
                        if confirmed_final_fixture(fixture):
                            print("Harmonogram: potwierdzony zakończony mecz po nazwach i dokładnym terminie", flush=True)
                            return fixture
                        print("Harmonogram: nazwy pasują, ale rekord nie jest potwierdzonym zakończonym meczem — pomijam.", flush=True)
            except Exception as exc:
                print(
                    f"Harmonogram drużyny {team_slug}: {type(exc).__name__}: {exc}",
                    flush=True,
                )

        for day in dict.fromkeys(dates):
            for team_slug in queries:
                params = {
                    "sport": "football", "date": day.isoformat(),
                    "status": "finished", "limit": 200,
                }
                if team_slug:
                    params["team"] = team_slug
                try:
                    payload = api_get("/api/v1/fixtures/", params)
                    fixtures = extract_matches(payload)
                    print(
                        f"Archiwum finished {day} team={team_slug or 'ALL'}: "
                        f"{len(fixtures)} rekordów dla {pick.get('home')} - {pick.get('away')}",
                        flush=True,
                    )
                    for fixture in fixtures:
                        if not isinstance(fixture, dict):
                            continue
                        fixture_id = match_identifier(fixture) or id_from_url(
                            first(fixture, "url", "match_url", "link", default="")
                        )
                        if pick.get("id") and fixture_id and str(pick["id"]).lower() == fixture_id.lower():
                            if confirmed_final_fixture(fixture):
                                print(f"Archiwum: potwierdzony zakończony mecz po ID {fixture_id}", flush=True)
                                return fixture
                            print(f"Archiwum: ID {fixture_id} bez potwierdzonego wyniku końcowego — pomijam.", flush=True)
                            continue
                        fh, fa = teams(fixture)
                        direct = (
                            normalized_team_name(fh) == normalized_team_name(pick.get("home"))
                            and normalized_team_name(fa) == normalized_team_name(pick.get("away"))
                        )
                        reverse = (
                            normalized_team_name(fh) == normalized_team_name(pick.get("away"))
                            and normalized_team_name(fa) == normalized_team_name(pick.get("home"))
                        )
                        if direct or reverse:
                            if not settlement_fixture_matches_pick(fixture, pick):
                                print("Archiwum: nazwy pasują, ale data meczu jest inna lub brak daty — pomijam.", flush=True)
                                continue
                            if confirmed_final_fixture(fixture):
                                print(
                                    f"Archiwum: potwierdzony zakończony mecz po nazwach drużyn "
                                    f"({'odwrócona kolejność' if reverse else 'zgodna kolejność'})",
                                    flush=True,
                                )
                                return fixture
                            print("Archiwum: pasujące nazwy, ale brak potwierdzonego statusu końcowego i wyniku — pomijam.", flush=True)
                except Exception as exc:
                    print(
                        f"Archiwum finished {day} team={team_slug or 'ALL'}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )

    if live_detail:
        print("Archiwum nie zwróciło zakończonego meczu — używam szczegółów LIVE jako danych tymczasowych.", flush=True)
        return live_detail
    return {}

def current_live_matches():
    """
    Pobiera mecze piłkarskie LIVE z kilku wariantów feedu SportScore.
    Nie zakładamy, że parametr status=live jest obsługiwany przez każdy feed:
    jeśli pierwszy endpoint zwróci rekordy, ale filtr lokalny nie znajdzie LIVE,
    sprawdzamy także feed widgetowy i pełniejszą listę fixtures.
    """
    sources = [
        ("fixtures/live", "/api/v1/fixtures/", {"sport": "football", "status": "live", "limit": 200}),
        ("widget/matches", "/api/widget/matches/", {"sport": "football", "limit": 200}),
        ("fixtures/all", "/api/v1/fixtures/", {"sport": "football", "limit": 200}),
    ]

    def is_live_fixture(match):
        if not isinstance(match, dict):
            return False

        status_parts = []
        for key in ("status", "state", "match_status", "fixture_status",
                    "status_text", "elapsed_time", "period", "phase"):
            value = match.get(key)
            if isinstance(value, dict):
                value = first(value, "short", "long", "name", "description",
                              "label", "elapsed", "minute", default="")
            if value is not None:
                status_parts.append(str(value))
        status_text = " ".join(status_parts).strip().lower()
        normalized = re.sub(r"[_-]+", " ", status_text)

        finished_tokens = (
            "finished", "full time", "full-time", "ended", "complete",
            "completed", "after extra time", "penalties", "ft",
        )
        if any(token in normalized for token in finished_tokens):
            return False

        live_flag = first(match, "in_play", "inplay", "is_live", "live", default=False)
        if live_flag is True or live_flag == 1 or str(live_flag).strip().lower() in ("true", "1", "yes"):
            return True

        live_tokens = (
            "live", "in play", "in progress", "playing", "1h", "2h",
            "1st half", "2nd half", "first half", "second half",
            "half time", "halftime", "ht", "inplay", "extra time",
            "1st period", "2nd period",
        )
        if any(token in normalized for token in live_tokens):
            return True

        # Nie używamy pola 'time' — zwykle jest to godzina rozpoczęcia,
        # a nie zegar meczu. Akceptujemy tylko jawny zegar/minutę LIVE.
        clock = first(match, "minute", "live_minute", "elapsed", "time_elapsed",
                      "current_minute", "elapsed_minute", "currentMinute",
                      "status_text", "elapsed_time", default="")
        if isinstance(clock, dict):
            clock = first(clock, "minute", "elapsed", "text", "short", default="")
        clock_text = str(clock).strip()
        numeric_clock = as_int(clock_text)
        if numeric_clock is not None and 1 <= numeric_clock <= 130:
            return True
        if re.search(r"\b\d{1,3}(?:\s*\+\s*\d+)?\s*['′’]", clock_text):
            return True
        return False

    errors = []
    unique = {}

    # Nie kończ na pierwszym feedzie, który zwróci jakiekolwiek LIVE.
    # Różne endpointy SportScore potrafią mieć różne zestawy spotkań;
    # zbieramy więc wszystkie i łączymy rekordy po identyfikatorze.
    for label, endpoint, params in sources:
        try:
            payload = api_get(endpoint, params)
            records = extract_matches(payload)
            live = [item for item in records if is_live_fixture(item)]
            print(
                f"SportScore {label}: {len(records)} rekordów, {len(live)} meczów LIVE",
                flush=True,
            )
            if records:
                sample = records[0]
                print(
                    f"SportScore {label} — pola: {', '.join(sorted(str(k) for k in sample.keys()))}; "
                    f"status przykładu: {first(sample, 'status', 'state', 'match_status', 'status_text', default='brak')}",
                    flush=True,
                )

            for item in live:
                identity = match_identifier(item) or match_slug(item)
                if not identity:
                    home, away = teams(item)
                    identity = f"{normalized_team_name(home)}:{normalized_team_name(away)}"
                identity = str(identity).strip().lower()
                if identity not in unique:
                    unique[identity] = dict(item)
                    unique[identity]["_sportscore_sources"] = [label]
                    continue

                # Zachowaj wartości z pierwszego feedu; uzupełnij wyłącznie
                # brakujące pola z pozostałych endpointów, bez nadpisywania
                # wyniku/minuty inną, potencjalnie opóźnioną kopią.
                merged = unique[identity]
                merged["_sportscore_sources"].append(label)
                for key, value in item.items():
                    if key not in merged or merged[key] in (None, "", [], {}):
                        if value not in (None, "", [], {}):
                            merged[key] = value

        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"Błąd feedu SportScore {label}: {type(exc).__name__}: {exc}", flush=True)

    result = list(unique.values())
    in_windows = 0
    missing_minutes = 0
    outside_windows = 0
    for item in result:
        minute = score_and_minute(item)[2]
        if minute is None:
            missing_minutes += 1
        elif 25 <= minute <= 36 or 60 <= minute <= 75:
            in_windows += 1
        else:
            outside_windows += 1

    print(
        f"SportScore: łącznie {len(result)} unikalnych meczów LIVE z połączonych feedów; "
        f"w oknach typowania={in_windows}, poza oknami={outside_windows}, "
        f"bez minuty={missing_minutes}.",
        flush=True,
    )
    if errors:
        print("Problemy feedów SportScore: " + " | ".join(errors), flush=True)
    return result

def score_pair(value):
    if isinstance(value, dict):
        home = as_int(first(value, "home", "home_score", "homeScore", "local"))
        away = as_int(first(value, "away", "away_score", "awayScore", "visitor"))
        if home is not None and away is not None:
            return home, away
        for key in ("score", "result", "value", "goals"):
            if key in value:
                pair = score_pair(value[key])
                if pair[0] is not None and pair[1] is not None:
                    return pair
        return None, None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return as_int(value[0]), as_int(value[1])
    if isinstance(value, str):
        nums = re.findall(r"\d+", value)
        if len(nums) >= 2:
            return as_int(nums[0]), as_int(nums[1])
    return None, None


def extract_half_time_score(match):
    """Find explicit HT/first-half score in flat or nested provider data."""
    # SportScore match-detail response exposes the HT result as two flat fields.
    # Read these before looking for nested score objects.
    home_ht = as_int(first(match, "home_ht_score", "home_half_time_score", "homeHalftimeScore"))
    away_ht = as_int(first(match, "away_ht_score", "away_half_time_score", "awayHalftimeScore"))
    if home_ht is not None and away_ht is not None:
        return home_ht, away_ht

    preferred = ("half_time_score", "halftime_score", "ht_score", "score_ht", "half_time", "halftime")
    for key in preferred:
        if isinstance(match, dict) and key in match:
            pair = score_pair(match[key])
            if pair[0] is not None and pair[1] is not None:
                return pair

    def walk(obj, depth=0):
        if depth > 5:
            return None
        if isinstance(obj, dict):
            for key, value in obj.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in {
                    "ht", "htscore", "halftime", "halftimescore", "firsthalf",
                    "firsthalfscore", "period1", "period1score", "scoreht", "score1h"
                }:
                    pair = score_pair(value)
                    if pair[0] is not None and pair[1] is not None:
                        return pair
            for value in obj.values():
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        return None

    return walk(match) or (None, None)


def btts_first_half_won(home_ht, away_ht):
    """Return BTTS 1H result; None means the halftime score is not usable."""
    home = as_int(home_ht)
    away = as_int(away_ht)
    if home is None or away is None or home < 0 or away < 0:
        return None
    return home > 0 and away > 0


def final_status(match):
    """Accept only explicit full-time/final states, never a loose 'ft' substring."""
    values = []
    for key in ("status", "state", "match_status", "fixture_status", "status_text", "elapsed"):
        value = match.get(key) if isinstance(match, dict) else None
        if isinstance(value, dict):
            value = first(value, "short", "long", "name", "description", "status", default="")
        if value is not None:
            values.append(str(value).strip().lower())
    status = " ".join(values)
    normalized = re.sub(r"[^a-z0-9]+", " ", status).strip()
    tokens = set(normalized.split())
    final_tokens = {"finished", "ended", "complete", "completed", "ft", "aet", "pen"}
    final_phrases = (
        "full time", "after extra time", "after penalties",
        "match finished", "game finished", "final whistle",
    )
    explicit_final = bool(tokens & final_tokens) or any(
        phrase in normalized for phrase in final_phrases
    )
    if not explicit_final:
        return False

    # A stale/mislabelled "FT" status must never overrule a live match clock.
    # Full-time football cannot be confirmed while the provider still reports
    # a minute below 90. Extra time (90+) remains eligible for final status.
    reported_minute = parse_minute(match)
    if reported_minute is not None and reported_minute < 90:
        return False
    return True


def confirmed_final_fixture(match):
    """A settlement candidate must have an explicit final status and a complete score."""
    if not isinstance(match, dict) or not final_status(match):
        return False
    home_score, away_score, _, _ = score_and_minute(match)
    return home_score is not None and away_score is not None

def over_result_is_win(goal_line, final_total, period_complete):
    """Return a result only for a confirmed completed period and a valid OVER line."""
    if not period_complete or final_total is None:
        return None
    try:
        line = float(goal_line)
        total = int(final_total)
    except (TypeError, ValueError):
        return None
    if line < 0 or total < 0:
        return None
    return total > line


def settlement_confirmation_ready(pick, score_text, now, confirmation_seconds=180):
    """Require the same confirmed FT score on two checks at least 3 minutes apart."""
    pending_score = str(pick.get("settlement_pending_score") or "")
    pending_since = pick.get("settlement_pending_since")
    if pending_score != str(score_text) or not pending_since:
        pick["settlement_pending_score"] = str(score_text)
        pick["settlement_pending_since"] = now.isoformat()
        return False
    try:
        started = datetime.fromisoformat(str(pending_since))
        if started.tzinfo is None:
            started = started.replace(tzinfo=now.tzinfo)
        elapsed = (now - started).total_seconds()
    except (TypeError, ValueError):
        pick["settlement_pending_since"] = now.isoformat()
        return False
    return elapsed >= confirmation_seconds


def pick_local_date(pick):
    """Return the fixture's Warsaw-local calendar date, preferring its kickoff."""
    raw_kickoff = pick.get("kickoff_time")
    if raw_kickoff:
        kickoff = kickoff_datetime({"kickoff_time": raw_kickoff})
        if kickoff is not None:
            return kickoff.date()
    raw_date = str(pick.get("date") or "").strip()
    try:
        return datetime.fromisoformat(raw_date[:10]).date()
    except (TypeError, ValueError):
        return None


def kickoff_reached_for_settlement(pick, now=None):
    """Fail closed: automated settlement is forbidden before a known kickoff."""
    kickoff = kickoff_datetime(pick)
    if kickoff is None:
        return False
    current = now or now_local()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    return current.astimezone(TZ) >= kickoff


def settle_picks(state, allowed_markets=None):
    # Non-BTTS live goal-line markets retain a 3-minute score confirmation.
    # BTTS first-half markets settle immediately on a confirmed HT score.
    confirmation_seconds = 180

    allowed_markets = set(allowed_markets) if allowed_markets is not None else None
    settlement_day = now_local().date()
    for pick in sorted(
        state["picks"],
        key=lambda item: (
            str(item.get("date") or ""),
            int(item.get("pick_number") or 999999),
            str(item.get("kickoff_time") or ""),
        ),
    ):
        if allowed_markets is not None and pick.get("market") not in allowed_markets:
            continue
        # BTTS FT picks must remain eligible for reconciliation after midnight:
        # a missed result lookup must not leave yesterday's wager pending forever.
        # Other legacy markets retain the original same-day settlement boundary.
        fixture_day = pick_local_date(pick)
        if fixture_day != settlement_day and pick.get("market") != BTTS_FT_MARKET:
            continue
        if pick.get("delivery_status", "sent") != "sent":
            continue
        if pick.get("settled"):
            continue
        if not pick.get("telegram_message_id"):
            print(
                f"ROZLICZENIE WSTRZYMANE — {pick.get('home')} - {pick.get('away')}: "
                "brak telegram_message_id; nie wysyłam wyniku jako osobnej wiadomości.",
                flush=True,
            )
            continue
        try:
            manual = pick.get("manual_result")
            if manual:
                won = bool(manual.get("won"))
                score_text = str(manual.get("score", "brak"))
                result_text = "🟢 WYGRANA" if won else "🔴 PRZEGRANA"
                score_match = re.search(r"(\d+)\s*[-:]\s*(\d+)", score_text)
                goals_text = str(int(score_match.group(1)) + int(score_match.group(2))) if score_match else "nieznana"
                try:
                    odds = float(pick.get("odds"))
                except (TypeError, ValueError):
                    odds = None
                stake = float(pick.get("stake", STAKE) or STAKE)
                if won and odds is not None:
                    result = calculate_bet_result(stake, odds)
                    winnings_text = (
                        f"\nStawka: {result['stake']:.2f} zł | Kurs: {odds:.2f}"
                        f"\nPodatek 12%: {result['tax']:.2f} zł"
                        f"\nWypłata po podatku: {result['payout']:.2f} zł"
                        f"\nZYSK NETTO: {result['profit']:+.2f} zł"
                    )
                else:
                    winnings_text = f"\nSTRATA: {-stake:.2f} zł" if not won else ""
                yield_text = ""
                pick_label = f"TYP NR. {pick['pick_number']}\n" if pick.get("pick_number") else ""
                settlement_message = (
                    f"{pick_label}{result_text}\n{pick['home']} - {pick['away']}\n"
                    f"Wynik: {score_text} | Gole: {goals_text}"
                    f"{winnings_text}{yield_text}"
                )
                telegram_reply(
                    settlement_message,
                    reply_to_message_id=pick.get("telegram_message_id"),
                )
                pick["settled"] = True
                pick["won"] = won
                pick["settled_score"] = score_text
                pick["settled_at"] = now_local().isoformat()
                pick["settlement_method"] = "user_confirmed"
                pick.pop("manual_result", None)
                pick.pop("goal_pending_since", None)
                save_state(state)
                print(f"ROZLICZONO RĘCZNIE: {pick.get('key')} -> {result_text}", flush=True)
                continue

            # Never trust stale provider/archive scores before the scheduled kickoff.
            # Missing kickoff is also blocked for automatic settlement (manual results remain available).
            if not kickoff_reached_for_settlement(pick):
                print(
                    f"ROZLICZENIE ZABLOKOWANE — {pick.get('home')} - {pick.get('away')}: "
                    f"mecz jeszcze się nie rozpoczął lub brak prawidłowej godziny startu "
                    f"({pick.get('kickoff_time')}).",
                    flush=True,
                )
                continue

            # Prematch Superbet BTTS picks intentionally have no Open Matchday slug.
            # match_detail() can still settle them from the exact Open Matchday
            # finished-results CSV by team names, competition and kickoff date.
            if not pick.get("slug") and pick.get("market") != BTTS_FT_MARKET:
                print(f"ROZLICZENIE OCZEKUJE: brak slug dla {pick.get('key')}", flush=True)
                continue
            match = match_detail(pick.get("slug", ""), pick)
            if not match:
                print(
                    f"ROZLICZENIE OCZEKUJE: brak wyniku w plikach wynikowych Open Matchday dla "
                    f"{pick.get('home')} - {pick.get('away')} ({pick.get('key')}).",
                    flush=True,
                )
                continue

            # BTTS markets settle from their own confirmed period score.
            # It has no live-entry baseline score; requiring one would block every settlement.
            if pick.get("market") not in (BTTS_HT_MARKET, BTTS_FT_MARKET, OVER_15_HT_MARKET):
                baseline_home = as_int(pick.get("score_home"))
                baseline_away = as_int(pick.get("score_away"))
                if baseline_home is None or baseline_away is None:
                    print(f"Pomijam rozliczenie typu bez wyniku początkowego: {pick.get('key')}")
                    continue
            # Never settle against a different fixture returned by an archive/search endpoint.
            fixture_home, fixture_away = teams(match)
            fixture_competition = fixture_competition_label(match)
            pick_competition = pick.get("league") or pick.get("competition") or ""
            if not fixture_categories_compatible(
                fixture_home, fixture_away, fixture_competition,
                pick.get("home"), pick.get("away"), pick_competition,
            ):
                print(
                    f"ROZLICZENIE ZABLOKOWANE — inna kategoria: "
                    f"{fixture_category_signature(fixture_home, fixture_away, fixture_competition)} "
                    f"vs {fixture_category_signature(pick.get('home'), pick.get('away'), pick_competition)}.",
                    flush=True,
                )
                continue
            direct_match = (
                team_name_similarity(fixture_home, pick.get("home")) >= 0.86
                and team_name_similarity(fixture_away, pick.get("away")) >= 0.86
            )
            reverse_match = (
                team_name_similarity(fixture_home, pick.get("away")) >= 0.86
                and team_name_similarity(fixture_away, pick.get("home")) >= 0.86
            )
            if not (direct_match or reverse_match):
                print(
                    f"ROZLICZENIE ZABLOKOWANE — niezgodne drużyny: "
                    f"{fixture_home} - {fixture_away}; oczekiwano "
                    f"{pick.get('home')} - {pick.get('away')}.",
                    flush=True,
                )
                continue

            home_now, away_now, minute, status = score_and_minute(match)
            print(
                f"Szczegóły rozliczenia {pick.get('key')}: pola={','.join(sorted(str(k) for k in match.keys()))}; "
                f"HT={first(match, 'half_time_score', 'halftime_score', 'ht_score', 'score_ht', 'half_time')!r}; "
                f"events={type(first(match, 'events', 'timeline', 'incidents')).__name__}",
                flush=True,
            )
            current_total = home_now + away_now if home_now is not None and away_now is not None else None
            finished = final_status(match)
            now = now_local()
            print(
                f"Sprawdzam rozliczenie {pick.get('key')}: wynik={home_now}-{away_now}, "
                f"status={status!r}, finished={finished}, minuta={minute}",
                flush=True,
            )

            target_total = None
            target_score = None
            period_complete = False

            if pick["market"] in ("HT", BTTS_HT_MARKET, OVER_15_HT_MARKET):
                ht_home, ht_away = extract_half_time_score(match)
                phase_values = []
                for phase_key in (
                    "status", "state", "match_status", "fixture_status",
                    "status_text", "period", "phase", "current_period",
                    "period_name", "periodName", "currentPeriod",
                ):
                    phase_value = match.get(phase_key) if isinstance(match, dict) else None
                    if isinstance(phase_value, dict):
                        phase_value = first(
                            phase_value, "short", "long", "name", "description",
                            "label", "status", "period", "value", default=""
                        )
                    if phase_value is not None:
                        phase_values.append(str(phase_value))
                phase_text = re.sub(
                    r"[_-]+", " ", " ".join(phase_values).lower()
                )
                at_half_time = any(re.search(pattern, phase_text) for pattern in (
                    r"\bhalf\s*time\b", r"\bhalftime\b",
                    r"\binterval\b", r"\bbreak\b",
                    r"\bprzerwa\b", r"\bht\b",
                ))
                in_second_half = any(re.search(pattern, phase_text) for pattern in (
                    r"\b2nd\s*half\b", r"\bsecond\s*half\b",
                    r"\b2h\b", r"\bdruga\s*polowa\b",
                ))
                # A populated, explicit HT score is itself evidence that the
                # first-half period has been recorded. Otherwise require an
                # explicit break/second-half/final status; 45+ stoppage time
                # alone is NOT enough to settle a first-half market.
                explicit_ht_score = ht_home is not None and ht_away is not None
                halftime_reached = finished or at_half_time or in_second_half or explicit_ht_score

                # At the actual half-time break, the live score is also the
                # HT score if SportScore omits its separate HT fields.
                # Never use a second-half or full-time score as an HT fallback.
                if (not explicit_ht_score and at_half_time
                        and home_now is not None and away_now is not None):
                    ht_home, ht_away = home_now, away_now
                    explicit_ht_score = True

                if explicit_ht_score and halftime_reached:
                    target_total = ht_home + ht_away
                    target_score = f"{ht_home}-{ht_away}"
                    period_complete = True
                elif not halftime_reached:
                    target_total = current_total
                    target_score = f"{home_now}-{away_now}" if current_total is not None else None
                elif finished:
                    # If provider omits HT score and no explicit break score
                    # exists, never infer it from the full-time score.
                    print(
                        f"BRAK WYNIKU HT dla {pick.get('key')}; "
                        f"rekord API ma pola: {', '.join(sorted(str(k) for k in match.keys()))}. "
                        "Nie rozliczam z wyniku FT, żeby nie podać błędnego rezultatu.",
                        flush=True,
                    )
                    continue
            elif pick.get("market") == BTTS_FT_MARKET:
                regulation_home, regulation_away = extract_regulation_time_score(match)
                if regulation_home is None or regulation_away is None:
                    print(
                        f"BTTS FT: brak wiarygodnego wyniku po 90 minutach dla {pick.get('key')}; "
                        "nie używam wyniku dogrywki ani serii karnych.",
                        flush=True,
                    )
                    continue
                target_total = regulation_home + regulation_away
                target_score = f"{regulation_home}-{regulation_away}"
                period_complete = finished
            else:
                target_total = current_total
                target_score = f"{home_now}-{away_now}" if current_total is not None else None
                period_complete = finished

            # OVER X.5 is a win only when the final score contains at least
            # X+1 goals. Never settle from a single goal while the match is live.
            # For HT picks, period_complete means the first half is confirmed over.
            if not period_complete:
                pending_since = pick.pop("settlement_pending_since", None)
                pending_score = pick.pop("settlement_pending_score", None)
                had_pending = bool(pending_since or pending_score)
                if had_pending:
                    save_state(state)
                    print(
                        f"Kasuję oczekujące potwierdzenie wyniku dla {pick.get('key')}; "
                        "mecz nie ma już potwierdzonego statusu końcowego.",
                        flush=True,
                    )
                continue

            if target_total is None or not target_score:
                print(f"BRAK POTWIERDZONEGO WYNIKU dla {pick.get('key')} — nie rozliczam.", flush=True)
                continue

            if pick.get("market") in (BTTS_HT_MARKET, BTTS_FT_MARKET):
                score_parts = re.search(r"(\d+)\s*[-:]\s*(\d+)", str(target_score))
                if not score_parts:
                    print(f"ROZLICZENIE BTTS ZABLOKOWANE — nieczytelny wynik dla {pick.get('key')}.", flush=True)
                    continue
                won = btts_first_half_won(int(score_parts.group(1)), int(score_parts.group(2)))
            else:
                try:
                    selected_line = float(pick.get("goal_line"))
                except (TypeError, ValueError):
                    print(f"BRAK POPRAWNEJ LINII OVER dla {pick.get('key')} — nie rozliczam.", flush=True)
                    continue
                won = over_result_is_win(selected_line, target_total, period_complete)
                if won is None:
                    print(f"ROZLICZENIE ZABLOKOWANE — nieprawidłowy wynik/okres dla {pick.get('key')}.", flush=True)
                    continue
            # Cross-check the numeric total against the exact score text before posting.
            parsed_target_score = re.search(r"(\d+)\s*[-:]\s*(\d+)", str(target_score or ""))
            if not parsed_target_score:
                print(f"ROZLICZENIE ZABLOKOWANE — brak czytelnego wyniku dla {pick.get('key')}.", flush=True)
                continue
            score_total_check = int(parsed_target_score.group(1)) + int(parsed_target_score.group(2))
            if score_total_check != int(target_total):
                print(
                    f"ROZLICZENIE ZABLOKOWANE — suma bramek {target_total} "
                    f"nie zgadza się z wynikiem {target_score} dla {pick.get('key')}.",
                    flush=True,
                )
                continue
            # For live goal-line markets, a single stale FT response must
            # never settle a newly sent tip: retain the 180-second check.
            # BTTS 1H settles at HT; BTTS FT waits for the confirmed final score.
            # score/period. The extra 180-second debounce is only for live
            # goal-line markets and must not delay a completed first half.
            if (pick.get("market") not in (BTTS_HT_MARKET, BTTS_FT_MARKET, OVER_15_HT_MARKET)
                    and not settlement_confirmation_ready(pick, target_score, now, confirmation_seconds)):
                save_state(state)
                print(
                    f"ROZLICZENIE WSTRZYMANE: {pick.get('key')} — "
                    f"potwierdzam wynik {target_score} przez {confirmation_seconds}s.",
                    flush=True,
                )
                continue
            pick.pop("settlement_pending_since", None)
            pick.pop("settlement_pending_score", None)
            pick.pop("goal_pending_since", None)
            result_text = "🟢 WYGRANA" if won else "🔴 PRZEGRANA"
            score_text = target_score or "brak"
            goals_text = str(target_total) if target_total is not None else "nieznana"
            try:
                odds = float(pick.get("odds"))
            except (TypeError, ValueError):
                odds = None
            stake = float(pick.get("stake", STAKE) or STAKE)
            if won and odds is not None:
                result = calculate_bet_result(stake, odds)
                winnings_text = (
                    f"\nStawka: {result['stake']:.2f} zł | Kurs: {odds:.2f}"
                    f"\nPodatek 12%: {result['tax']:.2f} zł"
                    f"\nWypłata po podatku: {result['payout']:.2f} zł"
                    f"\nZYSK NETTO: {result['profit']:+.2f} zł"
                )
            else:
                winnings_text = f"\nSTRATA: {-stake:.2f} zł" if not won else ""
            yield_text = ""
            pick_label = f"TYP NR. {pick['pick_number']}\n" if pick.get("pick_number") else ""
            settlement_message = (
                f"{pick_label}{result_text}\n{pick['home']} - {pick['away']}\n"
                f"Wynik: {score_text} | Gole: {goals_text}"
                f"{winnings_text}{yield_text}"
            )
            telegram_reply(
                    settlement_message,
                    reply_to_message_id=pick.get("telegram_message_id"),
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
            print(f"Settlement error for {pick.get('key')}: {type(exc).__name__}: {exc}", flush=True)



def retry_pending_settlement_notifications(state):
    """Retry explicitly queued settlement notices and record Telegram's message ID."""
    for pick in sorted(
        state.get("picks", []),
        key=lambda item: (
            str(item.get("date") or ""),
            int(item.get("pick_number") or 999999),
            str(item.get("kickoff_time") or ""),
        ),
    ):
        if not pick.get("settlement_notification_retry"):
            continue
        if not pick.get("settled") or pick.get("market") != BTTS_FT_MARKET:
            continue
        if not pick.get("telegram_message_id"):
            print(
                f"PONOWIENIE ROZLICZENIA WSTRZYMANE — {pick.get('home')} - {pick.get('away')}: "
                "brak telegram_message_id; nie wysyłam osobnej wiadomości.",
                flush=True,
            )
            continue
        score = str(pick.get("settled_score") or "brak")
        try:
            odds = float(pick.get("odds"))
        except (TypeError, ValueError):
            odds = None
        stake = float(pick.get("stake", STAKE) or STAKE)
        won = bool(pick.get("won"))
        result_text = "🟢 WYGRANA" if won else "🔴 PRZEGRANA"
        if won and odds is not None:
            result = calculate_bet_result(stake, odds)
            financial_text = (
                f"\nStawka: {result['stake']:.2f} zł | Kurs: {odds:.2f}"
                f"\nPodatek 12%: {result['tax']:.2f} zł"
                f"\nWypłata po podatku: {result['payout']:.2f} zł"
                f"\nZYSK NETTO: {result['profit']:+.2f} zł"
            )
        else:
            financial_text = f"\nSTRATA: {-stake:.2f} zł" if not won else ""
        pick_label = f"TYP NR. {pick['pick_number']}\n" if pick.get("pick_number") else ""
        message = (
            f"🔁 PONOWIONE ROZLICZENIE — BTTS CAŁY MECZ\n"
            f"{pick_label}{result_text}\n{pick.get('home')} - {pick.get('away')}\n"
            f"Wynik: {score}"
        )
        if odds is not None:
            message += f"\nKurs: {odds:.2f}"
        message += financial_text
        try:
            response = telegram_reply(
                    message,
                    reply_to_message_id=pick.get("telegram_message_id"),
                )
            telegram_message = response.get("result", {}) if isinstance(response, dict) else {}
            pick["settlement_notification_retry"] = False
            pick["settlement_notification_retry_sent"] = True
            pick["settlement_telegram_message_id"] = telegram_message.get("message_id")
            pick["settlement_notification_retry_at"] = now_local().isoformat()
            save_state(state)
            print(
                f"PONOWIONE ROZLICZENIE TELEGRAM OK: {pick.get('key')} "
                f"message_id={pick.get('settlement_telegram_message_id')}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"PONOWIONE ROZLICZENIE TELEGRAM BŁĄD: {pick.get('key')} — "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )



def daily_btts_net_profit(picks):
    """Return net profit/loss for settled BTTS FT picks after the configured tax."""
    total = 0.0
    for pick in picks:
        stake = float(pick.get("stake", STAKE) or STAKE)
        if pick.get("won") is True:
            odds = float(pick.get("odds") or 0)
            total += calculate_bet_result(stake, odds)["profit"]
        elif pick.get("won") is False:
            total -= stake
        elif pick.get("settlement_outcome") in {"void", "cancelled", "refunded"}:
            continue
        else:
            raise ValueError(
                f"Nieznany wynik rozliczenia dla {pick.get('key') or pick.get('home')}: "
                "nie można zamknąć dnia ani policzyć bilansu."
            )
    return round(total, 2)


def format_pln(value):
    return f"{float(value):+.2f}".replace(".", ",") + " zł"


def make_daily_btts_closer_image(day_key, wins, losses, net_profit):
    """Create the daily BTTS FT closing graphic with final net profit/loss."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1200, 675
    image = Image.new("RGB", (width, height), (8, 16, 30))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        blend = y / max(1, height - 1)
        color = (int(8 + 8 * blend), int(16 + 14 * blend), int(30 + 24 * blend))
        draw.line((0, y, width, y), fill=color)

    def font(size):
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
        return ImageFont.load_default()

    def centered(text, y, selected_font, fill):
        bounds = draw.textbbox((0, 0), text, font=selected_font)
        x = (width - (bounds[2] - bounds[0])) // 2
        draw.text((x, y), text, font=selected_font, fill=fill)

    draw.rounded_rectangle(
        (54, 48, width - 54, height - 48),
        radius=38,
        fill=(12, 27, 46),
        outline=(52, 104, 143),
        width=4,
    )
    draw.rounded_rectangle((90, 86, width - 90, 102), radius=8, fill=(35, 190, 155))
    centered("BTTS  •  FT", 135, font(76), (246, 250, 255))
    centered("ROZLICZENIA ZAKONCZONE", 225, font(39), (134, 213, 204))

    total = wins + losses
    draw.rounded_rectangle((175, 315, 555, 445), radius=24, fill=(20, 78, 68))
    draw.rounded_rectangle((645, 315, 1025, 445), radius=24, fill=(92, 39, 49))
    centered(f"WYGRANE  {wins}", 355, font(38), (130, 245, 192))
    centered(f"PRZEGRANE  {losses}", 355, font(34), (255, 155, 160))
    profit_color = (130, 245, 192) if net_profit >= 0 else (255, 155, 160)
    centered(f"ZYSK NETTO / STRATA: {format_pln(net_profit)}", 475, font(37), profit_color)
    centered(f"ROZLICZONE: {total}", 535, font(27), (207, 222, 238))
    centered(datetime.fromisoformat(day_key).strftime("%d.%m.%Y"), 580, font(24), (125, 155, 184))

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92, optimize=True)
    output.seek(0)
    return output


def telegram_daily_btts_closer(day_key, picks):
    """Post the daily BTTS FT closing graphic and final financial balance."""
    wins = sum(1 for pick in picks if pick.get("won") is True)
    losses = sum(1 for pick in picks if pick.get("won") is False)
    total = len(picks)
    net_profit = daily_btts_net_profit(picks)
    day_label = datetime.fromisoformat(day_key).strftime("%d.%m.%Y")
    caption = (
        f"🏁 KONIEC ROZLICZEŃ BTTS FT — {day_label}\n"
        f"🟢 Wygrane: {wins} | 🔴 Przegrane: {losses}\n"
        f"📊 Rozliczone typy: {total}\n"
        f"💰 ZYSK NETTO / STRATA: {format_pln(net_profit)}\n"
        f"🔒 DZIEŃ ZAMKNIĘTY"
    )
    image_file = make_daily_btts_closer_image(day_key, wins, losses, net_profit)
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
        data={"chat_id": CHAT_ID, "caption": caption},
        files={"photo": (f"btts-zamkniecie-{day_key}.jpg", image_file, "image/jpeg")},
        timeout=45,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto error: {result}")
    return result


def send_daily_btts_closers(state):
    """Close completed previous BTTS days only after every pick is settled."""
    today_key = now_local().date().isoformat()
    forced_close = set(state.get("daily_btts_force_close", []))
    closed = set(state.get("daily_btts_closers_sent", []))
    by_day = {}
    for pick in state.get("picks", []):
        if pick.get("market") != BTTS_FT_MARKET or pick.get("delivery_status", "sent") != "sent":
            continue
        day_key = str(pick.get("date") or "")[:10]
        if not day_key or (day_key >= today_key and day_key not in forced_close) or day_key in closed:
            continue
        by_day.setdefault(day_key, []).append(pick)

    for day_key, picks in sorted(by_day.items()):
        if not picks or not all(
            pick.get("settled")
            and pick.get("telegram_message_id")
            and not pick.get("settlement_notification_retry")
            for pick in picks
        ):
            pending = sum(1 for pick in picks if not pick.get("settled"))
            print(
                f"DZIEŃ BTTS {day_key} NIEZAMKNIĘTY — oczekujące rozliczenia: {pending}.",
                flush=True,
            )
            continue
        try:
            response = telegram_daily_btts_closer(day_key, picks)
            result = response.get("result", {}) if isinstance(response, dict) else {}
            if isinstance(result, list):
                result = result[0] if result else {}
            state.setdefault("daily_btts_closers_sent", []).append(day_key)
            state.setdefault("daily_btts_closer_messages", {})[day_key] = result.get("message_id")
            state.setdefault("daily_btts_daily_profit", {})[day_key] = daily_btts_net_profit(picks)
            save_state(state)
            print(
                f"WYSŁANO GRAFIKĘ ZAMKNIĘCIA BTTS FT {day_key}; "
                f"bilans={format_pln(daily_btts_net_profit(picks))}; "
                f"message_id={result.get('message_id')}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"Nie udało się wysłać grafiki zamknięcia BTTS FT {day_key}: "
                f"{type(exc).__name__}: {exc}; ponowię przy następnym skanie.",
                flush=True,
            )


def send_monthly_reports(state):
    today = now_local().date()
    sent = set(state.get("monthly_reports_sent", []))
    months = sorted({str(p.get("date", ""))[:7] for p in state["picks"] if p.get("date")})
    for month in months:
        if not month or month in sent:
            continue
        year, month_num = (int(part) for part in month.split("-"))
        # Do not publish a monthly balance until the month has ended.
        if (today.year, today.month) <= (year, month_num):
            continue
        month_picks = [p for p in state["picks"] if str(p.get("date", "")).startswith(month)]
        if not month_picks or not all(p.get("settled") for p in month_picks):
            continue

        wins = sum(1 for p in month_picks if p.get("won"))
        losses = len(month_picks) - wins
        turnover = len(month_picks) * STAKE
        profit = 0.0
        missing_odds = False
        for pick in month_picks:
            try:
                odds = float(pick.get("odds"))
            except (TypeError, ValueError):
                missing_odds = True
                continue
            stake = float(pick.get("stake", STAKE) or STAKE)
            profit += calculate_bet_result(stake, odds)["profit"] if pick.get("won") else -stake

        report = (
            f"📊 BILANS MIESIĄCA {month}\n"
            f"Typy: {len(month_picks)} | 🟢 {wins} | 🔴 {losses}\n"
            f"Stawka: {STAKE:.0f} zł / typ | Obrót: {turnover:.2f} zł\n"
        )
        if missing_odds:
            report += f"Zysk/strata: {profit:+.2f} zł (część kursów brak)\nYield: niepełne dane"
        else:
            yield_pct = profit / turnover * 100 if turnover else 0.0
            report += f"ZYSK/STRATA: {profit:+.2f} zł\nYield: {yield_pct:+.2f}%"
        telegram(report)
        sent.add(month)
        state["monthly_reports_sent"] = sorted(sent)
        save_state(state)



def update_existing_pick_minutes(state):
    """Send one Telegram follow-up with the current clock/score for older open picks."""
    for pick in state.get("picks", []):
        if pick.get("delivery_status", "sent") != "sent" or pick.get("settled"):
            continue
        # Re-send legacy updates once in the corrected plain-pick format.
        # Only skip after this format version has been confirmed sent.
        if pick.get("minute_update_sent") and int(pick.get("minute_update_format_version", 0) or 0) >= 2:
            continue
        slug = pick.get("slug")
        if not slug:
            continue
        try:
            match = match_detail(slug, pick)
            if not match:
                print(f"AKTUALIZACJA MINUTY OCZEKUJE: brak szczegółów dla {pick.get('home')} - {pick.get('away')}", flush=True)
                continue
            home_score, away_score, minute, status = score_and_minute(match)
            if minute is None:
                print(f"AKTUALIZACJA MINUTY OCZEKUJE: API nie podało minuty dla {pick.get('home')} - {pick.get('away')}", flush=True)
                continue
            score_text = (
                f"{home_score}-{away_score}"
                if home_score is not None and away_score is not None
                else "wynik niedostępny"
            )
            market_text = f"OVER {float(pick.get('goal_line', 0)):.1f} {pick.get('market', '')}"
            telegram(
                f"⚽ {market_text}\n"
                f"{pick.get('home')} - {pick.get('away')}\n"
                f"Liga: {pick.get('league', 'Liga nieznana')}\n"
                f"Minuta: {minute}' | Wynik: {score_text}\n"
                f"Stawka: {STAKE:.0f} zł"
            )
            pick["minute_update_sent"] = True
            pick["minute_update_format_version"] = 2
            pick["minute_update_at"] = now_local().isoformat()
            pick["latest_minute"] = minute
            pick["latest_score"] = score_text
            save_state(state)
            print(f"WYSŁANO AKTUALIZACJĘ MINUTY: {pick.get('home')} - {pick.get('away')} ({minute}', {score_text})", flush=True)
        except Exception as exc:
            print(f"BŁĄD AKTUALIZACJI MINUTY {pick.get('home')} - {pick.get('away')}: {type(exc).__name__}: {exc}", flush=True)



# ---------------------------------------------------------------------------
# PRE-MATCH BTTS FIRST-HALF AND FULL-TIME STRATEGIES
# ---------------------------------------------------------------------------
BTTS_HT_MARKET = "BTTS_HT"  # legacy historical market; do not use for new tips
BTTS_FT_MARKET = "BTTS_FT"  # legacy historical market; do not use for new tips
OVER_15_HT_MARKET = "OVER_15_HT"
MIN_OVER_15_HT_ODDS = 1.90
MAX_OVER_15_HT_ODDS = 3.50
MIN_IDDAA_HT_PERCENT = 40
MIN_HT_HISTORY = 3
HISTORY_MATCHES = 5
MIN_MODEL_PROBABILITY = 0.08

class IddaaHTTableParser(HTMLParser):
    """Read the visible match rows from iddaaorantahmin's HT Over 1.5 page."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.in_row = False
        self.in_cell = False
        self.current_row = []
        self.current_cell = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.in_row = True
            self.current_row = []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.current_cell = []

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            self.current_row.append(" ".join(" ".join(self.current_cell).split()))
            self.current_cell = []
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
            self.in_row = False


def iddaa_over_15_ht_predictions():
    """Fetch today's first-half Over 1.5 percentages from the public Iddaa page."""
    url = "https://www.iddaaorantahmin.com/en/soccer-odds-analysis/ht-over-1-5-goals-tips"
    response = requests.get(url, headers={**HEADERS, "Accept": "text/html"}, timeout=35)
    response.raise_for_status()
    parser = IddaaHTTableParser()
    parser.feed(response.text)
    predictions = []
    for cells in parser.rows:
        joined = " | ".join(cells)
        date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", joined)
        time_match = re.search(r"\b([0-2]?\d:[0-5]\d)\b", joined)
        percent_match = re.search(r"\b(\d{1,2})\s*%", joined)
        if not (date_match and time_match and percent_match):
            continue
        match_cell = next((cell for cell in cells if re.search(r"\s(?:-|vs)\s", cell, re.I)), "")
        if not match_cell:
            continue
        split = re.split(r"\s+(?:-|vs)\s+", match_cell, maxsplit=1, flags=re.I)
        if len(split) != 2:
            continue
        home, away = (part.strip() for part in split)
        if not home or not away:
            continue
        odds = None
        for cell in reversed(cells):
            if re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*", cell or ""):
                try:
                    value = float(cell.replace(",", "."))
                    if 1.01 <= value <= 1000:
                        odds = value
                        break
                except ValueError:
                    pass
        try:
            # The page declares GMT+03:00; convert its kickoff to Warsaw time.
            source_kickoff = datetime.strptime(
                date_match.group(1) + " " + time_match.group(1), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=ZoneInfo("Europe/Istanbul"))
            kickoff = source_kickoff.astimezone(TZ)
        except (TypeError, ValueError):
            continue
        predictions.append({
            "home": home, "away": away, "kickoff": kickoff,
            "date": kickoff.date().isoformat(),
            "probability": int(percent_match.group(1)),
            "source_odds": odds, "source": "iddaaorantahmin.com",
        })
    print(f"IddaaOranTahmin Over 1.5 HT: pobrano {len(predictions)} wierszy z procentami.", flush=True)
    return predictions


def match_iddaa_ht_prediction(home, away, kickoff, predictions):
    """Match a website row to the exact fixture, date and approximate kickoff."""
    matches = []
    for item in predictions:
        if item["date"] != kickoff.date().isoformat():
            continue
        direct = min(
            team_name_similarity(home, item["home"]),
            team_name_similarity(away, item["away"]),
        )
        reverse = min(
            team_name_similarity(home, item["away"]),
            team_name_similarity(away, item["home"]),
        )
        similarity = max(direct, reverse)
        if similarity < 0.78:
            continue
        time_gap = abs((item["kickoff"] - kickoff).total_seconds()) / 60
        if time_gap > 240:
            continue
        matches.append((similarity, -time_gap, item))
    if not matches:
        return None
    matches.sort(key=lambda value: (value[0], value[1]), reverse=True)
    if len(matches) > 1 and matches[0][:2] == matches[1][:2]:
        return None
    return matches[0][2]


def extract_superbet_over_15_ht(event_detail):
    """Return one verified active Superbet Over 1.5 goals first-half quote."""
    def norm(value):
        value = str(value or "").replace("ł", "l").replace("Ł", "L")
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()

    market_keys = (
        "marketName", "market_name", "marketGroupName", "market_group_name",
        "marketTitle", "market_title", "market", "marketGroup", "market_group",
        "title", "groupName", "group_name", "period", "periodName",
        "period_name", "phase", "phaseName", "phase_name", "info", "marketInfo", "line", "handicap", "total",
    )
    selection_keys = ("name", "selectionName", "selection_name", "oddName",
                      "outcomeName", "label", "displayName")
    price_keys = ("price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec")
    rows = []

    def walk(node, inherited=""):
        if isinstance(node, list):
            for child in node:
                walk(child, inherited)
            return
        if not isinstance(node, dict):
            return
        labels = [str(node.get(key)) for key in market_keys
                  if node.get(key) not in (None, "") and not isinstance(node.get(key), (dict, list))]
        context = " ".join(part for part in (inherited, " ".join(labels)) if part)
        if any(node.get(key) not in (None, "") for key in price_keys) and any(
            node.get(key) not in (None, "") for key in selection_keys
        ):
            row = dict(node)
            row["_context"] = context
            rows.append(row)
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, context)

    walk(event_detail)
    candidates = []
    for row in rows:
        status = str(first(row, "status", "oddStatus", "state", default="")).lower()
        if status not in ("active", "opened", "open", "1", "true"):
            continue
        selection = norm(first(row, *selection_keys, default=""))
        market = norm(row.get("_context", "") + " " + " ".join(
            str(row.get(key, "")) for key in market_keys if row.get(key) not in (None, "")
        ) + " " + selection)
        is_first_half = any(token in market for token in (
            "1 polowa", "pierwsza polowa", "first half", "1st half", "1 pol", "1h",
        ))
        is_over_selection = selection in ("over", "over 1 5", "powyzej", "powyzej 1 5")
        is_over_15 = bool(re.search(r"\bover 1 5\b", market)
                          or re.search(r"\b1 5 over\b", market)
                          or "powyzej 1 5" in market
                          or ("over under 1 5" in market and is_over_selection))

        # Accept only the match's aggregate first-half goals market.
        # Team totals, corners and cards share the same Superbet event payload.
        is_match_total_goals = bool(
            re.search(
                r"(?:^|\s)(?:1 polowa|pierwsza polowa|first half|1st half)\s+"
                r"(?:liczba|suma)\s+goli(?:\s|$)",
                market,
            )
            or re.search(r"(?:^|\s)(?:first half|1st half)\s+total goals(?:\s|$)", market)
            or re.search(r"(?:^|\s)total goals first half(?:\s|$)", market)
        )
        if not (is_first_half and is_over_15 and is_over_selection and is_match_total_goals):
            continue
        values = []
        for key in price_keys:
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                value = float(str(raw).replace(",", "."))
            except (TypeError, ValueError):
                values = []
                break
            if not 1.01 <= value <= 1000:
                values = []
                break
            values.append(value)
        if not values or max(values) - min(values) > 0.005:
            continue
        candidates.append({
            "odds": values[0], "market": market, "selection": "Over 1.5",
            "market_group_id": first(row, "marketGroupId", "market_group_id", "groupId", "group_id"),
            "source": "Superbet",
            "updated_at": first(row.get("extra", {}), "feedPlatformTimestamp", "feedOriginTimestamp"),
        })
    grouped = {}
    for item in candidates:
        key = str(item.get("market_group_id") or item["market"])
        grouped.setdefault(key, set()).add(round(item["odds"], 3))
    valid = [(key, prices) for key, prices in grouped.items() if len(prices) == 1]
    if len(valid) != 1:
        print("Superbet Over 1.5 HT: brak jednoznacznego aktywnego kursu; "
              + json.dumps([{"market": x["market"], "odds": x["odds"]} for x in candidates[:8]],
                           ensure_ascii=False), flush=True)
        return None
    key, prices = valid[0]
    chosen_odds = next(iter(prices))
    return next(item for item in candidates
                if str(item.get("market_group_id") or item["market"]) == key
                and round(item["odds"], 3) == chosen_odds)


def first_half_over_15_probability(home_profile, away_profile):
    """Poisson estimate for at least two first-half goals in total."""
    if not home_profile or not away_profile:
        return None
    home_lambda = max(0.01, (home_profile["scored_avg"] + away_profile["conceded_avg"]) / 2)
    away_lambda = max(0.01, (away_profile["scored_avg"] + home_profile["conceded_avg"]) / 2)
    total_lambda = home_lambda + away_lambda
    probability = 1 - math.exp(-total_lambda) * (1 + total_lambda)
    sample_factor = min(home_profile["sample"], away_profile["sample"]) / HISTORY_MATCHES
    probability = probability * sample_factor + 0.30 * (1 - sample_factor)
    return {
        "probability": max(0.0, min(0.95, probability)),
        "home_lambda": home_lambda, "away_lambda": away_lambda,
        "total_lambda": total_lambda,
        "home_sample": home_profile["sample"], "away_sample": away_profile["sample"],
    }


def kickoff_datetime(match):
    raw = first(match, "time", "start_time", "kickoff", "kickoff_time",
                "startDate", "start_date", "date", default="")
    if isinstance(raw, dict):
        raw = first(raw, "utc", "iso", "date", "time", "timestamp", default="")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def extract_superbet_competition_name(event):
    """Return the actual competition/league name from Superbet event metadata."""
    preferred = (
        "competition_name", "competitionName", "league_name", "leagueName",
        "tournament_name", "tournamentName", "championship_name",
        "championshipName", "competition", "tournament", "league",
    )
    generic = ("name", "title", "display_name", "displayName", "label")

    def read_name(value):
        if isinstance(value, str):
            value = value.strip()
            return value if value else None
        if isinstance(value, dict):
            for key in generic:
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return None

    if not isinstance(event, dict):
        return None
    for key in preferred:
        name = read_name(event.get(key))
        if name:
            return name

    # Competition data can be nested under fixture/category metadata.
    queue = [event]
    seen = set()
    while queue:
        node = queue.pop(0)
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        for key in preferred:
            if key in node:
                name = read_name(node.get(key))
                if name:
                    return name
        for value in node.values():
            if isinstance(value, dict):
                queue.append(value)
            elif isinstance(value, list):
                queue.extend(item for item in value if isinstance(item, dict))
    return None

def scheduled_fixtures_today(superbet_events=None, target_dates=None):
    """Build the BTTS fixture slate exclusively from Superbet's date-scoped event feed."""
    local_now = now_local()
    scan_days = set(target_dates or [local_now.date()])
    scan_days = {
        datetime.fromisoformat(day).date() if isinstance(day, str) else day
        for day in scan_days
    }
    fixtures = []
    seen_pairs = set()
    for event in superbet_events or []:
        home = str(event.get("home") or "").strip()
        away = str(event.get("away") or "").strip()
        kickoff = kickoff_datetime(event)
        event_id = event.get("event_id")
        if not home or not away or not event_id or not kickoff:
            continue
        if kickoff.date() not in scan_days or kickoff <= local_now + timedelta(minutes=15):
            continue
        team_pair = tuple(sorted((
            normalized_team_name(home), normalized_team_name(away)
        )))
        if team_pair in seen_pairs:
            continue
        seen_pairs.add(team_pair)

        def slugify(value):
            value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
            return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value)).strip("-")

        slug = f"{slugify(home)}-vs-{slugify(away)}"
        fixtures.append({
            "id": f"superbet:{event_id}", "slug": slug,
            "_bot_slug": slug, "home": home, "away": away,
            "time": kickoff.isoformat(), "date": kickoff.date().isoformat(),
            "competition_name": extract_superbet_competition_name(event) or "",
            "_bot_kickoff": kickoff.isoformat(), "_source": "Superbet",
        })

    fixtures.sort(key=lambda item: item["_bot_kickoff"])
    print(
        f"Terminarz Superbet na daty {', '.join(day.isoformat() for day in sorted(scan_days))}: "
        f"{len(fixtures)} przyszłych meczów; Open Matchday nie uczestniczy w wyborze terminarza.",
        flush=True,
    )
    return fixtures


_TEAM_HISTORY_INDEX = None


def _team_history_index():
    """Build one in-memory index of Open Matchday results instead of rescanning
    the entire CSV for every team in every fixture."""
    global _TEAM_HISTORY_INDEX
    if _TEAM_HISTORY_INDEX is not None:
        return _TEAM_HISTORY_INDEX

    by_alias = {}
    all_rows = []
    for row in open_matchday_data()["results"]:
        home, away = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
        if not home or not away:
            continue
        kickoff = open_matchday_kickoff(row)
        fh, fa = as_int(row.get("ft_home")), as_int(row.get("ft_away"))
        if not kickoff or fh is None or fa is None:
            continue
        item = {
            "home": home, "away": away, "ht_home": as_int(row.get("ht_home")),
            "ht_away": as_int(row.get("ht_away")), "ft_home": fh, "ft_away": fa,
            "kickoff": kickoff.isoformat(),
            "_kickoff": kickoff,
            "_identity": (row.get("date"), normalized_team_name(home), normalized_team_name(away)),
        }
        all_rows.append(item)
        for team in (home, away):
            for alias in team_name_aliases(team):
                by_alias.setdefault(alias, []).append(item)

    _TEAM_HISTORY_INDEX = {"by_alias": by_alias, "all_rows": all_rows}
    print(
        f"Open Matchday: zbudowano indeks historii {len(all_rows)} wyników "
        f"({len(by_alias)} aliasów drużyn).",
        flush=True,
    )
    return _TEAM_HISTORY_INDEX


def _team_history_rows(team_slug, cutoff):
    """Recent completed matches for a team, sourced only from indexed Open Matchday."""
    if not team_slug:
        return []
    target = normalized_team_name_from_slug(team_slug)
    target_aliases = team_name_aliases(target)
    index = _team_history_index()
    candidates = {}
    for alias in target_aliases:
        for row in index["by_alias"].get(alias, ()):
            candidates[row["_identity"]] = row

    # Fast path only: never scan the complete 9k-row archive for an unmatched
    # team. Provider aliases are indexed once; unknown names are skipped.

    usable = [
        row for row in candidates.values()
        if row["_kickoff"] < cutoff
    ]
    usable.sort(key=lambda item: item["kickoff"], reverse=True)
    return [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in usable[:HISTORY_MATCHES]
    ]


def _team_history_count_fast(team_slug, cutoff):
    """Count indexed prior results for shortlist ranking without building profiles."""
    if not team_slug:
        return 0
    target = normalized_team_name_from_slug(team_slug)
    index = _team_history_index()
    seen = set()
    for alias in team_name_aliases(target):
        for row in index["by_alias"].get(alias, ()):
            if row["_kickoff"] < cutoff:
                seen.add(row["_identity"])
    return len(seen)


def team_first_half_profile(team_slug, cutoff, venue, cache=None):
    """Compute first-half goals scored/conceded rates from real completed matches."""
    cache_key = f"{cutoff.date().isoformat()}:{team_slug}"
    if isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]
    rows = _team_history_rows(team_slug, cutoff)
    if venue in ("home", "away"):
        same_venue = [
            row for row in rows
            if (venue == "home" and normalized_team_name(row["home"]) == normalized_team_name_from_slug(team_slug))
            or (venue == "away" and normalized_team_name(row["away"]) == normalized_team_name_from_slug(team_slug))
        ]
        if len(same_venue) >= MIN_HT_HISTORY:
            rows = same_venue
    scored, conceded = [], []
    for row in rows:
        # Team slug is derived from the fixture orientation; only compare known
        # home/away sides, and never infer orientation from a fuzzy name.
        if row.get("ht_home") is None or row.get("ht_away") is None:
            continue
        if team_name_similarity(row["home"], normalized_team_name_from_slug(team_slug)) >= 0.88:
            scored.append(row["ht_home"])
            conceded.append(row["ht_away"])
        elif team_name_similarity(row["away"], normalized_team_name_from_slug(team_slug)) >= 0.88:
            scored.append(row["ht_away"])
            conceded.append(row["ht_home"])
    if len(scored) < MIN_HT_HISTORY:
        profile = None
    else:
        profile = {
            "sample": len(scored),
            "scored_avg": sum(scored) / len(scored),
            "conceded_avg": sum(conceded) / len(conceded),
            "scored_rate": sum(1 for value in scored if value > 0) / len(scored),
            "conceded_rate": sum(1 for value in conceded if value > 0) / len(conceded),
        }
    if isinstance(cache, dict):
        cache[cache_key] = profile
    return profile


def normalized_team_name_from_slug(slug):
    value = re.sub(r"[-_]+", " ", str(slug or ""))
    return normalized_team_name(value)



def team_full_time_profile(team_slug, cutoff, venue, cache=None):
    """Compute full-time goals scored/conceded rates from completed fixtures."""
    cache_key = f"FT:{cutoff.date().isoformat()}:{team_slug}"
    if isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]
    rows = _team_history_rows(team_slug, cutoff)
    scored, conceded = [], []
    team_name = normalized_team_name_from_slug(team_slug)
    team_aliases = team_name_aliases(team_name)
    for row in rows:
        home_match = bool(team_aliases & team_name_aliases(row["home"])) or team_name_similarity(row["home"], team_name) >= 0.90
        away_match = bool(team_aliases & team_name_aliases(row["away"])) or team_name_similarity(row["away"], team_name) >= 0.90
        if home_match:
            scored.append(row.get("ft_home")); conceded.append(row.get("ft_away"))
        elif away_match:
            scored.append(row.get("ft_away")); conceded.append(row.get("ft_home"))
    pairs = [(a,b) for a,b in zip(scored,conceded)
             if isinstance(a,int) and isinstance(b,int) and a >= 0 and b >= 0]
    profile = None if len(pairs) < MIN_HT_HISTORY else {
        "sample": len(pairs),
        "scored_avg": sum(a for a,_ in pairs)/len(pairs),
        "conceded_avg": sum(b for _,b in pairs)/len(pairs),
        "scored_rate": sum(a > 0 for a,_ in pairs)/len(pairs),
        "conceded_rate": sum(b > 0 for _,b in pairs)/len(pairs),
    }
    if isinstance(cache,dict): cache[cache_key]=profile
    return profile


def full_time_btts_probability(home_profile, away_profile):
    """Independent Poisson estimate for BTTS across the full match."""
    if not home_profile or not away_profile: return None
    lh=max(0.01,(home_profile["scored_avg"]+away_profile["conceded_avg"])/2)
    la=max(0.01,(away_profile["scored_avg"]+home_profile["conceded_avg"])/2)
    probability=(1-math.exp(-lh))*(1-math.exp(-la))
    factor=min(home_profile["sample"],away_profile["sample"])/HISTORY_MATCHES
    probability=0.5*probability+0.5*(probability*factor+0.48*(1-factor))
    return {
        "probability":max(0.0,min(0.95,probability)),
        "home_lambda":lh,"away_lambda":la,
        "home_sample":home_profile["sample"],"away_sample":away_profile["sample"],
        "home_scored_rate":home_profile["scored_rate"],
        "away_scored_rate":away_profile["scored_rate"],
        "home_conceded_rate":home_profile["conceded_rate"],
        "away_conceded_rate":away_profile["conceded_rate"],
    }


def first_half_btts_probability(home_profile, away_profile):
    """Independent Poisson estimate using first-half scoring/conceding history."""
    if not home_profile or not away_profile:
        return None
    home_lambda = max(0.01, (home_profile["scored_avg"] + away_profile["conceded_avg"]) / 2)
    away_lambda = max(0.01, (away_profile["scored_avg"] + home_profile["conceded_avg"]) / 2)
    probability = (1 - math.exp(-home_lambda)) * (1 - math.exp(-away_lambda))
    # Shrink extreme estimates slightly toward the observed baseline to reduce
    # overconfidence from small samples. This remains a model estimate.
    sample_factor = min(home_profile["sample"], away_profile["sample"]) / HISTORY_MATCHES
    probability = 0.5 * probability + 0.5 * (
        probability * sample_factor + 0.18 * (1 - sample_factor)
    )
    return {
        "probability": max(0.0, min(0.95, probability)),
        "home_lambda": home_lambda,
        "away_lambda": away_lambda,
        "home_sample": home_profile["sample"],
        "away_sample": away_profile["sample"],
        "home_scored_rate": home_profile["scored_rate"],
        "away_scored_rate": away_profile["scored_rate"],
        "home_conceded_rate": home_profile["conceded_rate"],
        "away_conceded_rate": away_profile["conceded_rate"],
    }


def extract_superbet_btts_first_half(event_detail):
    """Return only an active, actual Superbet YES quote for first-half BTTS."""
    def norm(value):
        value = str(value or "").replace("ł", "l").replace("Ł", "L")
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()

    rows = []
    market_keys = ("marketName", "market_name", "marketGroupName", "market_group_name",
                   "marketTitle", "market_title", "market", "marketGroup", "market_group", "title",
                   "period", "periodName", "period_name", "phase", "info", "marketInfo",
                   "market_info", "groupName", "group_name")
    selection_keys = ("name", "selectionName", "selection_name", "oddName",
                      "outcomeName", "label", "displayName")
    price_keys = ("price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec")

    def nested_market_text(value, depth=0):
        if depth > 3:
            return ""
        if isinstance(value, dict):
            parts = []
            for key, child in value.items():
                if child in (None, ""):
                    continue
                if isinstance(child, (dict, list)):
                    nested = nested_market_text(child, depth + 1)
                    if nested:
                        parts.append(nested)
                elif str(key).lower() not in {"id", "uuid", "price", "status", "code"}:
                    parts.append(str(child))
            return " ".join(parts)
        if isinstance(value, list):
            return " ".join(nested_market_text(child, depth + 1) for child in value)
        return str(value)

    def walk(node, inherited=""):
        if isinstance(node, list):
            for child in node:
                walk(child, inherited)
            return
        if not isinstance(node, dict):
            return
        local_parts = [
            str(node[k]) for k in market_keys
            if node.get(k) not in (None, "") and not isinstance(node.get(k), (dict, list))
        ]
        for nested_key in ("market", "marketGroup", "market_group", "period", "phase"):
            nested_value = node.get(nested_key)
            if isinstance(nested_value, (dict, list)):
                nested_text = nested_market_text(nested_value)
                if nested_text:
                    local_parts.append(nested_text)
        local = " ".join(local_parts)
        context = " ".join(part for part in (inherited, local) if part)
        if any(node.get(k) not in (None, "") for k in price_keys) and any(node.get(k) not in (None, "") for k in selection_keys):
            row = dict(node)
            row["_market_context"] = context
            rows.append(row)
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, context)

    walk(event_detail)
    candidates = []
    for row in rows:
        status = str(first(row, "status", "oddStatus", "state", default="")).lower()
        if status not in ("active", "opened", "open", "1", "true"):
            continue
        market = norm(" ".join([row.get("_market_context", "")] + [
            str(row.get(k, "")) for k in market_keys if row.get(k) not in (None, "")
        ]))
        # Both market identity and first-half period must be explicit.
        is_btts = ("obie druzyny strzela" in market or "both teams to score" in market
                   or re.search(r"\bbtts\b", market))
        is_first_half = any(token in market for token in
                            ("1 polowa", "pierwsza polowa", "first half", "1st half", "1 pol"))
        if not is_btts or not is_first_half or "2 polowa" in market or "second half" in market:
            continue
        selection = norm(first(row, *selection_keys, default=""))
        if selection not in ("tak", "yes", "true"):
            continue
        values = []
        invalid = False
        for key in price_keys:
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                value = float(str(raw).replace(",", "."))
            except (TypeError, ValueError):
                invalid = True
                break
            if not 1.01 <= value <= 1000:
                invalid = True
                break
            values.append(value)
        if invalid or not values or max(values) - min(values) > 0.005:
            continue
        candidates.append({
            "odds": values[0], "market": market, "selection": "Tak",
            "market_group_id": first(
                row, "marketGroupId", "market_group_id", "marketGroupID",
                "groupId", "group_id", "marketId", "market_id"
            ),
            "source": "Superbet",
            "updated_at": first(row.get("extra", {}), "feedPlatformTimestamp", "feedOriginTimestamp"),
        })
    if not candidates:
        sample = []
        seen = set()
        for row in rows:
            market_text = " ".join(str(row.get(k, "")) for k in market_keys if row.get(k) not in (None, ""))
            selection_text = str(first(row, *selection_keys, default=""))
            signature = (market_text, selection_text, str(first(row, *price_keys, default="")),
                         str(first(row, "status", "oddStatus", "state", default="")))
            if signature in seen:
                continue
            seen.add(signature)
            sample.append({
                "market": market_text[:160], "market_raw": row.get("market"),
                "market_group_raw": row.get("marketGroup", row.get("market_group")),
                "info_raw": row.get("info"), "period_raw": row.get("period"),
                "selection": selection_text[:50], "price": first(row, *price_keys),
                "status": first(row, "status", "oddStatus", "state"),
                "row_keys": sorted(key for key in row if not str(key).startswith("_")),
                "raw_sample": {key: value for key, value in row.items()
                               if not str(key).startswith("_")},
            })
            if len(sample) >= 10:
                break
        print("Superbet BTTS 1H — brak dopasowania; klucze eventu=" +
              json.dumps(sorted(event_detail.keys()) if isinstance(event_detail, dict) else [], ensure_ascii=False) +
              "; próbka rynków: " + json.dumps(sample, ensure_ascii=False)[:3200], flush=True)
        return None
    prices = {round(item["odds"], 3) for item in candidates}
    if len(prices) != 1:
        print(f"Superbet BTTS 1H: sprzeczne aktywne kursy {sorted(prices)} — odrzucam.", flush=True)
        return None
    return candidates[0]



def extract_superbet_1x2_full_time(event_detail):
    """Read the verified Polish Superbet full-time 1X2 market (group 547)."""
    market_keys = (
        "marketName", "market_name", "marketGroupName", "market_group_name",
        "marketTitle", "market_title", "market", "marketGroup", "market_group",
        "title", "groupName", "group_name",
    )
    selection_keys = (
        "name", "selectionName", "selection_name", "oddName",
        "outcomeName", "label", "displayName",
    )
    price_keys = ("price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec")
    group_keys = (
        "marketGroupId", "market_group_id", "marketGroupID",
        "groupId", "group_id", "marketId", "market_id",
    )

    def label(value):
        if isinstance(value, dict):
            return str(first(value, "name", "title", "label", "description", "displayName", default="") or "").strip()
        return str(value or "").strip()

    rows = []
    def walk(node, inherited_market="", inherited_group=None):
        if isinstance(node, list):
            for child in node:
                walk(child, inherited_market, inherited_group)
            return
        if not isinstance(node, dict):
            return
        local_market = " ".join(
            label(node.get(key)) for key in market_keys if node.get(key) not in (None, "")
        ).strip()
        market = local_market or inherited_market
        group_id = first(node, *group_keys, default=inherited_group)
        selection = next((label(node.get(key)) for key in selection_keys
                          if node.get(key) not in (None, "")), "")
        metadata = node.get("metadata")
        if not selection and isinstance(metadata, dict):
            selection = label(first(metadata, "name", "label", default=""))
        price = first(node, *price_keys, default=None)
        if selection and price not in (None, ""):
            rows.append({
                "market": market,
                "group": str(group_id) if group_id is not None else "",
                "selection": selection.strip().upper(),
                "price": price,
                "status": str(first(node, "status", "oddStatus", "state", default="")).strip().lower(),
            })
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, market, group_id)

    walk(event_detail)
    found = {"1": set(), "X": set(), "2": set()}
    for row in rows:
        market = re.sub(r"\\s+", " ", row["market"].strip().lower())
        if row["group"] != "547" or market not in {"mecz", "match"}:
            continue
        if row["selection"] not in found or row["status"] not in {"active", "opened", "open", "1", "true"}:
            continue
        try:
            price = float(str(row["price"]).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if not 1.01 <= price <= 1000:
            continue
        found[row["selection"]].add(round(price, 3))

    if any(len(found[key]) != 1 for key in ("1", "X", "2")):
        print(
            "Superbet 1X2: brak kompletnego, jednoznacznego aktywnego rynku Mecz/grupa 547; "
            + json.dumps({key: sorted(values) for key, values in found.items()}, ensure_ascii=False),
            flush=True,
        )
        return None
    result = {key: next(iter(found[key])) for key in ("1", "X", "2")}
    return {"home": result["1"], "draw": result["X"], "away": result["2"], "market_group_id": 547}



def extract_superbet_btts_full_time(event_detail):
    """Return one verified active YES quote from the full-match BTTS market only."""
    def norm(value):
        value = str(value or "").replace("ł", "l").replace("Ł", "L")
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
        return re.sub(r"\\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()

    market_keys = (
        "marketName", "market_name", "marketGroupName", "market_group_name",
        "marketTitle", "market_title", "market", "marketGroup", "market_group",
        "title", "groupName", "group_name",
    )
    selection_keys = (
        "name", "selectionName", "selection_name", "oddName",
        "outcomeName", "label", "displayName",
    )
    price_keys = ("price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec")
    group_keys = (
        "marketGroupId", "market_group_id", "marketGroupID",
        "groupId", "group_id", "marketId", "market_id",
    )
    generic_market_labels = {"mecz", "match", "main", "glowne", "rynek", "market", "all markets"}
    info_keys = ("info", "marketInfo", "market_info")

    def label_text(value):
        if isinstance(value, dict):
            value = first(value, "name", "title", "label", "description", "displayName", default="")
        if isinstance(value, (list, tuple)):
            return " ".join(label_text(item) for item in value if item not in (None, ""))
        return str(value or "").strip()

    rows = []

    def walk(node, inherited_market="", inherited_group=None, inherited_period=""):
        if isinstance(node, list):
            for child in node:
                walk(child, inherited_market, inherited_group, inherited_period)
            return
        if not isinstance(node, dict):
            return

        local_labels = [
            label_text(node.get(key)) for key in market_keys
            if node.get(key) not in (None, "")
        ]
        local_labels = [value for value in local_labels if value]
        local_market = " ".join(local_labels)
        normalized_local = norm(local_market)
        local_info = " ".join(label_text(node.get(key)) for key in info_keys
                              if node.get(key) not in (None, ""))
        normalized_info = norm(local_info)
        info_is_btts_label = (
            "obie druzyny strzela" in normalized_info
            or "both teams to score" in normalized_info
            or re.search(r"\\bbtts\\b", normalized_info) is not None
        )
        # Generic wrappers such as "Mecz" must not erase a more specific
        # market name inherited from their parent. A specific child market
        # replaces the parent's label so neighbouring markets cannot leak in.
        context_market = inherited_market
        if local_market and normalized_local not in generic_market_labels:
            context_market = local_market
        # Superbet sometimes puts the actual market description in "info"
        # while marketName is only the generic category "Mecz". Inherit info
        # only when it explicitly names BTTS; selection descriptions such as
        # "Team wins the match" must never contaminate neighbouring rows.
        if info_is_btts_label:
            context_market = " ".join(part for part in (context_market, local_info) if part)

        group_id = first(node, *group_keys, default=inherited_group)
        period_values = [
            label_text(node.get(key)) for key in
            ("period", "periodName", "period_name", "phase", "phaseName", "phase_name")
            if node.get(key) not in (None, "")
        ]
        period_context = " ".join(x for x in (inherited_period, *period_values) if x)

        has_price = any(node.get(key) not in (None, "") for key in price_keys)
        has_selection = any(node.get(key) not in (None, "") for key in selection_keys)
        if has_price and has_selection:
            row = dict(node)
            row["_market_context"] = context_market
            row["_period_context"] = period_context
            row["_market_group_id"] = group_id
            rows.append(row)

        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, context_market, group_id, period_context)

    walk(event_detail)

    candidates = []
    for row in rows:
        status = str(first(row, "status", "oddStatus", "state", default="")).strip().lower()
        if status not in {"active", "opened", "open", "1", "true"}:
            continue

        direct_market = " ".join(
            [label_text(row.get(key)) for key in market_keys + info_keys
             if row.get(key) not in (None, "")]
        )
        market = norm(" ".join(
            part for part in (row.get("_market_context", ""), direct_market, row.get("_period_context", ""))
            if part
        ))
        is_btts = (
            "obie druzyny strzela" in market
            or "both teams to score" in market
            or re.search(r"\\bbtts\\b", market) is not None
        )
        # Superbet also lists combination markets containing the words BTTS,
        # e.g. "BTTS + over 2.5", "BTTS or over 2.5", and "BTTS in either half".
        # Those are NOT the plain full-time BTTS market and must never compete
        # with the standalone market's price.
        # Strict allow-list: only the standalone full-match BTTS market.
        # Any extra condition or another market in the label is rejected.
        allowed_btts_words = {
            "mecz", "meczu", "caly", "calym", "spotkaniu", "w",
            "obie", "druzyny", "strzela", "gola", "gol",
            "both", "teams", "to", "score", "btts",
        }
        market_words = set(market.split())
        is_combined_market = not market_words.issubset(allowed_btts_words)
        is_first_half = any(token in market for token in (
            "1 polowa", "pierwsza polowa", "first half", "1st half", "1 pol",
            "half time", "halftime",
        ))
        is_second_half = any(token in market for token in (
            "2 polowa", "druga polowa", "second half", "2nd half",
        ))
        if not is_btts or is_combined_market or is_first_half or is_second_half:
            continue

        selection = norm(first(row, *selection_keys, default=""))
        if selection not in {"tak", "yes", "true"}:
            continue

        values = []
        invalid = False
        for key in price_keys:
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                value = float(str(raw).replace(",", "."))
            except (TypeError, ValueError):
                invalid = True
                break
            if not 1.01 <= value <= 1000:
                invalid = True
                break
            values.append(value)
        if invalid or not values or max(values) - min(values) > 0.005:
            continue

        candidates.append({
            "odds": values[0],
            "market": market,
            "selection": "Tak",
            "market_group_id": row.get("_market_group_id"),
            "source": "Superbet",
            "updated_at": first(row.get("extra", {}), "feedPlatformTimestamp", "feedOriginTimestamp"),
        })

    # There should be exactly one full-time BTTS YES price. Duplicate feed
    # rows in the same market are collapsed; competing groups/prices are not guessed.
    grouped = {}
    for item in candidates:
        key = str(item.get("market_group_id") or item["market"])
        grouped.setdefault(key, set()).add(round(item["odds"], 3))
    valid = [(key, prices) for key, prices in grouped.items() if len(prices) == 1]
    if len(valid) == 1:
        key, prices = valid[0]
        chosen_odds = next(iter(prices))
        chosen = next(item for item in candidates
                      if str(item.get("market_group_id") or item["market"]) == key
                      and round(item["odds"], 3) == chosen_odds)
        print(
            f"Superbet BTTS FT: potwierdzony rynek {chosen['market']} "
            f"@ {chosen['odds']:.2f}, grupa={chosen.get('market_group_id')}",
            flush=True,
        )
        return chosen

    if candidates:
        print(
            "Superbet BTTS FT: niejednoznaczne aktywne oferty Tak — "
            + json.dumps([
                {"market": item["market"], "group": item.get("market_group_id"), "odds": item["odds"]}
                for item in candidates[:12]
            ], ensure_ascii=False),
            flush=True,
        )
        return None

    sample = []
    for row in rows[:12]:
        sample.append({
            "market_context": row.get("_market_context"),
            "period_context": row.get("_period_context"),
            "group": row.get("_market_group_id"),
            "selection": first(row, *selection_keys),
            "price": first(row, *price_keys),
            "status": first(row, "status", "oddStatus", "state"),
            "row_keys": sorted(key for key in row if not str(key).startswith("_")),
        })
    print(
        "Superbet BTTS FT — brak dopasowania; klucze eventu="
        + json.dumps(sorted(event_detail.keys()) if isinstance(event_detail, dict) else [], ensure_ascii=False)
        + "; próbka ofert: " + json.dumps(sample, ensure_ascii=False)[:3200],
        flush=True,
    )
    return None


def send_prematch_over_15_ht_picks(state):
    today_date = now_local().date()
    today = today_date.isoformat()
    # New picks are restricted strictly to the current Warsaw-local calendar day.
    scan_dates = [today_date]
    print(
        f"Limit Over 1.5 HT: dziś {daily_sent_match_count(state, today)}/{MAX_DAILY_BTTS_FT_PICKS}; "
        f"zakres kursu: {MIN_OVER_15_HT_ODDS:.2f}–{MAX_OVER_15_HT_ODDS:.2f}. Skanuję wyłącznie dzisiaj.",
        flush=True,
    )
    try:
        iddaa_predictions = iddaa_over_15_ht_predictions()
    except Exception as exc:
        print(f"IddaaOranTahmin niedostępne — nie wysyłam typów bez procentów: {type(exc).__name__}: {exc}", flush=True)
        return
    superbet_events = []
    for scan_day in scan_dates:
        superbet_events.extend(superbet_prematch_event_links(scan_day))
    fixtures = scheduled_fixtures_today(superbet_events, scan_dates)
    if not fixtures:
        print("Brak przyszłych spotkań Over 1.5 HT w dzisiejszym terminarzu.", flush=True)
        return
    # No historical team-form or head-to-head statistics are used for picks.
    # Open Matchday is used only for the fixture list and scheduled kickoff.
    open_matchday_data()
    event_by_id = {
        str(event.get("event_id")): event
        for event in superbet_events
        if event.get("event_id") not in (None, "")
    }
    pre_candidates = []
    rejected_duplicate = 0
    for match in fixtures:
        home, away = teams(match)
        kickoff = kickoff_datetime(match)
        slug = match.get("_bot_slug") or match_slug(match)
        if not kickoff or not slug or "-vs-" not in slug:
            continue
        fixture_day = kickoff.date().isoformat()
        identifier = match_identifier(match) or slug
        event_id = str(match.get("id", "")).removeprefix("superbet:")
        event = event_by_id.get(event_id)
        if not event:
            continue
        league_for_identity = (
            extract_superbet_competition_name(event)
            or first(match, "competition_name", "competition", "league_name", default="")
        )
        if isinstance(league_for_identity, dict):
            league_for_identity = first(league_for_identity, "name", "title", default="")
        if same_fixture_pick_exists(
            state, identifier, home, away,
            fixture_day, OVER_15_HT_MARKET, slug=slug,
            competition=str(league_for_identity or ""),
        ):
            rejected_duplicate += 1
            continue
        iddaa = match_iddaa_ht_prediction(home, away, kickoff, iddaa_predictions)
        if not iddaa or iddaa["probability"] < MIN_IDDAA_HT_PERCENT:
            continue
        # Candidate ranking is based only on Iddaa's published percentage.
        # No H2H, recent-form, team-history or Poisson calculations.
        pre_candidates.append({
            "match": match, "event": event, "event_id": event_id,
            "home": home, "away": away, "slug": slug,
            "id": identifier, "kickoff": kickoff,
            "iddaa": iddaa,
        })

    pre_candidates.sort(key=lambda item: (
        -item["iddaa"]["probability"],
        item["kickoff"],
    ))
    DETAIL_SHORTLIST_SIZE = 20
    shortlisted = pre_candidates[:DETAIL_SHORTLIST_SIZE]
    print(
        f"Over 1.5 HT szybki wybór: terminarz={len(fixtures)}, "
        f"spełnia próg Iddaa={len(pre_candidates)}, "
        f"szczegółowa analiza maksymalnie={len(shortlisted)}, "
        f"duplikaty={rejected_duplicate}. Bez historii drużyn i H2H.",
        flush=True,
    )
    if not shortlisted:
        print("Over 1.5 HT: brak meczów spełniających próg Iddaa.", flush=True)
        return

    # The only probability used in selection and reporting is Iddaa's value.
    for item in shortlisted:
        item["estimate"] = {
            "probability": float(item["iddaa"]["probability"]) / 100.0,
            "home_sample": 0,
            "away_sample": 0,
            "method": "Iddaa only; historical team statistics disabled",
        }

    event_details = {}
    def fetch_event_detail(event_id):
        return event_id, superbet_event_detail(event_id)

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [
            executor.submit(fetch_event_detail, item["event_id"])
            for item in shortlisted
        ]
        for future in as_completed(futures):
            try:
                event_id, detail = future.result()
                event_details[str(event_id)] = detail
            except Exception as exc:
                print(
                    f"Superbet: błąd pobierania szczegółów shortlisty: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

    print(
        f"Superbet: pobrano szczegóły {len(event_details)}/{len(shortlisted)} "
        "wybranych kandydatów (zamiast całej oferty).",
        flush=True,
    )

    def analyze_fixture(index_match):
        index, item = index_match
        match = item["match"]
        home, away = item["home"], item["away"]
        kickoff, slug = item["kickoff"], item["slug"]
        event = item["event"]
        detail = event_details.get(item["event_id"])
        if not detail:
            print(f"[{index}] Brak szczegółów Superbet: {home} - {away}", flush=True)
            return None
        try:
            quote = extract_superbet_over_15_ht(detail)
            if not quote:
                print(f"[{index}] Brak aktywnego kursu Superbet Over 1.5 HT: {home} - {away}", flush=True)
                return None
            if not MIN_OVER_15_HT_ODDS <= quote["odds"] <= MAX_OVER_15_HT_ODDS:
                print(
                    f"[{index}] Kurs Over 1.5 HT {quote['odds']:.2f} poza zakresem "
                    f"{MIN_OVER_15_HT_ODDS:.2f}–{MAX_OVER_15_HT_ODDS:.2f}: {home} - {away}.",
                    flush=True,
                )
                return None
            estimate = item["estimate"]
            league = (
                extract_superbet_competition_name(event)
                or first(match, "competition_name", "competition", "league_name", default="Liga nieznana")
            )
            if isinstance(league, dict):
                league = first(league, "name", "title", default="Liga nieznana")
            league = format_fixture_competition(str(league), home, away)
            return {
                "match": match, "home": home, "away": away, "slug": slug,
                "id": item["id"], "kickoff": kickoff,
                "league": str(league), "event": event, "quote": quote,
                "estimate": estimate, "iddaa": item["iddaa"],
            }
        except Exception as exc:
            print(f"[{index}] Analiza kursu {home} - {away}: {type(exc).__name__}: {exc}", flush=True)
            return None

    indexed_fixtures = list(enumerate(shortlisted, 1))
    analyzed = [analyze_fixture(item) for item in indexed_fixtures]
    candidates = [candidate for candidate in analyzed if candidate]
    print(
        f"Ranking Over 1.5 HT zakończony: {len(candidates)} kandydatów z aktywnym "
        f"rynkiem i kursem {MIN_OVER_15_HT_ODDS:.2f}–{MAX_OVER_15_HT_ODDS:.2f}; "
        f"bez analizy historii, sprawdzono {len(shortlisted)} z {len(fixtures)} meczów.",
        flush=True,
    )
    # Prioritize today's fixtures, then the strongest Iddaa percentage.
    # Deliver at most one pick per workflow run (cron runs every five minutes)
    # to stay below Telegram group/channel flood limits.
    candidates.sort(key=lambda item: (item["kickoff"].date(), -item["iddaa"]["probability"], -item["estimate"]["probability"]))
    for candidate in candidates:
        candidate_day = candidate["kickoff"].date().isoformat()
        day_count = daily_sent_match_count(state, candidate_day)
        if day_count >= MAX_DAILY_BTTS_FT_PICKS:
            print(
                f"Limit {MAX_DAILY_BTTS_FT_PICKS} Over 1.5 HT osiągnięty dla {candidate_day}; "
                f"pomijam {candidate['home']} - {candidate['away']}.",
                flush=True,
            )
            continue
        # Last-moment independent price refresh; never reuse a stale or guessed quote.
        try:
            final_detail = superbet_event_detail(candidate["event"]["event_id"])
            final_quote = extract_superbet_over_15_ht(final_detail)
        except Exception as exc:
            print(f"Końcowa kontrola Superbet nieudana: {type(exc).__name__}: {exc}", flush=True)
            continue
        if not final_quote:
            print(f"Brak aktualnego kursu Over 1.5 HT dla {candidate['home']} - {candidate['away']}; pomijam.", flush=True)
            continue
        if not MIN_OVER_15_HT_ODDS <= final_quote["odds"] <= MAX_OVER_15_HT_ODDS:
            print(f"Końcowy kurs Over 1.5 HT {final_quote['odds']:.2f} poniżej minimum {MIN_OVER_15_HT_ODDS:.2f}: {candidate['home']} - {candidate['away']}; BLOKADA wysyłki.", flush=True)
            continue
        if abs(final_quote["odds"] - candidate["quote"]["odds"]) > 0.005:
            print(f"Kurs Over 1.5 HT zmienił się dla {candidate['home']} - {candidate['away']}; pomijam i sprawdzę ponownie.", flush=True)
            continue
        estimate = candidate["estimate"]
        probability = float(candidate["iddaa"]["probability"])
        bet_url = superbet_btts_full_time_url(
            candidate["event"]["url"], final_quote
        )
        if not bet_url:
            print(
                f"Pomijam {candidate['home']} - {candidate['away']}: "
                "brak potwierdzonego linku do grupy Over 1.5 HT w Superbet.",
                flush=True,
            )
            continue
        pick = {
            "key": f"{candidate['id']}:{OVER_15_HT_MARKET}",
            "id": candidate["id"], "slug": candidate["slug"],
            "home": candidate["home"], "away": candidate["away"],
            "league": candidate["league"],
            "competition_category": fixture_category_signature(
                candidate["home"], candidate["away"], candidate["league"]
            ),
            "date": candidate_day,
            "kickoff_time": candidate["kickoff"].isoformat(),
            "market": OVER_15_HT_MARKET, "stake": STAKE,
            "odds": final_quote["odds"], "odds_source": "Superbet",
            "odds_market": final_quote["market"],
            "odds_updated_at": final_quote.get("updated_at"),
            "superbet_url": bet_url,
            "probability": float(candidate["iddaa"]["probability"]), "iddaa_probability": candidate["iddaa"]["probability"], "iddaa_source_odds": candidate["iddaa"].get("source_odds"), "probability_method": "Iddaa HT Over 1.5 only; no team-history or H2H statistics",
            "model_inputs": estimate, "iddaa_source": "https://www.iddaaorantahmin.com/en/soccer-odds-analysis/ht-over-1-5-goals-tips", "goal_line": 1.5, "settled": False,
            "delivery_status": "pending",
        }
        state.setdefault("picks", []).append(pick)
        state.setdefault("duplicate_guard", []).append(pick)
        save_state(state)
        try:
            payout = calculate_bet_result(STAKE, final_quote["odds"])
            kickoff_text = candidate["kickoff"].strftime("%H:%M")
            message = (
                f"🕒 START GODZINA {kickoff_text} CZASU POLSKIEGO\n"
                f"🎯 OVER 1.5 GOLA W 1. POŁOWIE (HT)\n"
                f"⚽ {candidate['home']} – {candidate['away']}\n"
                f"🏆 Liga: {candidate['league']}\n"
                f"📈 Kurs Superbet: {final_quote['odds']:.2f}\n"
                f"📊 Procent iddaaorantahmin: {probability:.0f}%\n"
                f"Stawka: {STAKE:.2f} zł | Potencjalna wypłata po podatku: {payout['payout']:.2f} zł\n"
                f"Selekcja: procent Iddaa + kurs Superbet; bez statystyk historii drużyn\n"
                f"👉 OTWÓRZ OVER 1.5 HT W SUPERBET: {bet_url}\n"
                f"Dane meczowe: Open Matchday — https://github.com/oladeeayo/open-matchday"
            )
            image_cursor = int(state.get("btts_image_cursor", 0)) % len(BTTS_IMAGE_POOL)
            image_url = BTTS_IMAGE_POOL[image_cursor]
            response = telegram_btts_ft(message, image_url)
            state["btts_image_cursor"] = (image_cursor + 1) % len(BTTS_IMAGE_POOL)
            telegram_result = response.get("result", []) if isinstance(response, dict) else []
            if isinstance(telegram_result, list) and telegram_result:
                pick["telegram_message_id"] = telegram_result[0].get("message_id")
            elif isinstance(telegram_result, dict):
                pick["telegram_message_id"] = telegram_result.get("message_id")
            pick["delivery_status"] = "sent"
            pick["sent_at"] = now_local().isoformat()
            save_state(state)
            print(f"WYSŁANO Over 1.5 HT: {candidate['home']} - {candidate['away']} @ {final_quote['odds']:.2f}, P={probability}%", flush=True)
            break
        except Exception as exc:
            pick["delivery_status"] = "failed"
            pick["delivery_error"] = f"{type(exc).__name__}: {exc}"
            save_state(state)
            print(f"Telegram Over 1.5 HT nieudany: {type(exc).__name__}: {exc}", flush=True)
            # Stop after any Telegram delivery error; do not flood the channel.
            # The failed fixture remains retryable on the next scheduled scan.
            break



MAX_DAILY_BTTS_FT_PICKS = 10


def btts_value_gate(model_probability, odds, min_probability=0.52, min_edge=0.01):
    """Use a softer probability/value threshold while retaining the BTTS odds band."""
    try:
        probability = float(model_probability)
        price = float(odds)
    except (TypeError, ValueError):
        return False
    if not (0.0 < probability < 1.0 and 1.55 <= price <= 2.40):
        return False
    implied_probability = 1.0 / price
    return probability >= min_probability and probability - implied_probability >= min_edge


def btts_probability_estimate(home_all, away_all, home_venue, away_venue):
    """Blend venue, season, recent form and optional shot/corner evidence; shrink small samples."""
    venue_rate = (home_venue["btts"] + away_venue["btts"]) / 2.0
    season_rate = (home_all["btts"] + away_all["btts"]) / 2.0
    home_recent = home_all["last5"] / max(1, home_all["last5_played"])
    away_recent = away_all["last5"] / max(1, away_all["last5_played"])
    recent_rate = (home_recent + away_recent) / 2.0
    lambda_home = max(0.05, (home_venue["goals_for_avg"] + away_venue["goals_against_avg"]) / 2.0)
    lambda_away = max(0.05, (away_venue["goals_for_avg"] + home_venue["goals_against_avg"]) / 2.0)
    poisson_rate = (1.0 - math.exp(-lambda_home)) * (1.0 - math.exp(-lambda_away))
    raw_probability = 0.40 * venue_rate + 0.25 * season_rate + 0.20 * recent_rate + 0.15 * poisson_rate

    overall_sample = min(home_all["played"], away_all["played"])
    venue_sample = min(home_venue["played"], away_venue["played"])
    reliability = min(1.0, overall_sample / 8.0) * (0.75 + 0.25 * min(1.0, venue_sample / 5.0))
    probability = 0.50 + reliability * (raw_probability - 0.50)

    # Optional attack-volume corroboration. Missing statistics are not zeros.
    shots_sample = min(home_all["shots_sample"], away_all["shots_sample"])
    shots_on_target_sample = min(home_all["shots_on_target_sample"], away_all["shots_on_target_sample"])
    corners_sample = min(home_all["corners_sample"], away_all["corners_sample"])
    activity_adjustment = 0.0
    if shots_sample >= 3:
        average_shots = (home_all["shots_avg"] + away_all["shots_avg"]) / 2.0
        activity_adjustment += max(-0.006, min(0.006, (average_shots - 10.0) * 0.001))
    if shots_on_target_sample >= 3:
        average_sot = (home_all["shots_on_target_avg"] + away_all["shots_on_target_avg"]) / 2.0
        activity_adjustment += max(-0.010, min(0.010, (average_sot - 3.0) * 0.004))
    if corners_sample >= 3:
        average_corners = (home_all["corners_avg"] + away_all["corners_avg"]) / 2.0
        activity_adjustment += max(-0.004, min(0.004, (average_corners - 4.5) * 0.001))
    activity_adjustment = max(-0.015, min(0.015, activity_adjustment))
    probability += activity_adjustment

    return {
        "probability": max(0.01, min(0.90, probability)),
        "raw_probability": max(0.01, min(0.90, raw_probability)),
        "poisson_probability": poisson_rate,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "venue_rate": venue_rate,
        "season_rate": season_rate,
        "recent_rate": recent_rate,
        "reliability": reliability,
        "shots_sample": shots_sample,
        "shots_on_target_sample": shots_on_target_sample,
        "corners_sample": corners_sample,
        "activity_adjustment": activity_adjustment,
    }


def btts_recent_form_analysis(home, away, kickoff, league="", odds=None, one_x_two=None):
    """Evaluate a prematch BTTS FT fixture using current-season Open Matchday data."""
    data = open_matchday_data()

    def reject(reason):
        print(f"BTTS ANALYSIS REJECT: {home} - {away}: {reason}", flush=True)
        return None

    try:
        cutoff = kickoff.date() if hasattr(kickoff, "date") else datetime.fromisoformat(str(kickoff)).date()
    except (TypeError, ValueError):
        return reject("brak prawidłowej daty rozpoczęcia")

    def valid_result(row):
        try:
            played = datetime.fromisoformat(str(row.get("date", ""))[:10]).date()
        except (TypeError, ValueError):
            return False
        return (
            played < cutoff
            and as_int(row.get("ft_home")) is not None
            and as_int(row.get("ft_away")) is not None
            and row.get("home") and row.get("away")
        )

    # Keep the history in the same competition whenever the provider identifies it.
    league_rows = []
    for row in data.get("results", []):
        if not valid_result(row):
            continue
        row_league = str(row.get("_league") or "").strip().casefold()
        if row_league and row_league not in {"unknown", "liga nieznana"}:
            if (
                team_name_similarity(row.get("home"), home) >= 0.88
                or team_name_similarity(row.get("away"), home) >= 0.88
                or team_name_similarity(row.get("home"), away) >= 0.88
                or team_name_similarity(row.get("away"), away) >= 0.88
            ):
                league_rows.append(row)

    requested_league = str(league or "").strip().casefold()
    if requested_league:
        requested_key = re.sub(r"[^a-z0-9]+", "", requested_league)
        same_competition = []
        for row in league_rows:
            row_league = str(row.get("_league") or "").casefold()
            row_folder = str(row.get("_folder") or "").casefold()
            row_key = re.sub(r"[^a-z0-9]+", "", row_league + " " + row_folder)
            if (
                requested_league in row_league
                or row_league in requested_league
                or (requested_key and requested_key in row_key)
            ):
                same_competition.append(row)
    else:
        same_competition = []

    team_rows = same_competition if same_competition else league_rows
    if not team_rows:
        # Open Matchday is an optional evidence source, never a fixture-eligibility
        # gate. When it has no history for either side, retain the match for a
        # clearly-labelled market-only fallback instead of silently discarding it.
        try:
            market_odds = float(odds)
        except (TypeError, ValueError):
            return reject("brak prawidłowego kursu BTTS do analizy awaryjnej")
        if not 1.55 <= market_odds <= 2.40:
            return reject("kurs BTTS poza zakresem 1.55-2.40 w analizie awaryjnej")
        if isinstance(one_x_two, dict):
            try:
                home_price = float(one_x_two["home"])
                away_price = float(one_x_two["away"])
                if min(home_price, away_price) <= 1.0:
                    raise ValueError("invalid 1X2")
                underdog_price = max(home_price, away_price)
                if underdog_price >= 8.0:
                    return reject(
                        f"brak historii Open Matchday i bardzo wysoki kurs 1X2 "
                        f"na słabszą drużynę ({underdog_price:.2f})"
                    )
            except (KeyError, TypeError, ValueError):
                return reject("brak wiarygodnego rynku 1X2 w analizie awaryjnej")
        implied = 1.0 / market_odds
        league_name = str(league or "Liga nieznana").strip()
        metrics = {
            "home_btts": None, "away_btts": None,
            "home_venue_btts": None, "away_venue_btts": None,
            "home_btts_played": 0, "away_btts_played": 0,
            "home_venue_played": 0, "away_venue_played": 0,
            "home_recent_played": 0, "away_recent_played": 0,
            "home_recent_btts": None, "away_recent_btts": None,
            "home_last5_clean_sheets": None, "away_last5_clean_sheets": None,
            "home_clean_sheets": None, "away_clean_sheets": None,
            "home_failed_to_score": None, "away_failed_to_score": None,
            "home_avg_goals": None, "away_avg_goals": None,
            "home_scored_rate": None, "away_scored_rate": None,
            "home_venue_scored_rate": None, "away_venue_scored_rate": None,
            "home_venue_goals_for_avg": None, "away_venue_goals_for_avg": None,
            "home_last3_venue_btts_count": None, "away_last3_venue_btts_count": None,
            "model_probability": implied, "raw_model_probability": None,
            "implied_probability": implied, "value_edge": 0.0,
            "poisson_probability": None, "expected_home_goals": None,
            "expected_away_goals": None, "sample_reliability": 0.0,
            "home_shots_avg": None, "away_shots_avg": None,
            "home_shots_on_target_avg": None, "away_shots_on_target_avg": None,
            "home_corners_avg": None, "away_corners_avg": None,
            "shots_sample": 0, "shots_on_target_sample": 0, "corners_sample": 0,
            "shots_stats_available": False, "shots_total_stats_available": False,
            "corners_stats_available": False, "league": league_name,
            "xg_available": False, "lineups_available": False,
            "analysis_confidence": "niska — brak historii Open Matchday",
        }
        print(
            f"BTTS ANALYSIS FALLBACK: {home} - {away}: brak historii Open Matchday; "
            f"pozostawiam do oceny rynkowej (BTTS={market_odds:.2f}, "
            f"prawdopodobieństwo implikowane={implied:.1%}).",
            flush=True,
        )
        return {
            "score": 0.50,
            "metrics": metrics,
            "samples": {"home": 0, "away": 0, "home_venue": 0, "away_venue": 0},
            "method": "market_only_no_open_matchday_history",
            "league_folder": "",
        }

    def matches_team(row, team, side):
        return team_name_similarity(row.get(side), team) >= 0.88

    def summarize(rows, team, venue=None):
        selected = []
        for row in rows:
            is_home = matches_team(row, team, "home")
            is_away = matches_team(row, team, "away")
            if venue == "home" and not is_home:
                continue
            if venue == "away" and not is_away:
                continue
            if not is_home and not is_away:
                continue

            fh, fa = as_int(row.get("ft_home")), as_int(row.get("ft_away"))
            if fh is None or fa is None:
                continue
            scored, conceded = (fh, fa) if is_home else (fa, fh)
            shots_for = as_int(row.get("home_shots" if is_home else "away_shots"))
            target_for = as_int(row.get("home_target" if is_home else "away_target"))
            corners_for = as_int(row.get("home_corners" if is_home else "away_corners"))
            selected.append({
                "date": str(row.get("date") or ""),
                "btts": fh > 0 and fa > 0,
                "clean_sheet": conceded == 0,
                "failed_to_score": scored == 0,
                "goals_for": scored,
                "goals_against": conceded,
                "total_goals": fh + fa,
                "shots_for": shots_for,
                "shots_on_target_for": target_for,
                "corners_for": corners_for,
            })

        selected.sort(key=lambda item: item["date"])
        if not selected:
            return {
                "played": 0, "btts": 0.0, "clean_sheets": 0.0,
                "failed_to_score": 1.0, "scored_rate": 0.0,
                "avg_goals": 0.0, "goals_for_avg": 0.0,
                "goals_against_avg": 0.0, "last5": 0,
                "last5_played": 0, "last5_clean_sheets": 0,
                "last3_btts_count": 0, "last3_btts_streak": False,
                "shots_avg": None, "shots_sample": 0,
                "shots_on_target_avg": None, "shots_on_target_sample": 0,
                "corners_avg": None, "corners_sample": 0,
            }

        last5 = selected[-5:]
        last3 = selected[-3:]
        shots = [item["shots_for"] for item in selected if item["shots_for"] is not None]
        shots_on_target = [
            item["shots_on_target_for"] for item in selected
            if item["shots_on_target_for"] is not None
        ]
        corners = [
            item["corners_for"] for item in selected
            if item["corners_for"] is not None
        ]
        return {
            "played": len(selected),
            "btts": sum(item["btts"] for item in selected) / len(selected),
            "clean_sheets": sum(item["clean_sheet"] for item in selected) / len(selected),
            "failed_to_score": sum(item["failed_to_score"] for item in selected) / len(selected),
            "scored_rate": sum(item["goals_for"] > 0 for item in selected) / len(selected),
            "avg_goals": sum(item["total_goals"] for item in selected) / len(selected),
            "goals_for_avg": sum(item["goals_for"] for item in selected) / len(selected),
            "goals_against_avg": sum(item["goals_against"] for item in selected) / len(selected),
            "last5": sum(item["btts"] for item in last5),
            "last5_played": len(last5),
            "btts_count": sum(item["btts"] for item in selected),
            "last5_clean_sheets": sum(item["clean_sheet"] for item in last5),
            "last3_btts_count": sum(item["btts"] for item in last3),
            "last3_btts_streak": len(last3) == 3 and all(item["btts"] for item in last3),
            "shots_avg": sum(shots) / len(shots) if shots else None,
            "shots_sample": len(shots),
            "shots_on_target_avg": sum(shots_on_target) / len(shots_on_target) if shots_on_target else None,
            "shots_on_target_sample": len(shots_on_target),
            "corners_avg": sum(corners) / len(corners) if corners else None,
            "corners_sample": len(corners),
        }

    home_all = summarize(team_rows, home)
    away_all = summarize(team_rows, away)
    home_venue = summarize(team_rows, home, "home")
    away_venue = summarize(team_rows, away, "away")

    # If Open Matchday knows one team but has zero usable matches for the other,
    # use the same explicitly-labelled market-only fallback. Missing one side's
    # archive must not silently disqualify an otherwise listed fixture.
    if min(home_all["played"], away_all["played"]) == 0:
        try:
            market_odds = float(odds)
        except (TypeError, ValueError):
            return reject("brak prawidłowego kursu BTTS do analizy awaryjnej")
        if not 1.55 <= market_odds <= 2.40:
            return reject("kurs BTTS poza zakresem 1.55-2.40 w analizie awaryjnej")
        if isinstance(one_x_two, dict):
            try:
                underdog_price = max(float(one_x_two["home"]), float(one_x_two["away"]))
            except (KeyError, TypeError, ValueError):
                return reject("brak wiarygodnego rynku 1X2 w analizie awaryjnej")
            if underdog_price >= 8.0:
                return reject(
                    f"brak historii jednej z drużyn i bardzo wysoki kurs 1X2 "
                    f"na słabszą drużynę ({underdog_price:.2f})"
                )
        implied = 1.0 / market_odds
        return {
            "score": 0.50,
            "metrics": {
                "model_probability": implied,
                "implied_probability": implied,
                "value_edge": 0.0,
                "sample_reliability": 0.0,
                "league": str(league or "Liga nieznana").strip(),
                "analysis_confidence": "niska — brak historii jednej z drużyn w Open Matchday",
            },
            "samples": {
                "home": home_all["played"], "away": away_all["played"],
                "home_venue": home_venue["played"], "away_venue": away_venue["played"],
            },
            "method": "market_only_no_open_matchday_history",
            "league_folder": "",
        }

    # Przywrócone mocniejsze wymagania sprzed ostatniego poluzowania.
    if min(home_all["played"], away_all["played"]) < 4:
        return reject("za mała próba sezonowa: potrzeba min. 4 meczów na zespół")
    if min(home_venue["played"], away_venue["played"]) < 2:
        return reject("za mała próba dom/wyjazd: potrzeba min. 2 meczów")
    if min(home_all["last5_played"], away_all["last5_played"]) < 3:
        return reject("brak co najmniej 3 meczów w ostatniej próbce")
    if min(home_all["btts"], away_all["btts"]) < 0.30:
        return reject("BTTS sezonowe poniżej 30% u jednej z drużyn")
    if min(home_venue["btts"], away_venue["btts"]) < 0.25:
        return reject("BTTS dom/wyjazd poniżej 25%")
    if min(home_all["last5"], away_all["last5"]) < 1:
        return reject("brak BTTS w ostatnich 5 meczach jednej z drużyn")
    if min(home_venue["last3_btts_count"], away_venue["last3_btts_count"]) < 1:
        return reject("brak BTTS w ostatnich 3 meczach na odpowiednim terenie")
    if min(home_all["scored_rate"], away_all["scored_rate"]) < 0.50:
        return reject("jedna z drużyn strzelała w mniej niż 50% meczów")
    if min(home_venue["scored_rate"], away_venue["scored_rate"]) < 0.50:
        return reject("skuteczność strzelania dom/wyjazd poniżej 50%")
    if max(home_all["failed_to_score"], away_all["failed_to_score"]) > 0.60:
        return reject("jedna z drużyn zbyt często nie strzela gola")
    if max(home_all["clean_sheets"], away_all["clean_sheets"]) > 0.60:
        return reject("jedna z drużyn zbyt często zachowuje czyste konto")
    if min(home_all["avg_goals"], away_all["avg_goals"]) < 1.6:
        return reject("średnia liczba goli w meczach drużyny poniżej 1.6")

    estimate = btts_probability_estimate(home_all, away_all, home_venue, away_venue)
    probability = estimate["probability"]
    try:
        price = float(odds)
    except (TypeError, ValueError):
        return reject("brak prawidłowego kursu BTTS")
    implied_probability = 1.0 / price if price > 1.0 else 1.0
    edge = probability - implied_probability
    if not btts_value_gate(probability, price):
        return reject(
            f"brak przewagi modelu: model={probability:.1%}, "
            f"kurs={price:.2f}, implied={implied_probability:.1%}, edge={edge:.1%}"
        )

    league_name = str(league or "").strip()
    if not league_name:
        league_name = str(team_rows[0].get("_league") or "Liga nieznana")

    metrics = {
        "home_btts": home_all["btts"],
        "away_btts": away_all["btts"],
        "home_venue_btts": home_venue["btts"],
        "away_venue_btts": away_venue["btts"],
        "home_btts_played": home_all["played"],
        "away_btts_played": away_all["played"],
        "home_venue_played": home_venue["played"],
        "away_venue_played": away_venue["played"],
        "home_recent_played": home_all["last5_played"],
        "away_recent_played": away_all["last5_played"],
        "home_recent_btts": home_all["last5"],
        "away_recent_btts": away_all["last5"],
        "home_last5_clean_sheets": home_all["last5_clean_sheets"],
        "away_last5_clean_sheets": away_all["last5_clean_sheets"],
        "home_clean_sheets": home_all["clean_sheets"],
        "away_clean_sheets": away_all["clean_sheets"],
        "home_failed_to_score": home_all["failed_to_score"],
        "away_failed_to_score": away_all["failed_to_score"],
        "home_avg_goals": home_all["avg_goals"],
        "away_avg_goals": away_all["avg_goals"],
        "home_scored_rate": home_all["scored_rate"],
        "away_scored_rate": away_all["scored_rate"],
        "home_venue_scored_rate": home_venue["scored_rate"],
        "away_venue_scored_rate": away_venue["scored_rate"],
        "home_venue_goals_for_avg": home_venue["goals_for_avg"],
        "away_venue_goals_for_avg": away_venue["goals_for_avg"],
        "home_last3_venue_btts_count": home_venue["last3_btts_count"],
        "away_last3_venue_btts_count": away_venue["last3_btts_count"],
        "model_probability": probability,
        "raw_model_probability": estimate["raw_probability"],
        "implied_probability": implied_probability,
        "value_edge": edge,
        "poisson_probability": estimate["poisson_probability"],
        "expected_home_goals": estimate["lambda_home"],
        "expected_away_goals": estimate["lambda_away"],
        "sample_reliability": estimate["reliability"],
        "home_shots_avg": home_all["shots_avg"],
        "away_shots_avg": away_all["shots_avg"],
        "home_shots_on_target_avg": home_all["shots_on_target_avg"],
        "away_shots_on_target_avg": away_all["shots_on_target_avg"],
        "home_corners_avg": home_all["corners_avg"],
        "away_corners_avg": away_all["corners_avg"],
        "shots_sample": estimate["shots_sample"],
        "shots_on_target_sample": estimate["shots_on_target_sample"],
        "corners_sample": estimate["corners_sample"],
        "shots_stats_available": estimate["shots_on_target_sample"] >= 3,
        "shots_total_stats_available": estimate["shots_sample"] >= 3,
        "corners_stats_available": estimate["corners_sample"] >= 3,
        "league": league_name,
        "xg_available": False,
        "lineups_available": False,
    }
    risk_ok, risk_reason = btts_1x2_risk_gate(one_x_two, metrics)
    if not risk_ok:
        return reject(risk_reason)

    score = min(1.0, probability + max(0.0, min(0.05, edge)) * 0.10)
    return {
        "score": score,
        "metrics": metrics,
        "samples": {
            "home": home_all["played"],
            "away": away_all["played"],
            "home_venue": home_venue["played"],
            "away_venue": away_venue["played"],
        },
        "method": "open_matchday_btts_ft_value_v5",
        "league_folder": next((
            str(row.get("_folder") or "")
            for row in team_rows
            if team_name_similarity(row.get("home"), home) >= 0.88
            or team_name_similarity(row.get("away"), home) >= 0.88
        ), ""),
    }

def log_superbet_market_diagnostics(event_detail, fixture, max_rows=100):
    """Log real Superbet market/selection labels and prices; never log credentials."""
    market_keys = (
        "marketName", "market_name", "marketGroupName", "market_group_name",
        "marketTitle", "market_title", "market", "marketGroup", "market_group",
        "title", "groupName", "group_name",
    )
    group_keys = (
        "marketGroupId", "market_group_id", "marketGroupID",
        "groupId", "group_id", "marketId", "market_id",
    )
    selection_keys = (
        "name", "selectionName", "selection_name", "oddName",
        "outcomeName", "label", "displayName",
    )
    price_keys = ("price", "odds", "oddValue", "odd_value", "oddsValue", "odds_dec")

    def label(value):
        if isinstance(value, dict):
            for key in ("name", "title", "label", "description", "displayName"):
                if value.get(key) not in (None, ""):
                    return str(value[key]).strip()
            return ""
        return str(value).strip() if value not in (None, "") else ""

    rows = []
    def walk(node, inherited_market="", inherited_group=None):
        if isinstance(node, list):
            for child in node:
                walk(child, inherited_market, inherited_group)
            return
        if not isinstance(node, dict):
            return
        local_market = " ".join(
            label(node.get(key)) for key in market_keys if node.get(key) not in (None, "")
        ).strip()
        market = local_market or inherited_market
        group_id = next((node.get(key) for key in group_keys
                         if node.get(key) not in (None, "")), inherited_group)
        selection = next((label(node.get(key)) for key in selection_keys
                          if node.get(key) not in (None, "")), "")
        metadata = node.get("metadata")
        if not selection and isinstance(metadata, dict):
            selection = label(metadata.get("name") or metadata.get("label"))
        price = next((node.get(key) for key in price_keys
                      if node.get(key) not in (None, "")), None)
        if selection and price is not None:
            rows.append({
                "market": market[:180],
                "market_group_id": group_id,
                "selection": selection[:100],
                "metadata_name": label(metadata.get("name")) if isinstance(metadata, dict) else "",
                "price": str(price)[:30],
                "status": str(first(node, "status", "oddStatus", "state", default=""))[:40],
            })
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, market, group_id)

    walk(event_detail)
    diagnostic = {
        "event": f"{fixture.get('home', '')} - {fixture.get('away', '')}",
        "event_id": str(fixture.get("id", "")).replace("superbet:", "", 1),
        "market_rows_count": len(rows),
        "markets": rows[:max_rows],
        "truncated": len(rows) > max_rows,
    }
    print("SUPERBET MARKET DIAGNOSTIC: " +
          json.dumps(diagnostic, ensure_ascii=False, default=str)[:12000], flush=True)




def btts_1x2_risk_gate(one_x_two, metrics):
    """Reject heavily priced underdogs unless their scoring record supports BTTS."""
    if not isinstance(one_x_two, dict):
        return True, ""
    try:
        home_price = float(one_x_two["home"])
        away_price = float(one_x_two["away"])
    except (KeyError, TypeError, ValueError):
        return False, "nieprawidłowy rynek 1X2 — brak wiarygodnej oceny faworyta"
    if home_price <= 1.0 or away_price <= 1.0:
        return False, "nieprawidłowe kursy 1X2"

    if home_price >= away_price:
        underdog_price = home_price
        scored_rate = metrics.get("home_scored_rate")
        venue_scored_rate = metrics.get("home_venue_scored_rate")
        venue_goals_avg = metrics.get("home_venue_goals_for_avg")
        underdog_side = "gospodarze"
    else:
        underdog_price = away_price
        scored_rate = metrics.get("away_scored_rate")
        venue_scored_rate = metrics.get("away_venue_scored_rate")
        venue_goals_avg = metrics.get("away_venue_goals_for_avg")
        underdog_side = "goście"

    if underdog_price >= 8.0:
        return False, (
            f"odrzut 1X2: kurs na {underdog_side} {underdog_price:.2f} >= 8.00 — "
            "zbyt duże ryzyko, że słabsza drużyna nie strzeli gola"
        )
    if underdog_price >= 6.0:
        try:
            scored_rate = float(scored_rate)
            venue_scored_rate = float(venue_scored_rate)
            venue_goals_avg = float(venue_goals_avg)
        except (TypeError, ValueError):
            return False, f"kurs na {underdog_side} {underdog_price:.2f}; brak wystarczających danych ofensywnych"
        if scored_rate < 0.65 or venue_scored_rate < 0.65 or venue_goals_avg < 0.80:
            return False, (
                f"kurs na {underdog_side} {underdog_price:.2f}; słaba ofensywa słabszej drużyny "
                f"(strzela ogółem {scored_rate:.0%}, na swoim terenie {venue_scored_rate:.0%}, "
                f"średnia goli {venue_goals_avg:.2f})"
            )
    return True, ""


def btts_prematch_odds_gate(btts_quote):
    """Require standalone FT BTTS YES odds in the existing 1.55-2.40 range."""
    if not isinstance(btts_quote, dict):
        return False
    try:
        btts_odds = float(btts_quote.get("odds"))
    except (TypeError, ValueError):
        return False
    return 1.55 <= btts_odds <= 2.40


def _send_prematch_superbet_btts_picks_for_date(state, scan_date):
    """Scan and send prematch BTTS FT picks for one Warsaw-local calendar date."""
    today = scan_date
    day_key = today.isoformat()
    if day_key in set(state.get("daily_btts_closers_sent", [])):
        print(f"BTTS FT: dzień {day_key} ma już grafikę zamknięcia; nie dokładam nowych typów.", flush=True)
        return
    events = superbet_prematch_event_links(target_date=today)
    if not events:
        print(f"Superbet BTTS FT: brak wydarzeń na {today.isoformat()}; bez fallbacku do innego bukmachera.", flush=True)
        return
    fixtures = scheduled_fixtures_today(events, [today])
    event_by_id = {str(e.get("event_id")): e for e in events if e.get("event_id")}
    candidates = []
    diagnostic_events_logged = 0
    for fixture in fixtures:
        event_id = str(fixture.get("id", "")).replace("superbet:", "", 1)
        event = event_by_id.get(event_id)
        if not event:
            continue
        try:
            detail = superbet_event_detail(event_id)
            # Temporary read-only diagnostic: capture actual Polish Superbet
            # market names, group IDs, selections, prices and statuses.
            # It does not change pick eligibility or Telegram delivery.
            if diagnostic_events_logged < 8:
                log_superbet_market_diagnostics(detail, fixture)
                diagnostic_events_logged += 1
            one_x_two = extract_superbet_1x2_full_time(detail)
            # Ryzyko słabszej drużyny oceniamy razem z jej statystykami ofensywnymi poniżej.
            quote = extract_superbet_btts_full_time(detail)
        except Exception as exc:
            print(f"Superbet BTTS FT: błąd kursu {fixture['home']} - {fixture['away']}: {type(exc).__name__}: {exc}", flush=True)
            continue
        if not quote or not btts_prematch_odds_gate(quote):
            btts_price = (quote or {}).get("odds")
            print(
                f"Superbet BTTS FT: odrzucam {fixture['home']} - {fixture['away']} — "
                f"wymagany wyłącznie rynek BTTS Tak FT, kurs 1.55-2.40 "
                f"(BTTS={btts_price}).",
                flush=True,
            )
            continue
        kickoff = kickoff_datetime({"kickoff_time": fixture.get("_bot_kickoff")})
        if not kickoff or kickoff <= now_local() or kickoff.date() != today:
            continue
        # Use league metadata when Superbet provides it. If it is missing,
        # keep the pick: the league line will simply be omitted from Telegram.
        league_source = (
            extract_superbet_competition_name(detail)
            or extract_superbet_competition_name(event)
            or fixture.get("competition_name")
            or ""
        )
        if isinstance(league_source, str) and league_source.strip():
            league = format_fixture_competition(league_source.strip(), fixture["home"], fixture["away"])
            if league and league.strip().lower() not in {"liga nieznana", "unknown league", "unknown"}:
                league = league.strip()
            else:
                league = ""
        else:
            league = ""
        league_source_name = league
        if not league:
            league, league_source_name = resolve_btts_competition(
                fixture["home"], fixture["away"], kickoff, league_source
            )
        if not league:
            print(
                f"Superbet BTTS FT: nie udało się wiarygodnie ustalić ligi dla "
                f"{fixture['home']} - {fixture['away']}; typ pominięty — nie wysyłam "
                f"wiadomości z 'Liga nieznana'.",
                flush=True,
            )
            continue
        print(
            f"Superbet BTTS FT: liga rozpoznana dla {fixture['home']} - {fixture['away']}: "
            f"{league} (źródło: {league_source_name}).",
            flush=True,
        )
        if same_fixture_pick_exists(state, fixture["id"], fixture["home"], fixture["away"], today.isoformat(), BTTS_FT_MARKET, competition=league):
            continue
        bet_url = superbet_btts_full_time_url(event.get("url"), quote)
        if not bet_url or not bet_url.startswith("https://superbet.pl/"):
            print(f"Superbet BTTS FT: brak linku do meczu {fixture['home']} - {fixture['away']}; pomijam.", flush=True)
            continue
        analysis = btts_recent_form_analysis(fixture["home"], fixture["away"], kickoff, league, quote["odds"], one_x_two)
        if not analysis:
            continue
        # Ostateczna blokada: typ bez prawdziwej nazwy ligi nie może trafić
        # do Telegrama ani do listy kandydatów.
        if not league or league.strip().casefold() in {"unknown", "unknown league", "liga nieznana"}:
            print(
                f"Superbet BTTS FT: pomijam {fixture['home']} - {fixture['away']} — "
                "brak potwierdzonej ligi przed publikacją.",
                flush=True,
            )
            continue
        candidates.append({"id": fixture["id"], "event_id": event_id, "home": fixture["home"], "away": fixture["away"], "league": league, "kickoff": kickoff, "quote": quote, "url": bet_url, "analysis": analysis, "competition_category": fixture_category_signature(fixture["home"], fixture["away"], league)})
    candidates.sort(key=lambda x: (-x["analysis"]["score"], x["kickoff"], x["home"], x["away"]))
    print(f"Superbet BTTS FT: {len(candidates)} kandydatów po zaostrzonym filtrze BTTS FT 1.55-2.40, historii i przewagi nad kursem.", flush=True)
    day_key = today.isoformat()
    sent_today = btts_ft_sent_picks_on_day(state, day_key)
    # Older scheduled tips may have been posted days before their kickoff date.
    # They must not consume today's cap or today's numbering.
    for corrected_number, old_pick in enumerate(sent_today, 1):
        # A manually requested number override takes precedence over automatic daily numbering.
        target_number = int(old_pick.get("pick_number_override") or corrected_number)
        old_number = old_pick.get("pick_number")
        if old_number != target_number:
            message_id = old_pick.get("telegram_message_id")
            if message_id:
                try:
                    telegram_edit_btts_pick(
                        format_btts_ft_pick_message(old_pick, target_number),
                        message_id,
                    )
                    old_pick["pick_number"] = target_number
                    save_state(state)
                    print(f"Skorygowano numer BTTS FT: {old_pick.get('home')} - {old_pick.get('away')} → TYP NR. {target_number}.", flush=True)
                except Exception as exc:
                    print(f"Nie udało się poprawić numeru starej wiadomości BTTS FT: {type(exc).__name__}: {exc}", flush=True)
    sent_today = btts_ft_sent_picks_on_day(state, day_key)
    sent_count = len(sent_today)
    next_number = max((int(p.get("pick_number") or 0) for p in sent_today), default=0) + 1
    for candidate in candidates:
        if sent_count >= MAX_DAILY_BTTS_FT_PICKS:
            print(f"Limit {MAX_DAILY_BTTS_FT_PICKS} BTTS FT osiągnięty dla dnia wysyłki {day_key}; kończę wysyłkę.", flush=True)
            break
        try:
            final_detail = superbet_event_detail(candidate["event_id"])
            final_1x2 = extract_superbet_1x2_full_time(final_detail)
            final_quote = extract_superbet_btts_full_time(final_detail)
        except Exception as exc:
            print(f"Końcowa kontrola Superbet nieudana: {type(exc).__name__}: {exc}", flush=True)
            continue
        final_risk_ok, final_risk_reason = btts_1x2_risk_gate(final_1x2, candidate["analysis"]["metrics"])
        if (not final_risk_ok
                or not final_quote or not btts_prematch_odds_gate(final_quote)
                or abs(float(final_quote["odds"]) - float(candidate["quote"]["odds"])) > 0.005):
            print(f"Superbet: kurs/rynek zmienił się przed wysyłką — pomijam {candidate['home']} - {candidate['away']}.", flush=True)
            continue
        if same_fixture_pick_exists(state, candidate["id"], candidate["home"], candidate["away"], day_key, BTTS_FT_MARKET, competition=candidate["league"]):
            continue
        # A separate day header is sent once, immediately before the first
        # actual pick of this Warsaw-local date. The persisted date key prevents
        # duplicates across the five-minute scheduled workflow runs.
        headers_sent = state.setdefault("daily_headers_sent", [])
        if day_key not in headers_sent:
            header = f"🚀 𝗭𝗔𝗖𝗭𝗬𝗡𝗔𝗠𝗬 𝗞𝗢𝗟𝗘𝗝𝗡𝗬 𝗗𝗭𝗜𝗘Ń 𝗭 𝗧𝗬𝗣𝗔𝗠𝗜!\n📅 {today.strftime('%d.%m.%Y')}\n━━━━━━━━━━━━━━━━━━\n⚽ BTTS TAK — CAŁY MECZ (FT)\n━━━━━━━━━━━━━━━━━━"
            try:
                header_result = telegram(header)
                header_message = header_result.get("result", {}) if isinstance(header_result, dict) else {}
                if isinstance(header_message, list):
                    header_message = header_message[0] if header_message else {}
                headers_sent.append(day_key)
                state.setdefault("daily_header_messages", {})[day_key] = header_message.get("message_id")
                save_state(state)
                print(f"WYSŁANO NAGŁÓWEK DNIA {day_key}; message_id={header_message.get('message_id')}", flush=True)
            except Exception as exc:
                print(f"Nie udało się wysłać nagłówka dnia {day_key}: {type(exc).__name__}: {exc}; wstrzymuję typy do kolejnego skanu.", flush=True)
                return
        pick_number = next_number
        pick = {"key": f"{candidate['id']}:{BTTS_FT_MARKET}", "id": candidate["id"], "slug": "", "home": candidate["home"], "away": candidate["away"], "league": candidate["league"], "competition_category": candidate["competition_category"], "date": day_key, "kickoff_time": candidate["kickoff"].isoformat(), "market": BTTS_FT_MARKET, "stake": STAKE, "odds": float(final_quote["odds"]), "odds_source": "Superbet", "odds_market": final_quote["market"], "odds_updated_at": final_quote.get("updated_at"), "bet_url": candidate["url"], "analysis": candidate["analysis"], "settled": False, "delivery_status": "pending", "pick_number": pick_number}
        state.setdefault("picks", []).append(pick)
        state.setdefault("duplicate_guard", []).append(pick)
        save_state(state)
        payout = calculate_bet_result(STAKE, final_quote["odds"])
        message = format_btts_ft_pick_message(pick, pick_number)
        try:
            image_cursor = int(state.get("btts_image_cursor", 0)) % len(BTTS_IMAGE_POOL)
            response = telegram_btts_ft(message, image_cursor)
            state["btts_image_cursor"] = (image_cursor + 1) % len(BTTS_IMAGE_POOL)
            result = response.get("result", []) if isinstance(response, dict) else []
            if isinstance(result, list) and result:
                pick["telegram_message_id"] = result[0].get("message_id")
            elif isinstance(result, dict):
                pick["telegram_message_id"] = result.get("message_id")
            pick["delivery_status"] = "sent"
            pick["sent_at"] = now_local().isoformat()
            save_state(state)
            sent_count += 1
            next_number += 1
            print(f"WYSŁANO BTTS FT Superbet TYP NR. {pick_number}: {candidate['home']} - {candidate['away']} @ {final_quote['odds']:.2f}", flush=True)
        except Exception as exc:
            pick["delivery_status"] = "failed"
            pick["delivery_error"] = f"{type(exc).__name__}: {exc}"
            save_state(state)
            print(f"Telegram Superbet BTTS FT error: {type(exc).__name__}: {exc}", flush=True)
            return


def send_prematch_superbet_btts_picks(state):
    """Scan fixtures scheduled only for the current local day."""
    today = now_local().date()
    scan_dates = (today,)
    for scan_date in scan_dates:
        print(
            f"BTTS FT: rozpoczynam skan daty {scan_date.isoformat()} "
            f"(tylko dzisiaj; limit 10 typów).",
            flush=True,
        )
        try:
            _send_prematch_superbet_btts_picks_for_date(state, scan_date)
        except Exception as exc:
            print(
                f"BTTS FT: błąd skanowania daty {scan_date.isoformat()}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )


def send_manual_archive_resend(state):
    """Send one explicitly queued historical pick once, clearly marked as archive."""
    item = state.get("manual_archive_resend")
    if not item:
        return
    home, away = item["home"], item["away"]
    message = (
        "🧪 ARCHIWALNY TEST WYSYŁKI — MECZ ZAKOŃCZONY\n"
        f"⚽ {home} – {away}\n"
        f"🏆 Liga: {item.get('league', 'Brazylia')}\n"
        f"🎯 Rynek: {item.get('market', 'typ archiwalny')}\n"
        f"📈 Kurs z wcześniejszego typu: {float(item.get('odds', 0)):.2f}\n"
        f"🏁 Wynik końcowy: {item.get('result', 'brak')}\n"
        "To wyłącznie test wysyłki i herbów — nie jest aktualnym typem."
    )
    image_cursor = int(state.get("btts_image_cursor", 0)) % len(BTTS_IMAGE_POOL)
    response = telegram_btts_ft(message, BTTS_IMAGE_POOL[image_cursor])
    state["btts_image_cursor"] = (image_cursor + 1) % len(BTTS_IMAGE_POOL)
    save_state(state)
    result = response.get("result", []) if isinstance(response, dict) else []
    message_id = None
    if isinstance(result, list) and result:
        message_id = result[0].get("message_id")
    elif isinstance(result, dict):
        message_id = result.get("message_id")
    state.pop("manual_archive_resend", None)
    state["last_manual_archive_resend"] = {
        "home": home, "away": away, "sent_at": now_local().isoformat(),
        "telegram_message_id": message_id,
    }
    save_state(state)
    print(f"WYSŁANO ARCHIWALNY TEST: {home} - {away}; message_id={message_id}", flush=True)

def repair_sent_btts_league_captions(state):
    """Edit already-published Telegram captions with verified Polish competition names."""
    fixes = {
        ("aguila", "municipal limeno"): "Salwadorska Primera División",
        ("atletico balboa", "cd inca"): "Salwadorska Primera División",
        ("academia puerto cabello", "monagas"): "Wenezuelska Primera División — Clausura, grupa B",
        ("aurora fc", "mixco"): "Gwatemalska Liga Narodowa",
        ("colon santa fe (r)", "central cordoba (r)"): "Argentyńska Liga Rezerw — Torneo Proyección",
        ("ceara b", "atletico ce"): "Brazylijski Puchar Fares Lopes",
    }
    for pick in state.get("picks", []):
        if pick.get("market") != BTTS_FT_MARKET or pick.get("delivery_status") != "sent":
            continue
        if not pick.get("telegram_message_id") or not pick.get("league_caption_repair_pending"):
            continue
        key = (str(pick.get("home") or "").strip().casefold(),
               str(pick.get("away") or "").strip().casefold())
        league = fixes.get(key)
        if not league:
            continue
        pick["league"] = league
        pick.setdefault("analysis", {}).setdefault("metrics", {})["league"] = league
        try:
            message = format_btts_ft_pick_message(pick, int(pick.get("pick_number") or 1))
            telegram_edit_btts_pick(message, pick["telegram_message_id"])
            pick["league_caption_repair_pending"] = False
            pick["league_caption_repaired_at"] = now_local().isoformat()
            save_state(state)
            print(f"POPRAWIONO LIGĘ W TELEGRAMIE: {pick.get('home')} - {pick.get('away')} → {league}; message_id={pick.get('telegram_message_id')}", flush=True)
        except Exception as exc:
            print(f"Nie udało się poprawić ligi w Telegramie dla {pick.get('home')} - {pick.get('away')}: {type(exc).__name__}: {exc}", flush=True)


def main():
    print(f"=== TyperGOLE-OVERY PRE-MATCH BTTS GROUP — OVER 1.5 HT START {now_local().isoformat()} ===", flush=True)
    print(f"Telegram secrets: token={'OK' if TELEGRAM_TOKEN else 'BRAK'}, chat_id={'OK' if CHAT_ID else 'BRAK'}", flush=True)
    state = load_state()
    try:
        repair_sent_btts_league_captions(state)
    except Exception as exc:
        print(f"Błąd poprawiania nazw lig w już wysłanych typach: {type(exc).__name__}: {exc}", flush=True)
    try:
        send_manual_archive_resend(state)
    except Exception as exc:
        print(f"Nie udało się wysłać archiwalnego testu: {type(exc).__name__}: {exc}", flush=True)
    if state.get("telegram_test_pending"):
        telegram("🧪 TEST TELEGRAM — TyperGOLE-OVERY. To nie jest typ.")
        state["telegram_test_pending"] = False
        save_state(state)
    print(f"Historia typów: {len(state['picks'])}; nierozliczone: {sum(1 for p in state['picks'] if not p.get('settled'))}", flush=True)

    # New bot is FT BTTS only. Historical BTTS picks remain available for settlement.
    settle_picks(state, allowed_markets={BTTS_FT_MARKET})
    retry_pending_settlement_notifications(state)

    # Existing sent picks are left unchanged; new BTTS FT picks use Superbet links.
    try:
        send_prematch_superbet_btts_picks(state)
    except Exception as exc:
        print(f"Superbet BTTS FT scanner error: {type(exc).__name__}: {exc}", flush=True)
    try:
        send_daily_btts_closers(state)
    except Exception as exc:
        print(f"Daily BTTS closer error: {type(exc).__name__}: {exc}", flush=True)
    try:
        send_monthly_reports(state)
    except Exception as exc:
        print(f"Monthly report error: {type(exc).__name__}: {exc}", flush=True)
    save_state(state)
    print("=== TyperGOLE-OVERY PRE-MATCH BTTS GROUP — OVER 1.5 HT END ===", flush=True)


if __name__ == "__main__":
    main()
# Manual live scan trigger: 2026-11-06
# Duplicate guard reset: fresh picks only; historical settlement records retained.
# Daily counter reset: sent historical picks flagged false do not consume today's cap.
# Trigger a fresh scan after resetting BTTS caps for both scan dates.
