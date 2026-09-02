"""Betway — every competition, then each event page with all markets expanded."""
from __future__ import annotations

import asyncio
import re

from playwright.async_api import Page

from bookie_scraper.bookmakers.base import Bookmaker
from bookie_scraper.browser import dismiss_cookies, launch_browser, new_context, safe_goto
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.models import Event, ScrapeResult, utc_now
from bookie_scraper.normalize import build_event, group_outcomes, parse_number, parse_price

BASE = "https://betway.com/g/en"

SKIP_SPORTS = {
    "betway-boosts", "find", "live", "specials", "politics",
    "horse-racing", "greyhounds", "hub",
}

FALLBACK_SPORTS = [
    "soccer", "basketball", "tennis", "ice-hockey", "american-football",
    "baseball", "volleyball", "handball", "table-tennis", "esports",
    "darts", "snooker", "boxing", "ufc---martial-arts", "cricket",
    "rugby-union", "rugby-league", "golf", "formula-1", "futsal",
    "cycling", "badminton", "aussie-rules", "beach-volleyball",
    "field-hockey", "motor-sport", "e-leagues", "gaelic-sports",
]

EXTRACT_LISTING = """() => {
  const isOdds = (t) => /^\\d+[.,]\\d{2}$/.test((t || '').trim());
  const heading = (document.querySelector('h1')?.innerText || '').trim();
  const byId = {};
  for (const el of document.querySelectorAll('[data-eventid]')) {
    const eid = el.getAttribute('data-eventid');
    if (!eid) continue;
    const prev = byId[eid];
    const len = (el.innerText || '').length;
    if (!prev || len > prev.len) byId[eid] = {el, len};
  }
  const cards = [];
  for (const [eid, wrap] of Object.entries(byId)) {
    const raw = (wrap.el.innerText || '').trim();
    const lines = raw.split('\\n').map(s => s.trim()).filter(Boolean);
    const odds = [];
    const names = [];
    let time = '';
    for (const line of lines) {
      if (isOdds(line)) odds.push(line.replace(',', '.'));
      else if (/^\\d{1,2}:\\d{2}$/.test(line)) time = line;
      else if (!/^(more bets|watch|live|enhanced|home|draw|away|cash out)$/i.test(line)) names.push(line);
    }
    cards.push({
      eventId: eid,
      home: names[0] || '',
      away: names[1] || '',
      odds,
      time,
    });
  }
  const grp = [...new Set(
    [...document.querySelectorAll('a[href*="/sports/grp/"]')].map(a => a.href)
  )];
  return {heading, cards, eventIds: Object.keys(byId), grp};
}"""

