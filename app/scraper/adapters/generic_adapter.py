from bs4 import BeautifulSoup

from app.scraper.adapters.base import AdapterResult
from app.scraper.adapters.common import parse_price_from_text, title_name


class GenericAdapter:
    name = "generic"

    def matches(self, url: str, soup: BeautifulSoup, body: str) -> bool:
        return True

    def extract(self, url: str, soup: BeautifulSoup, body: str) -> AdapterResult:
        price, currency = parse_price_from_text(soup.get_text(" ", strip=True)[:10000])
        if price is not None:
            return AdapterResult(True, title_name(soup), price, currency, None)
        return AdapterResult(False, title_name(soup), None, None, "generic_price_not_found")
