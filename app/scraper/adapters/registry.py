from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.scraper.adapters.ameanopeptides_adapter import AmeanoPeptidesAdapter
from app.scraper.adapters.base import PriceAdapter
from app.scraper.adapters.bigcommerce_adapter import BigCommerceAdapter
from app.scraper.adapters.ezpeptides_adapter import EZPeptidesAdapter
from app.scraper.adapters.genpeptide_adapter import GenPeptideAdapter
from app.scraper.adapters.generic_adapter import GenericAdapter
from app.scraper.adapters.jsonld_adapter import JsonLdAdapter
from app.scraper.adapters.shopify_adapter import ShopifyAdapter
from app.scraper.adapters.woocommerce_adapter import WooCommerceAdapter


DOMAIN_HINTS: dict[str, str] = {
    "genpeptide.com": "genpeptide",
    "ezpeptides.com": "ezpeptides",
    "ameanopeptides.com": "ameanopeptides",
    "myshopify.com": "shopify",
}

# Platform name → adapter class (for admin-set platform override)
PLATFORM_ADAPTERS: dict[str, type] = {
    "woocommerce": WooCommerceAdapter,
    "shopify": ShopifyAdapter,
    "bigcommerce": BigCommerceAdapter,
}


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def adapter_chain(url: str, soup: BeautifulSoup, body: str, platform: str | None = None) -> list[PriceAdapter]:
    adapters: list[PriceAdapter] = []

    # If admin explicitly set the platform, put that adapter first
    if platform and platform in PLATFORM_ADAPTERS:
        adapters.append(PLATFORM_ADAPTERS[platform]())

    # Domain-specific adapters (vendor-specific logic)
    domain = _domain(url)
    hinted = DOMAIN_HINTS.get(domain)
    if hinted == "genpeptide":
        adapters.append(GenPeptideAdapter())
    elif hinted == "ezpeptides":
        adapters.append(EZPeptidesAdapter())
    elif hinted == "ameanopeptides":
        adapters.append(AmeanoPeptidesAdapter())
    elif hinted == "shopify":
        adapters.append(ShopifyAdapter())

    # Generic platform adapters (auto-detected by page fingerprint)
    adapters.extend([
        GenPeptideAdapter(),
        EZPeptidesAdapter(),
        AmeanoPeptidesAdapter(),
        ShopifyAdapter(),
        WooCommerceAdapter(),
        BigCommerceAdapter(),
        JsonLdAdapter(),
        GenericAdapter(),
    ])

    seen: set[str] = set()
    unique: list[PriceAdapter] = []
    for adapter in adapters:
        if adapter.name in seen:
            continue
        seen.add(adapter.name)
        unique.append(adapter)

    return [a for a in unique if a.matches(url, soup, body)]
