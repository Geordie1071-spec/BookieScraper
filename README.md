# BookieScraper

Multi-brand sportsbook odds scraper. Each bookmaker is an adapter that writes the same schema. The in-process API returns a **flat CSV** (one row per outcome) so a backend can scrape on click and send a file download.

HTTP books (`pinnacle`, `bwin`, `unibet`) need only `httpx`. Playwright books (`bet365`, `betsson`, `betway`, `ivybet`) need the `[playwright]` extra and Chromium — do not run those inside a web API process.

Playwright brands are gated by `HTTP_BOOKMAKERS` (the on-the-fly allowlist). `run()` will still accept `-b bet365` on a machine with Playwright installed; the backend should reject anything not in that set so Railway never launches Chromium. Adapters load on first use, so importing `pinnacle` / `bwin` / `unibet` does not import Playwright.

## Install (GitHub is the registry)

No PyPI. Pin a tag from this repo in the backend:

```text
bookie-scraper @ git+https://github.com/Geordie1071-spec/BookieScraper.git@v0.1.2
```

If the repo is private, use a token in the URL (`git+https://${GITHUB_TOKEN}@github.com/...`).

```bash
# HTTP books only (Railway / backend)
pip install "bookie-scraper @ git+https://github.com/Geordie1071-spec/BookieScraper.git@v0.1.2"

# Local CLI including Playwright brands
pip install "bookie-scraper[playwright] @ git+https://github.com/Geordie1071-spec/BookieScraper.git@v0.1.2"
python -m playwright install chromium
```

Or from a clone:

```bash
pip install -e .
# optional: pip install -e ".[playwright]" && python -m playwright install chromium
```

`requirements.txt` is the full local set (`httpx` + `playwright`).

## In-process CSV (backend)

User picks a sport and bookie. The request waits a couple of seconds and returns `text/csv`:

```python
from bookie_scraper import HTTP_BOOKMAKERS, ScrapeConfig, results_to_csv, run

async def scrape_csv(bookie: str, sport: str) -> str:
    if bookie not in HTTP_BOOKMAKERS:
        raise ValueError(f"{bookie} is not an HTTP book; use pinnacle, bwin, or unibet")
    results = await run(ScrapeConfig(
        bookmakers=[bookie],
        sports=[sport],
        depth="main",
        output_dir=None,  # no files on disk
    ))
    return results_to_csv(results)
```

Respond with:

- `Content-Type: text/csv; charset=utf-8`
- `Content-Disposition: attachment; filename="{bookie}_{sport}.csv"`

CSV columns: `bookmaker`, `sport`, `sport_key`, `competition`, `event`, `event_id`, `home`, `away`, `starts_at`, `status`, `market`, `market_key`, `outcome`, `odds`, `point`, `active`, `scraped_at`.

Every data cell is prefixed with `'` so Excel keeps values like `2-1` as text instead of a date. Headers are unchanged.

## CLI scrape

```bash
# all bookmakers, all sports, every market on every event (default)
python -m bookie_scraper

# one brand
python -m bookie_scraper -b pinnacle

# several brands, subset of sports
python -m bookie_scraper -b pinnacle,betsson,ivybet,bwin,bet365 -s football,tennis,basketball

# one sport on one book (flag can be repeated)
python -m bookie_scraper -b ivybet -s baseball
python -m bookie_scraper -b bet365 -s tennis -s football

# soccer is association football only (not NFL / NCAAF)
python -m bookie_scraper -b pinnacle -s soccer
python -m bookie_scraper -b pinnacle -s american-football

# list-page markets only (faster; skips per-event deep markets)
python -m bookie_scraper --depth main
```

Each bookie prints per-sport timings in this form, also stored in `summary.json`:

```
  timing  ivybet, baseball : 80.2s  (58 events, 6298 odds)
```

Pinnacle sports run in parallel, so those seconds are work time for that sport, not exclusive wall clock. Playwright books (Bet365, Betsson, IvyBet) should be run one brand at a time for stable timings.

CLI outputs land in `data/runs/<timestamp>/` and are copied to `data/latest/`:

| file | what |
|---|---|
| `*_events.json` | Odds-API-like nested events (swap-in backup) |
| `*_flat.csv` | one row per outcome (same as `results_to_csv()`) |
| `all_events.json` / `all_flat.csv` | combined |
| `summary.json` | event / odds counts and `sport_timings` per brand |

## Brands

| key | how it works | coverage |
|---|---|---|
| `pinnacle` | Guest Arcadia API. All lines, periods, team totals, and related player props on each event. | every sport with open matchups |
| `bet365` | Playwright: every sport on the home menu. Baseball Game Lines plus player prop tabs (Hits, HRs, total bases, pitcher Ks). Other sports parse competition coupons (1X2 / ML / outrights). | all sports listed on the site |
| `betsson` | Playwright session + `/api/sb` events-table and accordion per event | every competition under each sport |
| `betway` | Playwright: every competition, then each event page for all markets | all sports listed on the site |
| `ivybet` | Playwright + Digitain Socket.IO event odds (`sb.ivybet.com`). Baseball visits every event (player props included). Other sports cap event-page visits. F1 is parsed from the featured outright listing. | all sports on the sportsbook |
| `unibet` | Kambi offering API (`offering-api.kambicdn.com`). `listView` for main markets; per-event `betoffer` when `--depth full`. | sports on the Unibet Kambi tree |
| `bwin` | Entain CDS `fixtures` + `fixture-view?offerMapping=All`. Baseball fixture-view includes player hits/runs/HRs/Ks. | all sports with CDS fixtures |

Add a new brand by dropping a class in `bookie_scraper/bookmakers/` and registering it in `bookmakers/__init__.py` (`_ADAPTERS`).
