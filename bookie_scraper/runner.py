from __future__ import annotations

import asyncio

from bookie_scraper.bookmakers import REGISTRY
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.models import ScrapeResult
from bookie_scraper.storage import save_run


async def run(cfg: ScrapeConfig) -> list[ScrapeResult]:
    unknown = [b for b in cfg.bookmakers if b not in REGISTRY]
    if unknown:
        raise SystemExit(f"Unknown bookmaker(s): {unknown}. Valid: {list(REGISTRY)}")

    print(f"Scraping: {cfg.bookmakers}  depth={cfg.depth}")
    if cfg.sports:
        print(f"Sports filter: {cfg.sports}")

    async def one(key: str) -> ScrapeResult:
        scraper = REGISTRY[key]()
        try:
            return await scraper.scrape(cfg)
        except Exception as exc:
            res = ScrapeResult(bookmaker=key)
            res.errors.append(str(exc))
            print(f"[{key}] crashed: {exc}")
            return res

    results = await asyncio.gather(*(one(b) for b in cfg.bookmakers))
    save_run(cfg.output_dir, list(results))
    print("\nAll done.")
    return list(results)
