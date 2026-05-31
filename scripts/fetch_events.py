#!/usr/bin/env python3
"""
Fetch events from austinridgeriders.com and write events.json.

Tries three methods in order of reliability:
  1. Squarespace iCal feed  (?format=ical)
  2. Squarespace JSON API   (?format=json)
  3. HTML scrape            (BeautifulSoup)
"""

import json
import re
import sys
from datetime import date, datetime, timezone
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

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


# ── Date helpers ─────────────────────────────────────────────────────────────

def _infer_year(month: int, day: int, url: str = "") -> int:
    """Infer a year for a bare month+day.

    Priority:
      1. Explicit 4-digit year found in the event URL (e.g. cranksgiving-2024)
      2. The candidate year (this year ± 1) whose date is closest to today
    """
    m = re.search(r"(20\d{2})", url)
    if m:
        return int(m.group(1))

    today = date.today()
    best_year, best_delta = today.year, None
    for y in range(today.year - 2, today.year + 2):
        try:
            candidate = date(y, month, day)
            delta = abs((candidate - today).days)
            if best_delta is None or delta < best_delta:
                best_year, best_delta = y, delta
        except ValueError:
            pass
    return best_year


def parse_display_date(text: str, url: str = "") -> str | None:
    """Convert a human-readable date string to YYYY-MM-DD.

    Handles:
      - ISO dates already:   "2026-06-15", "2026-06-15T09:00:00-05:00"
      - Month+Day no year:   "Feb7", "Oct 26", "February 3"
      - Common full formats: "February 7, 2026", "Feb 7 2026"
    """
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return None

    # Already ISO
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)

    # Full date with year
    for fmt in (
        "%B %d, %Y", "%b %d, %Y",
        "%B %d %Y",  "%b %d %Y",
        "%m/%d/%Y",  "%Y/%m/%d",
        "%d %B %Y",  "%d %b %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass

    # Month+Day without year: "Feb7", "Feb 7", "February 7", etc.
    m = re.match(r"^([A-Za-z]+)\s*(\d{1,2})\s*$", text)
    if m:
        month_key = m.group(1).lower()
        day = int(m.group(2))
        month = MONTH_MAP.get(month_key) or MONTH_MAP.get(month_key[:3])
        if month:
            year = _infer_year(month, day, url)
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                pass

    return None


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def to_iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


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

        # If the server returned HTML instead of iCal, skip
        ct = resp.headers.get("Content-Type", "")
        if "text/html" in ct:
            print("  iCal: server returned HTML — feed not available")
            return None

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

        # Guard against HTML response
        ct = resp.headers.get("Content-Type", "")
        if "text/html" in ct:
            print("  JSON API: server returned HTML — endpoint not available")
            return None

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

            location = ""
            if item.get("location"):
                loc   = item["location"]
                parts = [
                    loc.get("addressLine1", ""), loc.get("addressLine2", ""),
                    loc.get("city", ""),         loc.get("state", ""),
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
            # ── Title + URL ──────────────────────────────────────────────────
            title_a = el.select_one(
                ".eventlist-title a, .summary-title a, h1 a, h2 a, h3 a"
            )
            if not title_a:
                continue
            title = title_a.get_text(strip=True)
            href  = title_a.get("href", "")
            event_url = href if href.startswith("http") else BASE_URL + href

            # ── Date ─────────────────────────────────────────────────────────
            # Prefer a <time datetime="..."> attribute; fall back to text.
            # For Squarespace, the datetag has separate month/day child spans —
            # we read month and day separately to avoid "Feb7" concatenation.
            date_str = ""

            time_tag = el.select_one("time[datetime]")
            if time_tag:
                date_str = time_tag.get("datetime", "")
            else:
                month_el = el.select_one(
                    ".eventlist-datetag-startdate-month, [class*='month']"
                )
                day_el   = el.select_one(
                    ".eventlist-datetag-startdate-day, [class*='startdate-day']"
                )
                if month_el and day_el:
                    date_str = (
                        month_el.get_text(strip=True)
                        + " "
                        + day_el.get_text(strip=True)
                    )
                else:
                    date_el = el.select_one(
                        ".eventlist-datetag-startdate, [class*='startdate']"
                    )
                    if date_el:
                        date_str = date_el.get("datetime") or date_el.get_text(strip=True)

            # Convert display date to ISO, using event URL to help infer year
            iso_date = parse_display_date(date_str, url=event_url)
            if not iso_date:
                print(f"  Skipping '{title}': unparseable date {date_str!r}")
                continue

            # ── Time ─────────────────────────────────────────────────────────
            time_el  = el.select_one(
                ".event-time-12hr, .eventlist-meta-time, .eventlist-datetag-starttime"
            )
            time_str = time_el.get_text(strip=True) if time_el else ""

            # ── Location ─────────────────────────────────────────────────────
            loc_el   = el.select_one(
                ".eventlist-address, .eventlist-meta-address"
            )
            location = loc_el.get_text(strip=True) if loc_el else ""
            # Strip trailing "(map)" links
            location = re.sub(r"\s*\(map\)\s*$", "", location, flags=re.I).strip()

            # ── Description ──────────────────────────────────────────────────
            desc_el  = el.select_one(".eventlist-description, .summary-excerpt")
            desc     = strip_html(desc_el.get_text()) if desc_el else ""

            events.append({
                "title":       title,
                "start":       iso_date,
                "time":        time_str,
                "description": desc,
                "location":    location,
                "url":         event_url,
            })

        print(f"  HTML scrape: got {len(events)} events with valid dates")
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
