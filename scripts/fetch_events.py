#!/usr/bin/env python3
"""
Fetch events from austinridgeriders.com and write events.json.

Sources tried (results are merged and deduplicated):
  1. /ride/ page  — Sunday ride schedule (user-specified source)
  2. iCal feed    — ?format=ical (most reliable for Squarespace)
  3. JSON API     — ?format=json (Squarespace infinite-scroll endpoint)
  4. /events HTML — BeautifulSoup scrape fallback
"""

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

BASE_URL   = "https://www.austinridgeriders.com"
EVENTS_URL = f"{BASE_URL}/events"
RIDE_URL   = f"{BASE_URL}/ride/"

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

# Regex building blocks
_MONTH_PAT = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_WEEKDAY_PAT = r"(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"

# Matches "June 8", "Jun 8", "Sunday June 8", "Sunday, June 8, 2026", etc.
_DATE_RE = re.compile(
    r"(?:" + _WEEKDAY_PAT + r"\s*,?\s*)?"
    r"(" + _MONTH_PAT + r")\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(\d{4}))?",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\b", re.IGNORECASE)


# ── Date helpers ─────────────────────────────────────────────────────────────

def _infer_year(month: int, day: int, url: str = "") -> int:
    m = re.search(r"(20\d{2})", url)
    if m:
        return int(m.group(1))
    today = date.today()
    best_year, best_delta = today.year, None
    for y in range(today.year - 1, today.year + 3):
        try:
            candidate = date(y, month, day)
            delta = abs((candidate - today).days)
            if best_delta is None or delta < best_delta:
                best_year, best_delta = y, delta
        except ValueError:
            pass
    return best_year


def parse_display_date(text: str, url: str = "") -> str | None:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
                "%m/%d/%Y", "%Y/%m/%d", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
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


def _date_from_match(m: re.Match, url: str = "") -> str | None:
    month = MONTH_MAP.get(m.group(1).lower()[:3])
    if not month:
        return None
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else _infer_year(month, day, url)
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def to_iso(dt) -> str | None:
    return dt.isoformat() if dt and hasattr(dt, "isoformat") else None


# ── Method 0: /ride/ page — Sunday rides ─────────────────────────────────────

def fetch_ride_page() -> list[dict] | None:
    """Scrape /ride/#sunday-rides using Playwright (JS-rendered page)."""
    print(f"Trying ride page: {RIDE_URL}#sunday-rides")

    # Primary: Playwright renders the JS so we get real content
    html = _playwright_fetch(RIDE_URL)
    if html:
        events = _parse_ride_html(html)
        if events:
            return events

    print("  Playwright returned no events")
    return None


def _playwright_fetch(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright not installed")
        return None

    print("  Launching headless Chromium...")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page    = browser.new_page(user_agent=BROWSER_HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=60_000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()
        print(f"  Playwright: got {len(html)} bytes of rendered HTML")
        Path("_ride_debug.html").write_text(html[:60000], encoding="utf-8")
        return html
    except Exception as exc:
        print(f"  Playwright failed: {exc}")
        return None


def _parse_ride_html(html: str) -> list[dict] | None:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Find the sunday-rides section
    section = soup.find(id=re.compile(r"sunday.?ride", re.I))
    if not section:
        for attr in ("data-section-id", "data-anchor-id", "data-anchor"):
            section = soup.find(attrs={attr: re.compile(r"sunday", re.I)})
            if section:
                break
    if not section:
        for tag in ("h1", "h2", "h3", "h4"):
            h = soup.find(tag, string=re.compile(r"sunday", re.I))
            if h:
                section = h.find_parent("section") or h.find_parent("div") or h.find_parent("article")
                break
    if not section:
        print("  No sunday-rides section found — scanning full page")
        section = soup

    plain = section.get_text("\n", strip=True)
    print(f"  Section text preview:\n---\n{plain[:1200]}\n---")

    events: list[dict] = []

    # Table
    table = section.find("table")
    if table:
        rows    = table.find_all("tr")
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th","td"])] if rows else []
        d_col = next((i for i,h in enumerate(headers) if "date" in h), 0)
        t_col = next((i for i,h in enumerate(headers) if "time" in h), -1)
        l_col = next((i for i,h in enumerate(headers) if any(k in h for k in ("location","trail","park","where"))), -1)
        for row in rows[1:]:
            cells = row.find_all(["td","th"])
            if not cells:
                continue
            dt = cells[d_col].get_text(strip=True) if d_col < len(cells) else ""
            iso = parse_display_date(dt, RIDE_URL) or (_date_from_match(m, RIDE_URL) if (m := _DATE_RE.search(dt)) else None)
            if not iso:
                continue
            events.append({
                "title": "Sunday Ride", "start": iso,
                "time":     cells[t_col].get_text(strip=True) if 0<=t_col<len(cells) else "",
                "location": cells[l_col].get_text(strip=True) if 0<=l_col<len(cells) else "",
                "description": "", "url": RIDE_URL + "#sunday-rides",
            })

    # List items
    if not events:
        for li in section.find_all("li"):
            text = li.get_text(strip=True)
            m = _DATE_RE.search(text)
            if not m:
                continue
            iso = _date_from_match(m, RIDE_URL)
            if iso:
                tm = _TIME_RE.search(text)
                events.append({"title":"Sunday Ride","start":iso,
                    "time":tm.group(0) if tm else "","description":text,
                    "location":_extract_location(text),"url":RIDE_URL+"#sunday-rides"})

    # Line-by-line scan
    if not events:
        for line in plain.splitlines():
            line = line.strip()
            m = _DATE_RE.search(line)
            if not m:
                continue
            iso = _date_from_match(m, RIDE_URL)
            if iso:
                tm = _TIME_RE.search(line)
                events.append({"title":"Sunday Ride","start":iso,
                    "time":tm.group(0) if tm else "","description":line,
                    "location":_extract_location(line),"url":RIDE_URL+"#sunday-rides"})

    # Deduplicate by date
    seen: set[str] = set()
    unique = [e for e in events if e["start"] not in seen and not seen.add(e["start"])]  # type: ignore[func-returns-value]
    print(f"  Ride page: {len(unique)} events")
    return unique if unique else None


def _extract_location(text: str) -> str:
    m = re.search(r"(?:[-–|@]|at\s+)(.+)$", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


# ── Method 1: iCal ───────────────────────────────────────────────────────────

def fetch_ical() -> list[dict] | None:
    try:
        from icalendar import Calendar
    except ImportError:
        print("icalendar not available — skipping")
        return None

    url = f"{EVENTS_URL}?format=ical"
    print(f"Trying iCal: {url}")
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        if "text/html" in resp.headers.get("Content-Type", ""):
            print("  iCal: got HTML — feed unavailable")
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
            events.append({
                "title":       str(component.get("summary", "")).strip(),
                "start":       to_iso(dtstart.dt),
                "end":         to_iso(dtend.dt) if dtend else None,
                "description": strip_html(str(component.get("description", "") or "")),
                "location":    str(component.get("location", "") or "").strip(),
                "url":         str(component.get("url", "") or ""),
            })
        events.sort(key=lambda x: x["start"] or "")
        print(f"  iCal: {len(events)} events")
        return events if events else None
    except Exception as exc:
        print(f"  iCal failed: {exc}")
        return None


# ── Method 2: Squarespace JSON API ───────────────────────────────────────────

def fetch_json_api() -> list[dict] | None:
    url = f"{EVENTS_URL}?format=json"
    print(f"Trying JSON API: {url}")
    try:
        resp = requests.get(url, headers={**BROWSER_HEADERS, "Accept": "application/json"}, timeout=30)
        resp.raise_for_status()
        if "text/html" in resp.headers.get("Content-Type", ""):
            print("  JSON API: got HTML — endpoint unavailable")
            return None
        data  = resp.json()
        items = data.get("items", [])
        if not items:
            return None
        events = []
        for item in items:
            start_ms = item.get("startDate") or item.get("publishOn")
            end_ms   = item.get("endDate")
            if not start_ms:
                continue
            start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
            end_dt   = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc) if end_ms else None
            loc      = item.get("location") or {}
            location = ", ".join(p for p in [
                loc.get("addressLine1",""), loc.get("city",""), loc.get("state","")
            ] if p)
            events.append({
                "title":       item.get("title", "").strip(),
                "start":       start_dt.isoformat(),
                "end":         end_dt.isoformat() if end_dt else None,
                "description": strip_html(item.get("excerpt", "")),
                "location":    location,
                "url":         BASE_URL + item.get("fullUrl", ""),
            })
        events.sort(key=lambda x: x["start"])
        print(f"  JSON API: {len(events)} events")
        return events if events else None
    except Exception as exc:
        print(f"  JSON API failed: {exc}")
        return None


# ── Method 3: /events HTML scrape ────────────────────────────────────────────

def fetch_events_html() -> list[dict] | None:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    print(f"Trying /events HTML scrape: {EVENTS_URL}")
    try:
        resp = requests.get(EVENTS_URL, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        events: list[dict] = []

        event_els = []
        for sel in (".eventlist-event", ".summary-item[data-event]",
                    ".summary-item", "article[class*='event']"):
            event_els = soup.select(sel)
            if event_els:
                break

        print(f"  Found {len(event_els)} candidate elements")

        for el in event_els:
            title_a = el.select_one(".eventlist-title a, .summary-title a, h1 a, h2 a, h3 a")
            if not title_a:
                continue
            title     = title_a.get_text(strip=True)
            href      = title_a.get("href", "")
            event_url = href if href.startswith("http") else BASE_URL + href

            # Prefer <time datetime="...">, then read month/day spans separately
            date_str = ""
            time_tag = el.select_one("time[datetime]")
            if time_tag:
                date_str = time_tag.get("datetime", "")
            else:
                month_el = el.select_one(
                    ".eventlist-datetag-startdate-month, [class*='startdate-month']")
                day_el   = el.select_one(
                    ".eventlist-datetag-startdate-day, [class*='startdate-day']")
                if month_el and day_el:
                    date_str = month_el.get_text(strip=True) + " " + day_el.get_text(strip=True)
                else:
                    date_el = el.select_one(".eventlist-datetag-startdate, [class*='startdate']")
                    if date_el:
                        date_str = date_el.get("datetime") or date_el.get_text(strip=True)

            iso = parse_display_date(date_str, url=event_url)
            if not iso:
                print(f"  Skipping '{title}': unparseable date {date_str!r}")
                continue

            time_el  = el.select_one(".event-time-12hr, .eventlist-meta-time, .eventlist-datetag-starttime")
            time_str = time_el.get_text(strip=True) if time_el else ""
            loc_el   = el.select_one(".eventlist-address, .eventlist-meta-address")
            location = re.sub(r"\s*\(map\)\s*$", "", loc_el.get_text(strip=True), flags=re.I).strip() if loc_el else ""
            desc_el  = el.select_one(".eventlist-description, .summary-excerpt")
            desc     = strip_html(desc_el.get_text()) if desc_el else ""

            events.append({
                "title": title, "start": iso, "time": time_str,
                "description": desc, "location": location, "url": event_url,
            })

        print(f"  /events HTML: {len(events)} events with valid dates")
        return events if events else None
    except Exception as exc:
        print(f"  /events HTML failed: {exc}")
        return None


# ── Merge & deduplicate ───────────────────────────────────────────────────────

def merge_events(lists: list[list[dict]]) -> list[dict]:
    """Combine multiple event lists, deduplicating by (date, title)."""
    seen:   set[tuple[str, str]] = set()
    merged: list[dict] = []
    for events in lists:
        for ev in (events or []):
            key = (ev.get("start", ""), ev.get("title", "").lower()[:40])
            if key not in seen:
                seen.add(key)
                merged.append(ev)
    merged.sort(key=lambda x: x.get("start") or "")
    return merged


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("events.json")

    ride_events   = fetch_ride_page()
    ical_events   = fetch_ical()
    json_events   = fetch_json_api() if not ical_events else None
    html_events   = fetch_events_html() if not ical_events and not json_events else None

    all_events = merge_events([
        ride_events or [],
        ical_events or json_events or html_events or [],
    ])

    if not all_events:
        print("\nNo events found from any source.")
        return 1

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "source":  EVENTS_URL,
        "events":  all_events,
    }
    output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {len(all_events)} events → {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
