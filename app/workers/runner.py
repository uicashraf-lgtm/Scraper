import json
import logging
import threading
import time
from datetime import datetime, timezone

from app.db.session import SessionLocal
from app.models.entities import CoaDocument, ListingVariant, PriceHistory, ProductTag, Vendor, VendorListing, VendorTargetURL
from app.scraper.discovery import discover_product_urls
from app.scraper.fetch import ScrapeHints, ScrapeResult, scrape_url
from app.services.pricing import create_crawl_log, is_blocked_response, maybe_raise_block_alert
from app.services.product_mapper import resolve_or_create_canonical_product
from app.services.queue import QUEUE_KEY, publish_event, redis_client

logger = logging.getLogger(__name__)


def _upsert_listing(db, vendor_id: int, url: str) -> VendorListing:
    listing = db.query(VendorListing).filter(VendorListing.vendor_id == vendor_id, VendorListing.url == url).first()
    if listing:
        return listing
    listing = VendorListing(vendor_id=vendor_id, url=url)
    db.add(listing)
    db.flush()
    return listing


def _get_or_refresh_session(db, vendor: Vendor) -> list[dict] | None:
    """
    Return a valid session cookie list for vendors that require login.
    If no valid session exists, perform Playwright login and store the result.
    Returns None if vendor has no login credentials or login fails.
    """
    if not vendor.login_email or not vendor.login_password_enc:
        return None

    from app.scraper.session_manager import load_session, save_session
    cookies = load_session(db, vendor.id)
    if cookies:
        return cookies

    # No valid session — log in now
    from app.scraper.login import playwright_login
    logger.info("Performing login for vendor '%s' (%s)", vendor.name, vendor.base_url)
    cookies = playwright_login(
        base_url=vendor.base_url,
        email=vendor.login_email,
        password_enc=vendor.login_password_enc,
        login_url_path=vendor.login_url_path,
        bypass_strategy=vendor.bypass_strategy,
        proxy_url=vendor.proxy_url,
    )
    if cookies:
        save_session(db, vendor.id, cookies)
    else:
        logger.warning("Login failed for vendor '%s'", vendor.name)
    return cookies


def _persist_variants(db, listing_id: int, variants: list[dict]):
    """Replace listing variants with fresh data. Each variant: {"dosage": float, "unit": str, "price": float|None, "in_stock": bool|None}.
    When the listing has dose_locked=True, the admin's curated variant set is
    authoritative — only refresh price/in_stock on rows that already exist;
    do not add scraper-discovered doses (the admin may have deliberately removed
    them) and do not delete admin-created rows."""
    listing = db.query(VendorListing).filter(VendorListing.id == listing_id).first()
    if listing and listing.dose_locked:
        for v in variants:
            dosage = v["dosage"]
            unit = v.get("unit", "mg")
            existing = (
                db.query(ListingVariant)
                .filter(ListingVariant.listing_id == listing_id,
                        ListingVariant.dosage == dosage,
                        ListingVariant.unit == unit)
                .first()
            )
            if existing:
                if v.get("price") is not None:
                    existing.price = v["price"]
                if v.get("in_stock") is not None:
                    existing.in_stock = v["in_stock"]
    else:
        db.query(ListingVariant).filter(ListingVariant.listing_id == listing_id).delete()
        seen: set[tuple] = set()
        for v in variants:
            key = (v["dosage"], v.get("unit", "mg"))
            if key in seen:
                continue
            seen.add(key)
            db.add(ListingVariant(
                listing_id=listing_id,
                dosage=v["dosage"],
                unit=v.get("unit", "mg"),
                price=v.get("price"),
                in_stock=v.get("in_stock"),
            ))
    db.flush()


