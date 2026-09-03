from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from bookie_scraper import (
    HTTP_BOOKMAKERS,
    PLAYWRIGHT_BOOKMAKERS,
    REGISTRY,
    ScrapeConfig,
    results_to_csv,
    run,
)
from bookie_scraper.models import Event, Market, Outcome, ScrapeResult
from bookie_scraper.storage import CSV_FIELDS


def test_public_exports() -> None:
    assert callable(run)
    assert callable(results_to_csv)
    assert "pinnacle" in REGISTRY
    assert "bwin" in REGISTRY
    assert "unibet" in REGISTRY
    assert HTTP_BOOKMAKERS == frozenset({"pinnacle", "bwin", "unibet"})


def test_registry_lists_keys_without_loading_playwright() -> None:
    keys = list(REGISTRY)
    assert "pinnacle" in keys
    assert "bet365" in keys
    assert set(HTTP_BOOKMAKERS) <= set(keys)
    assert PLAYWRIGHT_BOOKMAKERS.isdisjoint(HTTP_BOOKMAKERS)
    assert "playwright" not in sys.modules
    assert "bookie_scraper.bookmakers.bet365" not in sys.modules


def test_http_adapters_load_without_playwright() -> None:
    pinnacle = REGISTRY["pinnacle"]
    bwin = REGISTRY["bwin"]
    unibet = REGISTRY["unibet"]
    assert pinnacle.key == "pinnacle"
    assert bwin.key == "bwin"
    assert unibet.key == "unibet"
    assert "playwright" not in sys.modules
    assert "bookie_scraper.bookmakers.bet365" not in sys.modules
    assert "bookie_scraper.bookmakers.pinnacle" in sys.modules
    assert "bookie_scraper.bookmakers.bwin" in sys.modules
    assert "bookie_scraper.bookmakers.unibet" in sys.modules


def test_results_to_csv_header_and_row() -> None:
    event = Event(
        bookmaker="pinnacle",
        event_id="1",
        sport_key="soccer",
        sport_title="Football",
        competition="EPL",
        name="A vs B",
        home="A",
        away="B",
        starts_at="2026-01-01T12:00:00Z",
        status="upcoming",
        markets=[
            Market(
                key="h2h",
                name="Moneyline",
                outcomes=[
                    Outcome(name="A", price=1.9),
                    Outcome(name="B", price=2.1),
                    Outcome(name="2-1", price=3.4),
                    Outcome(name="skip", price=None),
                ],
            )
        ],
        scraped_at="2026-01-01T00:00:00Z",
    )
    csv_text = results_to_csv([ScrapeResult(bookmaker="pinnacle", events=[event])])
    lines = csv_text.strip().splitlines()
    header = lines[0].split(",")
    assert header == CSV_FIELDS
    assert len(lines) == 4
    cells = lines[1].split(",")
    assert all(cell.startswith("'") for cell in cells)
    assert "'pinnacle,'Football,'soccer,'EPL,'A vs B,'1,'A,'B" in lines[1]
    assert "'1.9" in lines[1]
    assert cells[CSV_FIELDS.index("point")] == "'"
    assert "'2-1" in lines[3]
    assert "skip" not in csv_text


def test_results_to_csv_empty_still_has_header() -> None:
    csv_text = results_to_csv([ScrapeResult(bookmaker="pinnacle")])
    assert csv_text.startswith("bookmaker,sport,sport_key,")
    assert csv_text.count("\n") == 1


def test_unknown_bookmaker_raises_value_error() -> None:
    async def _go() -> None:
        with pytest.raises(ValueError, match="Unknown bookmaker"):
            await run(ScrapeConfig(bookmakers=["not-a-book"]))

    asyncio.run(_go())


def test_run_skips_disk_when_output_dir_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bookie_scraper import runner

    class Fake:
        async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
            return ScrapeResult(bookmaker="pinnacle")

    class FakeReg:
        def __contains__(self, key: object) -> bool:
            return key == "pinnacle"

        def __getitem__(self, key: str) -> type:
            return Fake

    saved: list[object] = []
    monkeypatch.setattr(runner, "REGISTRY", FakeReg())
    monkeypatch.setattr(runner, "save_run", lambda *args, **kwargs: saved.append(args))

    async def skip_disk() -> list[ScrapeResult]:
        return await runner.run(ScrapeConfig(bookmakers=["pinnacle"], output_dir=None))

    results = asyncio.run(skip_disk())
    assert saved == []
    assert results[0].bookmaker == "pinnacle"

    async def write_disk() -> list[ScrapeResult]:
        return await runner.run(ScrapeConfig(bookmakers=["pinnacle"], output_dir=str(tmp_path)))

    asyncio.run(write_disk())
    assert saved


def test_unibet_kambi_odds_and_event() -> None:
    from bookie_scraper.bookmakers.unibet import event_from_kambi, kambi_odds

    assert kambi_odds(1930) == 1.93
    assert kambi_odds(1.91) == 1.91
    event = event_from_kambi(
        {
            "id": 9,
            "englishName": "Home - Away",
            "homeName": "Home",
            "awayName": "Away",
            "start": "2026-01-01T12:00:00Z",
            "state": "NOT_STARTED",
            "group": "EPL",
            "path": [
                {"englishName": "Football"},
                {"englishName": "EPL"},
            ],
        },
        [
            {
                "criterion": {"englishLabel": "Full Time"},
                "betOfferType": {"englishName": "Match"},
                "outcomes": [
                    {"type": "OT_ONE", "odds": 1930, "status": "OPEN"},
                    {"type": "OT_CROSS", "odds": 3500, "status": "OPEN"},
                    {"type": "OT_TWO", "odds": 4000, "status": "OPEN"},
                    {
                        "label": "Over",
                        "odds": 1900,
                        "line": 2500,
                        "status": "OPEN",
                        "englishLabel": "Over",
                    },
                ],
            }
        ],
    )
    assert event is not None
    assert event.bookmaker == "unibet"
    assert event.home == "Home"
    names = {o.name: o for m in event.markets for o in m.outcomes}
    assert names["Home"].price == 1.93
    assert names["Draw"].price == 3.5
    assert names["Over"].point == 2.5

