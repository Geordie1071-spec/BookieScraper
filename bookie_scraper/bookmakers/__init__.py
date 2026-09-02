from bookie_scraper.bookmakers.bet365 import Bet365
from bookie_scraper.bookmakers.betsson import Betsson
from bookie_scraper.bookmakers.betway import Betway
from bookie_scraper.bookmakers.bwin import Bwin
from bookie_scraper.bookmakers.ivybet import IvyBet
from bookie_scraper.bookmakers.pinnacle import Pinnacle

REGISTRY = {
    Pinnacle.key: Pinnacle,
    Bet365.key: Bet365,
    Betsson.key: Betsson,
    Betway.key: Betway,
    IvyBet.key: IvyBet,
    Bwin.key: Bwin,
}