def _enrich_listing_with_coa(db, vendor: Vendor, listing_id: int, product_url: str) -> None:
    """For API-path crawls (no HTML scrape), fetch the product page once and run
    COA discovery + extraction on it. No-op when extraction is disabled.

    The WC REST/Store API responses don't expose certificate-of-analysis docs,
    so without this extra fetch the API path would silently skip COA extraction
    for every vendor that uses it. Cost: one HTTP request per listing when the
    flag is on. Failures are non-fatal — the price update is already committed."""
    from app.core.config import settings
    if not settings.coa_extraction_enabled or not product_url:
        return
    try:
        from app.scraper.coa_extractor import extract_for_listing
        from app.scraper.fetch import ScrapeHints, fetch_page
        from app.scraper.session_manager import load_session
        from bs4 import BeautifulSoup

        cookies = load_session(db, vendor.id) if vendor.login_email else None
        hints = ScrapeHints(
            cookies=cookies,
            proxy_url=vendor.proxy_url,
            bypass_strategy=vendor.bypass_strategy,
            popup_close_selector=vendor.popup_close_selector,
        )
        _status, html, _err = fetch_page(product_url, hints=hints)
        if not html:
            logger.debug("[coa_api_path] fetch returned no HTML for %s", product_url)
            return

        soup = BeautifulSoup(html, "html.parser")
        rows = extract_for_listing(
            soup, product_url,
            cookies=cookies, proxy_url=vendor.proxy_url, bypass_strategy=vendor.bypass_strategy,
        )
        docs = [
            {
                "source_url": cand.url,
                "source_type": cand.source_type,
                "source_hash": sha,
                "extractor": coa.extractor or "unknown",
                "purity_pct": coa.purity_pct,
                "content_mg": coa.content_mg,
                "content_unit": coa.content_unit,
                "molecular_weight": coa.molecular_weight,
                "sequence": coa.sequence,
                "raw_text": coa.raw_text,
                "confidence": coa.confidence,
            }
            for cand, coa, sha in rows[: settings.coa_max_documents_per_listing]
        ]
        added = _persist_coa_documents(db, listing_id, docs)
        if added:
            logger.info("[coa_api_path] persisted listing_id=%d added=%d url=%s",
                        listing_id, added, product_url)
    except Exception as exc:
        logger.debug("[coa_api_path] skipped for %s: %s", product_url, exc)


def _persist_coa_documents(db, listing_id: int, docs: list[dict]) -> int:
    """Insert any new COA rows; skip docs whose source_hash already exists for
    this listing (so we don't re-store the same PDF every crawl). Returns the
    number of new rows added."""
    if not docs:
        return 0
    inserted = 0
    for d in docs:
        sha = d.get("source_hash")
        if not sha:
            continue
        exists = (
            db.query(CoaDocument)
            .filter(CoaDocument.listing_id == listing_id, CoaDocument.source_hash == sha)
            .first()
        )
        if exists:
            continue
        db.add(CoaDocument(
            listing_id=listing_id,
            source_url=d["source_url"][:2048],
            source_type=d["source_type"],
            source_hash=sha,
            extractor=d.get("extractor") or "unknown",
            purity_pct=d.get("purity_pct"),
            content_mg=d.get("content_mg"),
            content_unit=d.get("content_unit"),
            molecular_weight=d.get("molecular_weight"),
            sequence=d.get("sequence"),
            raw_text=d.get("raw_text"),
            confidence=d.get("confidence"),
        ))
        inserted += 1
    return inserted


def _persist_tags(db, canonical_product_id: int, tags: list[str]):
    """Insert new product tags; skip duplicates."""
    for tag_val in tags:
        exists = (
            db.query(ProductTag)
            .filter(
                ProductTag.canonical_product_id == canonical_product_id,
                ProductTag.tag == tag_val,
            )
            .first()
        )
        if not exists:
            db.add(ProductTag(
                canonical_product_id=canonical_product_id,
                tag=tag_val,
                source="crawler",
            ))


