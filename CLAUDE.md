# Notes for Claude

## When the user asks for a scan

Examples: "look through social media for X", "what's trending", "scan for topics about Y".

Don't scrape from your own session. Cloud sessions usually can't reach the social sites, so the scan runs on GitHub Actions and publishes to GitHub Pages:

1. Start the workflow with the GitHub MCP tool `actions_run_trigger`:
   owner `Inasjackw321`, repo `Social-media-analysis`, workflow `scan.yml`, ref `main`, with these inputs:
   - `query`: the user's search terms, comma-separated. Use `""` when the user just wants "what's trending" or "latest".
   - `platforms`: default `youtube,twitter,facebook,instagram`. Narrow it if the user names particular platforms.
   - `lookback_hours`: default `"24"`. Use "this week" → `"168"`.
2. Find the run with `actions_list` (list workflow runs for `scan.yml`) and wait for it to finish. Don't poll in a tight loop: check about every 2 minutes, because a scan takes about 3–8 minutes.
3. When it succeeds, read `site/data/reports/run-<run_id>.json` from `main` (`get_file_contents`). Reply with:
   - the top 5–10 topics, with their platforms and post counts, plus any marked `new`
   - any platforms with `status` `error` or `skipped`, and why (from `message`)
   - the link `https://inasjackw321.github.io/Social-media-analysis/#report=run-<run_id>`
4. If the run fails, read the job logs (`get_job_logs`) and explain what happened. Exit code 1 from the Scan step means no platform returned any data.

## When the user asks about planes / squawks / ACARS

Examples: "any 7700s right now?", "check for diverted or bomb-threat flights", "make a card for AE6808".

The ADS-B feeds can't be reached from cloud sessions either, so use GitHub Actions:

1. Trigger `plane-watch.yml` with `actions_run_trigger`: owner `Inasjackw321`, repo `hybrid-war-tracker`, ref `main`. Inputs: `squawks` (default `"7500,7600,7700"`), `watch_minutes` (default `"5"`), `hex` (ICAO hex codes to render, comma-separated, or `""`).
2. Wait for the run with `actions_list`, checking about every 2 minutes. A run takes roughly `watch_minutes` + 3 minutes.
3. Read `site/planes/data/runs/run-<run_id>.json` from `main`. Reply with each alert's callsign, type, category and evidence (squawk, ACARS excerpt, diversion airport), plus any `errors`, and link `https://inasjackw321.github.io/hybrid-war-tracker/planes/#run=run-<run_id>`. Say that a squawk or keyword match isn't confirmation.

`python -m plane_watch demo` renders sample cards offline when you just need to check the drawing code. For the local app, users run `python planewatch.pyz` (built by `tools/build_app.py` and published by `release-app.yml`); `python -m plane_watch` with no command starts the same app (`plane_watch/app.py`).

## Development

- `python -m unittest` runs the tests. They must pass offline, so mock `social_topics.scrape.get` / `get_json` / `browse` in tests. For plane_watch, mock `plane_watch.http.get_json` / `post_json` / `get_bytes` (all its network access goes through `plane_watch/http.py`); `PLANE_WATCH_OFFLINE=1` makes any unmocked fetch fail fast.
- All scraping goes through `social_topics/scrape.py` (Scrapling `Fetcher` / `StealthyFetcher`).
- Each collector in `social_topics/collectors/` raises `NotConfigured` when it has nothing it can scan. When only part of a scan fails, the collector returns `finish(posts, errors)`, so partial failures show up as warnings instead of failing the scan.
- To use a local Chromium instead of the one `scrapling install` downloads, set `CHROMIUM_PATH` (e.g. `/opt/pw-browsers/chromium` in cloud sessions).
