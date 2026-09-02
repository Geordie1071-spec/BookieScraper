"""Bet365 — Playwright DOM scrape across the sports menu.

Baseball still pulls Game Lines plus player-prop tabs. Other sports parse the
competition coupon (1X2 / moneyline / match winner) and F1-style winner lists.
"""
from __future__ import annotations

import re

from playwright.async_api import Page

from bookie_scraper.bookmakers.base import Bookmaker
from bookie_scraper.browser import dismiss_cookies, launch_browser, new_context, safe_goto
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.models import Event, ScrapeResult, utc_now, SportClock
from bookie_scraper.normalize import build_event, group_outcomes, parse_price

BASE = "https://www.bet365.com"
HOME = f"{BASE}/#/HO/"

SKIP_COMPS = {
    "outrights", "offers", "featured", "competitions", "virtual baseball",
    "all sports", "in-play", "casino", "trending", "table", "all",
    "popular", "challenger tour", "world tennis tour men", "world tennis tour women",
}

FALLBACK_COMPS = [
    "MLB", "Japan NPB", "Korean KBO", "CPBL", "Mexico LMB",
    "Japan NPB Reserve League",
]

# Left-nav labels on bet365.com. Discovery keeps whichever of these appear.
KNOWN_SPORTS = [
    "Soccer",
    "Tennis",
    "Basketball",
    "American Football",
    "Baseball",
    "Ice Hockey",
    "Boxing/UFC",
    "Cricket",
    "Golf",
    "Rugby Union",
    "Rugby League",
    "Darts",
    "Snooker",
    "Volleyball",
    "Handball",
    "Table Tennis",
    "Esports",
    "E-Sports",
    "Formula 1",
    "Cycling",
    "Aussie Rules",
    "Gaelic Sports",
    "Futsal",
    "Badminton",
    "Water Polo",
    "Beach Volleyball",
    "Motor Sport",
]

MAX_COMPS_FULL = 15
MAX_COMPS_MAIN = 6

SKIP_LINES = {
    "run line", "total", "money line", "spread", "game lines", "hits",
    "pitcher strikeouts", "home runs", "total bases", "props", "outrights",
    "table", "all", "acca boost", "bet boost", "main", "innings", "team",
    "bb", "view all", "yes", "no", "player / last 5", "n/a",
}

DATE_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", re.I)
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
SPREAD_RE = re.compile(r"^[+-]\d+(?:\.\d+)?$")
OU_RE = re.compile(r"^([OU])\s*(\d+(?:\.\d+)?)$", re.I)
PITCHER_RE = re.compile(r"^[A-Z]\.?\s+[A-Za-z]")
PRICE_RE = re.compile(r"^\d+[.,]\d{1,3}$")
FOOTER_RE = re.compile(
    r"(Major League Baseball|Information and transmission|"
    r"Receive live updates|Unable to display|Sorry, this page)",
    re.I,
)


def _is_price_line(text: str) -> bool:
    if not PRICE_RE.match((text or "").strip()):
        return False
    return parse_price(text) is not None


