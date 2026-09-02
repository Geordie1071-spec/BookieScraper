from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from bookie_scraper.models import ScrapeResult, utc_now


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def save_run(output_dir: str, results: list[ScrapeResult]) -> Path:
    run_id = utc_now().replace(":", "").replace("-", "")
    run_dir = Path(output_dir) / "runs" / run_id
    latest_dir = Path(output_dir) / "latest"
    run_dir.mkdir(parents=True, exist_ok=True)
    latest_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict] = []
    all_events: list[dict] = []
    summary = {"run_id": run_id, "bookmakers": []}

    for result in results:
        stats = result.stats()
        summary["bookmakers"].append(stats)
        print(
            f"\n[{result.bookmaker}] {stats['sports']} sports  "
            f"{stats['competitions']} competitions  "
            f"{stats['events']} events  {stats['odds_rows']} odds rows"
        )
        if result.sport_timings:
            print("  timings:")
            for row in result.sport_timings:
                print(
                    f"    {result.bookmaker}, {row['sport']} : {row['seconds']:.1f}s  "
                    f"({row['events']} events, {row['odds_rows']} odds)"
                )
        if result.errors:
            for err in result.errors:
                print(f"  ! {err}")

        events_payload = [e.to_odds_api() for e in result.events]
        rows: list[dict] = []
        for event in result.events:
            rows.extend(event.to_rows())
        all_rows.extend(rows)
        all_events.extend(events_payload)

        bm_json = run_dir / f"{result.bookmaker}_events.json"
        bm_csv = run_dir / f"{result.bookmaker}_flat.csv"
        _write_json(bm_json, events_payload)
        _write_csv(bm_csv, rows)
        _write_json(latest_dir / f"{result.bookmaker}_events.json", events_payload)
        _write_csv(latest_dir / f"{result.bookmaker}_flat.csv", rows)
        print(f"  Saved -> {bm_json.name}  {bm_csv.name}")

    _write_json(run_dir / "all_events.json", all_events)
    _write_csv(run_dir / "all_flat.csv", all_rows)
    _write_json(run_dir / "summary.json", summary)
    _write_json(latest_dir / "all_events.json", all_events)
    _write_csv(latest_dir / "all_flat.csv", all_rows)
    _write_json(latest_dir / "summary.json", summary)

    print(f"\nRun directory: {run_dir}")
    print(f"Latest copies: {latest_dir}")
    return run_dir


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = [
        "bookmaker", "sport", "sport_key", "competition", "event", "event_id",
        "home", "away", "starts_at", "status", "market", "market_key",
        "outcome", "odds", "point", "active", "scraped_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
