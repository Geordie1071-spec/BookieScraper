from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Outcome:
    name: str
    price: float | None
    point: float | None = None
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "price": self.price}
        if self.point is not None:
            d["point"] = self.point
        d["active"] = self.active
        return d


@dataclass
class Market:
    key: str
    name: str
    outcomes: list[Outcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


@dataclass
class Event:
    bookmaker: str
    event_id: str
    sport_key: str
    sport_title: str
    competition: str
    name: str
    home: str
    away: str
    starts_at: str
    status: str
    markets: list[Market] = field(default_factory=list)
    scraped_at: str = field(default_factory=utc_now)

    def to_odds_api(self) -> dict[str, Any]:
        """Shape close to The Odds API so a backup feed can be swapped in."""
        return {
            "id": f"{self.bookmaker}:{self.event_id}",
            "sport_key": self.sport_key,
            "sport_title": self.sport_title,
            "commence_time": self.starts_at,
            "home_team": self.home,
            "away_team": self.away,
            "event_name": self.name,
            "competition": self.competition,
            "status": self.status,
            "bookmakers": [
                {
                    "key": self.bookmaker,
                    "title": self.bookmaker,
                    "last_update": self.scraped_at,
                    "markets": [
                        {
                            "key": m.key,
                            "name": m.name,
                            "last_update": self.scraped_at,
                            "outcomes": [o.to_dict() for o in m.outcomes],
                        }
                        for m in self.markets
                    ],
                }
            ],
        }

    def to_rows(self) -> list[dict[str, Any]]:
        rows = []
        for market in self.markets:
            for out in market.outcomes:
                if out.price is None or not out.name:
                    continue
                rows.append({
                    "bookmaker": self.bookmaker,
                    "sport": self.sport_title,
                    "sport_key": self.sport_key,
                    "competition": self.competition,
                    "event": self.name,
                    "event_id": self.event_id,
                    "home": self.home,
                    "away": self.away,
                    "starts_at": self.starts_at,
                    "status": self.status,
                    "market": market.name,
                    "market_key": market.key,
                    "outcome": out.name,
                    "odds": out.price,
                    "point": out.point,
                    "active": out.active,
                    "scraped_at": self.scraped_at,
                })
        return rows


@dataclass
class ScrapeResult:
    bookmaker: str
    events: list[Event] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    sport_timings: list[dict[str, Any]] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        sports = {e.sport_key for e in self.events}
        comps = {(e.sport_key, e.competition) for e in self.events}
        odds = sum(len(e.to_rows()) for e in self.events)
        return {
            "bookmaker": self.bookmaker,
            "sports": len(sports),
            "competitions": len(comps),
            "events": len(self.events),
            "odds_rows": odds,
            "sport_timings": self.sport_timings,
            "errors": self.errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.stats()


def note_sport_timing(
    result: ScrapeResult,
    sport: str,
    seconds: float,
    events: int,
    odds_rows: int,
) -> None:
    row = {
        "sport": sport,
        "seconds": round(seconds, 2),
        "events": events,
        "odds_rows": odds_rows,
    }
    result.sport_timings.append(row)
    print(
        f"  timing  {result.bookmaker}, {sport} : {row['seconds']:.1f}s  "
        f"({events} events, {odds_rows} odds)"
    )


class SportClock:
    """Wall-clock timer for one sequential sport scrape."""

    def __init__(self, result: ScrapeResult, sport: str) -> None:
        self.result = result
        self.sport = sport
        self.t0 = time.perf_counter()
        self.n0 = len(result.events)

    def done(self) -> None:
        added = self.result.events[self.n0 :]
        odds = sum(len(e.to_rows()) for e in added)
        note_sport_timing(
            self.result,
            self.sport,
            time.perf_counter() - self.t0,
            len(added),
            odds,
        )