def _is_pitcher(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 32:
        return False
    parts = t.split()
    if len(parts) < 2 or len(parts) > 3:
        return False
    return bool(PITCHER_RE.match(t))


def _coupon_slice(text: str) -> str:
    raw = text or ""
    foot = FOOTER_RE.search(raw)
    if foot:
        raw = raw[: foot.start()]
    for marker in ("Game Lines", "Run Line", "Money Line", "Spread"):
        idx = raw.find(marker)
        if idx >= 0:
            return raw[idx:]
    return raw


def competitions_from_hub(text: str, sport: str = "baseball") -> list[str]:
    start = (text or "").find("Competitions")
    chunk = text[start:] if start >= 0 else (text or "")
    end = re.search(r"\n(?:Virtual Baseball|Information and transmission)\b", chunk)
    if end:
        chunk = chunk[: end.start()]
    names: list[str] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line or line.lower() in SKIP_COMPS:
            continue
        if line in KNOWN_SPORTS:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if len(line) > 50 or _is_price_line(line):
            continue
        if line not in names:
            names.append(line)
    ordered: list[str] = []
    labels = {ln.strip() for ln in (text or "").splitlines() if ln.strip()}
    if sport.lower() == "baseball":
        for name in FALLBACK_COMPS:
            if name in labels and name not in ordered:
                ordered.append(name)
    for name in names:
        if name not in ordered:
            ordered.append(name)
    if ordered:
        return ordered
    return ["MLB"] if sport.lower() == "baseball" else []


def sports_from_home(text: str, cfg: ScrapeConfig) -> list[str]:
    labels = {ln.strip() for ln in (text or "").splitlines() if ln.strip()}
    found = [s for s in KNOWN_SPORTS if s in labels]
    if not found:
        found = list(KNOWN_SPORTS)
    out: list[str] = []
    seen: set[str] = set()
    for label in found:
        aliases = _sport_aliases(label)
        if not cfg.wants_sport(*aliases):
            continue
        key = label.lower().replace("e-sports", "esports")
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def _sport_aliases(label: str) -> list[str]:
    aliases = [label, label.lower(), label.replace(" ", "-"), label.replace("/", " ")]
    low = label.lower()
    if "boxing" in low or "ufc" in low:
        aliases += ["boxing", "ufc", "mma"]
    if "esport" in low:
        aliases += ["esports", "e-sports"]
    if "formula" in low:
        aliases += ["formula-1", "f1", "formula1"]
    if label == "Soccer":
        aliases += ["football", "soccer"]
    if label == "American Football":
        aliases += ["nfl", "american-football"]
    return aliases


def parse_winner_list(text: str) -> list[dict]:
    """Outright / F1-style name + price rows as one event."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    items: list = []
    skip = {s.lower() for s in SKIP_LINES} | {s.lower() for s in SKIP_COMPS} | {
        s.lower() for s in KNOWN_SPORTS
    }
    i = 0
    while i < len(lines) - 1:
        name, nxt = lines[i], lines[i + 1]
        if TIME_RE.match(name) or DATE_RE.match(name) or FOOTER_RE.search(name):
            i += 1
            continue
        if (
            _is_price_line(nxt)
            and 2 < len(name) < 55
            and name.lower() not in skip
            and not _is_price_line(name)
        ):
            price = parse_price(nxt)
            if price is not None:
                items.append(("Winner", name, price, True, None))
            i += 2
            continue
        i += 1
    markets = group_outcomes(items)
    if not markets:
        return []
    return [{"away": "", "home": "", "time": "", "markets": markets}]


def parse_match_coupon(text: str) -> list[dict]:
    """Generic Bet365 coupon: kickoff time, two names, 2-way or 1X2 prices."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    events: list[dict] = []
    i = 0
    while i < len(lines):
        if not TIME_RE.match(lines[i]):
            i += 1
            continue
        time = lines[i]
        i += 1
        names: list[str] = []
        while i < len(lines):
            cur = lines[i]
            if TIME_RE.match(cur) or DATE_RE.match(cur) or _is_price_line(cur):
                break
            if cur.lower() in SKIP_LINES or cur.lower() == "competitions":
                i += 1
                continue
            if len(cur) > 60:
                i += 1
                continue
            names.append(cur)
            i += 1
            if len(names) >= 2:
                break
        if len(names) < 2:
            continue
        away, home = names[0], names[1]
        prices: list[float] = []
        while i < len(lines) and len(prices) < 3:
            cur = lines[i]
            if TIME_RE.match(cur) or DATE_RE.match(cur):
                break
            if _is_price_line(cur):
                p = parse_price(cur)
                if p is not None:
                    prices.append(p)
                i += 1
                continue
            i += 1
            if names and cur in names:
                break
        items = []
        if len(prices) >= 3:
            items = [
                ("Full Time Result", home, prices[0], True, None),
                ("Full Time Result", "Draw", prices[1], True, None),
                ("Full Time Result", away, prices[2], True, None),
            ]
        elif len(prices) >= 2:
            items = [
                ("Match Winner", away, prices[0], True, None),
                ("Match Winner", home, prices[1], True, None),
            ]
        markets = group_outcomes(items)
        if not markets:
            continue
        events.append({"away": away, "home": home, "time": time, "markets": markets})
    return events


def parse_game_lines(text: str) -> list[dict]:
    """Pull 3-way baseball game lines (run line / total / ML) from coupon text."""
    lines = [ln.strip() for ln in _coupon_slice(text).splitlines() if ln.strip()]
    events: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not TIME_RE.match(line):
            i += 1
            continue
        time = line
        i += 1
        names: list[str] = []
        while i < len(lines):
            cur = lines[i]
            if TIME_RE.match(cur) or DATE_RE.match(cur):
                break
            if SPREAD_RE.match(cur) or OU_RE.match(cur) or _is_price_line(cur):
                break
            if cur.lower() in SKIP_LINES:
                i += 1
                continue
            if re.fullmatch(r"[1-9]", cur) and len([n for n in names if not _is_pitcher(n)]) >= 2:
                i += 1
                break
            names.append(cur)
            i += 1
        teams = [n for n in names if not _is_pitcher(n)]
        if len(teams) < 2:
            continue
        away, home = teams[0], teams[1]
        spreads: list[tuple[str, float]] = []
        totals: list[tuple[str, float, float]] = []
        moneys: list[float] = []
        while i < len(lines) and len(moneys) < 2:
            cur = lines[i]
            if TIME_RE.match(cur) or DATE_RE.match(cur):
                break
            if cur.lower() in SKIP_LINES or re.fullmatch(r"[1-9]", cur):
                i += 1
                continue
            if SPREAD_RE.match(cur) and i + 1 < len(lines) and _is_price_line(lines[i + 1]):
                price = parse_price(lines[i + 1])
                if price is not None:
                    spreads.append((cur, price))
                i += 2
                continue
            ou = OU_RE.match(cur)
            if ou and i + 1 < len(lines) and _is_price_line(lines[i + 1]):
                price = parse_price(lines[i + 1])
                if price is not None:
                    side = "Over" if ou.group(1).upper() == "O" else "Under"
                    totals.append((side, float(ou.group(2)), price))
                i += 2
                continue
            if _is_price_line(cur):
                moneys.append(parse_price(cur))
                i += 1
                continue
            i += 1
        items: list[tuple[str, str, float | None, bool, float | None]] = []
        if len(spreads) >= 2:
            items.append(("Run Line", away, spreads[0][1], True, float(spreads[0][0])))
            items.append(("Run Line", home, spreads[1][1], True, float(spreads[1][0])))
        if len(totals) >= 2:
            items.append(("Total", totals[0][0], totals[0][2], True, totals[0][1]))
            items.append(("Total", totals[1][0], totals[1][2], True, totals[1][1]))
        if len(moneys) >= 2:
            items.append(("Money Line", away, moneys[0], True, None))
            items.append(("Money Line", home, moneys[1], True, None))
        markets = group_outcomes(items)
        if not markets:
            continue
        events.append({
            "away": away,
            "home": home,
            "time": time,
            "markets": markets,
        })
    return events


def parse_first_inning(text: str, known: list[dict]) -> dict[str, list]:
    """Map extra Yes/No first-inning markets onto parsed events by team names."""
    extra: dict[str, list] = {}
    blob = text or ""
    for ev in known:
        away, home = ev["away"], ev["home"]
        key = _event_key(away, home, ev.get("time") or "")
        pat = re.escape(away) + r"\s*[@v]\s*" + re.escape(home)
        m = re.search(pat, blob, re.I)
        if not m:
            continue
        window = blob[m.start(): m.start() + 400]
        ym = re.search(r"\bYes\s+(\d+[.,]\d{2})\s+No\s+(\d+[.,]\d{2})", window, re.I)
        if not ym:
            continue
        yes, no = parse_price(ym.group(1)), parse_price(ym.group(2))
        extra[key] = group_outcomes([
            ("A Run in the 1st Inning", "Yes", yes, True, None),
            ("A Run in the 1st Inning", "No", no, True, None),
        ])
    return extra


def parse_event_extras(text: str, away: str, home: str) -> list:
    """Alt run lines and first-inning Yes/No from an event page."""
    items: list[tuple[str, str, float | None, bool, float | None]] = []
    blob = text or ""
    ym = re.search(
        r"A Run in the 1st Inning.{0,80}?Yes\s+(\d+[.,]\d{2})\s+No\s+(\d+[.,]\d{2})",
        blob,
        re.I | re.S,
    )
    if ym:
        items.append(("A Run in the 1st Inning", "Yes", parse_price(ym.group(1)), True, None))
        items.append(("A Run in the 1st Inning", "No", parse_price(ym.group(2)), True, None))
    # Second Run Line block is alt handicaps: Team / ±n.5 / price repeating.
    parts = re.split(r"\nRun Line\n", blob)
    if len(parts) >= 3:
        alt = parts[-1]
        current = ""
        lines = [ln.strip() for ln in alt.splitlines() if ln.strip()]
        i = 0
        while i < len(lines):
            cur = lines[i]
            if cur in (away, home):
                current = cur
                i += 1
                continue
            if current and SPREAD_RE.match(cur) and i + 1 < len(lines) and _is_price_line(lines[i + 1]):
                items.append(("Run Line", current, parse_price(lines[i + 1]), True, float(cur)))
                i += 2
                continue
            if cur.lower() in SKIP_LINES or FOOTER_RE.search(cur):
                break
            i += 1
    return group_outcomes(items)


PROP_TABS = (
    "Hits",
    "Pitcher Strikeouts",
    "Home Runs",
    "Total Bases",
)

PROP_TITLES = {
    "Hits": "Player Hits",
    "Pitcher Strikeouts": "Pitcher Strikeouts",
    "Home Runs": "Player Home Runs",
    "Total Bases": "Player Total Bases",
}

# Coupon player grids: left column of names, columns of 1+/2+/… prices.
EXTRACT_GRIDS = r"""() => {
  const isPrice = (t) => /^\d+[.,]\d{1,3}$/.test((t || '').trim());
  const linesOf = (el) => (el.innerText || '').trim().split('\n').map(s => s.trim()).filter(Boolean);
  const body = document.body.innerText || '';
  const closed = /Unable to display|no longer available/i.test(body);
  const lefts = [...document.querySelectorAll('div')].filter(d => {
    const lines = linesOf(d);
    return lines[0] === 'Player / Last 5' && d.childElementCount >= 2 && (d.innerText || '').length < 8000;
  });
  const smallest = lefts.filter(a => !lefts.some(b => b !== a && a.contains(b)));
  const blocks = [];
  for (const left of smallest) {
    const wrap = left.parentElement;
    let matchup = '';
    let n = wrap;
    for (let i = 0; i < 8 && n; i++) {
      let prev = n.previousElementSibling;
      while (prev) {
        const first = ((prev.innerText || '').trim().split('\n')[0] || '').trim();
        if (first && first.length < 80 && /@/.test(first)) { matchup = first; break; }
        prev = prev.previousElementSibling;
      }
      if (matchup) break;
      n = n.parentElement;
    }
    const players = [...left.children].slice(1).map(row => {
      const ln = linesOf(row);
      return ln.find(l => /[A-Za-z]{2}/.test(l) && !isPrice(l)) || '';
    }).filter(Boolean);
    const root = wrap || left;
    const cand = [...root.querySelectorAll('div')].filter(d => {
      if (left.contains(d) || d === left) return false;
      const lines = linesOf(d);
      if (lines.length < 3) return false;
      const prices = lines.slice(1).filter(isPrice);
      if (prices.length < 2) return false;
      return prices.length >= Math.floor((lines.length - 1) * 0.7);
    });
    const cols = cand.filter(c => !cand.some(o => o !== c && c.contains(o))).map(c => {
      const ln = linesOf(c);
      return {head: ln[0] || '', prices: ln.slice(1).filter(isPrice)};
    });
    if (players.length && cols.length) blocks.push({matchup, players, cols});
  }
  return {closed, blocks};
}"""


def _split_matchup(matchup: str) -> tuple[str, str]:
    t = (matchup or "").split("\n")[0].strip()
    for sep in (" @ ", " v ", " vs "):
        if sep in t:
            a, b = t.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""


def _row_for_matchup(matchup: str, rows: list[dict]) -> dict | None:
    away, home = _split_matchup(matchup)
    if not away:
        return None
    for row in rows:
        if row["away"] == away and row["home"] == home:
            return row
        if row["away"] == home and row["home"] == away:
            return row
    away_l, home_l = away.lower(), home.lower()
    for row in rows:
        if away_l in row["away"].lower() and home_l in row["home"].lower():
            return row
    return None


def grids_to_markets(tab: str, blocks: list[dict], rows: list[dict]) -> dict[str, list]:
    title = PROP_TITLES.get(tab, tab)
    extra: dict[str, list] = {}
    for block in blocks or []:
        row = _row_for_matchup(block.get("matchup") or "", rows)
        if not row:
            continue
        players = [p for p in (block.get("players") or []) if p]
        items: list[tuple[str, str, float | None, bool, float | None]] = []
        for col in block.get("cols") or []:
            head = str(col.get("head") or "").strip()
            prices = list(col.get("prices") or [])
            point = float(head) if re.fullmatch(r"\d{1,2}", head) else None
            suffix = f"{head}+" if point is not None else (head or tab)
            for i, player in enumerate(players):
                if i >= len(prices):
                    break
                price = parse_price(prices[i])
                if price is None:
                    continue
                items.append((title, f"{player} {suffix}", price, True, point))
        markets = group_outcomes(items)
        if not markets:
            continue
        key = _event_key(row["away"], row["home"], row.get("time") or "")
        extra[key] = list(extra.get(key) or []) + markets
    return extra


def _event_key(away: str, home: str, time: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{away}-{home}-{time}".lower()).strip("-")
    return slug or f"{away}|{home}|{time}"


def _to_event(competition: str, row: dict, extra=None, sport: str = "baseball") -> Event:
    markets = list(row["markets"])
    if extra:
        have = {m.name: m for m in markets}
        for mk in extra:
            if mk.name in have:
                existing = have[mk.name]
                seen = {(o.name, o.point) for o in existing.outcomes}
                for oc in mk.outcomes:
                    if (oc.name, oc.point) not in seen:
                        existing.outcomes.append(oc)
                        seen.add((oc.name, oc.point))
                continue
            markets.append(mk)
            have[mk.name] = mk
    away, home = row.get("away") or "", row.get("home") or ""
    if away and home:
        name = f"{away} vs {home}"
        eid = _event_key(away, home, row.get("time") or "")
    else:
        name = competition or sport
        eid = _event_key(sport, competition, str(len(markets)))
    return build_event(
        bookmaker="bet365",
        event_id=eid,
        sport_raw=sport,
        competition=competition,
        name=name,
        starts_at=row.get("time") or "",
        status="",
        markets=markets,
        home=home,
        away=away,
    )


class Bet365(Bookmaker):
    key = "bet365"
    title = "Bet365"

    async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
        result = ScrapeResult(bookmaker=self.key, started_at=utc_now())
        print("\n" + "=" * 60)
        print("  Bet365")
        print("=" * 60)

        pw, browser = await launch_browser(cfg.headed)
        try:
            ctx = await new_context(browser)
            page = await ctx.new_page()
            await self._open(page)
            sports = sports_from_home(await self._body(page), cfg)
            print(f"[bet365] sports: {', '.join(sports) or '(none)'}")
            await ctx.close()
            for sport in sports:
                clock = SportClock(result, sport.lower())
                try:
                    await self._scrape_sport(browser, sport, cfg, result)
                except Exception as exc:
                    result.errors.append(f"{sport}: {exc}")
                    print(f"    error {sport}: {exc}")
                clock.done()
        except Exception as exc:
            result.errors.append(str(exc))
            print(f"[bet365] Error: {exc}")
        finally:
            await browser.close()
            await pw.stop()

        if not result.events:
            result.errors.append("No Bet365 odds found.")
        result.finished_at = utc_now()
        nmk = sum(len(e.markets) for e in result.events)
        print(
            f"[bet365] {len(result.events)} events  {nmk} markets  "
            f"{sum(len(e.to_rows()) for e in result.events)} odds rows"
        )
        return result

    async def _open(self, page: Page) -> None:
        await safe_goto(page, HOME, timeout=45_000)
        await page.wait_for_timeout(2500)
        await dismiss_cookies(page)
        await page.wait_for_timeout(800)

    async def _goto_sport(self, page: Page, sport: str) -> bool:
        if not await self._click_sport(page, sport):
            return False
        needles = ("Competitions", "Virtual Baseball", "MLB") if sport == "Baseball" else (
            "Competitions", "Full Time Result", "Match Winner", "Money Line", "Winner",
        )
        body = await self._wait_text(page, *needles, timeout=12_000)
        await page.wait_for_timeout(800)
        if "Unable to display" in body or "no longer available" in body.lower():
            return False
        return True

    async def _click_sport(self, page: Page, sport: str) -> bool:
        loc = page.get_by_text(sport, exact=True)
        try:
            if await loc.count() == 0:
                return False
            await loc.first.click(timeout=6000)
            await page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    async def _scrape_sport(self, browser, sport: str, cfg: ScrapeConfig, result: ScrapeResult) -> None:
        print(f"  > {sport}")
        ctx = await new_context(browser)
        page = await ctx.new_page()
        try:
            await self._open(page)
            if not await self._goto_sport(page, sport):
                print(f"    (could not open {sport})")
                result.errors.append(f"{sport}: could not open hub")
                return
            comps = competitions_from_hub(await self._body(page), sport)
        finally:
            await ctx.close()
        if sport != "Baseball":
            cap = MAX_COMPS_MAIN if cfg.depth == "main" else MAX_COMPS_FULL
            if len(comps) > cap:
                print(f"    {len(comps)} competitions, scraping first {cap}")
                comps = comps[:cap]
        print(f"    competitions: {', '.join(comps) or '(none)'}")
        if not comps:
            await self._scrape_competition(browser, sport, sport, cfg, result, use_props=False)
            return
        use_props = sport == "Baseball"
        for comp in comps:
            try:
                await self._scrape_competition(browser, sport, comp, cfg, result, use_props=use_props)
            except Exception as exc:
                result.errors.append(f"{sport}/{comp}: {exc}")
                print(f"    error {comp}: {exc}")

    async def _scrape_competition(
        self, browser, sport: str, competition: str, cfg: ScrapeConfig,
        result: ScrapeResult, use_props: bool = False,
    ) -> None:
        print(f"    coupon {competition}")
        ctx, page = await self._coupon_session(browser, sport, competition)
        if not page:
            print("      (could not open coupon)")
            return
        extras: dict[str, list] = {}
        rows: list[dict] = []
        try:
            body = await self._body(page)
            rows = parse_game_lines(body) if sport == "Baseball" else []
            if not rows:
                rows = parse_match_coupon(body)
            if not rows:
                rows = parse_winner_list(body)
            tabs = [t for t in PROP_TABS if t in body]
            if use_props and cfg.depth == "full" and rows and tabs:
                extras = await self._scrape_prop_tabs(browser, ctx, page, sport, competition, rows, tabs)
                ctx, page = None, None
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
        if not rows:
            print("      (no odds on coupon)")
            return
        seen = {e.event_id for e in result.events}
        nprop = 0
        sport_raw = sport.lower()
        for row in rows:
            extra = extras.get(_event_key(row.get("away") or "", row.get("home") or "", row.get("time") or ""))
            event = _to_event(competition, row, extra, sport=sport_raw)
            if extra:
                nprop += sum(len(m.outcomes) for m in extra)
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            result.events.append(event)
        nmk = sum(len(r["markets"]) for r in rows)
        print(f"      {len(rows)} events  {nmk} markets  {nprop} prop outcomes")

    async def _coupon_session(self, browser, sport: str, competition: str):
        ctx = await new_context(browser)
        page = await ctx.new_page()
        try:
            await self._open(page)
            if not await self._goto_sport(page, sport):
                await ctx.close()
                return None, None
            if competition != sport and not await self._click_label(page, competition):
                await ctx.close()
                return None, None
            body = await self._wait_text(
                page,
                "Money Line", "Run Line", "Game Lines", "Full Time Result",
                "Match Winner", "To Win", "Winner", "Spread",
                timeout=12_000,
            )
            if self._closed(body):
                await ctx.close()
                return None, None
            return ctx, page
        except Exception:
            try:
                await ctx.close()
            except Exception:
                pass
            return None, None

    async def _read_prop_tab(self, page: Page, tab: str, rows: list[dict]) -> dict[str, list]:
        if not await self._click_label(page, tab):
            print(f"    {tab}: tab not listed")
            return {}
        await page.wait_for_timeout(3500)
        body = await self._wait_text(page, "Player / Last 5", timeout=8000)
        if self._closed(body):
            print(f"    {tab}: coupon closed")
            return {}
        try:
            data = await page.evaluate(EXTRACT_GRIDS)
        except Exception:
            data = {}
        blocks = (data or {}).get("blocks") or []
        got = grids_to_markets(tab, blocks, rows)
        n = sum(len(m.outcomes) for mkts in got.values() for m in mkts)
        print(f"    {tab}: {len(blocks)} grids  {n} outcomes")
        return got

    async def _scrape_prop_tabs(
        self, browser, ctx, page: Page, sport: str, competition: str,
        rows: list[dict], tabs: tuple[str, ...] | list[str],
    ) -> dict[str, list]:
        extras: dict[str, list] = {}
        tab_list = list(tabs)
        try:
            for key, mkts in (await self._read_prop_tab(page, tab_list[0], rows)).items():
                extras[key] = list(extras.get(key) or []) + mkts
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
        for tab in tab_list[1:]:
            ctx2, page2 = await self._coupon_session(browser, sport, competition)
            if not page2:
                print(f"    {tab}: could not reopen coupon")
                continue
            try:
                got = await self._read_prop_tab(page2, tab, rows)
                for key, mkts in got.items():
                    extras[key] = list(extras.get(key) or []) + mkts
            finally:
                try:
                    await ctx2.close()
                except Exception:
                    pass
        return extras

    def _closed(self, body: str) -> bool:
        t = body or ""
        return "Unable to display" in t or "no longer available" in t.lower()

    async def _click_label(self, page: Page, label: str) -> bool:
        loc = page.get_by_text(label, exact=True)
        try:
            count = await loc.count()
        except Exception:
            return False
        if not count:
            return False
        try:
            await loc.last.click(timeout=5000)
            await page.wait_for_timeout(2500)
            return True
        except Exception:
            try:
                await loc.first.click(timeout=5000)
                await page.wait_for_timeout(2500)
                return True
            except Exception:
                return False

    async def _wait_text(self, page: Page, *needles: str, timeout: int = 8000) -> str:
        steps = max(1, timeout // 400)
        body = ""
        for _ in range(steps):
            body = await self._body(page)
            if any(n in body for n in needles):
                return body
            await page.wait_for_timeout(400)
        return body

    async def _body(self, page: Page) -> str:
        try:
            return await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            return ""
