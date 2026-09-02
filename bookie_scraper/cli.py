from __future__ import annotations

import argparse
import asyncio
import sys

from bookie_scraper.bookmakers import REGISTRY
from bookie_scraper.config import ScrapeConfig
from bookie_scraper.runner import run


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> None:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="Scrape sportsbook odds from multiple brands into a unified backup feed.",
    )
    parser.add_argument(
        "-b", "--bookmaker",
        default="all",
        help="Bookmaker(s): all, or comma-separated. "
             f"Valid: {', '.join(REGISTRY)}. Default: all",
    )
    parser.add_argument(
        "-s", "--sport", "--sports",
        dest="sports",
        action="append",
        default=None,
        metavar="SPORT",
        help="Sport to scrape. Repeat the flag or use commas. "
             "Examples: -s esports   -s tennis -s football   -s football,tennis. "
             "Default: all sports.",
    )
    parser.add_argument(
        "--depth",
        choices=("main", "full"),
        default="full",
        help="full = every market on every event (default). main = list-page markets only (faster).",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--output", default="data", help="Output directory. Default: data")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    raw = args.bookmaker.lower().strip()
    targets = list(REGISTRY.keys()) if raw == "all" else [b.strip() for b in raw.split(",") if b.strip()]
    sports: list[str] = []
    for chunk in args.sports or []:
        sports.extend(s.strip() for s in chunk.split(",") if s.strip())

    cfg = ScrapeConfig(
        bookmakers=targets,
        sports=sports,
        depth=args.depth,
        headed=args.headed,
        output_dir=args.output,
        concurrency=args.concurrency,
        debug=args.debug,
    )
    try:
        asyncio.run(run(cfg))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
