# Social-media-analysis

Finds what people are talking about on **YouTube, X/Twitter, Facebook and Instagram**, and publishes the results to a GitHub Pages site:

**https://inasjackw321.github.io/Social-media-analysis/**

The posts are scraped with [Scrapling](https://github.com/D4Vinci/Scrapling). The app then groups the posts into topics: keyphrases and hashtags that several different accounts use, ranked by engagement and by how many platforms mention them. Each scan is saved to `site/data/`, so the site keeps a history you can switch between. Topics that weren't in the previous scan are marked **new**.

## Running a scan

**Ask Claude:** for example, *"scan social media for topics about the election"* or *"look through social media for what's trending"*. Claude starts the workflow, waits for it to finish, and sends you a summary with a link to the report. [`CLAUDE.md`](CLAUDE.md) explains how.

**Run it yourself:** go to **Actions → Scan social media → Run workflow**. You can give it:
- `query`: comma-separated search terms. Leave it blank to analyse the latest posts from the accounts in `config.json`.
- `platforms`: any of `youtube,twitter,facebook,instagram`.
- `lookback_hours`: how far back to look (default 24).

**Run it locally:**
```bash
pip install -r requirements.txt && scrapling install
python -m social_topics scan --query "ai, climate" --platforms youtube,instagram
python -m http.server -d site   # then open http://localhost:8000
```

## What gets scraped

| Platform | No login needed | Optional secret, which also enables keyword search |
|---|---|---|
| YouTube | Search results for your terms, plus the RSS feeds of the channels in `config.json` | — |
| X / Twitter | Recent posts from `twitter.accounts`, via X's public embed timeline | `X_AUTH_TOKEN` (plus `X_CT0` if needed): a stealth browser searches x.com |
| Instagram | Latest posts from `instagram.accounts` | `INSTAGRAM_SESSIONID`: searches each term as a hashtag |
| Facebook | Public Page feeds from `facebook.pages`, read with a stealth browser | `FACEBOOK_COOKIES` (`c_user=…; xs=…`): full feeds and post search |

To get one of these values, log in to the site in your browser and open DevTools → Application → Cookies. Then add the value under **Settings → Secrets and variables → Actions**. **Use a spare account, not your main one**, because platforms can rate-limit or lock accounts that scrape.

You can edit the accounts, pages and channels to watch in [`config.json`](config.json).

**Known limits.** The platforms change their pages often and limit logged-out visitors, so expect some failures. If a platform fails or is skipped, the report still covers the other platforms, and it says what went wrong with each one. Facebook doesn't show post dates to scrapers, so the lookback window can't filter Facebook posts. Scraping may go against a platform's terms of service, so use this for personal research.

## One-time setup

1. Merge this into `main`. GitHub only lets you run workflows that are on the default branch.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. Optional: add the login secrets above.

## Layout

```
social_topics/
  scrape.py          Scrapling wrappers (Fetcher for HTTP, StealthyFetcher for JS pages)
  collectors/        one scraper per platform
  topics.py          topic extraction
  report.py          writes site/data/index.json and site/data/reports/<id>.json
site/                the GitHub Pages site (static HTML/JS)
config.json          what to watch
.github/workflows/   scan.yml (scan + deploy), tests.yml
```

To run the tests: `python -m unittest`

---

# Plane watch

`plane_watch/` finds aircraft in trouble and draws a map card for each one, for data analysis. It combines:

- **ADS-B flight tracking** from the free community feeds [adsb.lol](https://adsb.lol), [airplanes.live](https://airplanes.live) and [adsb.fi](https://adsb.fi): every aircraft squawking **7500** (hijack), **7600** (radio failure) or **7700** (emergency), plus the transponder's own emergency status (`general`, `lifeguard`, `minfuel`, `nordo`, `unlawful`, `downed`). Optionally 7400 (UAV lost link) and 7777 (US military intercept).
- **ACARS messages**, scanned for threats: bomb threats, hijacking, security/unruly passengers, mayday/pan-pan, fire/smoke, engine and pressurisation failures, medical emergencies, fuel emergencies and **diversions**. When a message names the diversion airport (`DIVERTING TO LBSF`), the card draws a dashed line to it.
- **Route checks**: the planned origin and destination come from adsb.lol's route database. A flagged aircraft that's more than 300 km off its planned route gets a *possible diversion* flag.

![Sample card: ACARS bomb threat with diversion](docs/plane-watch-bomb-threat.png)

![Sample card: squawk 7700 with an ACARS medical diversion](docs/plane-watch-7700.png)

*(These two cards use made-up sample data, which is why they're stamped SAMPLE DATA.)*

Each card shows the aircraft (callsign, type, operator, registering country, a planespotters.net photo when there is one), altitude, speed, heading and squawk, then the track coloured by altitude, the diversion or route airports, every signal that flagged it, and the ACARS message with the matched words highlighted.

## Running it

**On GitHub Actions** (recommended, because the feeds block some cloud IPs): **Actions → Plane watch → Run workflow**. Inputs:
- `squawks`: default `7500,7600,7700`
- `watch_minutes`: how long to keep polling to build live tracks (default 5; the track history files are used when available)
- `hex`: optional ICAO hex codes to render even if nothing is wrong

The results go to `site/planes/` and the Pages site at **`/planes/`**: a gallery of cards with category filters, an overview map, and downloads.

**Locally:**
```bash
pip install -r requirements.txt
python -m plane_watch demo                       # sample cards in sample_output/, no network needed
python -m plane_watch scan --out out/            # what's squawking 7500/7600/7700 right now
python -m plane_watch scan --acars-file acars.jsonl --watch-minutes 10
python -m plane_watch render --hex AE6808        # card for any aircraft
python -m plane_watch classify "BOMB THREAT REC. DIVERTING TO EGLL"   # test the ACARS rules
```

## ACARS sources

| Source | How |
|---|---|
| Your own receiver | Point `--acars-file` at the JSON lines written by **acarsdec** (`-j`/`--output json:file:path=…`), **dumpvdl2** or **dumphfdl** (`--output decoded:json:file:path=…`), **vdlm2dec** (`-j`), or ACARS Hub's relayed decoder output. The formats can be mixed in one file. Repeat `--acars-file` for several files. |
| airframes.io | Add an `AIRFRAMES_API_KEY` secret (Settings → Secrets and variables → Actions). If airframes changes its API, override the endpoint with `AIRFRAMES_API_URL`. |

ACARS messages are matched to aircraft by ICAO hex, registration or callsign. When a flagged message belongs to an aircraft that isn't squawking, it's looked up on the ADS-B feeds so it still gets a position and track.

## Data for analysis

Each run writes, under the output folder (default `site/planes/`):

| File | What |
|---|---|
| `cards/<date>-<hex>-<category>.png` | one 1600×900 card per flagged aircraft |
| `cards/overview-<run>.png` | all flagged aircraft on one map |
| `data/runs/<run>.json` | every alert: aircraft, flags with evidence, ACARS messages, diversion airport, route |
| `data/alerts.csv` | one row per alert per run, appended over time; load it into pandas or a spreadsheet |
| `data/acars_flagged.jsonl` | every flagged ACARS message with its categories |
| `data/index.json` | list of runs with counts per category |
| `data/track_history.json` | recent positions, so tracks continue across runs |

Card images older than the last 60 runs are deleted automatically (`--keep-runs`) so the repo doesn't grow forever. The CSV and JSON are kept.

**Caveats.** A 7x00 squawk is sometimes a pilot dialling the wrong code for a moment. ACARS keyword matching finds words, not intent: a message can mention a diversion during planning, and maintenance texts mention fire tests. Common planning and test phrasing is filtered out, but check the evidence on each card before treating it as confirmed. Country is inferred from the ICAO address block.

## Layout

```
plane_watch/
  sources/adsb.py    readsb-compatible ADS-B APIs, track history, routes
  sources/acars.py   decoder JSON + airframes.io
  sources/photos.py  planespotters.net photos
  classify.py        squawk / emergency / ACARS keyword rules and categories
  pipeline.py        builds alerts, enriches them, writes the data files
  render.py          the cards and the overview map
  basemap.py         Natural Earth land + cities (downloads 1:50m, falls back to bundled 1:110m)
  sample.py          made-up demo data
site/planes/         Pages gallery
.github/workflows/plane-watch.yml
```
