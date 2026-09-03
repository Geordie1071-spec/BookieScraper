"""Unibet — Kambi offering API (httpx, no browser)."""
from __future__ import annotations

import asyncio

import httpx

from bookie_scraper.bookmakers.base import Bookmaker
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.http import DEFAULT_TIMEOUT
from bookie_scraper.models import Event, ScrapeResult, utc_now, SportClock
from bookie_scraper.normalize import build_event, group_outcomes, parse_number, parse_price

ROOT = "https://eu.offering-api.kambicdn.com/offering/v2018/ub"
PARAMS = {"lang": "en_GB", "market": "GB"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.unibet.com/",
    "Origin": "https://www.unibet.com",
}
SKIP_SPORTS = {
    "politics",
    "trotting",
    "tv___novelty",
    "tv & novelty",
    "horse racing",
    "greyhounds",
}


def kambi_odds(raw) -> float | None:
    """Kambi stores decimal odds in milles (1930 -> 1.93)."""
    n = parse_number(raw)
    if n is None:
        return None
    if n >= 100:
        n = n / 1000.0
    return parse_price(n)


def kambi_line(raw) -> float | None:
    n = parse_number(raw)
    if n is None:
        return None
    if abs(n) >= 100:
        n = n / 1000.0
    return n


def _outcome_name(out: dict, home: str, away: str) -> str:
    participant = str(out.get("participant") or "").strip()
    if participant:
        return participant
    typ = str(out.get("type") or "")
    if typ == "OT_ONE" and home:
        return home
    if typ == "OT_TWO" and away:
        return away
    if typ == "OT_CROSS":
        return "Draw"
    return str(out.get("englishLabel") or out.get("label") or "").strip()


def _market_name(offer: dict) -> str:
    crit = offer.get("criterion") or {}
    otype = offer.get("betOfferType") or {}
    criterion = str(crit.get("englishLabel") or crit.get("label") or "").strip()
    kind = str(otype.get("englishName") or otype.get("name") or "").strip()
    if criterion and kind and kind.lower() not in {"match", criterion.lower()}:
        if kind.lower() not in criterion.lower():
            return f"{kind} {criterion}"
    return criterion or kind or "Market"


def items_from_offers(offers: list, home: str, away: str) -> list:
    items = []
    for offer in offers or []:
        if not isinstance(offer, dict):
            continue
        mname = _market_name(offer)
        for out in offer.get("outcomes") or []:
            if not isinstance(out, dict):
                continue
            status = str(out.get("status") or "OPEN").upper()
            if status in {"SUSPENDED", "CLOSED", "SETTLED"}:
                continue
            price = kambi_odds(out.get("odds"))
            if price is None:
                continue
            oname = _outcome_name(out, home, away)
            if not oname:
                continue
            items.append((mname, oname, price, status == "OPEN", kambi_line(out.get("line"))))
    return items


def event_from_kambi(event: dict, offers: list) -> Event | None:
    if not isinstance(event, dict):
        return None
    home = str(event.get("homeName") or "").strip()
    away = str(event.get("awayName") or "").strip()
    name = str(event.get("englishName") or event.get("name") or "").strip()
    if not name:
        name = f"{home} vs {away}".strip(" vs")
    path = event.get("path") or []
    sport = "unknown"
    competition = str(event.get("group") or "Unknown")
    if path and isinstance(path[0], dict):
        sport = str(path[0].get("englishName") or path[0].get("name") or sport)
    if path and isinstance(path[-1], dict):
        competition = str(path[-1].get("englishName") or path[-1].get("name") or competition)
    items = items_from_offers(offers, home, away)
    grouped = group_outcomes(items)
    if not grouped:
        return None
    return build_event(
        bookmaker="unibet",
        event_id=str(event.get("id") or ""),
        sport_raw=sport,
        competition=competition,
        name=name,
        starts_at=str(event.get("start") or ""),
        status=str(event.get("state") or ""),
        markets=grouped,
        home=home,
        away=away,
    )


class Unibet(Bookmaker):
    key = "unibet"
    title = "Unibet"

    async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
        result = ScrapeResult(bookmaker=self.key, started_at=utc_now())
        print("\n" + "=" * 60)
        print("  Unibet")
        print("=" * 60)

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            sports = await self._sports(client, cfg)
            print(f"[unibet] {len(sports)} sports")
            for term, sname, sid in sports:
                try:
                    await self._scrape_sport(client, term, sname, cfg, result)
                except Exception as exc:
                    result.errors.append(f"{sname}: {exc}")
                    print(f"    error {sname}: {exc}")

        if not result.events:
            result.errors.append("No Unibet odds found.")
        result.finished_at = utc_now()
        nmk = sum(len(e.markets) for e in result.events)
        print(
            f"[unibet] {len(result.events)} events  {nmk} markets  "
            f"{sum(len(e.to_rows()) for e in result.events)} odds rows"
        )
        return result

    async def _get(self, client: httpx.AsyncClient, path: str) -> dict:
        resp = await client.get(f"{ROOT}{path}", params=PARAMS)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _sports(self, client: httpx.AsyncClient, cfg: ScrapeConfig) -> list[tuple[str, str, int]]:
        data = await self._get(client, "/group.json")
        groups = ((data.get("group") or {}).get("groups")) or []
        out: list[tuple[str, str, int]] = []
        for sp in groups:
            if not isinstance(sp, dict):
                continue
            name = str(sp.get("englishName") or sp.get("name") or "").strip()
            term = str(sp.get("termKey") or "").strip()
            sid = sp.get("id")
            if not name or not term:
                continue
            if name.lower() in SKIP_SPORTS or term.lower() in SKIP_SPORTS:
                continue
            if cfg.sports and not cfg.wants_sport(name, term, str(sid or "")):
                continue
            out.append((term, name, int(sid) if sid is not None else 0))
        return out

    async def _scrape_sport(
        self,
        client: httpx.AsyncClient,
        term: str,
        sname: str,
        cfg: ScrapeConfig,
        result: ScrapeResult,
    ) -> None:
        clock = SportClock(result, sname)
        print(f"  > {sname}")
        data = await self._get(client, f"/listView/{term}.json")
        rows = [r for r in (data.get("events") or []) if isinstance(r, dict)]
        print(f"    {len(rows)} events")

        sem = asyncio.Semaphore(max(1, min(cfg.concurrency, 4)))
        lock = asyncio.Lock()
        kept = 0

        async def one(row: dict) -> None:
            nonlocal kept
            event = row.get("event") or {}
            offers = row.get("betOffers") or []
            if cfg.depth == "full":
                eid = event.get("id") if isinstance(event, dict) else None
                if eid is not None:
                    async with sem:
                        try:
                            full = await self._get(client, f"/betoffer/event/{eid}.json")
                            offers = full.get("betOffers") or offers
                            evs = full.get("events") or []
                            if evs and isinstance(evs[0], dict):
                                event = evs[0]
                        except Exception as exc:
                            result.errors.append(f"{sname} {eid}: {exc}")
                        await asyncio.sleep(cfg.request_delay)
            built = event_from_kambi(event if isinstance(event, dict) else {}, offers)
            if built:
                async with lock:
                    result.events.append(built)
                    kept += 1

        if cfg.depth == "full":
            await asyncio.gather(*(one(row) for row in rows))
        else:
            for row in rows:
                await one(row)
        print(f"    kept {kept} events")
        clock.done()