def _crawl_vendor_via_wc_api(db, vendor: Vendor, base_url_override: str | None = None, use_store_api: bool = False, cookies: list[dict] | None = None):
    """Fetch all products via WooCommerce REST API or Store API — no web crawling needed."""
    import json as _json

    base_url = base_url_override or vendor.base_url

    if use_store_api:
        from app.scraper.wc_api import build_store_api_headers, fetch_wc_store_products, process_wc_store_product
        logger.info("[crawl_vendor] WC Store API for '%s' (%s) auth=%s", vendor.name, base_url, bool(cookies))
        req_headers = build_store_api_headers(base_url, cookies) if cookies else None
        products = fetch_wc_store_products(base_url, cookies=cookies)
        def _process(prod):
            return process_wc_store_product(prod, base_url=base_url, req_headers=req_headers)
    else:
        from app.scraper.wc_api import fetch_wc_products, process_wc_product
        ck = vendor.wc_consumer_key
        cs = vendor.wc_consumer_secret
        logger.info("[crawl_vendor] WC REST API for '%s' (%s) auth=%s", vendor.name, base_url, bool(ck))
        products = fetch_wc_products(base_url, ck, cs)
        def _process(prod):
            return process_wc_product(prod, base_url, ck, cs)

    logger.info("[crawl_vendor] WC API returned %d products for '%s'", len(products), vendor.name)

    if not products:
        # API returned nothing — likely auth failure (401) or endpoint gone
        logger.warning("[crawl_vendor] WC API returned 0 products for '%s' — treating as failure", vendor.name)
        create_crawl_log(db, listing_id=None, vendor_id=vendor.id,
                         status="error", message="WC API returned 0 products (possible auth failure)")
        db.commit()
        return False  # signal caller to try next priority

    updated = 0
    for prod in products:
        try:
            data = _process(prod)
        except Exception as exc:
            logger.error("[crawl_vendor] Product processing crashed for '%s': %s",
                         prod.get("name", "?"), exc, exc_info=True)
            continue
        if not data["url"]:
            logger.warning("[crawl_vendor] Product '%s' has no URL — skipping", data.get("name"))
            continue
        if data["price"] is None:
            logger.warning("[crawl_vendor] Product '%s' has no price — skipping", data.get("name"))
            continue

        listing = _upsert_listing(db, vendor.id, data["url"])
        old_price = listing.last_price
        listing.last_fetched_at = datetime.utcnow()
        listing.last_status = "ok"
        listing.last_price = data["price"]
        listing.price_min = data["price"]
        listing.price_max = data.get("price_max") or data["price"]
        listing.currency = data["currency"]
        listing.in_stock = data["in_stock"]
        if not listing.dose_locked:
            listing.amount_mg = data["amount_mg"]
            listing.amount_unit = data["amount_unit"]
            if data["amount_mg"] and data["price"]:
                listing.price_per_mg = data["price"] / data["amount_mg"]
        elif listing.amount_mg and data["price"]:
            listing.price_per_mg = data["price"] / listing.amount_mg
        if not listing.dose_locked:
            listing.variant_amounts = _json.dumps(data["variant_amounts"]) if data["variant_amounts"] else None
        listing.vendor_product_name = data["name"]
        if data.get("sku"):
            listing.sku = data["sku"]
        logger.info("[crawl_vendor]   listing url=%s name='%s' price=%s-%s in_stock=%s sku=%s",
                    data["url"], data["name"], data["price"], data.get("price_max"), data["in_stock"], data.get("sku"))
        # Persist structured variants
        db.flush()  # ensure listing.id is assigned
        if data.get("variants"):
            _persist_variants(db, listing.id, data["variants"])

        if data["name"]:
            canonical = resolve_or_create_canonical_product(db, data["name"])
            listing.canonical_product_id = canonical.id
            if data.get("category") and not canonical.category:
                canonical.category = data["category"]
            if data.get("tags"):
                _persist_tags(db, canonical.id, data["tags"])

        # Record price history only when price changes
        if old_price is None or abs((old_price or 0) - data["price"]) > 0.001:
            db.add(PriceHistory(
                listing_id=listing.id,
                source="api_sync_done",
                price=data["price"],
                currency=data["currency"] or "USD",
            ))

        db.commit()

        # API path doesn't see the rendered product page, so COA discovery
        # never fires from the JSON. If extraction is enabled, do one HTML
        # fetch per listing here and run the extractor on the post-render DOM.
        _enrich_listing_with_coa(db, vendor, listing.id, data["url"])

        updated += 1

    logger.info("[crawl_vendor] WC API DONE for '%s' — %d/%d listings updated", vendor.name, updated, len(products))
    create_crawl_log(db, listing_id=None, vendor_id=vendor.id,
                     status="api_sync_done", message=f"updated={updated} total={len(products)}")
    db.commit()
    return True


