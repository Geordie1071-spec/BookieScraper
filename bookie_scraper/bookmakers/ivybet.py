"""IvyBet — Digitain sportsbook at sb.ivybet.com.

Odds arrive over Socket.IO when an event page loads (subscribe type=odds,
room getSelectedEvent). That payload includes every market, including baseball
player props (hits, runs, HRs, strikeouts, pitcher lines, etc.).
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from playwright.async_api import Page

from bookie_scraper.bookmakers.base import Bookmaker
from bookie_scraper.browser import dismiss_cookies, launch_browser, new_context, safe_goto
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.models import Event, ScrapeResult, utc_now, SportClock
from bookie_scraper.normalize import build_event, fractional_to_decimal, group_outcomes

BASE = "https://sb.ivybet.com"
SKIP_SPORTS = {"horseracing", "horse-racing", "politics", "horse racing"}
F1_SLUGS = {"formula-1", "formula1", "formula_1", "f1"}

# Digitain baseball (and generic) line-entity labels. Used with market_key to
# name Totals / Player Hits / Pitcher Ks / etc.
LINE_ENTITY = {
    "43": "Runs",
    "234": "Home Runs",
    "235": "Hits",
    "260": "Singles",
    "261": "Hits",
    "262": "Doubles",
    "263": "Total Bases",
    "264": "Triples",
    "266": "Pitcher Earned Runs",
    "267": "Pitcher Hits Allowed",
    "268": "Pitcher Strikeouts",
    "269": "Pitcher Outs",
    "271": "Stolen Bases",
    "391": "Pitcher Earned First Run",
    "392": "First Run",
    "393": "First Hit",
    "394": "First Home Run",
    "395": "Walks",
    "396": "RBIs",
    "397": "Hits, Runs & RBIs",
    "398": "Walks Allowed",
    "399": "First Strikeout",
    "400": "First RBI",
    "470": "Strikeouts",
}

GAME_PERIOD = {
    "31": "",
    "30": "1st 5 Innings ",
    "25": "1st Inning ",
    "822": "No Extra Innings ",
}

EXTRACT_EVENTS = r"""() => {
  const hrefs = [...document.querySelectorAll('a[href*="/euro/event/"]')]
    .map(a => a.href.split('?')[0])
    .filter(h => /\/euro\/event\/[^/]+\/\d+/.test(h));
  return [...new Set(hrefs)];
}"""

EXTRACT_LEAGUES = r"""(sportKey) => {
  const re = new RegExp('/euro/sport/' + sportKey + '/[^/?#]+/\\d+');
  const hrefs = [...document.querySelectorAll('a[href*="/euro/sport/"]')]
    .map(a => a.href.split('?')[0])
    .filter(h => re.test(h));
  return [...new Set(hrefs)];
}"""

EXTRACT_SPORTS = r"""() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/euro/sport/"]')) {
    const m = a.href.match(/\/euro\/sport\/([^/?#]+)/);
    if (!m) continue;
    const key = m[1].toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({key, href: a.href.split('?')[0]});
  }
  return out;
}"""


def _price(raw) -> float | None:
    text = str(raw or "").strip().replace(",", ".")
    if "/" in text:
        return fractional_to_decimal(text)
    try:
        n = float(text)
    except (TypeError, ValueError):
        return None
    return n if n > 1.0 else None


def _point(raw) -> float | None:
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _league_id_from_url(url: str) -> str:
    m = re.search(r"/euro/sport/[^/]+/[^/]+/(\d+)", url)
    return m.group(1) if m else url


def _league_name_from_url(url: str) -> str:
    m = re.search(r"/euro/sport/[^/]+/([^/]+)/\d+", url)
    if not m:
        return ""
    slug = m.group(1).replace("%28", "(").replace("%29", ")")
    return slug.replace("-", " ").replace("%20", " ").title()


DATE_LINE = re.compile(
    r"^(?:mon|tue|wed|thu|fri|sat|sun)\b|"
    r"^[a-z]{3} \d{1,2}, \d{4}",
    re.I,
)
OUTRIGHT_MARKET = re.compile(
    r"^(winner|podium|fastest|pole|qualifying|head to head|to finish|"
    r"championship|outright|top \d|points|constructor|drivers?|"
    r"winning|to be classified|not to finish|safety car|grid)",
    re.I,
)
OUTRIGHT_SKIP = {
    "expand_more", "expand_less", "star", "grade", "equalizer", "search", "live",
    "all", "main", "outrights", "more markets", "sports", "esports", "bet slip",
    "my bets", "single", "combo", "if bet", "system", "in-play betting",
    "calendar", "information", "results", "featured", "early", "international",
}


def _listing_price(raw) -> float | None:
    """Prices on Digitain listing pages are fractional or dotted decimals, not sport counts."""
    text = str(raw or "").strip().replace(",", ".")
    if "/" in text:
        return fractional_to_decimal(text)
    if re.fullmatch(r"\d+\.\d{1,3}", text):
        return _price(text)
    return None


def parse_outright_text(blob: str) -> list:
    """Parse Digitain outright listing text: heading, then name/price rows."""
    lines = [ln.strip() for ln in (blob or "").splitlines() if ln.strip()]
    items = []
    current = "Winner"
    i = 0
    while i < len(lines):
        ln = lines[i]
        low = ln.lower()
        if low in OUTRIGHT_SKIP or low.startswith("in order to") or re.fullmatch(r"\(\d+\)", ln):
            i += 1
            continue
        if DATE_LINE.search(ln):
            i += 1
            continue
        if _listing_price(ln) is not None:
            i += 1
            continue
        if OUTRIGHT_MARKET.search(ln) and _listing_price(ln) is None and len(ln) < 80:
            current = ln
            i += 1
            continue
        name = ln
        if i + 1 < len(lines) and _listing_price(lines[i + 1]) is not None:
            price = _listing_price(lines[i + 1])
            if price is not None and 1 < len(name) < 80 and not name.isdigit():
                items.append((current, name, price, True, None))
            i += 2
            continue
        i += 1
    return items


def _event_from_outright(sport: str, competition: str, name: str, eid: str, items: list) -> Event | None:
    grouped = group_outcomes(items)
    if not grouped:
        return None
    return build_event(
        bookmaker="ivybet",
        event_id=eid,
        sport_raw=sport if sport not in F1_SLUGS else "formula-1",
        competition=competition or name or "Formula 1",
        name=name or competition or eid,
        starts_at="",
        status="",
        markets=grouped,
        home="",
        away="",
    )


def _event_id_from_url(url: str) -> str:
    m = re.search(r"/euro/event/[^/]+/(\d+)", url)
    return m.group(1) if m else url


def _slug_name(url: str) -> str:
    m = re.search(r"/euro/event/([^/]+)/", url)
    if not m:
        return ""
    return m.group(1).replace("-", " ").title()


def _parse_sio(payload: str):
    if not payload or not payload.startswith("42"):
        return None
    try:
        return json.loads(payload[2:])
    except Exception:
        return None


class _WsCatcher:
    """Collect Digitain Socket.IO `initial` frames on a Playwright page."""

    def __init__(self) -> None:
        self.initials: list = []

    def attach(self, page: Page) -> None:
        def on_ws(ws) -> None:
            def recv(payload: str) -> None:
                msg = _parse_sio(payload or "")
                if not msg or msg[0] != "initial":
                    return
                self.initials.append(msg)

            ws.on("framereceived", recv)

        page.on("websocket", on_ws)

    def objects(self) -> list[dict]:
        out: list[dict] = []
        for msg in self.initials:
            payload = msg[2] if len(msg) > 2 else None
            if not isinstance(payload, list):
                continue
            for item in payload:
                if isinstance(item, list) and len(item) >= 3 and isinstance(item[2], dict):
                    out.append(item[2])
                elif isinstance(item, dict):
                    out.append(item)
        return out

    def event_hrefs(self) -> list[str]:
        hrefs = []
        seen = set()
        for obj in self.objects():
            eid = str(obj.get("event_id") or "")
            if not eid or "odds" in obj or obj.get("market_id"):
                continue
            sport = (obj.get("sport_key") or "event").lower()
            home = obj.get("home_team") or "home"
            away = obj.get("away_team") or "away"
            slug = re.sub(r"[^a-z0-9]+", "-", f"{home}-vs-{away}".lower()).strip("-")
            url = f"{BASE}/en/euro/event/{slug}/{eid}"
            if eid not in seen:
                seen.add(eid)
                hrefs.append(url)
        return hrefs

    def odds_payload(self, event_id: str) -> tuple[dict | None, list[dict]]:
        meta = None
        markets: list[dict] = []
        for obj in self.objects():
            if str(obj.get("event_id") or "") != str(event_id):
                continue
            if obj.get("market_id") and obj.get("odds"):
                markets.append(obj)
            elif obj.get("home_team") or obj.get("sport_key"):
                meta = obj
        return meta, markets


def _entity_label(line_entity_id: str, market_key: str) -> str:
    named = LINE_ENTITY.get(str(line_entity_id) or "")
    if named:
        return named
    if market_key in ("one_two", "x12"):
        return "To Win"
    if market_key == "euro_handicap":
        return "Handicap"
    if market_key in ("euro_over_under", "htt", "att"):
        return "Total"
    if market_key in ("odd_even", "htoe", "atoe"):
        return "Odd/Even"
    if market_key == "overtime_yes_no":
        return "Extra Inning"
    return (market_key or "Market").replace("_", " ").title()


def _market_title(mk: dict, home: str, away: str) -> str:
    key = mk.get("market_key") or "market"
    entity = _entity_label(str(mk.get("line_entity_id") or ""), key)
    period = GAME_PERIOD.get(str(mk.get("game_period_id") or ""), "")
    if period is None:
        period = ""
    prefix = period
    if key == "one_two":
        return f"{prefix}To Win".strip() or "To Win"
    if key == "x12":
        return f"{prefix}Money Line 3-Way".strip()
    if key == "euro_handicap":
        return f"{prefix}Handicap {entity}".strip()
    if key == "euro_over_under":
        if entity.lower().startswith("total"):
            return f"{prefix}{entity}".strip()
        return f"{prefix}Total {entity}".strip()
    if key == "htt":
        team = home or "Home"
        return f"{prefix}{team} Total {entity}".strip()
    if key == "att":
        team = away or "Away"
        return f"{prefix}{team} Total {entity}".strip()
    if key == "odd_even":
        return f"{prefix}Total {entity}".strip()
    if key == "htoe":
        return f"{prefix}{home or 'Home'} {entity}".strip()
    if key == "atoe":
        return f"{prefix}{away or 'Away'} {entity}".strip()
    if key == "overtime_yes_no":
        return f"{prefix}Extra Inning".strip()
    if key in ("player_over_under", "player_yes_no"):
        if entity.lower().startswith(("pitcher", "player", "first")):
            return f"{prefix}{entity}".strip()
        return f"{prefix}Player {entity}".strip()
    return f"{prefix}{entity}".strip() or key


def _outcome_name(odd: dict, home: str, away: str) -> tuple[str, float | None]:
    k = str(odd.get("k") or "").strip()
    title = str(odd.get("title") or "").strip()
    point = _point(odd.get("es") if odd.get("es") not in (None, "") else odd.get("as"))
    low = k.lower()
    if low in ("home", "1"):
        return home or "Home", point
    if low in ("away", "2"):
        return away or "Away", point
    if low == "draw":
        return "Draw", point
    if low in ("over", "under", "yes", "no", "odd", "even"):
        return low.title(), point
    if low.startswith("home_"):
        rest = low[5:].replace("_", " ").title()
        return f"{home or 'Home'} {rest}".strip(), point
    if low.startswith("away_"):
        rest = low[5:].replace("_", " ").title()
        return f"{away or 'Away'} {rest}".strip(), point
    m = re.match(r"(over|under)_(\d+(?:\.\d+)?)_(.+)", low)
    if m:
        player = title or m.group(3).replace("_", " ").title()
        player = re.sub(r"\s+(Over|Under)$", "", player, flags=re.I)
        if point is None:
            point = _point(m.group(2))
        return f"{player} {m.group(1).title()}", point
    m = re.match(r"(.+)_(yes|no)$", low)
    if m:
        player = title or m.group(1).replace("_", " ").title()
        player = re.sub(r"\s+(Yes|No)$", "", player, flags=re.I)
        return f"{player} {m.group(2).title()}", point
    if title:
        return title, point
    if k:
        return k.replace("_", " ").title(), point
    return "", point


def _items_from_markets(markets: list[dict], home: str, away: str) -> list:
    items = []
    for mk in markets:
        if mk.get("is_suspended") in (1, "1", True):
            active_market = False
        else:
            active_market = True
        title = _market_title(mk, home, away)
        spread = _point(mk.get("spread"))
        odds = mk.get("odds") or {}
        if not isinstance(odds, dict):
            continue
        for odd in odds.values():
            if not isinstance(odd, dict):
                continue
            vis = str(odd.get("v", "1"))
            if vis in ("0", "false", "False"):
                continue
            price = _price(odd.get("o"))
            if price is None:
                continue
            name, point = _outcome_name(odd, home, away)
            if not name:
                continue
            if point is None:
                point = spread
            items.append((title, name, price, active_market, point))
    return items


def _event_from_ws(
    sport: str,
    meta: dict | None,
    markets: list[dict],
    eid: str,
    fallback_name: str,
) -> Event | None:
    meta = meta or {}
    home = meta.get("home_team") or ""
    away = meta.get("away_team") or ""
    competition = meta.get("league_title") or meta.get("country_title") or ""
    sport_raw = meta.get("sport_title") or meta.get("sport_key") or sport
    starts = meta.get("start_date") or ""
    if starts and "T" not in starts:
        starts = starts.replace(" ", "T") + "Z" if len(starts) >= 19 else starts
    status = meta.get("event_status") or meta.get("event_state") or ""
    name = f"{away} vs {home}" if away and home else fallback_name or eid
    items = _items_from_markets(markets, home, away)
    grouped = group_outcomes(items)
    if not grouped:
        return None
    return build_event(
        bookmaker="ivybet",
        event_id=eid,
        sport_raw=sport_raw,
        competition=competition or "Unknown",
        name=name,
        starts_at=starts,
        status=status,
        markets=grouped,
        home=home,
        away=away,
    )


class IvyBet(Bookmaker):
    key = "ivybet"
    title = "IvyBet"

    async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
        result = ScrapeResult(bookmaker=self.key, started_at=utc_now())
        print("\n" + "=" * 60)
        print("  IvyBet")
        print("=" * 60)

        pw, browser = await launch_browser(cfg.headed)
        try:
            ctx = await new_context(browser)
            page = await ctx.new_page()
            sports = await self._sports(page, cfg)
            print(f"[ivybet] sports: {', '.join(s for s, _ in sports)}")
            for key, href in sports:
                clock = SportClock(result, key)
                try:
                    await self._scrape_sport(ctx, page, key, href, cfg, result)
                except Exception as exc:
                    result.errors.append(f"{key}: {exc}")
                    print(f"    error {key}: {exc}")
                clock.done()
        except Exception as exc:
            result.errors.append(str(exc))
            print(f"[ivybet] Error: {exc}")
        finally:
            await browser.close()
            await pw.stop()

        if not result.events:
            result.errors.append("No IvyBet odds found.")
        result.finished_at = utc_now()
        nmk = sum(len(e.markets) for e in result.events)
        print(
            f"[ivybet] {len(result.events)} events  {nmk} markets  "
            f"{sum(len(e.to_rows()) for e in result.events)} odds rows"
        )
        return result

    async def _sports(self, page: Page, cfg: ScrapeConfig) -> list[tuple[str, str]]:
        await safe_goto(page, f"{BASE}/en/", timeout=45_000)
        await dismiss_cookies(page)
        await page.wait_for_timeout(3500)
        found: list[tuple[str, str]] = []
        try:
            raw = await page.evaluate(EXTRACT_SPORTS)
        except Exception:
            raw = []
        for row in raw or []:
            key = (row.get("key") or "").lower()
            href = row.get("href") or f"{BASE}/en/euro/sport/{key}"
            if key in SKIP_SPORTS:
                continue
            if cfg.sports and not cfg.wants_sport(key, key.replace("-", " ")):
                continue
            found.append((key, href if href.startswith("http") else BASE + href))
        if cfg.wants_sport("baseball") and not any(k == "baseball" for k, _ in found):
            found.insert(0, ("baseball", f"{BASE}/en/euro/sport/baseball"))
        if cfg.wants_sport("formula-1", "f1", "formula1") and not any(k in F1_SLUGS for k, _ in found):
            found.append(("formula1", f"{BASE}/en/euro/sport/formula1"))
        # Digitain uses /sport/formula1; /formula-1 is a 404.
        f1_hits = [(k, h) for k, h in found if k in F1_SLUGS]
        if f1_hits:
            found = [(k, h) for k, h in found if k not in F1_SLUGS]
            preferred = next(
                (h for _, h in f1_hits if "/formula1" in h and "/formula-1" not in h),
                f"{BASE}/en/euro/sport/formula1",
            )
            found.append(("formula1", preferred))
        if not found:
            if cfg.sports:
                slug = re.sub(r"[^a-z0-9]+", "-", cfg.sports[0].lower()).strip("-")
                found = [(slug, f"{BASE}/en/euro/sport/{slug}")]
            else:
                found = [("baseball", f"{BASE}/en/euro/sport/baseball")]
        found.sort(key=lambda x: 0 if x[0] == "baseball" else 1)
        # de-dupe
        seen = set()
        out = []
        for key, href in found:
            if key in seen:
                continue
            seen.add(key)
            out.append((key, href))
        return out

    async def _scrape_sport(
        self, ctx, page: Page, sport: str, href: str, cfg: ScrapeConfig, result: ScrapeResult,
    ) -> None:
        print(f"  > {sport}")
        catcher = _WsCatcher()
        catcher.attach(page)
        listing = href if "tab=" in href else (href + ("?tab=featured" if "/sport/" in href else ""))
        await safe_goto(page, listing, timeout=40_000)
        await page.wait_for_timeout(3500)
        if sport not in F1_SLUGS:
            for label in ("All Leagues", "ALL LEAGUES", "Upcoming", "Early"):
                try:
                    await page.get_by_text(label, exact=False).first.click(timeout=1500)
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass
        # Open each visible league so Digitain emits the rest of the events.
        try:
            leagues = await page.evaluate(
                """() => [...document.querySelectorAll('div,span,a')]
                    .map(e => (e.innerText||'').trim())
                    .filter(t => t && t.length < 40 && !/\\d{2,}/.test(t))
                """
            )
        except Exception:
            leagues = []
        league_clicks = []
        for name in (
            "MLB", "USA Minor League", "CPBL", "Japanese Baseball", "KBO League",
            "Mexican League", "NPB", "KBO",
        ):
            if any(str(x).strip() == name for x in (leagues or [])):
                league_clicks.append(name)
        for name in league_clicks[:12]:
            try:
                await page.get_by_text(name, exact=True).first.click(timeout=1500)
                await page.wait_for_timeout(1800)
            except Exception:
                pass

        try:
            hrefs = await page.evaluate(EXTRACT_EVENTS)
        except Exception:
            hrefs = []
        hrefs = list(dict.fromkeys((hrefs or []) + catcher.event_hrefs()))
        league_hrefs: list[str] = []
        try:
            league_hrefs = await page.evaluate(EXTRACT_LEAGUES, sport if sport not in F1_SLUGS else "formula1")
        except Exception:
            league_hrefs = []
        # Live tab as well for baseball (F1 is outrights, not live matches).
        if sport == "baseball":
            live_url = f"{BASE}/en/euro/sport/{sport}?tab=live"
            await safe_goto(page, live_url, timeout=25_000)
            await page.wait_for_timeout(2500)
            try:
                live_hrefs = await page.evaluate(EXTRACT_EVENTS)
            except Exception:
                live_hrefs = []
            hrefs = list(dict.fromkeys(hrefs + list(live_hrefs or []) + catcher.event_hrefs()))
            try:
                more = await page.evaluate(EXTRACT_LEAGUES, sport if sport not in F1_SLUGS else "formula1")
                league_hrefs = list(dict.fromkeys(list(league_hrefs or []) + list(more or [])))
            except Exception:
                pass

        print(f"    {len(hrefs)} event links  {len(league_hrefs)} league pages")
        listing_items = []
        if sport in F1_SLUGS or not hrefs:
            if sport in F1_SLUGS:
                await safe_goto(page, f"{BASE}/en/euro/sport/formula1?tab=featured", timeout=40_000)
                await page.wait_for_timeout(5000)
            try:
                listing_blob = await page.evaluate("() => document.body.innerText || ''")
                listing_items = parse_outright_text(listing_blob)
                print(f"    listing page outright rows: {len(listing_items)}")
            except Exception:
                listing_blob = ""
            if listing_items:
                label = "Formula 1" if sport in F1_SLUGS else sport.replace("-", " ").title()
                ev = _event_from_outright(
                    sport, label, f"{label} outrights", f"{sport}-listing", listing_items,
                )
                if ev:
                    result.events.append(ev)
        deep = sport == "baseball"
        if deep or cfg.depth == "full":
            visit = hrefs if deep else hrefs[:60]
        else:
            visit = hrefs[:12]
        if not visit:
            visit = hrefs[:12]
        workers = max(1, min(cfg.concurrency, 1 if sport in F1_SLUGS else (3 if deep else 2)))
        pages = [page]
        catchers = [catcher]
        for _ in range(workers - 1):
            wp = await ctx.new_page()
            wc = _WsCatcher()
            wc.attach(wp)
            pages.append(wp)
            catchers.append(wc)
        sem = asyncio.Semaphore(workers)
        done = 0
        lock = asyncio.Lock()

        async def one(url: str, worker: Page, ws: _WsCatcher):
            nonlocal done
            async with sem:
                event = await self._event_page(worker, ws, url, sport)
                async with lock:
                    if event:
                        result.events.append(event)
                    done += 1
                    if done % 8 == 0 or done == len(visit):
                        print(f"    events {done}/{len(visit)}  kept {len(result.events)}")
                await asyncio.sleep(cfg.request_delay)

        await asyncio.gather(
            *(one(url, pages[i % workers], catchers[i % workers]) for i, url in enumerate(visit))
        )
        if league_hrefs and len(listing_items) < 10:
            print(f"    scraping {len(league_hrefs)} outright/league pages")
            done_l = 0

            async def one_league(url: str, worker: Page):
                nonlocal done_l
                async with sem:
                    event = await self._league_page(worker, url, sport)
                    async with lock:
                        if event:
                            result.events.append(event)
                        done_l += 1
                        if done_l == len(league_hrefs) or done_l % 4 == 0:
                            print(f"    leagues {done_l}/{len(league_hrefs)}  kept {len(result.events)}")
                    await asyncio.sleep(cfg.request_delay)

            await asyncio.gather(
                *(one_league(url, pages[i % workers]) for i, url in enumerate(league_hrefs))
            )

    async def _league_page(self, page: Page, url: str, sport: str) -> Event | None:
        if "tab=" not in url:
            url = url + ("&" if "?" in url else "?") + "tab=outrights"
        await dismiss_cookies(page)
        await safe_goto(page, url, timeout=30_000)
        await dismiss_cookies(page)
        await page.wait_for_timeout(6000)
        try:
            await page.evaluate(
                """() => {
                  for (const el of document.querySelectorAll('i,span,div,button')) {
                    const t = (el.innerText || '').trim();
                    if (t === 'expand_more') {
                      try { el.click(); } catch (e) {}
                    }
                  }
                }"""
            )
            await page.wait_for_timeout(1200)
        except Exception:
            pass
        try:
            blob = await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            blob = ""
        items = parse_outright_text(blob)
        if len(items) < 3:
            try:
                rows = await page.evaluate(
                    """() => {
                      const isPrice = (t) => /^\\d+\\/\\d+$/.test(t) || /^\\d+\\.\\d{1,3}$/.test(t);
                      const out = [];
                      const seen = new Set();
                      for (const el of document.querySelectorAll('div,span,button,li,a')) {
                        const t = (el.innerText || '').trim();
                        if (!isPrice(t) || (el.innerText || '').includes('\\n')) continue;
                        const parent = el.parentElement;
                        const lines = ((parent && parent.innerText) || '').split('\\n').map(s => s.trim()).filter(Boolean);
                        const idx = lines.indexOf(t);
                        const name = idx > 0 ? lines[idx - 1] : '';
                        if (!name || isPrice(name) || name.length > 70) continue;
                        const key = name + '|' + t;
                        if (seen.has(key)) continue;
                        seen.add(key);
                        out.push({name, price: t});
                      }
                      return out;
                    }"""
                )
            except Exception:
                rows = []
            for row in rows or []:
                price = _listing_price(row.get("price"))
                name = (row.get("name") or "").strip()
                if price and name:
                    items.append(("Winner", name, price, True, None))
        name = _league_name_from_url(url)
        eid = "league-" + _league_id_from_url(url)
        if not items:
            print(f"    no odds on {name} ({len(blob)} chars)")
        return _event_from_outright(sport, name, name, eid, items)

    async def _event_page(self, page: Page, ws: _WsCatcher, url: str, sport: str) -> Event | None:
        eid = _event_id_from_url(url)
        before = len(ws.initials)
        if "marketCategory=" not in url:
            url = url + ("&" if "?" in url else "?") + "marketCategory=all"
        await safe_goto(page, url, timeout=30_000)
        meta, markets = await self._wait_odds(ws, eid, before, timeout=14.0)
        if not markets:
            # DOM fallback: expand coupons and harvest whatever text is visible.
            try:
                await page.evaluate(
                    """() => {
                      for (const el of document.querySelectorAll('i,span,div,button')) {
                        if ((el.innerText || '').trim() === 'expand_more') {
                          try { el.click(); } catch (e) {}
                        }
                      }
                    }"""
                )
                await page.wait_for_timeout(800)
            except Exception:
                pass
            meta, markets = ws.odds_payload(eid)
        if not markets:
            return None
        return _event_from_ws(sport, meta, markets, eid, _slug_name(url))

    async def _wait_odds(
        self, ws: _WsCatcher, eid: str, before: int, timeout: float,
    ) -> tuple[dict | None, list[dict]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            meta, markets = ws.odds_payload(eid)
            if markets:
                return meta, markets
            await asyncio.sleep(0.25)
        return ws.odds_payload(eid)
