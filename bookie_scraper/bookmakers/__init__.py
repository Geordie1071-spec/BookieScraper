from __future__ import annotations

from collections.abc import Iterator, Mapping
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bookie_scraper.bookmakers.base import Bookmaker

# module path, class name — loaded on first use so HTTP books do not import Playwright
_ADAPTERS: dict[str, tuple[str, str]] = {
    "pinnacle": ("bookie_scraper.bookmakers.pinnacle", "Pinnacle"),
    "bet365": ("bookie_scraper.bookmakers.bet365", "Bet365"),
    "betsson": ("bookie_scraper.bookmakers.betsson", "Betsson"),
    "betway": ("bookie_scraper.bookmakers.betway", "Betway"),
    "ivybet": ("bookie_scraper.bookmakers.ivybet", "IvyBet"),
    "unibet": ("bookie_scraper.bookmakers.unibet", "Unibet"),
    "bwin": ("bookie_scraper.bookmakers.bwin", "Bwin"),
}

HTTP_BOOKMAKERS = frozenset({"pinnacle", "bwin", "unibet"})
PLAYWRIGHT_BOOKMAKERS = frozenset(_ADAPTERS) - HTTP_BOOKMAKERS


class _Registry(Mapping[str, type["Bookmaker"]]):
    def __init__(self) -> None:
        self._cache: dict[str, type[Bookmaker]] = {}

    def __contains__(self, key: object) -> bool:
        return key in _ADAPTERS

    def __getitem__(self, key: str) -> type[Bookmaker]:
        if key not in _ADAPTERS:
            raise KeyError(key)
        if key not in self._cache:
            module_name, class_name = _ADAPTERS[key]
            try:
                module = import_module(module_name)
            except ModuleNotFoundError as exc:
                if key in PLAYWRIGHT_BOOKMAKERS and "playwright" in str(exc).lower():
                    raise ModuleNotFoundError(
                        f"{key} requires Playwright. Install with: "
                        "pip install 'bookie-scraper[playwright]' "
                        "and run python -m playwright install chromium"
                    ) from exc
                raise
            self._cache[key] = getattr(module, class_name)
        return self._cache[key]

    def __iter__(self) -> Iterator[str]:
        return iter(_ADAPTERS)

    def __len__(self) -> int:
        return len(_ADAPTERS)


REGISTRY: Mapping[str, type[Bookmaker]] = _Registry()
