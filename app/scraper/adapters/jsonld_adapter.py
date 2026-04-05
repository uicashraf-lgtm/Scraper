from bs4 import BeautifulSoup

from app.scraper.adapters.base import AdapterResult
from app.scraper.adapters.common import extract_from_jsonld


class JsonLdAdapter:
    name = "jsonld"

    def matches(self, url: str, soup: BeautifulSoup, body: str) -> bool:
        return bool(soup.find("script", attrs={"type": "application/ld+json"}))

    def extract(self, url: str, soup: BeautifulSoup, body: str) -> AdapterResult:
        return extract_from_jsonld(soup)
