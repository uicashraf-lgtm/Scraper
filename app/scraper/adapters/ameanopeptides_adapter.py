from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.scraper.adapters.base import AdapterResult
from app.scraper.adapters.common import extract_from_jsonld, parse_price_from_text, read_attr, read_text, title_name


class AmeanoPeptidesAdapter:
    name = "ameanopeptides"

    def matches(self, url: str, soup: BeautifulSoup, body: str) -> bool:
        domain = (urlparse(url).netloc or "").lower()
        return "ameanopeptides.com" in domain

    def extract(self, url: str, soup: BeautifulSoup, body: str) -> AdapterResult:
        jsonld = extract_from_jsonld(soup)
        if jsonld.ok:
            return jsonld

        meta_price = read_attr(soup, ["meta[property='product:price:amount']", "meta[itemprop='price']"], "content")
        if meta_price:
            price, currency = parse_price_from_text(meta_price)
            if price is not None:
                return AdapterResult(True, title_name(soup), price, currency, None)

        price_text = read_text(
            soup,
            [
                "span.price",
                "p.price",
                "[itemprop='price']",
                "[class*='price']",
            ],
        )
        if price_text:
            price, currency = parse_price_from_text(price_text)
            if price is not None:
                return AdapterResult(True, title_name(soup), price, currency, None)

        fallback_price, fallback_currency = parse_price_from_text(soup.get_text(" ", strip=True)[:10000])
        if fallback_price is not None:
            return AdapterResult(True, title_name(soup), fallback_price, fallback_currency, None)

        return AdapterResult(False, title_name(soup), None, None, "ameanopeptides_price_not_found")
