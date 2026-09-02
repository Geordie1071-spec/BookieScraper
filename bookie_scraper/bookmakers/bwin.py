"""Bwin — Entain CDS API (cds-api/bettingoffer). Baseball fixture-view includes player props."""
from __future__ import annotations

import asyncio
import re

import httpx

from bookie_scraper.bookmakers.base import Bookmaker
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.http import DEFAULT_TIMEOUT
from bookie_scraper.models import Event, ScrapeResult, utc_now, SportClock
from bookie_scraper.normalize import build_event, group_outcomes, parse_price

ACCESS_ID = "NTZiMjk3OGMtNjU5Mi00NjA5LWI2MWItZmU4MDRhN2QxZmEz"
CDS = "https://www.bwin.com/cds-api/bettingoffer"
SKIP_SPORTS = {"horse racing", "greyhounds", "politics"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bwin.com/en/sports",
    "Origin": "https://www.bwin.com",
}

# Known Entain sport ids (filled/updated from the fixtures listing at runtime).
SPORT_IDS = {
    4: "Football",
    5: "Tennis",
    6: "Formula 1",
    7: "Basketball",
    9: "Alpine Skiing",
    10: "Cycling",
    11: "American Football",
    12: "Ice Hockey",
    13: "Golf",
    16: "Handball",
    18: "Volleyball",
    22: "Cricket",
    23: "Baseball",
    24: "Boxing",
    25: "Specials",
    29: "Horse Racing",
    31: "Rugby League",
    32: "Rugby Union",
    33: "Snooker",
    34: "Darts",
    37: "Greyhounds",
    39: "NASCAR",
    42: "Speedway",
    44: "Badminton",
    45: "Combat sports",
    48: "Gaelic Games",
    52: "Water Polo",
    56: "Table Tennis",
    60: "Entertainment",
    61: "Politics",
    64: "Biathlon",
    67: "Chess",
    94: "Cross Country Skiing",
    105: "CS2",
    106: "League of Legends",
    107: "Dota 2",
    108: "eSoccer",
}


def _nv(value) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or "")
    return str(value or "")


def _point(raw) -> float | None:
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        m = re.search(r"[+-]?\d+(?:\.\d+)?", text)
        return float(m.group(0)) if m else None


def _base_params() -> dict[str, str]:
    return {
        "x-bwin-accessid": ACCESS_ID,
        "lang": "en",
        "country": "MT",
        "userCountry": "MT",
        "scoreboardMode": "Full",
    }


def _teams(fx: dict) -> tuple[str, str]:
    home = away = ""
    for p in fx.get("participants") or []:
        if not isinstance(p, dict):
            continue
        props = p.get("properties") or {}
        kind = str(props.get("type") or "")
        name = _nv(p.get("name"))
        if kind == "HomeTeam":
            home = name
        elif kind == "AwayTeam":
            away = name
    if not home or not away:
        names = []
        for p in fx.get("participants") or []:
            if not isinstance(p, dict):
                continue
            props = p.get("properties") or {}
            if str(props.get("type") or "") == "Player":
                continue
            names.append(_nv(p.get("name")))
        names = [n for n in names if n]
        # first two unique team-like names
        uniq = []
        for n in names:
            if n not in uniq:
                uniq.append(n)
        if not away and len(uniq) >= 1:
            away = uniq[0]
        if not home and len(uniq) >= 2:
            home = uniq[1]
        elif not home and len(uniq) == 1:
            home = uniq[0]
    return home, away


def _items_from_options(markets: list[dict]) -> list:
    items = []
    for om in markets:
        if not isinstance(om, dict):
            continue
        status = str(om.get("status") or "Visible")
        if status.lower() in ("hidden", "archived"):
            continue
        mname = _nv(om.get("name")) or "Market"
        attr_point = _point(om.get("attr"))
        spread_point = _point(om.get("spread")) if om.get("spread") not in (0, 0.0, None) else None
        for opt in om.get("options") or []:
            if not isinstance(opt, dict):
                continue
            ost = str(opt.get("status") or "Visible")
            if ost.lower() in ("hidden",):
                continue
            price_obj = opt.get("price") or {}
            price = parse_price(price_obj.get("odds") if isinstance(price_obj, dict) else price_obj)
            if price is None:
                continue
            oname = _nv(opt.get("name")) or _nv(opt.get("sourceName"))
            if not oname:
                continue
            prefix = opt.get("totalsPrefix")
            point = attr_point
            m = re.match(r"^(Over|Under)\s+(.+)$", oname, re.I)
            if m:
                oname = m.group(1).title()
                point = _point(m.group(2)) or point
            elif prefix:
                oname = str(prefix).title()
            if point is None:
                point = spread_point
            active = ost.lower() == "visible" and status.lower() == "visible"
            items.append((mname, oname, price, active, point))
    return items


def _items_from_games(games: list[dict]) -> list:
    items = []
    for game in games:
        if not isinstance(game, dict):
            continue
        mname = _nv(game.get("name")) or "Market"
        for res in game.get("results") or []:
            if not isinstance(res, dict):
                continue
            vis = str(res.get("visibility") or "Visible")
            if vis.lower() == "hidden":
                continue
            price = parse_price(res.get("odds"))
            if price is None:
                continue
            oname = _nv(res.get("name"))
            if not oname:
                continue
            items.append((mname, oname, price, vis.lower() == "visible", None))
    return items