def crawl_vendor(vendor_id: int):
    db = SessionLocal()
    try:
        vendor = db.query(Vendor).filter(Vendor.id == vendor_id, Vendor.enabled.is_(True)).first()
        if not vendor:
            logger.warning("[crawl_vendor] vendor_id=%d not found or disabled — skipping", vendor_id)
            return

        logger.info("[crawl_vendor] START vendor='%s' (id=%d)", vendor.name, vendor_id)

        # Priority 1: WooCommerce REST API (admin-provided key+secret)
        if vendor.wc_consumer_key and vendor.wc_consumer_secret:
            logger.info("[crawl_vendor] Source: WooCommerce API (key+secret)")
            success = _crawl_vendor_via_wc_api(db, vendor)
            if success:
                return
            logger.warning("[crawl_vendor] WC REST API failed for '%s' — falling through to next source", vendor.name)

        # Priority 2: WooCommerce Store API (with session cookies when the endpoint is auth-protected)
        # Use vendor.base_url — wc_api_url may contain a full endpoint path which would
        # cause fetch_wc_store_products to construct a malformed URL.
        store_base = vendor.base_url
        if store_base:
            store_cookies = None
            if vendor.login_email and vendor.login_password_enc:
                logger.info("[crawl_vendor] Login credentials found for '%s' — fetching session for Store API", vendor.name)
                store_cookies = _get_or_refresh_session(db, vendor)
                if store_cookies:
                    logger.info("[crawl_vendor] Got session for '%s' (%d cookies) — Store API will use auth", vendor.name, len(store_cookies))
                else:
                    logger.warning("[crawl_vendor] Could not get session for '%s' — trying Store API unauthenticated", vendor.name)
            logger.info("[crawl_vendor] Source: WooCommerce Store API (%s)", store_base)
            success = _crawl_vendor_via_wc_api(db, vendor, base_url_override=store_base, use_store_api=True, cookies=store_cookies)
            if success:
                return
            logger.warning("[crawl_vendor] WC Store API failed for '%s' — falling through to web crawl", vendor.name)

        # Priority 3: Web crawling (discovery + per-listing scrape)
        logger.info("[crawl_vendor] Source: web crawl")

        targets = (
            db.query(VendorTargetURL)
            .filter(VendorTargetURL.vendor_id == vendor_id, VendorTargetURL.enabled.is_(True))
            .all()
        )

        if not targets:
            logger.warning("[crawl_vendor] No target URLs configured for vendor='%s'", vendor.name)

        # Login if credentials configured
        cookies = None
        if vendor.login_email:
            logger.info("[crawl_vendor] Login credentials found for '%s' — attempting login", vendor.name)
            cookies = _get_or_refresh_session(db, vendor)
            if cookies:
                logger.info("[crawl_vendor] Login SUCCESS for '%s' (%d cookies)", vendor.name, len(cookies))
            else:
                logger.warning("[crawl_vendor] Login FAILED for '%s' — discovery will proceed unauthenticated", vendor.name)

        # Build hints for discovery (passes cookies, proxy, etc.)
        discovery_hints = ScrapeHints(
            cookies=cookies,
            proxy_url=vendor.proxy_url,
            bypass_strategy=vendor.bypass_strategy,
            popup_close_selector=vendor.popup_close_selector,
        ) if (cookies or vendor.proxy_url or vendor.bypass_strategy) else None

        r = redis_client()
        enqueued = 0
        for t in targets:
            logger.info("[crawl_vendor] Discovering products from target: %s (auth=%s)", t.url, bool(cookies))
            discovered = discover_product_urls(t.url, vendor, hints=discovery_hints)
            urls = discovered if discovered else [t.url]
            logger.info("[crawl_vendor] Discovered %d product URLs from target %s", len(urls), t.url)
            if not discovered:
                logger.warning("[crawl_vendor] No product URLs discovered from %s — will crawl seed URL directly", t.url)

            for url in urls:
                listing = _upsert_listing(db, vendor_id, url)
                r.rpush(QUEUE_KEY, json.dumps({"type": "crawl_listing", "listing_id": listing.id}))
                enqueued += 1

        logger.info("[crawl_vendor] DONE vendor='%s' — %d listings enqueued", vendor.name, enqueued)
        create_crawl_log(
            db,
            listing_id=None,
            vendor_id=vendor_id,
            status="vendor_scan_enqueued",
            message=f"queued={enqueued}",
        )
        db.commit()
    finally:
        db.close()


