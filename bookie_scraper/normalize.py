from __future__ import annotations

import re

from bookie_scraper.models import Event, Market, Outcome, utc_now

# Canonical sport keys used across bookmakers (Odds-API-ish).
SPORT_ALIASES: dict[str, tuple[str, str]] = {
    "soccer": ("soccer", "Soccer"),
    "association-football": ("soccer", "Soccer"),
    # European books use "football" for soccer. NFL is american-football / nfl.
    "football": ("soccer", "Soccer"),
    "american-football": ("american_football", "American Football"),
    "american_football": ("american_football", "American Football"),
    "nfl": ("american_football", "American Football"),
    "ncaaf": ("american_football", "American Football"),
    "ncaafb": ("american_football", "American Football"),
    "college-football": ("american_football", "American Football"),
    "cfl": ("american_football", "American Football"),
    "canadian-football": ("american_football", "American Football"),
    "gridiron": ("american_football", "American Football"),
    "aussie-rules": ("aussie_rules", "Aussie Rules"),
    "australian-rules": ("aussie_rules", "Aussie Rules"),
    "australian_rules": ("aussie_rules", "Aussie Rules"),
    "basketball": ("basketball", "Basketball"),
    "basketball-3x3": ("basketball_3x3", "Basketball 3x3"),
    "basketball_3x3": ("basketball_3x3", "Basketball 3x3"),
    "tennis": ("tennis", "Tennis"),
    "table-tennis": ("table_tennis", "Table Tennis"),
    "table_tennis": ("table_tennis", "Table Tennis"),
    "ice-hockey": ("ice_hockey", "Ice Hockey"),
    "ice_hockey": ("ice_hockey", "Ice Hockey"),
    "hockey": ("ice_hockey", "Ice Hockey"),
    "field-hockey": ("field_hockey", "Field Hockey"),
    "baseball": ("baseball", "Baseball"),
    "mlb": ("baseball", "Baseball"),
    "npb": ("baseball", "Baseball"),
    "kbo": ("baseball", "Baseball"),
    "cpbl": ("baseball", "Baseball"),
    "volleyball": ("volleyball", "Volleyball"),
    "beach-volleyball": ("beach_volleyball", "Beach Volleyball"),
    "handball": ("handball", "Handball"),
    "futsal": ("futsal", "Futsal"),
    "boxing": ("boxing", "Boxing"),
    "boxing/ufc": ("mma", "MMA"),
    "mma": ("mma", "MMA"),
    "ufc": ("mma", "MMA"),
    "ufc---martial-arts": ("mma", "MMA"),
    "martial-arts": ("mma", "MMA"),
    "darts": ("darts", "Darts"),
    "snooker": ("snooker", "Snooker"),
    "cricket": ("cricket", "Cricket"),
    "rugby": ("rugby_union", "Rugby Union"),
    "rugby-union": ("rugby_union", "Rugby Union"),
    "rugby_union": ("rugby_union", "Rugby Union"),
    "rugby-league": ("rugby_league", "Rugby League"),
    "rugby_league": ("rugby_league", "Rugby League"),
    "golf": ("golf", "Golf"),
    "formula-1": ("formula_1", "Formula 1"),
    "formula_1": ("formula_1", "Formula 1"),
    "formula1": ("formula_1", "Formula 1"),
    "f1": ("formula_1", "Formula 1"),
    "motor-sport": ("motorsport", "Motorsport"),
    "motorsport": ("motorsport", "Motorsport"),
    "cycling": ("cycling", "Cycling"),
    "badminton": ("badminton", "Badminton"),
    "floorball": ("floorball", "Floorball"),
    "chess": ("chess", "Chess"),
    "pickleball": ("pickleball", "Pickleball"),
    "water-polo": ("water_polo", "Water Polo"),
    "gaelic-sports": ("gaelic_sports", "Gaelic Sports"),
    "esports": ("esports", "Esports"),
    "e-sports": ("esports", "Esports"),
    "e-leagues": ("esports", "Esports"),
    "eleagues": ("esports", "Esports"),
    "counter-strike": ("esports_cs", "Counter-Strike"),
    "counter-strike-2": ("esports_cs", "Counter-Strike"),
    "counter-strike-go": ("esports_cs", "Counter-Strike"),
    "esports_counter_strike": ("esports_cs", "Counter-Strike"),
    "cs2": ("esports_cs", "Counter-Strike"),
    "cs-2": ("esports_cs", "Counter-Strike"),
    "csgo": ("esports_cs", "Counter-Strike"),
    "cs-go": ("esports_cs", "Counter-Strike"),
    "dota": ("esports_dota2", "Dota 2"),
    "dota-2": ("esports_dota2", "Dota 2"),
    "dota2": ("esports_dota2", "Dota 2"),
    "esports_dota_2": ("esports_dota2", "Dota 2"),
    "league-of-legends": ("esports_lol", "League of Legends"),
    "esports_league_of_legends": ("esports_lol", "League of Legends"),
    "lol": ("esports_lol", "League of Legends"),
    "valorant": ("esports_valorant", "Valorant"),
    "esports_valorant": ("esports_valorant", "Valorant"),
    "apex-legends": ("esports_apex", "Apex Legends"),
    "esports_apex_legends": ("esports_apex", "Apex Legends"),
    "overwatch": ("esports_ow", "Overwatch"),
    "overwatch-2": ("esports_ow", "Overwatch"),
    "rainbow-six": ("esports_r6", "Rainbow Six"),
    "rainbow-six-siege": ("esports_r6", "Rainbow Six"),
    "rocket-league": ("esports_rl", "Rocket League"),
    "call-of-duty": ("esports_cod", "Call of Duty"),
    "king-of-glory": ("esports_kog", "King of Glory"),
    "mobile-legends": ("esports_mlbb", "Mobile Legends"),
    "starcraft": ("esports_sc", "StarCraft"),
    "starcraft-2": ("esports_sc", "StarCraft"),
    "horse-racing": ("horse_racing", "Horse Racing"),
    "greyhounds": ("greyhounds", "Greyhounds"),
    "politics": ("politics", "Politics"),
    "specials": ("specials", "Specials"),
}

