from __future__ import annotations

from dataclasses import dataclass, field

from bookie_scraper.normalize import canon_sport, is_american_football, sport_family


def _slug(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "-").replace("_", "-")


_ESPORTS_UMBRELLA = {"esports", "e-sports", "e-sport", "e-leagues", "eleagues", "e-league"}


@dataclass
class ScrapeConfig:
    bookmakers: list[str]
    sports: list[str] = field(default_factory=list)  # empty = all
    depth: str = "full"  # main | full
    headed: bool = False
    output_dir: str = "data"
    concurrency: int = 4
    request_delay: float = 0.15
    debug: bool = False

    def wants_sport(self, *names: str) -> bool:
        """True if this bookmaker sport/slug should be scraped.

        `-s esports` matches Esports plus CS, LoL, Dota, Valorant, e-leagues, etc.
        `-s valorant` matches only Valorant.
        `-s soccer` is association football only (not NFL).
        `-s football` is also soccer (European naming). Use `-s american-football` or `-s nfl` for NFL.
        """
        if not self.sports:
            return True
        wanted_slugs = {_slug(s) for s in self.sports}
        wanted_canon = {canon_sport(s)[0] for s in self.sports}
        esports_all = bool(wanted_slugs & _ESPORTS_UMBRELLA)
        soccer_only = "soccer" in wanted_canon and "american_football" not in wanted_canon
        gridiron_only = "american_football" in wanted_canon and "soccer" not in wanted_canon
        for name in names:
            slug = _slug(name)
            if not slug:
                continue
            if soccer_only and is_american_football(name):
                continue
            if gridiron_only and canon_sport(name)[0] == "soccer":
                continue
            if slug in wanted_slugs:
                return True
            key, _ = canon_sport(name)
            if key in wanted_canon:
                return True
            if key.replace("_", "-") in wanted_slugs:
                return True
            if esports_all and sport_family(name) == "esports":
                return True
        return False