def crawl_listing(listing_id: int):
    db = SessionLocal()
    try:
        listing = db.query(VendorListing).filter(VendorListing.id == listing_id).first()
        if not listing:
            logger.warning("[crawl_listing] listing_id=%d not found", listing_id)
            return

        vendor = db.query(Vendor).filter(Vendor.id == listing.vendor_id).first()
        vendor_name = vendor.name if vendor else "unknown"
        logger.info("[crawl_listing] START listing_id=%d vendor='%s' url=%s", listing_id, vendor_name, listing.url)

        # Build base scrape hints WITHOUT login cookies
        hints = ScrapeHints(
            price_selector=vendor.price_selector if vendor else None,
            price_attr=vendor.price_attr if vendor else None,
            name_selector=vendor.name_selector if vendor else None,
            platform=vendor.platform if vendor else None,
            dosage_selector=vendor.dosage_selector if vendor else None,
            dosage_attribute=vendor.dosage_attribute if vendor else None,
            popup_close_selector=vendor.popup_close_selector if vendor else None,
            cookies=None,
            proxy_url=vendor.proxy_url if vendor else None,
            bypass_strategy=vendor.bypass_strategy if vendor else None,
        )

        # WooCommerce Store API shortcut: when the vendor exposes the Store API,
        # use it instead of HTML scraping. The API gives per-variation price + stock,
        # which the HTML adapter often can't extract (Gutenberg/AJAX-loaded variants).
        result: ScrapeResult | None = None
        cookies = None
        if vendor and vendor.wc_api_url and vendor.base_url:
            from app.scraper.wc_api import build_store_api_headers, fetch_wc_store_product_by_url, process_wc_store_product
            from app.scraper.adapters.base import VariantData
            try:
                _listing_cookies = _get_or_refresh_session(db, vendor) if vendor.login_email else None
                _req_headers = build_store_api_headers(vendor.base_url, _listing_cookies) if _listing_cookies else None
                prod = fetch_wc_store_product_by_url(listing.url, vendor.base_url)
                if prod:
                    data = process_wc_store_product(prod, base_url=vendor.base_url, req_headers=_req_headers)
                    if data and data.get("price") is not None:
                        result = ScrapeResult(
                            ok=True,
                            status_code=200,
                            product_name=data.get("name"),
                            price=data["price"],
                            currency=data.get("currency") or "USD",
                            message=None,
                            body_excerpt=None,
                            adapter="wc_store_api",
                            in_stock=data.get("in_stock"),
                            amount_mg=data.get("amount_mg"),
                            amount_unit=data.get("amount_unit"),
                            variant_amounts=data.get("variant_amounts") or [],
                            variants=[
                                VariantData(
                                    dosage=v["dosage"],
                                    unit=v.get("unit", "mg"),
                                    price=v.get("price"),
                                    in_stock=v.get("in_stock"),
                                )
                                for v in (data.get("variants") or [])
                            ],
                            price_max=data.get("price_max"),
                            sku=data.get("sku"),
                            tags=data.get("tags") or [],
                        )
                        # category isn't a ScrapeResult field; attach it dynamically
                        # so the existing canonical-product code path picks it up.
                        if data.get("category"):
                            setattr(result, "category", data["category"])
                        if data.get("price") and data.get("amount_mg"):
                            result.price_per_mg = data["price"] / data["amount_mg"]
                        logger.info("[crawl_listing] Store API hit listing_id=%d url=%s price=%s in_stock=%s variants=%d",
                                    listing_id, listing.url, data["price"], data.get("in_stock"), len(result.variants))
            except Exception as exc:
                logger.warning("[crawl_listing] Store API path failed for listing_id=%d: %s — falling back to HTML",
                               listing_id, exc)

        # Fall back to HTML scrape when the Store API didn't return usable data.
        if result is None:
            logger.info("[crawl_listing] Attempt 1 (no login) url=%s", listing.url)
            result = scrape_url(listing.url, hints=hints)
        blocked = is_blocked_response(result.status_code, result.body_excerpt)

        # If first attempt failed/blocked and vendor has login credentials, retry with login
        has_login_creds = vendor and vendor.login_email and vendor.login_password_enc
        first_attempt_failed = blocked or not result.ok or result.price is None
        if first_attempt_failed and has_login_creds:
            logger.info("[crawl_listing] Attempt 1 failed (blocked=%s ok=%s price=%s) — retrying with login for '%s'",
                        blocked, result.ok, result.price, vendor_name)
            cookies = _get_or_refresh_session(db, vendor)
            if cookies:
                logger.info("[crawl_listing] Attempt 2 (with %d cookies) url=%s", len(cookies), listing.url)
                hints.cookies = cookies
                result = scrape_url(listing.url, hints=hints)
                blocked = is_blocked_response(result.status_code, result.body_excerpt)
            else:
                logger.warning("[crawl_listing] Login failed for '%s' — using first attempt result", vendor_name)

        listing.last_fetched_at = datetime.utcnow()
        listing.last_status = "ok" if result.ok else "error"
        listing.last_error = result.message

        if blocked:
            listing.blocked_count += 1
            listing.last_status = "blocked"
            logger.warning("[crawl_listing] BLOCKED listing_id=%d url=%s", listing_id, listing.url)
            maybe_raise_block_alert(db, listing)

            # If the vendor has a session, invalidate it — the block may be due to an expired session
            if cookies and vendor:
                from app.scraper.session_manager import invalidate_session
                invalidate_session(db, vendor.id)

        old_price = listing.last_price

        if result.ok and result.price is not None:
            logger.info("[crawl_listing] OK listing_id=%d product='%s' price=%s %s in_stock=%s",
                        listing_id, result.product_name, result.price, result.currency, result.in_stock)
            listing.last_price = result.price
            listing.price_min = result.price
            listing.price_max = result.price_max or result.price
            listing.currency = result.currency or "USD"

            # Always update enriched fields when crawl succeeds
            listing.in_stock = result.in_stock
            if not listing.dose_locked:
                listing.amount_mg = result.amount_mg
                listing.amount_unit = result.amount_unit
                listing.price_per_mg = result.price_per_mg
            elif listing.amount_mg and listing.last_price:
                # Recalculate price_per_mg with the locked dose and new price
                listing.price_per_mg = listing.last_price / listing.amount_mg
            import json as _json
            if not listing.dose_locked:
                listing.variant_amounts = _json.dumps(result.variant_amounts) if result.variant_amounts else None

            # Persist structured variants
            db.flush()
            if result.variants:
                _persist_variants(db, listing.id, [
                    {"dosage": v.dosage, "unit": v.unit, "price": v.price, "in_stock": v.in_stock}
                    for v in result.variants
                ])

            # Persist any peptide-data documents (purity / mass / content) extracted
            # from product images or PDFs on the page.
            if getattr(result, "coa_documents", None):
                added = _persist_coa_documents(db, listing.id, result.coa_documents)
                if added:
                    logger.info("[crawl_listing] COA docs persisted listing_id=%d added=%d",
                                listing.id, added)
            elif result.adapter == "wc_store_api" and vendor:
                # Store API hit means we never fetched HTML, so coa_documents is empty.
                # Run the API-path enricher to fetch the product page once and extract.
                _enrich_listing_with_coa(db, vendor, listing.id, listing.url)

            if result.product_name:
                listing.vendor_product_name = result.product_name
                canonical = resolve_or_create_canonical_product(db, result.product_name)
                listing.canonical_product_id = canonical.id

                # Persist category and tags linked to the canonical product
                if hasattr(result, 'category') and result.category and not canonical.category:
                    canonical.category = result.category
                if result.tags:
                    _persist_tags(db, canonical.id, result.tags)

            # Dedup: if another listing already exists for the same vendor + canonical product + dosage,
            # merge this result into that primary listing and delete the current duplicate URL listing.
            if listing.canonical_product_id and listing.amount_mg is not None:
                primary = (
                    db.query(VendorListing)
                    .filter(
                        VendorListing.vendor_id == listing.vendor_id,
                        VendorListing.canonical_product_id == listing.canonical_product_id,
                        VendorListing.amount_mg == listing.amount_mg,
                        VendorListing.id != listing.id,
                    )
                    .order_by(VendorListing.id)
                    .first()
                )
                if primary:
                    logger.info(
                        "[crawl_listing] DEDUP listing_id=%d → merging into primary listing_id=%d "
                        "(vendor=%d canonical=%d amount_mg=%s dose_locked=%s)",
                        listing.id, primary.id, listing.vendor_id,
                        listing.canonical_product_id, listing.amount_mg,
                        primary.dose_locked,
                    )
                    primary_old_price = primary.last_price
                    primary.last_price = result.price
                    primary.currency = listing.currency
                    primary.in_stock = listing.in_stock
                    # Respect dose_locked on the primary — never overwrite its
                    # admin-saved dose or price_per_mg during a dedup merge.
                    if not primary.dose_locked:
                        primary.amount_unit = listing.amount_unit
                        primary.price_per_mg = listing.price_per_mg
                    elif primary.amount_mg and result.price:
                        primary.price_per_mg = result.price / primary.amount_mg
                    primary.variant_amounts = listing.variant_amounts
                    primary.vendor_product_name = listing.vendor_product_name
                    primary.last_fetched_at = listing.last_fetched_at
                    primary.last_status = listing.last_status
                    if primary_old_price is None or abs((primary_old_price or 0) - result.price) > 0.001:
                        db.add(PriceHistory(
                            listing_id=primary.id,
                            source="crawler",
                            price=result.price,
                            currency=primary.currency or "USD",
                        ))
                    db.delete(listing)
                    db.flush()
                    create_crawl_log(
                        db,
                        listing_id=primary.id,
                        vendor_id=primary.vendor_id,
                        status="ok",
                        http_status=result.status_code,
                        is_blocked=False,
                        message=f"deduped from listing_id={listing_id}",
                    )
                    db.commit()
                    return

            # Record price history only when price changes
            if old_price is None or abs((old_price or 0) - result.price) > 0.001:
                db.add(
                    PriceHistory(
                        listing_id=listing.id,
                        source="crawler",
                        price=result.price,
                        currency=listing.currency or "USD",
                    )
                )

            publish_event(
                {
                    "type": "price_update",
                    "listing_id": listing.id,
                    "vendor_id": listing.vendor_id,
                    "price": listing.last_price,
                    "currency": listing.currency,
                    "in_stock": result.in_stock,
                    "source": "crawler",
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )
        else:
            logger.warning("[crawl_listing] FAILED listing_id=%d status=%s msg=%s url=%s",
                           listing_id, result.status_code, result.message, listing.url)

        create_crawl_log(
            db,
            listing_id=listing.id,
            vendor_id=listing.vendor_id,
            status=listing.last_status,
            http_status=result.status_code,
            is_blocked=blocked,
            message=result.message,
        )
        db.commit()
    except Exception as exc:
        logger.error("crawl_listing(%s) failed: %s", listing_id, exc, exc_info=True)
        try:
            listing = db.query(VendorListing).filter(VendorListing.id == listing_id).first()
            if listing:
                listing.last_status = "error"
                listing.last_error = str(exc)[:500]
                listing.last_fetched_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def check_broken_links_job(frontend_url: str | None = None):
    """Worker entrypoint for the periodic broken-link audit."""
    from app.scraper.broken_links import run_broken_link_check
    db = SessionLocal()
    try:
        run = run_broken_link_check(db, frontend_url=frontend_url)
        logger.info("[check_broken_links_job] run_id=%s status=%s broken=%d/%d",
                    run.id, run.status, run.broken_count, run.total_links)
    except Exception as exc:
        logger.error("check_broken_links_job failed: %s", exc, exc_info=True)
    finally:
        db.close()


WORKER_HEARTBEAT_KEY = "worker_heartbeat"
# TTL is intentionally generous so the monitoring page doesn't flip to
# "offline" while the worker is busy processing a long-running crawl job.
# The heartbeat is refreshed by a dedicated daemon thread every
# WORKER_HEARTBEAT_INTERVAL seconds, independent of job processing.
WORKER_HEARTBEAT_TTL = 30  # seconds
WORKER_HEARTBEAT_INTERVAL = 5  # seconds between heartbeat refreshes


def _heartbeat_loop(stop_event):
    """Refresh the worker heartbeat key on a fixed interval.

    Runs on a dedicated daemon thread so the heartbeat keeps getting
    refreshed even while the main worker loop is blocked inside a
    long-running crawl job (discovery, HTTP fetches, Playwright, etc.).
    Without this, the heartbeat TTL would expire during normal job
    processing and the monitoring UI would falsely report the worker
    as offline.
    """
    r = None
    while not (stop_event and stop_event.is_set()):
        if r is None:
            try:
                r = redis_client()
                r.ping()
            except Exception as exc:
                logger.debug("Heartbeat: Redis unavailable: %s", exc)
                r = None
                time.sleep(WORKER_HEARTBEAT_INTERVAL)
                continue
        try:
            r.setex(
                WORKER_HEARTBEAT_KEY,
                WORKER_HEARTBEAT_TTL,
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            logger.debug("Heartbeat: write failed: %s", exc)
            r = None
        time.sleep(WORKER_HEARTBEAT_INTERVAL)


def run_worker_loop(stop_event=None):
    logger.info("Worker loop started. Waiting for jobs on queue '%s'...", QUEUE_KEY)

    # Heartbeat runs on its own daemon thread so it keeps firing while
    # the main loop is busy inside a long crawl job.
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(stop_event,),
        daemon=True,
        name="worker-heartbeat",
    )
    hb_thread.start()

    r = None
    while not (stop_event and stop_event.is_set()):
        # Reconnect if needed
        if r is None:
            try:
                r = redis_client()
                r.ping()
                logger.info("Worker connected to Redis.")
            except Exception as exc:
                logger.error("Redis unavailable: %s — retrying in 5s", exc)
                r = None
                time.sleep(5)
                continue

        try:
            item = r.blpop(QUEUE_KEY, timeout=2)
        except Exception as exc:
            logger.error("Redis connection lost: %s — reconnecting...", exc)
            r = None
            continue

        if not item:
            continue
        _, raw = item
        try:
            job = json.loads(raw)
            if job.get("type") == "crawl_vendor":
                crawl_vendor(int(job["vendor_id"]))
            elif job.get("type") == "crawl_listing":
                crawl_listing(int(job["listing_id"]))
            elif job.get("type") == "check_broken_links":
                check_broken_links_job(job.get("frontend_url"))
            else:
                logger.warning("Unknown job type: %s", job.get("type"))
        except Exception as exc:
            logger.error("Job failed: %s | raw=%s", exc, raw)