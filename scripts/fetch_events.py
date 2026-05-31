#!/usr/bin/env python3
"""
Fetch events from austinridgeriders.com and write events.json.

Tries three methods in order of reliability:
  1. Squarespace iCal feed  (?format=ical)
  2. Squarespace JSON API   (?format=json)
  3. HTML scrape            (BeautifulSoup)

On success, writes/updates events.json in the repo root (or the path
given as the first CLI argument).  On total failure, exits non-zero so
the CI step is marked failed but the deploy step can still continue with
the existing cached data (continue-on-error: true in the workflow).
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL   = "https://www.austinridgeriders.com"
EVENTS_URL = f"{BASE_URL}/events"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def to_iso(dt) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


# ── Method 1: iCal ──────────────────────────────────────────────────────────

def fetch_ical() -> list[dict] | None:
    try:
        from icalendar import Calendar  # type: ignore
    except ImportError:
        print("icalendar package not available — skipping iCal method")
        return None

    url = f"{EVENTS_URL}?format=ical"
    print(f"Trying iCal feed: {url}")
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()

        cal    = Calendar.from_ical(resp.content)
        events = []

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            dtstart = component.get("dtstart")
            dtend   = component.get("dtend")
            if not dtstart:
                continue

            url_val  = str(component.get("url", "") or "")
            location = str(component.get("location", "") or "").strip()
            desc     = strip_html(str(component.get("description", "") or ""))

            events.append({
                "title":       str(component.get("summary", "")).strip(),
                "start":       to_iso(dtstart.dt),
                "end":         to_iso(dtend.dt) if dtend else None,
                "description": desc,
                "location":    location,
                "url":         url_val,
            })

        events.sort(key=lambda x: x["start"] or "")
        print(f"  iCal: got {len(events)} events")
        return events if events else None

    except Exception as exc:
        print(f"  iCal failed: {exc}")
        return None


# ── Method 2: Squarespace JSON API ───────────────────────────────────────────

def fetch_json_api() -> list[dict] | None:
    url = f"{EVENTS_URL}?format=json"
    print(f"Trying JSON API: {url}")
    try:
        resp = requests.get(
            url,
            headers={**BROWSER_HEADERS, "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data  = resp.json()
        items = data.get("items", [])
        if not items:
            print("  JSON API: no items returned")
            return None

        events = []
        for item in items:
            start_ms = item.get("startDate") or item.get("publishOn")
            end_ms   = item.get("endDate")
            if not start_ms:
                continue

            start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
            end_dt   = (
                datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
                if end_ms else None
            )

            # Build location string from structured data
            location = ""
            if item.get("location"):
                loc   = item["location"]
                parts = [
                    loc.get("addressLine1", ""),
                    loc.get("addressLine2", ""),
                    loc.get("city", ""),
                    loc.get("state", ""),
                ]
                location = ", ".join(p for p in parts if p)

            events.append({
                "title":       item.get("title", "").strip(),
                "start":       start_dt.isoformat(),
                "end":         end_dt.isoformat() if end_dt else None,
                "description": strip_html(item.get("excerpt", "")),
                "location":    location,
                "url":         BASE_URL + item.get("fullUrl", ""),
            })

        events.sort(key=lambda x: x["start"])
        print(f"  JSON API: got {len(events)} events")
        return events if events else None

    except Exception as exc:
        print(f"  JSON API failed: {exc}")
        return None


# ── Method 3: HTML scrape ────────────────────────────────────────────────────

def fetch_html() -> list[dict] | None:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        print("beautifulsoup4 not available — skipping HTML scrape")
        return None

    print(f"Trying HTML scrape: {EVENTS_URL}")
    try:
        resp = requests.get(EVENTS_URL, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        events: list[dict] = []

        # Squarespace event list — try multiple known selectors
        event_els = []
        for sel in (
            ".eventlist-event",
            ".summary-item[data-event]",
            ".summary-item",
            "article[class*='event']",
        ):
            event_els = soup.select(sel)
            if event_els:
                break

        print(f"  Found {len(event_els)} candidate elements")

        for el in event_els:
            # Title + URL
            title_a = el.select_one(
                ".eventlist-title a, .summary-title a, h1 a, h2 a, h3 a"
            )
            if not title_a:
                continue
            title = title_a.get_text(strip=True)
            href  = title_a.get("href", "")
            url   = href if href.startswith("http") else BASE_URL + href

            # Date — prefer machine-readable datetime attribute
            date_el  = el.select_one("time[datetime], .eventlist-datetag-startdate, [class*='date']")
            date_str = ""
            if date_el:
                date_str = date_el.get("datetime") or date_el.get_text(strip=True)

            # Time
            time_el  = el.select_one(".event-time-12hr, .eventlist-meta-time, [class*='time']")
            time_str = time_el.get_text(strip=True) if time_el else ""

            # Location
            loc_el   = el.select_one(
                ".eventlist-address, .eventlist-meta-address, [class*='location'], [class*='address']"
            )
            location = loc_el.get_text(strip=True) if loc_el else ""

            # Description
            desc_el  = el.select_one(".eventlist-description, .summary-excerpt, [class*='excerpt']")
            desc     = strip_html(desc_el.get_text()) if desc_el else ""

            events.append({
                "title":       title,
                "start":       date_str,
                "time":        time_str,
                "description": desc,
                "location":    location,
                "url":         url,
            })

        print(f"  HTML scrape: got {len(events)} events")
        return events if events else None

    except Exception as exc:
        print(f"  HTML scrape failed: {exc}")
        return None


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("events.json")

    events = fetch_ical()
    if not events:
        events = fetch_json_api()
    if not events:
        events = fetch_html()

    if events is None:
        print("\nAll fetch methods failed. Keeping existing events.json.")
        return 1

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "source":  EVENTS_URL,
        "events":  events,
    }

    output_path.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nWrote {len(events)} events → {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