EXTRACT_EVENT = """async () => {
  const isPrice = (t) => /^\\d+[.,]\\d{2}$/.test((t || '').trim());
  const skip = /^(cash out|sign up|log in|home|in-play|casino|starting soon|tomorrow|live|watch|more bets|accept|help|find games|upcoming matches|money)$/i;
  const h1 = document.querySelector('h1');
  let root = h1;
  for (let i = 0; i < 8 && root; i++) root = root.parentElement;
  root = root || document.body;

  const clickHeaders = () => {
    let n = 0;
    for (const el of root.querySelectorAll('div, span, h2, h3, [role="button"]')) {
      const t = (el.innerText || '').trim();
      if (!t || t.length > 90) continue;
      const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
      if (lines.length === 0 || lines.length > 3) continue;
      if (lines.some(isPrice)) continue;
      if (skip.test(lines[0])) continue;
      if (el.querySelector('button')) continue;
      if (el.offsetHeight > 100) continue;
      try { el.click(); n++; } catch (e) {}
    }
    return n;
  };
  clickHeaders();
  await new Promise(r => setTimeout(r, 400));
  clickHeaders();
  await new Promise(r => setTimeout(r, 500));
  clickHeaders();
  await new Promise(r => setTimeout(r, 400));

  const heading = (h1?.innerText || '').trim();
  const tables = [...root.querySelectorAll('[class*="marketTableItem__StyledDiv"]')];
  const markets = [];
  const seen = new Set();
  const titleFor = (table) => {
    let n = table;
    for (let i = 0; i < 10 && n; i++) {
      let sib = n.previousElementSibling;
      while (sib) {
        const raw = (sib.innerText || '').trim();
        const line = raw.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
        if (line && line.length < 90 && !isPrice(line) && !skip.test(line) && !sib.querySelector('button')) {
          const extra = raw.split('\\n').map(s => s.trim()).filter(l => l && l !== line && !skip.test(l) && !isPrice(l));
          return extra.length ? (line + ' ' + extra[0]) : line;
        }
        sib = sib.previousElementSibling;
      }
      n = n.parentElement;
    }
    return '';
  };
  const buttonOdds = (b, fallback) => {
    const lines = (b.innerText || '').trim().split('\\n').map(s => s.trim()).filter(Boolean);
    const price = lines.find(isPrice);
    if (!price) return null;
    const label = lines.filter(l => !isPrice(l)).join(' ') || fallback || lines[0];
    return {label, price: price.replace(',', '.')};
  };
  for (const table of tables) {
    const odds = [];
    const rows = [...table.querySelectorAll('tr')];
    if (rows.length >= 2) {
      let headerCells = [...rows[0].querySelectorAll('td, th')].map(td => (td.innerText || '').trim()).filter(Boolean);
      let priceRow = rows[rows.length - 1];
      for (const row of rows) {
        const texts = [...row.querySelectorAll('td, th')].map(td => (td.innerText || '').trim()).filter(Boolean);
        const hasBtn = !!row.querySelector('button');
        if (!hasBtn && texts.length && !texts.every(isPrice)) headerCells = texts;
        if (hasBtn) priceRow = row;
      }
      const priceBtns = [...priceRow.querySelectorAll('button')];
      for (let i = 0; i < priceBtns.length; i++) {
        const row = buttonOdds(priceBtns[i], headerCells[i] || '');
        if (row) odds.push(row);
      }
    }
    if (!odds.length) {
      for (const b of table.querySelectorAll('button')) {
        const row = buttonOdds(b, '');
        if (row) odds.push(row);
      }
    }
    if (odds.length < 1) continue;
    const title = titleFor(table) || 'Market';
    const key = title + '|' + odds.map(o => o.label + o.price).join(',');
    if (seen.has(key)) continue;
    seen.add(key);
    markets.push({title, odds});
  }
  if (!markets.length) {
    const buttons = [...root.querySelectorAll('button')].filter(b => isPrice((b.innerText || '').trim().split('\\n').pop()));
    const odds = [];
    for (const b of buttons) {
      const lines = (b.innerText || '').trim().split('\\n').map(s => s.trim()).filter(Boolean);
      const price = lines.find(isPrice);
      if (!price) continue;
      odds.push({label: lines.filter(l => !isPrice(l)).join(' ') || lines[0], price: price.replace(',', '.')});
    }
    if (odds.length >= 2) markets.push({title: heading.split('\\n')[0] || 'Winner', odds});
  }
  return {heading, markets};
}"""


def _sport_from_url(url: str, fallback: str) -> str:
    m = re.search(r"/sports/(?:cat|grp|evt|event)/([^/?]+)", url)
    slug = m.group(1) if m else fallback
    if slug.isdigit():
        return fallback
    return slug


def _comp_from_url(url: str) -> str:
    m = re.search(r"/sports/grp/[^/]+/(.+?)(?:\?|$)", url)
    if not m:
        return ""
    return m.group(1).replace("/", " / ").replace("-", " ").title()


def _heading_comp(heading: str, fallback: str) -> str:
    lines = [ln.strip() for ln in (heading or "").split("\n") if ln.strip()]
    if len(lines) >= 2:
        return lines[1]
    return lines[0] if lines else fallback


