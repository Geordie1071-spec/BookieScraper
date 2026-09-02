"""Betsson — session via Playwright, then replay /api/sb widget endpoints for all sports."""
from __future__ import annotations

import asyncio
import re

from urllib.parse import urlencode

from playwright.async_api import Page

from bookie_scraper.bookmakers.base import Bookmaker
from bookie_scraper.browser import dismiss_cookies, launch_browser, new_context, safe_goto
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.models import Event, ScrapeResult, utc_now, SportClock
from bookie_scraper.normalize import build_event, group_outcomes, is_american_football, parse_number, parse_price

BASE = "https://www.betsson.com"
API = f"{BASE}/api/sb/v1"

FALLBACK_SPORTS = [
    ("football", "1"),
    ("basketball", ""),
    ("tennis", ""),
    ("ice-hockey", ""),
    ("american-football", ""),
    ("baseball", ""),
    ("volleyball", ""),
    ("handball", ""),
    ("table-tennis", ""),
    ("esports", "119"),
    ("darts", ""),
    ("snooker", ""),
    ("boxing", ""),
    ("mma", ""),
    ("cricket", ""),
    ("rugby", ""),
    ("golf", ""),
    ("formula-1", ""),
    ("futsal", ""),
    ("floorball", ""),
    ("cycling", ""),
    ("badminton", ""),
    ("aussie-rules", ""),
    ("water-polo", ""),
    ("beach-volleyball", ""),
    ("motorsport", ""),
]


async def _api_get(page: Page, path: str, params: dict | None = None, headers: dict | None = None) -> dict | None:
    """Fetch from inside the page so WAF cookies/tokens go with the request."""
    url = path if path.startswith("http") else f"{API}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    hdrs = dict(headers or {})
    try:
        result = await page.evaluate(
            """async ({url, headers}) => {
                try {
                    const r = await fetch(url, {
                        credentials: 'include',
                        headers: Object.assign({'accept': 'application/json'}, headers || {}),
                    });
                    const text = await r.text();
                    let data = null;
                    try { data = JSON.parse(text); } catch (e) {}
                    return { ok: r.ok, status: r.status, data };
                } catch (e) {
                    return { ok: false, status: 0, error: String(e) };
                }
            }""",
            {"url": url, "headers": hdrs},
        )
    except Exception as exc:
        print(f"    fetch error {path}: {exc}")
        return None
    if not result or not result.get("ok"):
        status = (result or {}).get("status")
        err = (result or {}).get("error") or ""
        print(f"    GET {path} -> {status} {err}".rstrip())
        return None
    data = result.get("data")
    return data if isinstance(data, dict) else None


