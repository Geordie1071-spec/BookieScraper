# BookieScraper

Multi-brand sportsbook odds scraper. Each bookmaker is an adapter that writes the same schema, so the output can sit behind (or replace) a paid odds feed.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Scrape

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

Outputs land in `data/runs/<timestamp>/` and are copied to `data/latest/`:

| file | what |
|---|---|
| `*_events.json` | Odds-API-like nested events (swap-in backup) |
| `*_flat.csv` | one row per outcome |
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
| `bwin` | Entain CDS `fixtures` + `fixture-view?offerMapping=All`. Baseball fixture-view includes player hits/runs/HRs/Ks. | all sports with CDS fixtures |

Add a new brand by dropping a class in `bookie_scraper/bookmakers/` and registering it in `bookmakers/__init__.py`.
