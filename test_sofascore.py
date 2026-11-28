"""One-shot connectivity check for SofaScore's public live-events endpoint.

This is a diagnostic only: it makes one request, does not retry blocked responses,
and does not feed match data into the production betting scanner.
"""
import json
from curl_cffi import requests

URL = "https://www.sofascore.com/api/v1/event/live"

def main():
    try:
        response = requests.get(
            URL,
            impersonate="chrome",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        print(f"HTTP status: {response.status_code}")
        if response.status_code in (403, 429):
            print("SofaScore denied or rate-limited this request; stopping without retries.")
            return
        response.raise_for_status()
        payload = response.json()
        events = payload.get("events", []) if isinstance(payload, dict) else []
        print(f"JSON OK; live events: {len(events)}")
        if events:
            event = events[0]
            home = (event.get("homeTeam") or {}).get("name", "?")
            away = (event.get("awayTeam") or {}).get("name", "?")
            print(f"Sample fixture: {home} - {away}")
    except Exception as exc:
        print(f"SofaScore test failed: {type(exc).__name__}: {exc}")

if __name__ == "__main__":
    main()
