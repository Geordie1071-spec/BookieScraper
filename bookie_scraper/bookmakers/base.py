from __future__ import annotations

from abc import ABC, abstractmethod

from bookie_scraper.config import ScrapeConfig
from bookie_scraper.models import ScrapeResult


class Bookmaker(ABC):
    key: str
    title: str

    @abstractmethod
    async def scrape(self, cfg: ScrapeConfig) -> ScrapeResult:
        ...
