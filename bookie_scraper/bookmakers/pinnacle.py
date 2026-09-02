"""Pinnacle — guest Arcadia API used by pinnacle.com (no account)."""
from __future__ import annotations

import asyncio
import time

import httpx

from bookie_scraper.bookmakers.base import Bookmaker
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.http import DEFAULT_TIMEOUT, fetch_json
from bookie_scraper.models import Event, ScrapeResult, utc_now, note_sport_timing
from bookie_scraper.normalize import (
    american_to_decimal,
    build_event,
    group_outcomes,
)

CONFIG_URL = "https://www.pinnacle.com/config/app.json"
FALLBACK_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
GUEST_ROOT = "https://guest.api.arcadia.pinnacle.com/0.1"

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.pinnacle.com/",
    "Origin": "https://www.pinnacle.com",
}

MAIN_TYPES = {"moneyline", "spread", "total"}


def _pinnacle_sport(name: str) -> str:
    """Pinnacle uses 'Football' for NFL/NCAAF and 'Soccer' for association football."""
    n = (name or "").strip()
    if n.lower() == "football":
        return "American Football"
    return n


def _headers(api_key: str) -> dict[str, str]:
    return {**HEADERS_BASE, "X-API-Key": api_key}


async def _api_key(client: httpx.AsyncClient) -> str:
    try:
        cfg = await fetch_json(client, CONFIG_URL)
        key = ((cfg.get("api") or {}).get("haywire") or {}).get("apiKey")
        if key:
            return str(key)
    except Exception as exc:
        print(f"[pinnacle] config fetch failed ({exc}), using fallback key")
    return FALLBACK_KEY


def _participant_map(matchup: dict) -> tuple[str, str, dict[int, str]]:
    home = away = ""
    by_id: dict[int, str] = {}
    for p in matchup.get("participants") or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name") or ""
        pid = p.get("id")
        if pid is not None:
            by_id[int(pid)] = name
        al = (p.get("alignment") or "").lower()
        if al == "home":
            home = name
        elif al == "away":
            away = name
    if not home or not away:
        parts = [p.get("name") or "" for p in (matchup.get("participants") or []) if isinstance(p, dict)]
        if len(parts) >= 2 and not home:
            home, away = parts[0], parts[1]
    return home, away, by_id


def _outcome_name(price: dict, home: str, away: str, by_id: dict[int, str]) -> str:
    des = (price.get("designation") or "").lower()
    if des == "home":
        return home or "Home"
    if des == "away":
        return away or "Away"
    if des == "draw":
        return "Draw"
    if des in ("over", "under"):
        return des.capitalize()
    pid = price.get("participantId")
    if pid is not None and int(pid) in by_id:
        return by_id[int(pid)]
    return des or (by_id.get(int(pid), str(pid)) if pid is not None else "")


def _market_label(mk: dict, extra: str = "") -> str:
    mtype = mk.get("type") or "market"
    period = mk.get("period") or 0
    prefix = "" if period in (0, "0") else f"Period {period} "
    if extra:
        prefix = f"{extra} | {prefix}" if prefix else f"{extra} | "
    if mtype == "moneyline":
        return f"{prefix}Moneyline"
    if mtype == "spread":
        return f"{prefix}Spread"
    if mtype == "total":
        return f"{prefix}Total"
    if mtype == "team_total":
        side = (mk.get("side") or "").title()
        return f"{prefix}{side} Team Total".strip()
    return f"{prefix}{str(mtype).replace('_', ' ').title()}"


def _child_title(matchup: dict) -> str:
    spec = matchup.get("special") or {}
    desc = spec.get("description") or spec.get("category") or ""
    if desc:
        return str(desc)
    units = matchup.get("units") or ""
    if units and str(units).lower() not in ("regular", "points", ""):
        return str(units)
    parts = [p.get("name") or "" for p in (matchup.get("participants") or []) if isinstance(p, dict)]
    if parts and matchup.get("type") == "special":
        return spec.get("category") or "Special"
    return ""


