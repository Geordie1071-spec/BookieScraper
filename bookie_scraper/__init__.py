"""Multi-bookmaker odds scraper — backup (and potential replacement) for a paid odds feed."""

from bookie_scraper.bookmakers import HTTP_BOOKMAKERS, PLAYWRIGHT_BOOKMAKERS, REGISTRY
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.runner import run
from bookie_scraper.storage import results_to_csv

__version__ = "0.1.0"

__all__ = [
    "HTTP_BOOKMAKERS",
    "PLAYWRIGHT_BOOKMAKERS",
    "REGISTRY",
    "ScrapeConfig",
    "results_to_csv",
    "run",
    "__version__",
]
