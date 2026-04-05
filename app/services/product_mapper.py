import re
from sqlalchemy.orm import Session

from app.models.entities import CanonicalProduct

def normalize_product_name(name: str) -> str:
    # BPC-157, BPC157, bpc 157 -> BPC157
    return re.sub(r"[^A-Za-z0-9]", "", name).upper().strip()


def strip_dosage_suffix(name: str) -> str:
    """Strip dosage, packaging, and quantity suffixes to get a base product name.

    Examples:
        "5-Amino-1MQ 50mg (25 tabs/bottle)" -> "5-Amino-1MQ"
        "BPC-157 / TB4 Blend (10mg/10mg)"    -> "BPC-157 / TB4 Blend"
        "EZP-1P 10mg (GLP-1SG)"             -> "EZP-1P (GLP-1SG)"
        "CJC-1295 (No DAC)"                  -> "CJC-1295 (No DAC)"
        "Tesamorelin / Ipamorelin Blend – 10mg / 3mg" -> "Tesamorelin / Ipamorelin Blend"
    """
    s = name
    # 1. Remove trailing packaging like "(25 tabs/bottle)", "(10 vials/Kit)"
    s = re.sub(
        r'\s*[^\w\s]*\s*\(\d+\s*(?:tabs?|capsules?|caps?|vials?|bottles?)\s*/\s*\w+\)\s*$',
        '', s, flags=re.IGNORECASE,
    )
    # 2. Remove trailing dosage in parens like "(10mg/10mg)", "(500mg)"
    s = re.sub(
        r'\s*\(\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml)'
        r'(?:\s*/\s*\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml))*\)\s*$',
        '', s, flags=re.IGNORECASE,
    )
    # 3. Remove dosage that appears before a parenthesized qualifier: "10mg (GLP-1SG)"
    s = re.sub(
        r'\s+\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml)'
        r'(?:\s*/\s*\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml))*\s*(?=\()',
        ' ', s, flags=re.IGNORECASE,
    )
    # 4. Remove trailing separators
    s = re.sub(r'\s*[–—\u00e2\u0080\u0093�]+\s*$', '', s)
    # 5. Remove trailing dosage like "50mg", "5mg/5mg", "500mg-mL", "10mg / 3mg"
    s = re.sub(
        r'\s+\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml)(?:[/-](?:ml|vial))?'
        r'(?:\s*/\s*\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml))?\s*$',
        '', s, flags=re.IGNORECASE,
    )
    # 6. Clean up trailing separators again (e.g. "Blend – 10mg" -> "Blend –" -> "Blend")
    s = re.sub(r'\s*[–—\u00e2\u0080\u0093�]+\s*$', '', s)
    return s.strip()


def resolve_or_create_canonical_product(db: Session, raw_name: str) -> CanonicalProduct:
    base_name = strip_dosage_suffix(raw_name)
    base_normalized = normalize_product_name(base_name)
    full_normalized = normalize_product_name(raw_name)

    # Find canonical product by normalized key (base name preferred)
    existing = db.query(CanonicalProduct).filter(CanonicalProduct.normalized_key == base_normalized).first()
    if not existing and base_normalized != full_normalized:
        existing = db.query(CanonicalProduct).filter(CanonicalProduct.normalized_key == full_normalized).first()
        if existing:
            # Fix legacy key: update to dosage-stripped form
            existing.normalized_key = base_normalized
    if existing:
        return existing

    product = CanonicalProduct(name=base_name, alias=base_name, normalized_key=base_normalized)
    db.add(product)
    db.flush()
    return product
