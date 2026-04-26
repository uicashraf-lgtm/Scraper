"""
Extract peptide data (purity, content, mass, sequence) from COA / spec-sheet
documents linked from a product page.

Pipeline (per product page):
  1. discover_coa_urls(soup, base_url) — find PDF links + product images that
     look like a COA / certificate / spec sheet.
  2. extract_from_url(url) — download, route by content-type:
       - PDF: try text layer (pdfplumber), fall back to OCR if scanned.
       - Image: OCR via pytesseract.
     Then parse_peptide_fields() on the resulting text.
  3. If regex extraction yields nothing useful and ANTHROPIC_API_KEY is set,
     escalate to a vision LLM (Claude) for one more pass.

All heavy deps (pdfplumber, pytesseract, anthropic) are lazy-imported so the
module degrades gracefully if any are missing — the worst case is that an
extractor returns None and the caller skips persistence.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.scraper.rate_limiter import http_get_with_retry

logger = logging.getLogger(__name__)


# ---- public dataclasses --------------------------------------------------

@dataclass
class CoaCandidate:
    url: str
    source_type: str  # "pdf" | "image"


@dataclass
class CoaData:
    purity_pct: float | None = None
    content_mg: float | None = None
    content_unit: str | None = None
    molecular_weight: float | None = None
    sequence: str | None = None
    raw_text: str | None = None
    extractor: str | None = None  # which extractor produced this
    confidence: float | None = None

    def is_useful(self) -> bool:
        return any(
            v is not None
            for v in (self.purity_pct, self.content_mg, self.molecular_weight, self.sequence)
        )


# ---- discovery -----------------------------------------------------------

# Words that strongly suggest a link / image is a COA or spec sheet.
_COA_HINT_PATTERN = re.compile(
    r"\b(coa|c\.o\.a|certificate[\s\-]?of[\s\-]?analysis|"
    r"hplc|spec(?:ification)?[\s\-]?sheet|analysis|purity[\s\-]?report|"
    r"mass[\s\-]?spec|ms[\s\-]?report|lab[\s\-]?report)\b",
    re.IGNORECASE,
)

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".tiff", ".bmp")


def _looks_like_coa(*texts: str | None) -> bool:
    blob = " ".join(t for t in texts if t)
    return bool(_COA_HINT_PATTERN.search(blob))


def discover_coa_urls(
    soup: BeautifulSoup,
    base_url: str,
    *,
    max_results: int = 6,
) -> list[CoaCandidate]:
    """Pull candidate COA / spec-sheet URLs from a product page.

    We grab:
      * Any <a href="...pdf"> (PDFs are almost always COAs on peptide sites).
      * <a> tags whose link text mentions COA / certificate / analysis,
        regardless of extension.
      * <img> tags whose alt/title/src mentions COA — these are scanned
        labels printed on the product image.
    """
    out: list[CoaCandidate] = []
    seen: set[str] = set()

    def _add(url: str, source_type: str) -> None:
        if url in seen:
            return
        seen.add(url)
        out.append(CoaCandidate(url=url, source_type=source_type))

    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        text = a.get_text(" ", strip=True)
        path = urlparse(href).path.lower()
        if path.endswith(".pdf"):
            _add(href, "pdf")
        elif _looks_like_coa(text, href):
            # Extension may be missing (e.g. /coa?id=123) — treat as pdf if
            # the URL contains "pdf", otherwise skip (we don't know what it is).
            if "pdf" in path or "pdf" in href.lower():
                _add(href, "pdf")
            elif any(path.endswith(ext) for ext in _IMAGE_EXT):
                _add(href, "image")

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        alt = img.get("alt", "")
        title = img.get("title", "")
        if _looks_like_coa(src, alt, title):
            _add(urljoin(base_url, src.strip()), "image")
        if len(out) >= max_results:
            break

    return out[:max_results]


# ---- download ------------------------------------------------------------

def _download(url: str) -> tuple[bytes | None, str | None]:
    """Return (bytes, content_type) or (None, None) on failure."""
    try:
        resp = http_get_with_retry(
            url,
            headers={"User-Agent": settings.scraper_user_agent},
            timeout=30.0,
            max_retries=2,
        )
        if resp.status_code != 200 or not resp.content:
            return None, None
        return resp.content, (resp.headers.get("content-type") or "").lower()
    except (httpx.HTTPError, Exception) as exc:  # broad: never break the crawl
        logger.debug("coa download failed for %s: %s", url, exc)
        return None, None


# ---- text extractors -----------------------------------------------------

def _extract_pdf_text(data: bytes) -> tuple[str, str] | None:
    """Try pdfplumber first (text layer). Returns (text, extractor_name)."""
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return None
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages).strip()
        if text:
            return text, "pdf_text"
    except Exception as exc:
        logger.debug("pdfplumber failed: %s", exc)
    return None


def _ocr_image_bytes(data: bytes) -> tuple[str, str] | None:
    """OCR an image with Tesseract. Returns (text, 'tesseract')."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            text = pytesseract.image_to_string(img) or ""
        text = text.strip()
        if text:
            return text, "tesseract"
    except Exception as exc:
        logger.debug("tesseract OCR failed: %s", exc)
    return None


