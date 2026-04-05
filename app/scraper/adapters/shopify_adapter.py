from bs4 import BeautifulSoup

from app.scraper.adapters.base import AdapterResult
from app.scraper.adapters.common import parse_price_from_text, read_text, title_name


class ShopifyAdapter:
    name = "shopify"

    def matches(self, url: str, soup: BeautifulSoup, body: str) -> bool:
        if "myshopify.com" in url.lower():
            return True
        if "shopify" in body.lower() and ("ProductJson" in body or "shopify-payment-button" in body):
            return True
        return False

    def extract(self, url: str, soup: BeautifulSoup, body: str) -> AdapterResult:
        price_text = read_text(
            soup,
            [
                "span.price-item--sale",
                "span.price-item--regular",
                "div.price__regular span.price-item",
                "[data-product-price]",
            ],
        )
        if price_text:
            price, currency = parse_price_from_text(price_text)
            if price is not None:
                return AdapterResult(True, title_name(soup), price, currency, None)

        fallback_price, fallback_currency = parse_price_from_text(soup.get_text(" ", strip=True)[:8000])
        if fallback_price is not None:
            return AdapterResult(True, title_name(soup), fallback_price, fallback_currency, None)

        return AdapterResult(False, title_name(soup), None, None, "shopify_price_not_found")