def _competition_ids(index: dict, sport_slug: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    prefix = sport_slug.rstrip("/") + "/"
    for slug, vals in (index or {}).items():
        if not isinstance(slug, str):
            continue
        if slug != sport_slug and not slug.startswith(prefix):
            continue
        if not isinstance(vals, list) or len(vals) < 3:
            continue
        cid = str(vals[-1])
        if cid.startswith("f-") and sport_slug not in ("formula-1", "formula1", "f1"):
            continue
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


def _parse_categories(data: dict) -> dict[str, str]:
    """Map top-level sport slug -> category id."""
    items = (data.get("data") or {}).get("items") or {}
    index = items.get("indexBySlug") or {}
    sports: dict[str, str] = {}
    if isinstance(index, dict):
        for slug, ids in index.items():
            if not isinstance(slug, str) or "/" in slug:
                continue
            if isinstance(ids, list) and ids:
                sports[slug] = str(ids[0])
            elif ids:
                sports[slug] = str(ids)
    # also walk named lists if present
    for key in ("categories", "sports", "list"):
        blob = items.get(key) if isinstance(items, dict) else None
        if isinstance(blob, list):
            for row in blob:
                if not isinstance(row, dict):
                    continue
                slug = row.get("slug") or row.get("readableId") or ""
                cid = row.get("id") or row.get("categoryId")
                if slug and cid and "/" not in str(slug):
                    sports.setdefault(str(slug), str(cid))
    return sports


def _event_matches_sport(ev: dict, slug: str) -> bool:
    key = (slug or "").lower().replace("_", "-")
    fields = [
        ev.get("categorySlug"), ev.get("categoryName"), ev.get("sportSlug"),
        ev.get("sportName"), ev.get("neutralPath"), ev.get("slug"),
    ]
    blob = " ".join(str(x or "") for x in fields).lower().replace("_", "-")
    if not blob.strip():
        return True
    soccer = key in ("soccer", "football")
    if soccer and is_american_football(blob):
        return False
    if key in ("american-football", "nfl") and not is_american_football(blob):
        if "soccer" in blob:
            return False
    if key and re.search(rf"(^|[\s/_-]){re.escape(key)}($|[\s/_-])", blob):
        if key == "football" and "american" in blob:
            return False
        return True
    if soccer and re.search(r"(^|[\s/_-])soccer($|[\s/_-])", blob):
        return True
    if key in ("formula-1", "formula1", "f1") and re.search(r"formula", blob):
        return True
    aliases = {
        "ufc---martial-arts": "mma", "mma": "ufc", "ice-hockey": "hockey",
        "hockey": "ice-hockey", "formula-1": "formula1", "formula1": "formula-1",
        "f1": "formula-1",
    }
    alt = aliases.get(key, "")
    return bool(alt) and bool(re.search(rf"(^|[\s/_-]){re.escape(alt)}($|[\s/_-])", blob))


def _event_name(event: dict) -> tuple[str, str, str]:
    parts = event.get("participants") or []
    if len(parts) >= 2:
        home = (parts[0].get("label") or parts[0].get("name") or "").strip()
        away = (parts[1].get("label") or parts[1].get("name") or "").strip()
        name = f"{home} vs {away}" if home and away else (event.get("label") or event.get("name") or "")
        return name, home, away
    name = event.get("label") or event.get("name") or str(event.get("id") or "")
    return name, "", ""


def _markets_from_event_payload(event: dict, extra_markets: list, extra_sels: dict) -> list:
    items = []
    inline = event.get("markets") or []
    if isinstance(inline, dict):
        inline = list(inline.values())
    # Accordion groups are the full market set. Listing-page markets can mix
    # neighbouring events in the same table payload, so ignore them once we
    # have accordion data.
    source = list(extra_markets) if extra_markets else list(inline)
    seen = set()
    for market in source:
        if not isinstance(market, dict):
            continue
        if market.get("status") == "Closed":
            continue
        mid = str(market.get("id") or "")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        mname = market.get("label") or market.get("marketFriendlyName") or market.get("name") or mid
        sels = market.get("selections") or extra_sels.get(mid) or []
        if isinstance(sels, dict):
            sels = list(sels.values())
        for sel in sels:
            if not isinstance(sel, dict):
                continue
            oname = sel.get("label") or sel.get("participant") or sel.get("alternateLabel") or ""
            oval = sel.get("odds")
            if not oval:
                fmt = sel.get("marketSelectionPriceFormats") or {}
                oval = next(iter(fmt.values()), None) if isinstance(fmt, dict) else None
            active = sel.get("status", "Open") in ("Open", "ACTIVE", "", None)
            items.append((mname, oname, parse_price(oval), active, parse_number(sel.get("line") or sel.get("handicap"))))
    return group_outcomes(items)


def _parse_events_table(data: dict) -> list[dict]:
    raw = data.get("data") or data
    if not isinstance(raw, dict):
        return []
    events = raw.get("events") or []
    if isinstance(events, dict):
        events = list(events.values())
    events = [e for e in events if isinstance(e, dict)]

    mkts = raw.get("markets") or []
    sels = raw.get("selections") or []
    if isinstance(mkts, dict):
        mkts = list(mkts.values())
    if isinstance(sels, dict):
        sels = list(sels.values())
    sels_by_market: dict[str, list] = {}
    for sel in sels:
        if isinstance(sel, dict):
            mid = str(sel.get("marketId") or "")
            if mid:
                sels_by_market.setdefault(mid, []).append(sel)
    mkts_by_event: dict[str, list] = {}
    for m in mkts:
        if not isinstance(m, dict):
            continue
        eid = str(m.get("eventId") or "")
        mid = str(m.get("id") or "")
        if mid and sels_by_market.get(mid) and not m.get("selections"):
            m = {**m, "selections": sels_by_market[mid]}
        if eid:
            mkts_by_event.setdefault(eid, []).append(m)

    out = []
    for ev in events:
        eid = str(ev.get("id") or ev.get("globalId") or "")
        extra = mkts_by_event.get(eid, [])
        if extra:
            existing = ev.get("markets") or []
            if isinstance(existing, dict):
                existing = list(existing.values())
            ev = {**ev, "markets": list(existing) + extra}
        out.append(ev)
    return out


def _parse_accordion(data: dict) -> tuple[list, dict[str, list]]:
    raw = data.get("data") or data
    if not isinstance(raw, dict):
        return [], {}
    markets: list = []
    sels_by_market: dict[str, list] = {}
    accordions = raw.get("accordions") or {}
    accordion_list = list(accordions.values()) if isinstance(accordions, dict) else accordions
    for acc in accordion_list:
        if not isinstance(acc, dict):
            continue
        mkts = acc.get("markets") or []
        sels = acc.get("selections") or []
        if isinstance(mkts, dict):
            mkts = list(mkts.values())
        if isinstance(sels, dict):
            sels = list(sels.values())
        markets.extend(mkts)
        for sel in sels:
            if isinstance(sel, dict):
                mid = str(sel.get("marketId") or "")
                if mid:
                    sels_by_market.setdefault(mid, []).append(sel)
    # some payloads put markets/selections at top level
    top_m = raw.get("markets") or []
    top_s = raw.get("selections") or []
    if isinstance(top_m, dict):
        top_m = list(top_m.values())
    if isinstance(top_s, dict):
        top_s = list(top_s.values())
    markets.extend(top_m)
    for sel in top_s:
        if isinstance(sel, dict):
            mid = str(sel.get("marketId") or "")
            if mid:
                sels_by_market.setdefault(mid, []).append(sel)
    return markets, sels_by_market


def _group_ids(payload: dict) -> list[str]:
    """Accordion group ids from event/v2 accordionSummaries (and legacy groupableIds)."""
    raw = payload.get("data") or payload
    if not isinstance(raw, dict):
        return []
    ids: list[str] = []
    seen: set[str] = set()

    def add(gid) -> None:
        if gid is None or gid == "":
            return
        sid = str(gid)
        if sid in seen:
            return
        seen.add(sid)
        ids.append(sid)

    def take(obj: dict) -> None:
        for key in ("groupableIds", "marketGroups"):
            val = obj.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        add(item.get("id") or item.get("groupableId"))
                    else:
                        add(item)
            elif isinstance(val, dict):
                for item in val.values():
                    if isinstance(item, dict):
                        add(item.get("id") or item.get("groupableId"))
                    else:
                        add(item)
        add(obj.get("groupableId"))
        summaries = obj.get("accordionSummaries")
        if isinstance(summaries, dict):
            for key, val in summaries.items():
                if isinstance(val, dict):
                    add(val.get("groupableId") or key)
                else:
                    add(key)
        elif isinstance(summaries, list):
            for val in summaries:
                if isinstance(val, dict):
                    add(val.get("groupableId") or val.get("id"))

    take(raw)
    evt = raw.get("event")
    if isinstance(evt, dict):
        take(evt)
    items = raw.get("items")
    if isinstance(items, dict):
        take(items)
        if isinstance(items.get("event"), dict):
            take(items["event"])
    return ids


class Betsson(Bookmaker):
    key = "betsson"
    title = "Betsson"

    async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
        result = ScrapeResult(bookmaker=self.key, started_at=utc_now())
        print("\n" + "=" * 60)
        print("  Betsson")
        print("=" * 60)

        pw, browser = await launch_browser(cfg.headed)
        captured: list[dict] = []
        sb_headers: dict[str, str] = {}
        try:
            ctx = await new_context(browser)
            page = await ctx.new_page()

            def on_request(req):
                if "/api/sb/" not in req.url:
                    return
                for k, v in req.headers.items():
                    kl = k.lower()
                    if kl.startswith("x-sb") or kl.startswith("x-obg") or kl in (
                        "brandid", "sessiontoken", "marketcode", "correlationid",
                        "accept", "content-type",
                    ):
                        sb_headers[k] = v

            async def on_response(resp):
                if "/api/sb/" not in resp.url or resp.status != 200:
                    return
                try:
                    data = await resp.json()
                except Exception:
                    return
                if isinstance(data, dict):
                    captured.append({"url": resp.url, "data": data})

            page.on("request", on_request)
            page.on("response", on_response)
            print("[betsson] Opening sportsbook...")
            await safe_goto(page, f"{BASE}/en/sportsbook", timeout=45_000, wait="domcontentloaded")
            await dismiss_cookies(page)
            await page.wait_for_timeout(5000)

            sports = await self._discover_sports(page, cfg, captured, sb_headers)
            print(f"[betsson] Sports: {list(sports.keys())}")
            cat_index = {}
            for row in captured:
                if "categories/v2" in row.get("url", ""):
                    items = ((row["data"].get("data") or {}).get("items") or {})
                    cat_index = items.get("indexBySlug") or {}
                    if cat_index:
                        break

            all_events: dict[str, dict] = {}
            for slug, cat_id in sports.items():
                clock = SportClock(result, slug)
                print(f"\n  > {slug}  (id={cat_id or '?'})")
                comp_ids = _competition_ids(cat_index, slug)
                print(f"    competitions: {len(comp_ids)}")
                found: list[dict] = []
                if cat_id:
                    found = await self._events_via_table(page, cat_id, cfg, sb_headers, comp_ids)
                    found = [ev for ev in found if _event_matches_sport(ev, slug)]
                    if not found:
                        found = await self._events_via_table(page, cat_id, cfg, sb_headers, [])
                        found = [ev for ev in found if _event_matches_sport(ev, slug)]
                if not found:
                    found = await self._events_via_nav(page, slug)
                    found = [ev for ev in found if _event_matches_sport(ev, slug)]
                print(f"    events: {len(found)}")
                sport_events: dict[str, dict] = {}
                for ev in found:
                    eid = str(ev.get("id") or ev.get("globalId") or "")
                    if eid:
                        sport_events[eid] = ev
                        all_events[eid] = ev
                if cfg.depth == "full" and sport_events:
                    await self._enrich_full(page, sport_events, cfg, result, sb_headers)
                else:
                    for ev in sport_events.values():
                        event = self._to_event(ev, [], {})
                        if event:
                            result.events.append(event)
                clock.done()

            print(f"\n[betsson] Unique events: {len(all_events)}")
        except Exception as exc:
            result.errors.append(str(exc))
            print(f"[betsson] Error: {exc}")
        finally:
            await browser.close()
            await pw.stop()

        result.finished_at = utc_now()
        return result

    async def _discover_sports(
        self, page: Page, cfg: ScrapeConfig, captured: list[dict], headers: dict,
    ) -> dict[str, str]:
        data = await _api_get(page, "/widgets/categories/v2", headers=headers or None)
        discovered = _parse_categories(data) if data else {}
        if not discovered:
            for row in captured:
                if "categories/v2" in row.get("url", ""):
                    discovered = _parse_categories(row["data"])
                    if discovered:
                        break
        if not discovered:
            print("[betsson] categories/v2 empty - using fallback slugs")
            discovered = {slug: cid for slug, cid in FALLBACK_SPORTS}
        else:
            print(f"[betsson] discovered {len(discovered)} sports from categories API")
        if cfg.sports:
            discovered = {
                slug: cid for slug, cid in discovered.items()
                if cfg.wants_sport(slug)
            }
        skip = {"live", "casino", "betway-boosts"}
        return {k: v for k, v in discovered.items() if k.lower() not in skip}

    async def _events_via_table(
        self, page: Page, cat_id: str, cfg: ScrapeConfig,
        headers: dict, competition_ids: list[str],
    ) -> list[dict]:
        found: dict[str, dict] = {}
        chunks: list[list[str]] = []
        if competition_ids:
            for i in range(0, len(competition_ids), 20):
                chunks.append(competition_ids[i:i + 20])
        else:
            chunks.append([])
        hdrs = {**headers, "x-sb-identifier": headers.get("x-sb-identifier") or "EVENT_TABLE_REQUEST"}
        for chunk in chunks:
            for phase in ("Prematch", "Live", "Outright"):
                page_no = 1
                while page_no <= 50:
                    params = {
                        "categoryIds": cat_id,
                        "eventPhase": phase,
                        "eventSortBy": "StartDate" if phase == "Prematch" else "Popularity",
                        "includeSkeleton": "true",
                        "maxEventCount": "50",
                        "maxMarketCount": "200",
                        "pageNumber": str(page_no),
                        "priceFormats": "1",
                    }
                    if chunk:
                        params["competitionIds"] = ",".join(chunk)
                    data = await _api_get(page, "/widgets/events-table/v2", params, hdrs)
                    events = _parse_events_table(data) if data else []
                    if not events:
                        break
                    for ev in events:
                        eid = str(ev.get("id") or ev.get("globalId") or "")
                        if eid:
                            found[eid] = ev
                    if len(events) < 50:
                        break
                    page_no += 1
                    await asyncio.sleep(cfg.request_delay)
        return list(found.values())

    async def _events_via_nav(self, page: Page, slug: str) -> list[dict]:
        captured: list[dict] = []

        async def on_response(resp):
            if "/api/sb/" not in resp.url or "events-table" not in resp.url:
                return
            if resp.status != 200:
                return
            try:
                data = await resp.json()
                captured.extend(_parse_events_table(data))
            except Exception:
                pass

        page.on("response", on_response)
        await safe_goto(page, f"{BASE}/en/sportsbook/{slug}", timeout=30_000, wait="domcontentloaded")
        await dismiss_cookies(page)
        await page.wait_for_timeout(4000)
        await page.wait_for_timeout(2500)
        page.remove_listener("response", on_response)
        seen = set()
        out = []
        for ev in captured:
            eid = str(ev.get("id") or ev.get("globalId") or "")
            if eid and eid not in seen:
                seen.add(eid)
                out.append(ev)
        return out

    async def _enrich_full(
        self, page: Page, events: dict[str, dict], cfg: ScrapeConfig,
        result: ScrapeResult, headers: dict,
    ):
        sem = asyncio.Semaphore(max(1, min(cfg.concurrency, 3)))
        total = len(events)
        done = 0

        async def one(ev: dict):
            nonlocal done
            extra_m: list = []
            extra_s: dict[str, list] = {}
            eid = str(ev.get("id") or ev.get("globalId") or "")
            async with sem:
                detail = await _api_get(page, "/widgets/event/v2", {"eventId": eid}, headers)
                queue = _group_ids(detail) if detail else []
                if not queue:
                    queue = [""]
                fetched: set[str] = set()
                while queue and len(fetched) < 80:
                    gid = queue.pop(0)
                    if gid in fetched:
                        continue
                    fetched.add(gid)
                    params = {"eventId": eid, "priceFormats": "1"}
                    if gid:
                        params["groupableId"] = gid
                    acc = await _api_get(page, "/widgets/accordion/v1", params, headers)
                    if acc:
                        mk, sl = _parse_accordion(acc)
                        extra_m.extend(mk)
                        for k, v in sl.items():
                            extra_s.setdefault(k, []).extend(v)
                        for extra_id in _group_ids(acc):
                            if extra_id not in fetched:
                                queue.append(extra_id)
                    await asyncio.sleep(cfg.request_delay)
                if not extra_m:
                    slug = ev.get("neutralPath") or ev.get("slug") or ""
                    if slug:
                        mk, sl = await self._accordion_via_nav(page, slug)
                        extra_m.extend(mk)
                        for k, v in sl.items():
                            extra_s.setdefault(k, []).extend(v)
            event = self._to_event(ev, extra_m, extra_s)
            if event:
                result.events.append(event)
            done += 1
            if done % 10 == 0 or done == total:
                nmk = sum(len(e.markets) for e in result.events)
                print(f"    full odds {done}/{total}  markets so far {nmk}")

        await asyncio.gather(*(one(ev) for ev in events.values()))

    async def _accordion_via_nav(self, page: Page, slug: str) -> tuple[list, dict]:
        extra_m: list = []
        extra_s: dict[str, list] = {}

        async def on_response(resp):
            if resp.status != 200 or "accordion" not in resp.url:
                return
            try:
                data = await resp.json()
            except Exception:
                return
            mk, sl = _parse_accordion(data)
            extra_m.extend(mk)
            for k, v in sl.items():
                extra_s.setdefault(k, []).extend(v)

        page.on("response", on_response)
        await safe_goto(page, f"{BASE}/en/sportsbook/{slug}", timeout=20_000, wait="domcontentloaded")
        await page.wait_for_timeout(2500)
        page.remove_listener("response", on_response)
        return extra_m, extra_s

    def _to_event(self, ev: dict, extra_m: list, extra_s: dict) -> Event | None:
        name, home, away = _event_name(ev)
        markets = _markets_from_event_payload(ev, extra_m, extra_s)
        if not markets:
            return None
        sport_raw = ev.get("categoryName") or ev.get("categorySlug") or str(ev.get("categoryId") or "")
        return build_event(
            bookmaker="betsson",
            event_id=str(ev.get("id") or ev.get("globalId") or ""),
            sport_raw=sport_raw,
            competition=ev.get("competitionName") or ev.get("regionName") or "Unknown",
            name=name,
            starts_at=ev.get("startDate") or ev.get("startTime") or "",
            status=ev.get("phase") or ev.get("status") or "",
            markets=markets,
            home=home,
            away=away,
        )
