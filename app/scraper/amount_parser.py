"""
Parses dosage amount and unit from product name strings.
Examples:
  "BPC-157 10mg"          → (10.0, "mg")
  "Tirzepatide 5 mg"      → (5.0, "mg")
  "IGF-1 LR3 1000mcg"     → (1000.0, "mcg")
  "HGH 100 IU"            → (100.0, "IU")
  "TB-500 2mg/5mL vial"   → (2.0, "mg")
"""
import re

_UNIT_NORMALIZE = {
    "mg": "mg",
    "mcg": "mcg",
    "ug": "mcg",
    "µg": "mcg",
    "g": "g",
    "iu": "IU",
    "ml": "mL",
    "mL": "mL",
}

# Match a number followed by a unit, optionally separated by a space
_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|mcg|ug|µg|g|iu|ml)\b",
    re.IGNORECASE,
)


def parse_amount(product_name: str) -> tuple[float | None, str | None]:
    """
    Return (numeric_value, normalized_unit) extracted from product name,
    or (None, None) if no dosage pattern is found.
    """
    m = _PATTERN.search(product_name)
    if not m:
        return None, None
    raw_value = m.group(1)
    raw_unit = m.group(2).lower()
    unit = _UNIT_NORMALIZE.get(raw_unit, raw_unit)
    return float(raw_value), unit


def compute_price_per_mg(price: float, amount: float, unit: str) -> float | None:
    """
    Convert price to price-per-mg (standard comparison unit).
    Returns None for non-weight units (IU, mL) where comparison isn't meaningful.
    """
    if amount <= 0:
        return None
    if unit == "mg":
        return round(price / amount, 6)
    if unit == "g":
        return round(price / (amount * 1000), 6)
    if unit == "mcg":
        return round(price / (amount / 1000), 6)
    return None  # IU, mL — not convertible to mg