def _items_from_markets(markets: list[dict], home: str, away: str, by_id: dict[int, str], extra: str = "") -> list:
    items = []
    for mk in markets:
        spec_title = extra
        # player-prop style: the child description IS the market
        if spec_title and mk.get("type") in ("total", "spread", "moneyline") and " | " not in spec_title:
            if mk.get("type") == "total" and spec_title:
                label = spec_title
            else:
                label = _market_label(mk, extra=spec_title)
        else:
            label = _market_label(mk, extra=spec_title) if spec_title else _market_label(mk)
        for price in mk.get("prices") or []:
            oname = _outcome_name(price, home, away, by_id)
            dec = american_to_decimal(price.get("price"))
            point = price.get("points")
            items.append((label, oname, dec, True, point if point is not None else None))
    return items


def _event_from_matchup(
    matchup: dict,
    markets: list[dict],
    depth: str,
    child_bits: list[tuple[dict, list[dict]]] | None = None,
) -> Event | None:
    home, away, by_id = _participant_map(matchup)
    mtype = matchup.get("type") or "matchup"
    if mtype == "special":
        spec = matchup.get("special") or {}
        name = spec.get("description") or spec.get("name") or "Special"
    else:
        name = f"{home} vs {away}" if home and away else str(matchup.get("id"))

    use = list(markets)
    if depth == "main":
        use = [m for m in use if (m.get("period") or 0) == 0]
        use = [m for m in use if (m.get("type") or "") in MAIN_TYPES]
        use = [m for m in use if _is_main_line(m, use)]

    items = _items_from_markets(use, home, away, by_id)
    if depth != "main":
        for child, cmkts in child_bits or []:
            chome, caway, cby = _participant_map(child)
            title = _child_title(child)
            items.extend(_items_from_markets(cmkts, chome or home, caway or away, {**by_id, **cby}, extra=title))

    grouped = group_outcomes(items)
    if not grouped:
        return None

    league = matchup.get("league") or {}
    sport = _pinnacle_sport(
        (league.get("sport") or {}).get("name") or league.get("name") or "unknown"
    )
    status = "LIVE" if matchup.get("isLive") else (matchup.get("status") or "")
    return build_event(
        bookmaker="pinnacle",
        event_id=str(matchup.get("id") or ""),
        sport_raw=sport,
        competition=league.get("name") or "Unknown",
        name=name,
        starts_at=matchup.get("startTime") or "",
        status=status,
        markets=grouped,
        home=home,
        away=away,
    )


def _is_main_line(mk: dict, siblings: list[dict]) -> bool:
    """Keep the spread/total closest to even money (typical 'main' line)."""
    mtype = mk.get("type")
    if mtype not in ("spread", "total"):
        return True
    same = [
        s for s in siblings
        if s.get("type") == mtype and s.get("period", 0) == mk.get("period", 0)
        and s.get("side") == mk.get("side")
    ]
    if len(same) <= 1:
        return True

    def evenness(item: dict) -> float:
        prices = item.get("prices") or []
        vals = [abs(american_to_decimal(p.get("price")) or 99) for p in prices]
        # closer to 2.0 is more even
        if not vals:
            return 99
        return sum(abs(v - 2.0) for v in vals) / len(vals)

    best = min(same, key=evenness)
    return best is mk or (
        best.get("key") == mk.get("key") and best.get("matchupId") == mk.get("matchupId")
    )