def _ocr_pdf_pages(data: bytes) -> tuple[str, str] | None:
    """For scanned PDFs (no text layer): rasterise + OCR each page."""
    try:
        import pdfplumber  # type: ignore
        import pytesseract  # type: ignore
    except ImportError:
        return None
    try:
        chunks: list[str] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:5]:  # cap pages — COAs are usually 1-2
                pil = page.to_image(resolution=200).original
                chunks.append(pytesseract.image_to_string(pil) or "")
        text = "\n".join(chunks).strip()
        if text:
            return text, "tesseract"
    except Exception as exc:
        logger.debug("scanned-pdf OCR failed: %s", exc)
    return None


def _extract_via_vision_llm(
    data: bytes, source_type: str
) -> tuple[CoaData, str] | None:
    """Last-resort extractor using Claude's vision model. Off by default —
    only runs when ANTHROPIC_API_KEY is set in the environment. Returns
    (CoaData, raw_text) when it produced something useful."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import base64
        import json as _json
        from anthropic import Anthropic  # type: ignore
    except ImportError:
        return None

    media_type = "application/pdf" if source_type == "pdf" else "image/jpeg"
    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=400,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document" if source_type == "pdf" else "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.b64encode(data).decode("ascii"),
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Extract peptide info from this certificate. "
                                "Reply with ONLY a JSON object with keys: "
                                "purity_pct (number), content_mg (number), "
                                "content_unit (string), molecular_weight (number, Da), "
                                "sequence (string). Use null for missing values."
                            ),
                        },
                    ],
                }
            ],
        )
        text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
        # Strip markdown fences if present
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        parsed = _json.loads(text)
        return (
            CoaData(
                purity_pct=_safe_float(parsed.get("purity_pct")),
                content_mg=_safe_float(parsed.get("content_mg")),
                content_unit=parsed.get("content_unit") or None,
                molecular_weight=_safe_float(parsed.get("molecular_weight")),
                sequence=(parsed.get("sequence") or None),
                extractor="vision_llm",
                confidence=0.85,
            ),
            text,
        )
    except Exception as exc:
        logger.debug("vision LLM extraction failed: %s", exc)
        return None


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---- text parsing --------------------------------------------------------

# Purity ≥ 98%, Purity: 99.5 %, HPLC 98%, "≥ 98% purity"
_PURITY_RE = re.compile(
    r"(?:purity|hplc)[^0-9]{0,20}(\d{1,3}(?:\.\d+)?)\s*%|"
    r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:purity|pure)",
    re.IGNORECASE,
)

# MW / molecular weight: "MW: 1216.4", "Molecular Weight 1216.4 Da", "1216.4 g/mol"
_MW_RE = re.compile(
    r"(?:molecular\s*weight|mol\.?\s*wt|mw|m\.w\.)\s*[:=]?\s*(\d{2,5}(?:\.\d+)?)|"
    r"(\d{3,5}(?:\.\d+)?)\s*(?:da|g/mol)\b",
    re.IGNORECASE,
)

# Net content: "Net Content 5 mg", "Quantity: 10 mg", "Contents: 5mg/vial"
_CONTENT_RE = re.compile(
    r"(?:net\s*(?:content|weight|quantity)|contents?|quantity|amount|fill)\s*[:=]?\s*"
    r"(\d+(?:\.\d+)?)\s*(mg|mcg|µg|ug|iu|ml)\b",
    re.IGNORECASE,
)

# Standalone amino-acid sequence: 3-letter codes joined by hyphens, ≥4 residues.
_SEQUENCE_RE = re.compile(
    r"\b((?:Ala|Arg|Asn|Asp|Cys|Gln|Glu|Gly|His|Ile|Leu|Lys|Met|Phe|Pro|"
    r"Ser|Thr|Trp|Tyr|Val)(?:-(?:Ala|Arg|Asn|Asp|Cys|Gln|Glu|Gly|His|Ile|"
    r"Leu|Lys|Met|Phe|Pro|Ser|Thr|Trp|Tyr|Val)){3,})\b"
)


def _normalize_unit(raw: str) -> str:
    raw = raw.lower()
    return {"ug": "mcg", "µg": "mcg", "iu": "IU", "ml": "mL"}.get(raw, raw)


def parse_peptide_fields(text: str) -> CoaData:
    """Pull purity / content / mass / sequence out of an OCR'd / extracted blob."""
    out = CoaData(raw_text=text[:4000] if text else None, extractor="regex", confidence=0.6)
    if not text:
        return out

    m = _PURITY_RE.search(text)
    if m:
        val = m.group(1) or m.group(2)
        try:
            v = float(val)
            # Sanity: purity is a percentage, not e.g. "99.5%" misread as 995
            if 50.0 <= v <= 100.0:
                out.purity_pct = v
        except ValueError:
            pass

    m = _MW_RE.search(text)
    if m:
        val = m.group(1) or m.group(2)
        try:
            v = float(val)
            # Peptides typically 300-30000 Da
            if 200.0 <= v <= 50000.0:
                out.molecular_weight = v
        except ValueError:
            pass

    m = _CONTENT_RE.search(text)
    if m:
        try:
            out.content_mg = float(m.group(1))
            out.content_unit = _normalize_unit(m.group(2))
        except ValueError:
            pass

    m = _SEQUENCE_RE.search(text)
    if m:
        seq = m.group(1)
        if len(seq) <= 500:
            out.sequence = seq

    return out


