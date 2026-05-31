# Austin Ridge Riders — Event Calendar

A clean, auto-updating calendar page for [Austin Ridge Riders](https://www.austinridgeriders.com/events) events, hosted on GitHub Pages.

## Features

- **Monthly calendar** view with clickable event cells
- **List view** showing upcoming events in chronological order
- **Auto-synced** twice daily via GitHub Actions
- **Mobile responsive**

## How it works

```
scripts/fetch_events.py   ← fetches events from austinridgeriders.com
         │
         ▼
     events.json           ← written into _site/ during CI
         │
         ▼
     index.html            ← reads events.json at page load
         │
         ▼
  GitHub Pages             ← served at https://rjsteen.github.io/austinrides/
```

The fetch script tries three methods in order:
1. **iCal feed** — `?format=ical` (most reliable for Squarespace sites)
2. **JSON API** — `?format=json` (Squarespace infinite-scroll endpoint)
3. **HTML scrape** — BeautifulSoup fallback

If all methods fail the workflow still deploys, keeping the previously fetched data visible.

## GitHub Pages setup (one-time)

1. Go to **Settings → Pages** in this repo.
2. Under **Source**, select **GitHub Actions**.
3. Trigger the workflow manually from **Actions → Fetch Events & Deploy → Run workflow**.

The page will be live at `https://rjsteen.github.io/austinrides/`.

## Local development

```bash
pip install -r scripts/requirements.txt
python scripts/fetch_events.py      # writes events.json
python -m http.server 8080          # open http://localhost:8080
```