class Pinnacle(Bookmaker):
    key = "pinnacle"
    title = "Pinnacle"

    async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
        result = ScrapeResult(bookmaker=self.key, started_at=utc_now())
        print("\n" + "=" * 60)
        print("  Pinnacle")
        print("=" * 60)

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS_BASE) as client:
            try:
                api_key = await _api_key(client)
                hdrs = _headers(api_key)
                sports = await fetch_json(client, f"{GUEST_ROOT}/sports", headers=hdrs)
            except Exception as exc:
                result.errors.append(f"sports list failed: {exc}")
                result.finished_at = utc_now()
                return result

            if not isinstance(sports, list):
                result.errors.append("unexpected sports payload")
                result.finished_at = utc_now()
                return result

            active = [
                s for s in sports
                if isinstance(s, dict)
                and not s.get("isHidden")
                and (s.get("matchupCount") or 0) > 0
                and cfg.wants_sport(_pinnacle_sport(s.get("name") or ""), str(s.get("id") or ""))
            ]
            print(f"[pinnacle] {len(active)} sports with matchups")

            sem = asyncio.Semaphore(max(1, min(cfg.concurrency, 3)))

            async def one_sport(sport: dict):
                sid = sport["id"]
                sname = _pinnacle_sport(sport.get("name") or str(sid))
                t0 = time.perf_counter()
                count = 0
                odds_n = 0
                async with sem:
                    try:
                        matchups, straight = await asyncio.gather(
                            fetch_json(client, f"{GUEST_ROOT}/sports/{sid}/matchups", headers=hdrs),
                            fetch_json(client, f"{GUEST_ROOT}/sports/{sid}/markets/straight", headers=hdrs),
                        )
                    except Exception as exc:
                        msg = f"{sname}: {exc}"
                        print(f"  ! {msg}")
                        result.errors.append(msg)
                        note_sport_timing(result, sname, time.perf_counter() - t0, 0, 0)
                        return
                    live: list = []
                    special: list = []
                    try:
                        live = await fetch_json(
                            client, f"{GUEST_ROOT}/sports/{sid}/markets/live/straight", headers=hdrs
                        )
                    except Exception:
                        live = []
                    if cfg.depth != "main":
                        try:
                            special = await fetch_json(
                                client, f"{GUEST_ROOT}/sports/{sid}/markets/special", headers=hdrs
                            )
                        except Exception:
                            special = []

                if not isinstance(matchups, list):
                    matchups = []
                if not isinstance(straight, list):
                    straight = []
                if not isinstance(live, list):
                    live = []
                if not isinstance(special, list):
                    special = []

                parents = [
                    m for m in matchups
                    if isinstance(m, dict) and m.get("type") == "matchup" and not m.get("parentId")
                ]
                children = [m for m in matchups if isinstance(m, dict) and m.get("parentId")]
                outrights = [
                    m for m in matchups
                    if isinstance(m, dict) and m.get("type") == "special" and not m.get("parentId")
                ]
                keep_ids = {m.get("id") for m in parents}
                if cfg.depth != "main":
                    keep_ids.update(m.get("id") for m in children)
                    keep_ids.update(m.get("id") for m in outrights)

                markets_by: dict[int, list] = {}
                seen_keys: set[tuple] = set()
                for mk in list(straight) + list(live) + list(special):
                    if not isinstance(mk, dict):
                        continue
                    mid = mk.get("matchupId")
                    if mid not in keep_ids:
                        continue
                    key = (mid, mk.get("key"), mk.get("type"), mk.get("period"))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    markets_by.setdefault(mid, []).append(mk)

                kids_by: dict = {}
                for c in children:
                    kids_by.setdefault(c.get("parentId"), []).append(c)

                count = 0
                odds_n = 0
                for m in parents:
                    child_bits = []
                    if cfg.depth != "main":
                        for c in kids_by.get(m.get("id"), []):
                            child_bits.append((c, markets_by.get(c.get("id"), [])))
                    event = _event_from_matchup(
                        m, markets_by.get(m.get("id"), []), cfg.depth, child_bits
                    )
                    if event:
                        result.events.append(event)
                        count += 1
                        odds_n += sum(len(x.outcomes) for x in event.markets)
                if cfg.depth != "main":
                    for m in outrights:
                        event = _event_from_matchup(m, markets_by.get(m.get("id"), []), cfg.depth)
                        if event:
                            result.events.append(event)
                            count += 1
                            odds_n += sum(len(x.outcomes) for x in event.markets)
                print(
                    f"  > {sname}: {count} events  "
                    f"({len(parents)} matches, {len(children)} related, {odds_n} odds)"
                )
                note_sport_timing(result, sname, time.perf_counter() - t0, count, odds_n)
                await asyncio.sleep(cfg.request_delay)

            await asyncio.gather(*(one_sport(s) for s in active))

        result.finished_at = utc_now()
        return result