def _outcome_bits(label: str) -> tuple[str, float | None]:
    t = (label or "").strip()
    m = re.match(r"^([OU])\s*([+-]?\d+(?:\.\d+)?)$", t, re.I)
    if m:
        name = "Over" if m.group(1).upper() == "O" else "Under"
        return name, float(m.group(2))
    if re.match(r"^[+-]\d+(?:\.\d+)?$", t):
        return t, float(t)
    m = re.match(r"^([+-]?\d+\.5)$", t)
    if m:
        return t, float(m.group(1))
    return t, None


def _from_markets(
    sport: str, competition: str, name: str, home: str, away: str, eid: str, markets_raw: list,
) -> Event | None:
    items = []
    for mk in markets_raw:
        title = (mk.get("title") or "Market").strip()
        raw_ocs = list(mk.get("odds") or [])
        labels = [(oc.get("label") or "").strip() for oc in raw_ocs]
        all_prices = bool(labels) and all(re.match(r"^\d+[.,]\d{2}$", lb) for lb in labels)
        for i, oc in enumerate(raw_ocs):
            label = (oc.get("label") or "").strip()
            if all_prices and home and away:
                if len(raw_ocs) == 2:
                    label = home if i == 0 else away
                elif len(raw_ocs) == 3:
                    label = [home, "Draw", away][i]
            name, point = _outcome_bits(label)
            items.append((title, name, parse_price(oc.get("price")), True, point))
    grouped = group_outcomes(items)
    if not grouped:
        return None
    if not home and not away and " vs" in name.lower():
        pass
    return build_event(
        bookmaker="betway",
        event_id=eid or name,
        sport_raw=sport,
        competition=competition or "Unknown",
        name=name,
        starts_at="",
        status="",
        markets=grouped,
        home=home,
        away=away,
    )


def _from_card(sport: str, competition: str, card: dict) -> Event | None:
    home = card.get("home") or ""
    away = card.get("away") or ""
    odds = [parse_price(o) for o in card.get("odds") or []]
    odds = [o for o in odds if o is not None]
    if len(odds) < 2 or not home:
        return None
    name = f"{home} vs {away}" if away else home
    if len(odds) >= 3:
        items = [
            ("1X2", home, odds[0], True, None),
            ("1X2", "Draw", odds[1], True, None),
            ("1X2", away or "Away", odds[2], True, None),
        ]
    else:
        items = [
            ("Match Winner", home, odds[0], True, None),
            ("Match Winner", away or "Away", odds[1], True, None),
        ]
    return build_event(
        bookmaker="betway",
        event_id=str(card.get("eventId") or f"{home}|{away}|{card.get('time') or ''}"),
        sport_raw=sport,
        competition=competition or "Unknown",
        name=name,
        starts_at=card.get("time") or "",
        status="",
        markets=group_outcomes(items),
        home=home,
        away=away,
    )