_H2H = re.compile(
    r"\b(match winner|winner|1x2|moneyline|match result|full time result|"
    r"to win|home/away|home away|draw no bet)\b",
    re.I,
)
_TOTALS = re.compile(r"\b(over/?under|total(?:s)?|o/u|total (?:goals|points|maps))\b", re.I)
_SPREADS = re.compile(r"\b(handicap|spread|asian handicap|puck line|run line)\b", re.I)
_BTTS = re.compile(r"\b(both teams? to score|btts)\b", re.I)


_ESPORTS_NAME_HINTS = (
    "esport", "e-league", "eleague", "counter-strike", "counter strike",
    "dota", "league-of-legends", "league of legends", "valorant",
    "overwatch", "rainbow-six", "rainbow six", "rocket-league",
    "call-of-duty", "call of duty", "king-of-glory", "king of glory",
    "mobile-legends", "starcraft", "cs2", "csgo",
)


def is_american_football(raw: str) -> bool:
    """True for NFL / college / CFL / gridiron — not association football (soccer)."""
    blob = (raw or "").strip().lower().replace("_", "-")
    if re.search(r"american[\s-]*football", blob):
        return True
    spaced = blob.replace("-", " ")
    if re.search(r"\b(nfl|ncaaf|ncaafb|cfl)\b", spaced):
        return True
    if "college football" in spaced or "canadian football" in spaced:
        return True
    return "gridiron" in blob


def canon_sport(raw: str) -> tuple[str, str]:
    """Return (sport_key, sport_title) from a bookmaker sport name/slug."""
    if not raw:
        return "unknown", "Unknown"
    if is_american_football(raw):
        return "american_football", "American Football"
    key = raw.strip().lower().replace(" ", "-").replace("_", "-")
    if key in SPORT_ALIASES:
        return SPORT_ALIASES[key]
    if key.startswith("esports-"):
        rest = key[len("esports-"):].replace("-", "_")
        return (f"esports_{rest}" if rest else "esports"), raw.replace("-", " ").title()
    slug = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    title = raw.replace("-", " ").replace("_", " ").title()
    return slug or "unknown", title or "Unknown"


def sport_family(raw: str) -> str:
    """Coarse sport bucket used by -s filters (esports, football, tennis, ...)."""
    key, _ = canon_sport(raw)
    if key.startswith("esports"):
        return "esports"
    blob = (raw or "").strip().lower().replace("_", "-")
    if any(hint in blob for hint in _ESPORTS_NAME_HINTS):
        return "esports"
    return key.replace("_", "-")


def canon_market(name: str) -> str:
    n = name or ""
    if _BTTS.search(n):
        return "btts"
    if _SPREADS.search(n):
        return "spreads"
    if _TOTALS.search(n):
        return "totals"
    if _H2H.search(n):
        return "h2h"
    slug = re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")
    return slug[:80] or "other"


def split_teams(event_name: str) -> tuple[str, str]:
    for sep in (" vs ", " vs. ", " v ", " @ ", " - "):
        if sep in event_name:
            a, b = event_name.split(sep, 1)
            return a.strip(), b.strip()
    return event_name.strip(), ""


def build_event(
    bookmaker: str,
    event_id: str,
    sport_raw: str,
    competition: str,
    name: str,
    starts_at: str,
    status: str,
    markets: list[Market],
    home: str = "",
    away: str = "",
) -> Event:
    sport_key, sport_title = canon_sport(sport_raw)
    if not home and not away:
        home, away = split_teams(name)
    return Event(
        bookmaker=bookmaker,
        event_id=str(event_id),
        sport_key=sport_key,
        sport_title=sport_title,
        competition=competition or "Unknown",
        name=name,
        home=home,
        away=away,
        starts_at=starts_at or "",
        status=status or "",
        markets=markets,
        scraped_at=utc_now(),
    )


def group_outcomes(items: list[tuple[str, str, float | None, bool, float | None]]) -> list[Market]:
    """items: (market_name, outcome_name, price, active, point)."""
    grouped: dict[str, Market] = {}
    order: list[str] = []
    for mname, oname, price, active, point in items:
        if not mname or not oname or price is None:
            continue
        if mname not in grouped:
            grouped[mname] = Market(key=canon_market(mname), name=mname, outcomes=[])
            order.append(mname)
        grouped[mname].outcomes.append(Outcome(name=oname, price=price, point=point, active=active))
    return [grouped[k] for k in order]


def parse_number(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        value = (
            value.get("decimal")
            or value.get("decimalPrice")
            or value.get("american")
            or value.get("value")
            or next(iter(value.values()), None)
        )
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_price(value) -> float | None:
    price = parse_number(value)
    if price is None or price <= 1.0:
        return None
    return price


def fractional_to_decimal(value) -> float | None:
    text = str(value or "").strip()
    if "/" not in text:
        return parse_price(text)
    try:
        num, den = text.replace(",", ".").split("/", 1)
        return round(float(num) / float(den) + 1.0, 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def american_to_decimal(american) -> float | None:
    n = parse_number(american)
    if n is None:
        return None
    if n >= 100:
        return round(n / 100.0 + 1.0, 3)
    if n <= -100:
        return round(100.0 / abs(n) + 1.0, 3)
    return None
