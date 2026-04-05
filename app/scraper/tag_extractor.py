"""
Extracts product category/type tags from product names.
Tags are used for front-end filtering and analytics.
"""
import re

# Each entry: (compiled pattern, tag string)
_TAG_RULES: list[tuple[re.Pattern, str]] = [
    # GLP-1 / weight loss peptides
    (re.compile(r"\b(GLP[\s\-]?1|semaglutide|tirzepatide|liraglutide|dulaglutide|retatrutide|cagrilintide)\b", re.I), "glp1"),
    # Healing / tissue repair
    (re.compile(r"\b(BPC[\s\-]?157|TB[\s\-]?500|thymosin[\s\-]?beta)\b", re.I), "healing"),
    # Growth hormone secretagogues
    (re.compile(r"\b(CJC[\s\-]?1295|GHRP[\s\-]?[26]|ipamorelin|sermorelin|hexarelin|tesamorelin|MK[\s\-]?677)\b", re.I), "gh_secretagogue"),
    # Growth hormones / IGF
    (re.compile(r"\b(IGF[\s\-]?1|LR3|des[\s\-]?IGF|mechano[\s\-]?growth)\b", re.I), "igf"),
    # Melanocortin / tanning
    (re.compile(r"\b(melanotan|MT[\s\-]?[12]|PT[\s\-]?141|bremelanotide)\b", re.I), "melanocortin"),
    # Cognitive / nootropic
    (re.compile(r"\b(selank|semax|dihexa|noopept|DSIP|epithalon)\b", re.I), "nootropic"),
    # Cosmetic / skin
    (re.compile(r"\b(GHK[\s\-]?Cu|palmitoyl|collagen|matrixyl|argireline)\b", re.I), "cosmetic"),
    # Hormone / reproductive
    (re.compile(r"\b(kisspeptin|gonadorelin|oxytocin|vasopressin|follicle)\b", re.I), "hormonal"),
    # Anti-aging / longevity
    (re.compile(r"\b(MOTS[\s\-]?c|SS[\s\-]?31|humanin|NAD|epitalon)\b", re.I), "longevity"),
    # Sleep / recovery
    (re.compile(r"\b(DSIP|delta[\s\-]?sleep|melatonin)\b", re.I), "sleep"),
    # Format tags
    (re.compile(r"\bkit\b", re.I), "kit"),
    (re.compile(r"\bnasal\b", re.I), "nasal"),
    (re.compile(r"\boral\b", re.I), "oral"),
    (re.compile(r"\b(inject|injectable)\b", re.I), "injectable"),
    (re.compile(r"\bblend\b", re.I), "blend"),
]


def extract_tags(product_name: str) -> list[str]:
    """Return a deduplicated list of tags matched in the product name."""
    found: list[str] = []
    for pattern, tag in _TAG_RULES:
        if pattern.search(product_name) and tag not in found:
            found.append(tag)
    return found