class Betway(Bookmaker):
    key = "betway"
    title = "Betway"

    async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
        result = ScrapeResult(bookmaker=self.key, started_at=utc_now())
        print("\n" + "=" * 60)
        print("  Betway")
        print("=" * 60)

        pw, browser = await launch_browser(cfg.headed)
        try:
            ctx = await new_context(browser)
            page = await ctx.new_page()
            sports = await self._sport_slugs(page, cfg)
            comp_links: list[str] = []
            pending: dict[str, dict] = {}
            cards_fallback: list[tuple[str, str, dict]] = []

            for slug in sports:
                print(f"  > {slug}")
                hubs = [
                    f"{BASE}/sports/cat/{slug}",
                    f"{BASE}/sports/cat/{slug}/all",
                    f"{BASE}/sports/cat/{slug}/live",
                    f"{BASE}/sports/cat/{slug}/popular",
                ]
                sport_comps = []
                for url in hubs:
                    listing = await self._listing(page, url)
                    for link in listing.get("grp") or []:
                        if f"/sports/grp/{slug}/" not in link:
                            continue
                        if link not in comp_links:
                            comp_links.append(link)
                            sport_comps.append(link)
                    self._ingest_listing(slug, listing, pending, cards_fallback)
                print(f"    competitions: {len(sport_comps)}")

            print(f"[betway] Visiting {len(comp_links)} competition pages...")
            outright_pages: list[tuple[str, str, str]] = []
            for i, url in enumerate(comp_links):
                listing = await self._listing(page, url)
                sport_slug = _sport_from_url(url, "unknown")
                heading = (listing.get("heading") or "").split("\n")[0].strip()
                comp = heading or _comp_from_url(url)
                n_before = len(pending)
                self._ingest_listing(sport_slug, listing, pending, cards_fallback, competition=comp)
                if len(pending) == n_before and not (listing.get("cards") or []):
                    outright_pages.append((url, sport_slug, comp))
                if (i + 1) % 5 == 0:
                    print(f"    {i + 1}/{len(comp_links)}  events queued: {len(pending)}")

            if cfg.depth == "full":
                await self._scrape_events(ctx, page, pending, outright_pages, cfg, result)
            else:
                seen: set[str] = set()
                for sport, comp, card in cards_fallback:
                    event = _from_card(sport, comp, card)
                    if event and event.event_id not in seen:
                        seen.add(event.event_id)
                        result.events.append(event)
                for url, sport, comp in outright_pages:
                    event = await self._event_from_page(page, url, sport, comp, "", "", comp, url)
                    if event and event.event_id not in seen:
                        seen.add(event.event_id)
                        result.events.append(event)
        except Exception as exc:
            result.errors.append(str(exc))
            print(f"[betway] Error: {exc}")
        finally:
            await browser.close()
            await pw.stop()

        if not result.events:
            result.errors.append("No Betway odds found on competition/event pages.")
        result.finished_at = utc_now()
        nmk = sum(len(e.markets) for e in result.events)
        print(f"[betway] {len(result.events)} events  {nmk} markets  {sum(len(e.to_rows()) for e in result.events)} odds rows")
        return result

    async def _sport_slugs(self, page: Page, cfg: ScrapeConfig) -> list[str]:
        await safe_goto(page, f"{BASE}/sports/live", timeout=40_000)
        await dismiss_cookies(page)
        await page.wait_for_timeout(2000)
        discovered: list[str] = []
        try:
            hrefs = await page.eval_on_selector_all(
                "a[href*='/sports/cat/']",
                "els => [...new Set(els.map(e => e.href))]",
            )
        except Exception:
            hrefs = []
        for href in hrefs or []:
            m = re.search(r"/sports/cat/([^/?#]+)", href)
            if not m:
                continue
            slug = m.group(1)
            if slug in SKIP_SPORTS or slug in discovered:
                continue
            discovered.append(slug)
        if not discovered:
            discovered = list(FALLBACK_SPORTS)
        if cfg.sports:
            discovered = [s for s in discovered if cfg.wants_sport(s)]
            extra = [s for s in FALLBACK_SPORTS if cfg.wants_sport(s) and s not in discovered]
            discovered.extend(extra)
        return discovered

    async def _listing(self, page: Page, url: str) -> dict:
        await safe_goto(page, url, timeout=25_000)
        await dismiss_cookies(page)
        await page.wait_for_timeout(1800)
        try:
            data = await page.evaluate(EXTRACT_LISTING)
        except Exception:
            data = {}
        return data or {}

    def _ingest_listing(
        self,
        sport: str,
        listing: dict,
        pending: dict[str, dict],
        cards_fallback: list,
        competition: str = "",
    ) -> None:
        heading = listing.get("heading") or ""
        comp = competition or _heading_comp(heading, sport)
        for card in listing.get("cards") or []:
            eid = str(card.get("eventId") or "")
            cards_fallback.append((sport, comp, card))
            if not eid:
                continue
            meta = pending.setdefault(eid, {
                "sport": sport,
                "competition": comp,
                "home": card.get("home") or "",
                "away": card.get("away") or "",
                "name": "",
                "time": card.get("time") or "",
                "odds": card.get("odds") or [],
            })
            if card.get("home"):
                meta["home"] = card["home"]
            if card.get("away"):
                meta["away"] = card["away"]
            if card.get("odds"):
                meta["odds"] = card["odds"]
            if not meta.get("competition"):
                meta["competition"] = comp
        for eid in listing.get("eventIds") or []:
            pending.setdefault(str(eid), {
                "sport": sport,
                "competition": comp,
                "home": "",
                "away": "",
                "name": "",
                "time": "",
            })

    async def _scrape_events(
        self, ctx, page: Page, pending: dict[str, dict],
        outright_pages: list[tuple[str, str, str]],
        cfg: ScrapeConfig, result: ScrapeResult,
    ) -> None:
        items = list(pending.items())
        print(f"[betway] Opening {len(items)} event pages for full markets...")
        workers = max(1, min(cfg.concurrency, 3))
        pages = [page]
        for _ in range(workers - 1):
            pages.append(await ctx.new_page())
        sem = asyncio.Semaphore(workers)
        done = 0
        lock = asyncio.Lock()

        async def one(eid: str, meta: dict, worker: Page):
            nonlocal done
            async with sem:
                url = f"{BASE}/sports/event/{eid}"
                home = meta.get("home") or ""
                away = meta.get("away") or ""
                name = meta.get("name") or (f"{home} vs {away}" if home else eid)
                event = await self._event_from_page(
                    worker, url, meta.get("sport") or "unknown",
                    meta.get("competition") or "Unknown",
                    home, away, name, eid,
                )
                if not event:
                    event = _from_card(meta.get("sport") or "unknown", meta.get("competition") or "", {
                        "eventId": eid, "home": home, "away": away,
                        "odds": meta.get("odds") or [], "time": meta.get("time") or "",
                    })
                async with lock:
                    if event:
                        result.events.append(event)
                    done += 1
                    if done % 10 == 0 or done == len(items):
                        nmk = sum(len(e.markets) for e in result.events)
                        print(f"    events {done}/{len(items)}  markets {nmk}")
                await asyncio.sleep(cfg.request_delay)

        tasks = []
        for i, (eid, meta) in enumerate(items):
            tasks.append(one(eid, meta, pages[i % workers]))
        await asyncio.gather(*tasks)

        for url, sport, comp in outright_pages:
            event = await self._event_from_page(page, url, sport, comp, "", "", comp, url)
            if event:
                result.events.append(event)

    async def _event_from_page(
        self, page: Page, url: str, sport: str, competition: str,
        home: str, away: str, name: str, eid: str,
    ) -> Event | None:
        await safe_goto(page, url, timeout=25_000)
        await dismiss_cookies(page)
        try:
            await page.wait_for_selector('[class*="marketTableItem"], [data-eventid]', timeout=6000)
        except Exception:
            pass
        await page.wait_for_timeout(800)
        try:
            detail = await page.evaluate(EXTRACT_EVENT)
        except Exception:
            detail = {}
        markets = (detail or {}).get("markets") or []
        title = ((detail or {}).get("heading") or "").strip()
        lines = [ln.strip() for ln in title.split("\n") if ln.strip()]
        if lines:
            name = lines[0].replace(" vs. ", " vs ") or name
            if len(lines) >= 2:
                competition = lines[1]
        if not home and " vs " in name:
            parts = name.split(" vs ", 1)
            home, away = parts[0].strip(), parts[1].strip()
        return _from_markets(sport, competition, name, home, away, eid, markets)
