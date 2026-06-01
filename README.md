# Austin Ridge Riders — Event Calendar

A clean, auto-updating event calendar for [Austin Ridge Riders](https://www.austinridgeriders.com) hosted on GitHub Pages.

Live site: **https://rjsteen.github.io/austinrides/**

## Features

- **Monthly calendar** view with color-coded event pills and clickable cells
- **List view** with selectable category filter chips — filter persists across months
- **Six event categories**, each with a distinct color: Sunday Group Ride, Advanced Group Ride, Monthly Group Ride, Ride Like A Girl, Skills Clinic, Special Event
- **Mobile responsive** — small screens show a labeled initial badge (S/A/M/G/C/E) per event instead of the full pill, plus a color legend in calendar view
- **Auto-synced** twice daily (08:00 and 20:00 UTC) via GitHub Actions, and on every push to `main`
- **iOS home screen** — includes `apple-touch-icon.png` and SVG favicon

## How it works

```
scripts/fetch_events.py   ← renders austinridgeriders.com/events with
                            headless Chromium (Playwright) and scrapes
                            the fully-rendered HTML with BeautifulSoup
         │
         ▼
     events.json           ← committed back to main [skip ci] so data
                            persists between workflow runs
         │
         ▼
     index.html            ← fetches events.json at page load, renders
                            calendar + list, no framework dependencies
         │
         ▼
  GitHub Pages             ← deployed from _site/ artifact
```

The scraper uses Playwright to render the Squarespace-powered events page (JavaScript-rendered) and extracts event titles, dates, times, and locations. If the fetch fails the workflow still deploys with the previously committed `events.json`.

## GitHub Pages setup (one-time)

1. Go to **Settings → Pages** in this repo.
2. Under **Source**, select **GitHub Actions**.
3. Trigger the workflow manually: **Actions → Fetch Events & Deploy → Run workflow**.

The page will be live at `https://rjsteen.github.io/austinrides/`.

## Local development

```bash
pip install -r scripts/requirements.txt
playwright install chromium --with-deps
python scripts/fetch_events.py      # writes events.json
python -m http.server 8080          # open http://localhost:8080
```

## Files

| File | Purpose |
|------|---------|
| `index.html` | Single-page calendar app (pure HTML/CSS/JS) |
| `events.json` | Cached event data committed by CI |
| `favicon.svg` | SVG favicon (navy + 🚵 emoji) |
| `apple-touch-icon.png` | 180×180 PNG for iOS home screen |
| `scripts/fetch_events.py` | Playwright scraper |
| `scripts/requirements.txt` | Python dependencies |
| `.github/workflows/fetch-events.yml` | CI/CD — fetch, commit, deploy |