def _event_from_fixture(fx: dict, extra_markets: dict | None = None) -> Event | None:
    fx = extra_markets or fx
    home, away = _teams(fx)
    name = _nv(fx.get("name")) or (f"{away} vs {home}" if away and home else str(fx.get("id") or ""))
    sport = _nv((fx.get("sport") or {}).get("name")) or "unknown"
    competition = _nv((fx.get("competition") or {}).get("name")) or "Unknown"
    starts = fx.get("startDate") or ""
    stage = fx.get("stage") or fx.get("liveType") or ""
    items = _items_from_options(fx.get("optionMarkets") or [])
    items.extend(_items_from_games(fx.get("games") or []))
    grouped = group_outcomes(items)
    if not grouped:
        return None
    return build_event(
        bookmaker="bwin",
        event_id=str(fx.get("id") or ""),
        sport_raw=sport,
        competition=competition,
        name=name,
        starts_at=str(starts),
        status=str(stage or ""),
        markets=grouped,
        home=home,
        away=away,
    )


class Bwin(Bookmaker):
    key = "bwin"
    title = "Bwin"

    async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
        result = ScrapeResult(bookmaker=self.key, started_at=utc_now())
        print("\n" + "=" * 60)
        print("  Bwin")
        print("=" * 60)

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            sports = await self._sports(client, cfg)
            print(f"[bwin] {len(sports)} sports")
            for sid, sname in sports:
                try:
                    await self._scrape_sport(client, sid, sname, cfg, result)
                except Exception as exc:
                    result.errors.append(f"{sname}: {exc}")
                    print(f"    error {sname}: {exc}")

        if not result.events:
            result.errors.append("No Bwin odds found.")
        result.finished_at = utc_now()
        nmk = sum(len(e.markets) for e in result.events)
        print(
            f"[bwin] {len(result.events)} events  {nmk} markets  "
            f"{sum(len(e.to_rows()) for e in result.events)} odds rows"
        )
        return result

    async def _sports(self, client: httpx.AsyncClient, cfg: ScrapeConfig) -> list[tuple[int, str]]:
        discovered = dict(SPORT_IDS)
        try:
            data = await self._get(
                client,
                f"{CDS}/fixtures",
                {**_base_params(), "maxItems": "80"},
            )
            for fx in (data or {}).get("fixtures") or []:
                sp = fx.get("sport") or {}
                sid = sp.get("id")
                name = _nv(sp.get("name"))
                if sid is not None and name:
                    discovered[int(sid)] = name
        except Exception as exc:
            print(f"[bwin] sports discovery: {exc}")
        out = []
        for sid, name in sorted(discovered.items(), key=lambda x: (0 if x[1].lower() == "baseball" else 1, x[1])):
            if name.lower() in SKIP_SPORTS:
                continue
            if cfg.sports and not cfg.wants_sport(name, str(sid), f"sport-{sid}"):
                continue
            out.append((sid, name))
        if cfg.wants_sport("baseball") and not any(n.lower() == "baseball" for _, n in out):
            out.insert(0, (23, "Baseball"))
        if cfg.wants_sport("formula-1", "f1", "formula1") and not any("formula" in n.lower() for _, n in out):
            out.append((6, "Formula 1"))
        return out

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict) -> dict:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _list_fixtures(self, client: httpx.AsyncClient, sport_id: int) -> list[dict]:
        fixtures: list[dict] = []
        offset = 0
        page = 200
        while True:
            data = await self._get(
                client,
                f"{CDS}/fixtures",
                {
                    **_base_params(),
                    "sportIds": str(sport_id),
                    "maxItems": str(page),
                    "skip": str(offset),
                    "offset": str(offset),
                },
            )
            batch = data.get("fixtures") or []
            if not isinstance(batch, list) or not batch:
                break
            fixtures.extend(f for f in batch if isinstance(f, dict))
            if len(batch) < page:
                break
            offset += len(batch)
            if offset > 4000:
                break
        # de-dupe by id
        seen = set()
        out = []
        for fx in fixtures:
            fid = fx.get("id")
            if fid in seen:
                continue
            seen.add(fid)
            out.append(fx)
        return out

    async def _fixture_view(self, client: httpx.AsyncClient, fixture_id) -> dict | None:
        data = await self._get(
            client,
            f"{CDS}/fixture-view",
            {
                **_base_params(),
                "offerMapping": "All",
                "fixtureIds": str(fixture_id),
                "state": "Latest",
            },
        )
        fx = data.get("fixture")
        if isinstance(fx, dict):
            return fx
        fixtures = data.get("fixtures")
        if isinstance(fixtures, list) and fixtures and isinstance(fixtures[0], dict):
            return fixtures[0]
        return None

    async def _scrape_sport(
        self,
        client: httpx.AsyncClient,
        sid: int,
        sname: str,
        cfg: ScrapeConfig,
        result: ScrapeResult,
    ) -> None:
        clock = SportClock(result, sname)
        print(f"  > {sname}")
        fixtures = await self._list_fixtures(client, sid)
        print(f"    {len(fixtures)} fixtures")
        baseball = sname.lower() == "baseball"
        if cfg.depth != "full" and not baseball:
            fixtures = fixtures[:20]
        sem = asyncio.Semaphore(max(1, min(cfg.concurrency, 4)))
        kept = 0
        lock = asyncio.Lock()

        async def one(fx: dict):
            nonlocal kept
            fid = fx.get("id")
            async with sem:
                try:
                    full = await self._fixture_view(client, fid)
                except Exception as exc:
                    result.errors.append(f"{sname} {fid}: {exc}")
                    full = None
                event = _event_from_fixture(full or fx, extra_markets=full)
                async with lock:
                    if event:
                        result.events.append(event)
                        kept += 1
                await asyncio.sleep(cfg.request_delay)

        await asyncio.gather(*(one(fx) for fx in fixtures))
        print(f"    kept {kept} events")
        clock.done()