# ---- top-level entry points ---------------------------------------------

def extract_from_url(url: str, source_type: str) -> tuple[CoaData, bytes] | None:
    """Download `url`, run the appropriate extractor, parse fields.
    Returns (CoaData, raw_bytes) so the caller can hash the bytes for dedup;
    returns None if the document couldn't be downloaded at all."""
    data, content_type = _download(url)
    if data is None:
        return None

    # Trust content-type over the URL extension when they disagree
    is_pdf = source_type == "pdf" or "pdf" in content_type
    text_result: tuple[str, str] | None = None
    if is_pdf:
        text_result = _extract_pdf_text(data) or _ocr_pdf_pages(data)
        actual_source_type = "pdf"
    else:
        text_result = _ocr_image_bytes(data)
        actual_source_type = "image"

    if text_result:
        text, extractor_name = text_result
        coa = parse_peptide_fields(text)
        coa.extractor = extractor_name if coa.extractor == "regex" else f"{extractor_name}+regex"
        if coa.is_useful():
            return coa, data

    # Regex on extracted text didn't find anything — try vision LLM as fallback.
    llm = _extract_via_vision_llm(data, actual_source_type)
    if llm:
        coa, raw_text = llm
        coa.raw_text = raw_text[:4000]
        return coa, data

    # No data, but still return the (empty) CoaData + bytes so the caller can
    # record that we tried (and won't retry on the next crawl).
    empty = CoaData(extractor="none", confidence=0.0)
    return empty, data


def extract_for_listing(soup: BeautifulSoup, base_url: str) -> list[tuple[CoaCandidate, CoaData, str]]:
    """Convenience: discover candidates, extract each, return rows ready for
    persistence. Each row: (candidate, coa_data, sha256_hex)."""
    rows: list[tuple[CoaCandidate, CoaData, str]] = []
    for cand in discover_coa_urls(soup, base_url):
        result = extract_from_url(cand.url, cand.source_type)
        if result is None:
            continue
        coa, raw = result
        if not coa.is_useful():
            continue
        sha = hashlib.sha256(raw).hexdigest()
        rows.append((cand, coa, sha))
    return rows
