from bs4 import BeautifulSoup

from app.scraper.adapters.base import AdapterResult
from app.scraper.adapters.common import parse_price_from_text, read_text, title_name


class BigCommerceAdapter:
    name = "bigcommerce"

    def matches(self, url: str, soup: BeautifulSoup, body: str) -> bool:
        lower = body.lower()
        return "stencil-utils" in lower or "bigcommerce" in lower

    def extract(self, url: str, soup: BeautifulSoup, body: str) -> AdapterResult:
        price_text = read_text(soup, ["[data-product-price-without-tax]", ".price--withoutTax", ".price.price--main"])
        if price_text:
            price, currency = parse_price_from_text(price_text)
            if price is not None:
                return AdapterResult(True, title_name(soup), price, currency, None)

        fallback_price, fallback_currency = parse_price_from_text(soup.get_text(" ", strip=True)[:8000])
        if fallback_price is not None:
            return AdapterResult(True, title_name(soup), fallback_price, fallback_currency, None)

        return AdapterResult(False, title_name(soup), None, None, "bigcommerce_price_not_found")
