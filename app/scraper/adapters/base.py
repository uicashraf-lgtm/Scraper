from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bs4 import BeautifulSoup


@dataclass
class VariantData:
    """A single dosage variant with its price."""
    dosage: float
    unit: str  # "mg", "mcg", "IU", etc.
    price: float | None  # None if per-variant price unknown


@dataclass
class AdapterResult:
    ok: bool
    product_name: str | None
    price: float | None      # lowest price (for simple products: the only price)
    currency: str | None
    message: str | None = None
    # Variable products: list of available variant labels e.g. ["5 mg", "10 mg"]
    variant_amounts: list = None
    # Structured variants with per-variant price (preferred over variant_amounts)
    variants: list[VariantData] = None
    price_max: float | None = None  # highest variant price (None for simple products)
    category: str | None = None
    tags: list[str] | None = None

    def __post_init__(self):
        if self.variant_amounts is None:
            self.variant_amounts = []
        if self.variants is None:
            self.variants = []
        if self.tags is None:
            self.tags = []


class PriceAdapter(Protocol):
    name: str

    def matches(self, url: str, soup: BeautifulSoup, body: str) -> bool:
        ...

    def extract(self, url: str, soup: BeautifulSoup, body: str) -> AdapterResult:
        ...
