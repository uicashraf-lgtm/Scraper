"""
Unit checks for the COA / spec-sheet text parser and link discovery.
Pure-python — does not download or OCR anything; safe to run anywhere.

Usage:  python test_coa_extractor.py
"""
from bs4 import BeautifulSoup

from app.scraper.coa_extractor import (
    discover_coa_urls,
    parse_peptide_fields,
)


def _check(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        raise AssertionError(label + " — " + detail)


# ---- parse_peptide_fields ------------------------------------------------

print("parse_peptide_fields:")

r = parse_peptide_fields("Purity (HPLC): 99.5%\nMolecular Weight: 1216.4 Da\nNet Content: 5 mg")
_check("purity 99.5",   r.purity_pct == 99.5,             f"got {r.purity_pct}")
_check("MW 1216.4",     r.molecular_weight == 1216.4,     f"got {r.molecular_weight}")
_check("content 5 mg",  r.content_mg == 5.0 and r.content_unit == "mg",
       f"got {r.content_mg} {r.content_unit}")

r = parse_peptide_fields("HPLC 98 % purity, MW 5135.92 g/mol, contents 10mg per vial")
_check("purity 98 (form 2)",   r.purity_pct == 98.0,             f"got {r.purity_pct}")
_check("MW 5135.92 g/mol",     r.molecular_weight == 5135.92,    f"got {r.molecular_weight}")
_check("content 10 mg (form 2)", r.content_mg == 10.0 and r.content_unit == "mg",
       f"got {r.content_mg} {r.content_unit}")

# Sequence: BPC-157 N-terminal fragment as 3-letter codes (>=4 residues)
r = parse_peptide_fields("Sequence: Gly-Glu-Pro-Pro-Pro-Gly-Lys-Pro-Ala-Asp")
_check("sequence captured", r.sequence and r.sequence.startswith("Gly-Glu-Pro"),
       f"got {r.sequence!r}")

# Out-of-range purity is rejected (e.g. an OCR misread that produced "995%")
r = parse_peptide_fields("Purity 995%")
_check("rejects nonsense purity", r.purity_pct is None, f"got {r.purity_pct}")

# Dosage units other than mg get normalized
r = parse_peptide_fields("Net Content: 1000 ug")
_check("ug normalized to mcg", r.content_mg == 1000.0 and r.content_unit == "mcg",
       f"got {r.content_mg} {r.content_unit}")

# Empty input is harmless
r = parse_peptide_fields("")
_check("empty input → no fields", not r.is_useful())


# ---- discover_coa_urls ---------------------------------------------------

print("\ndiscover_coa_urls:")

html = """
<html><body>
  <a href="/files/bpc157-coa.pdf">Download COA</a>
  <a href="/spec-sheet">Specification Sheet</a>
  <a href="/about">About us</a>
  <img src="/img/main.jpg" alt="BPC-157 5mg" />
  <img src="/img/coa-label.png" alt="Certificate of Analysis" />
  <img src="/img/coa.webp" alt="" title="HPLC report" />
</body></html>
"""
soup = BeautifulSoup(html, "html.parser")
cands = discover_coa_urls(soup, "https://example.com/product/bpc-157")
urls = [c.url for c in cands]
print(f"  discovered {len(cands)}: {urls}")

_check("found PDF link",
       any(u.endswith("/files/bpc157-coa.pdf") for u in urls))
_check("found COA-labeled image (alt)",
       any(u.endswith("/img/coa-label.png") for u in urls))
_check("found HPLC-labeled image (title)",
       any(u.endswith("/img/coa.webp") for u in urls))
_check("non-COA assets not picked up",
       not any("main.jpg" in u or "/about" in u for u in urls))

print("\nAll COA extractor checks passed.")
