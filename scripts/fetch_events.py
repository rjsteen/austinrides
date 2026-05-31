#!/usr/bin/env python3
"""Fetch events from austinridgeriders.com/events and write events.json."""

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL   = "https://www.austinridgeriders.com"
EVENTS_URL = f"{BASE_URL}/events"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _render(url: str) -> str:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(3_000)
        html = page.content()
        browser.close()
    return html


def _iso(month_str: str, day_str: str) -> Optional[str]:
    month = MONTH_MAP.get(month_str.lower()[:3])
    if not month:
        return None
    try:
        return date(2026, month, int(day_str)).isoformat()
    except ValueError:
        return None


def _parse_month_day(text: str) -> Optional[str]:
    """'May 10' or 'Jun 7' or 'June 1' → '2026-05-10'."""
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", text.strip())
    return _iso(m.group(1), m.group(2)) if m else None


def _external_links(section) -> List[tuple]:
    """Return [(text, href), ...] for external links only."""
    return [
        (a.get_text(strip=True), a.get("href", ""))
        for a in section.find_all("a")
        if a.get("href", "").startswith("http")
    ]


def _parse_alternating(section, title: str) -> List[dict]:
    """Sections where each date and location are wrapped in <a href=tickettailor>."""
    events: List[dict] = []
    links = _external_links(section)
    i = 0
    while i + 1 < len(links):
        text, href = links[i]
        iso = _parse_month_day(text)
        if iso:
            location = links[i + 1][0]
            events.append({
                "title":    title,
                "start":    iso,
                "time":     "",
                "location": location,
                "url":      href,
            })
            i += 2
        else:
            i += 1
    return events


def _parse_clinics(section) -> List[dict]:
    """'Level 1 Fundamentals\\nJune 13-14th, Walnut Creek\\n...\\nClick here to register'"""
    events: List[dict] = []
    lines = [l.strip() for l in section.get_text("\n").splitlines() if l.strip()]

    # Map each registration link to its position in the text
    reg_links: List[str] = [
        a.get("href", "")
        for a in section.find_all("a")
        if a.get("href", "").startswith("http")
    ]

    clinic_title = ""
    reg_idx = 0
    for line in lines:
        m = re.match(
            r"^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:-\d+(?:st|nd|rd|th)?)?"
            r",\s*(.+)$",
            line,
        )
        if m:
            iso = _iso(m.group(1), m.group(2))
            if iso:
                url = reg_links[reg_idx] if reg_idx < len(reg_links) else ""
                reg_idx += 1
                events.append({
                    "title":    clinic_title or "Skills Clinic",
                    "start":    iso,
                    "time":     "",
                    "location": m.group(3).strip(),
                    "url":      url,
                })
        elif not any(
            kw in line for kw in
            ("$", "register", "Register", "Opens", "certified", "learn",
             "neutral", "braking", "cornering", "gearing", "Click")
        ):
            clinic_title = line
    return events


def _parse_other(section) -> List[dict]:
    """'Member Appreciation: May 30th | details'"""
    events: List[dict] = []
    for line in [l.strip() for l in section.get_text("\n").splitlines() if l.strip()]:
        m = re.match(
            r"^(.+?):\s+([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b",
            line,
        )
        if m:
            iso = _iso(m.group(2), m.group(3))
            if iso:
                events.append({
                    "title":    m.group(1).strip(),
                    "start":    iso,
                    "time":     "",
                    "location": "",
                    "url":      "",
                })
    return events


def _find_section(soup, section_id: str):
    return soup.find(id=section_id)


def scrape_events(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    events: List[dict] = []

    # ── Eventlist entries (2026 only) ─────────────────────────────────────────
    for article in soup.select(".eventlist-event"):
        title_el = article.select_one(".eventlist-title-link")
        date_el  = article.select_one("time.event-date")
        if not title_el or not date_el:
            continue
        date_iso = date_el.get("datetime", "")
        if not date_iso.startswith("2026"):
            continue

        href   = title_el.get("href", "")
        loc_el = article.select_one(".eventlist-meta-address")
        location = ""
        if loc_el:
            for a in loc_el.find_all("a"):
                a.decompose()
            location = loc_el.get_text(strip=True).strip(",").strip()

        time_el = article.select_one(".event-time-localized-start")
        events.append({
            "title":    title_el.get_text(strip=True),
            "start":    date_iso,
            "time":     time_el.get_text(strip=True) if time_el else "",
            "location": location,
            "url":      href if href.startswith("http") else BASE_URL + href,
        })

    # ── Schedule sections (center h2 blocks) ─────────────────────────────────
    for section_id, title in [
        ("sunday-ride",   "Sunday Group Ride"),
        ("monthly-ride",  "Monthly Group Ride"),
        ("advanced-ride", "Advanced Group Ride"),
        ("rlag",          "Ride Like A Girl"),
    ]:
        section = _find_section(soup, section_id)
        if section:
            events.extend(_parse_alternating(section, title))

    section = _find_section(soup, "clinics")
    if section:
        events.extend(_parse_clinics(section))

    section = _find_section(soup, "other")
    if section:
        events.extend(_parse_other(section))

    events.sort(key=lambda e: e["start"])
    return events


def main() -> int:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("events.json")

    print(f"Rendering {EVENTS_URL} ...")
    html = _render(EVENTS_URL)

    events = scrape_events(html)
    if not events:
        print("No events found.")
        return 1

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "source":  EVENTS_URL,
        "events":  events,
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(events)} events → {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
