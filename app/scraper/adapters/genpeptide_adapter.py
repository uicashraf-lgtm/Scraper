from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.scraper.adapters.base import AdapterResult
from app.scraper.adapters.common import extract_from_jsonld, read_attr, parse_price_from_text
from app.scraper.adapters.woocommerce_adapter import (
    WooCommerceAdapter,
    _product_name,
    _extract_variant_amounts,
)


class GenPeptideAdapter:
    name = "genpeptide"

    def matches(self, url: str, soup: BeautifulSoup, body: str) -> bool:
        domain = (urlparse(url).netloc or "").lower()
        return "genpeptide.com" in domain

    def extract(self, url: str, soup: BeautifulSoup, body: str) -> AdapterResult:
        # GenPeptide is WooCommerce — delegate to WooCommerce adapter first
        wc = WooCommerceAdapter().extract(url, soup, body)
        if wc.ok:
            return wc

        # JSON-LD fallback
        jsonld = extract_from_jsonld(soup)
        if jsonld.ok:
            jsonld.product_name = jsonld.product_name or _product_name(soup)
            jsonld.variant_amounts = _extract_variant_amounts(soup)
            return jsonld

        # Meta tag fallback
        meta_price = read_attr(soup, [
            "meta[property='product:price:amount']",
            "meta[itemprop='price']",
        ], "content")
        if meta_price:
            price, currency = parse_price_from_text(meta_price)
            if price is not None:
                return AdapterResult(True, _product_name(soup), price, currency or "USD", None,
                                     variant_amounts=_extract_variant_amounts(soup))

        # Propagate variant amounts even on failure so _enrich can use them
        return AdapterResult(False, _product_name(soup), None, None, "genpeptide_price_not_found",
                             variant_amounts=wc.variant_amounts or _extract_variant_amounts(soup))
