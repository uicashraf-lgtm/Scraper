import asyncio
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from fastapi.responses import StreamingResponse
from redis import Redis
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.entities import (
    Alert,
    CanonicalProduct,
    CrawlLog,
    ListingVariant,
    ManualPriceOverride,
    PriceHistory,
    ProductTag,
    ScheduledCrawl,
    Vendor,
    VendorListing,
    VendorSession,
    VendorTargetURL,
)
from app.schemas.dto import (
    CanonicalProductCreate,
    CanonicalProductPatch,
    CrawlStatusView,
    ListingCanonicalPatch,
    ListingPatch,
    ManualListingCreate,
    ManualListingUpdate,
    ManualPriceIn,
    MerchantPriceView,
    ProductTagIn,
    ScheduledCrawlCreate,
    ScheduledCrawlPatch,
    TargetURLBulkImport,
    TargetURLCreate,
    VendorBasicPatch,
    VendorCreate,
    VendorMetaPatch,
    VendorScrapeConfigPatch,
    VendorSelectorTestIn,
)
from app.scraper.adapters.common import parse_price_from_text
from app.scraper.fetch import fetch_page
from app.services.affiliate import build_affiliate_link
from app.services.pricing import set_manual_price
from app.services.product_mapper import normalize_product_name
from app.services.queue import enqueue_listing_crawl, enqueue_vendor_crawl

router = APIRouter()

_DOSAGE_PREFIX_RE = re.compile(r'^(\d+(?:\.\d+)?(?:mg|mcg|ug|g|iu|ml))', re.IGNORECASE)
_DOSAGE_SLUG_RE = re.compile(r'(\d)-([a-z])', re.IGNORECASE)


def _normalize_variant_label(label: str) -> str:
    """Canonicalise dose labels for comparison: '10MG' / '10-mg' / '10mg' / '10 mg' all → '10 mg'."""
    s = _DOSAGE_SLUG_RE.sub(r'\1\2', str(label).strip())
    s = re.sub(r'\s+', '', s).lower()
    m = _DOSAGE_PREFIX_RE.match(s)
    if m:
        s = m.group(1)
    return re.sub(r'(\d)([a-z])', r'\1 \2', s)


def _manual_override(db: Session, listing_id: int):
    return (
        db.query(ManualPriceOverride)
        .filter(ManualPriceOverride.listing_id == listing_id, ManualPriceOverride.active.is_(True))
        .order_by(ManualPriceOverride.created_at.desc())
        .first()
    )


def _get_previous_prices(
    db: Session,
    listing_ids: list[int],
    current_prices: dict[int, float | None],
) -> dict[int, float | None]:
    """Return the most recent historical price that differs from current for each listing."""
    if not listing_ids:
        return {}
    from collections import defaultdict
    rows = (
        db.query(PriceHistory)
        .filter(PriceHistory.listing_id.in_(listing_ids))
        .order_by(PriceHistory.listing_id, PriceHistory.captured_at.desc())
        .all()
    )
    by_listing: dict[int, list[float]] = defaultdict(list)
    for h in rows:
        by_listing[h.listing_id].append(h.price)
    result: dict[int, float | None] = {}
    for lid in listing_ids:
        current = current_prices.get(lid)
        prices = by_listing.get(lid, [])
        result[lid] = next((p for p in prices if p != current), None)
    return result


def _effective_price_payload(db: Session, listing: VendorListing, vendor: Vendor, product_name: str):
    source = "crawler"
    price = listing.last_price
    currency = listing.currency
    manual = _manual_override(db, listing.id)
    if manual:
        source = "manual"
        price = manual.price
        currency = manual.currency

    link = listing.affiliate_url or build_affiliate_link(listing.url, vendor.affiliate_template)

    # Fetch variants for this listing
    variants = db.query(ListingVariant).filter(ListingVariant.listing_id == listing.id).all()
    variant_list = [
        {"dosage": v.dosage, "unit": v.unit, "price": v.price, "in_stock": v.in_stock}
        for v in variants
    ]

    return {
        "vendor": vendor.name,
        "listing_id": listing.id,
        "product": product_name,
        "effective_price": price,
        "price_min": listing.price_min or price,
        "price_max": listing.price_max or price,
        "currency": currency,
        "last_fetched_at": listing.last_fetched_at,
        "source": source,
        "link": link,
        "variants": variant_list,
        "variant_amounts_raw": listing.variant_amounts,
        "logo_url": vendor.logo_url,
        "coupon_code": vendor.coupon_code,
        "country": vendor.country,
        "rating": vendor.rating,
        "review_count": vendor.review_count,
        "in_stock": listing.in_stock if listing.in_stock is not None else (listing.last_status == "ok"),
        "amount_mg": listing.amount_mg,
        "amount_unit": listing.amount_unit,
        "price_per_mg": listing.price_per_mg,
        "product_name": listing.vendor_product_name,
        "dose_locked": listing.dose_locked,
    }


@router.post("/admin/vendors")
def create_vendor(payload: VendorCreate, db: Session = Depends(get_db)):
    config = payload.scrape_config
    auth = payload.auth

    # Encrypt password before storing
    password_enc = None
    if auth and auth.login_password:
        from app.services.crypto import encrypt_password
        password_enc = encrypt_password(auth.login_password)

    vendor = Vendor(
        name=payload.name,
        base_url=payload.base_url,
        affiliate_template=payload.affiliate_template,
        enabled=payload.enabled,
        # Scrape config
        product_link_selector=(config.product_link_selector if config else None),
        product_link_pattern=(config.product_link_pattern if config else None),
        price_selector=(config.price_selector if config else None),
        price_attr=(config.price_attr if config else None),
        name_selector=(config.name_selector if config else None),
        popup_close_selector=(config.popup_close_selector if config else None),
        max_discovered_urls=(config.max_discovered_urls if config else 120),
        max_discovery_pages=(config.max_discovery_pages if config else 8),
        # Display metadata
        logo_url=payload.logo_url,
        country=payload.country,
        shipping_info=payload.shipping_info,
        coupon_code=payload.coupon_code,
        payment_methods=payload.payment_methods,
        founded_year=payload.founded_year,
        # WooCommerce API credentials
        wc_consumer_key=(auth.wc_consumer_key if auth else None),
        wc_consumer_secret=(auth.wc_consumer_secret if auth else None),
        wc_api_url=(auth.wc_api_url if auth else None),
        # Auth / anti-bot
        login_email=(auth.login_email if auth else None),
        login_password_enc=password_enc,
        login_url_path=(auth.login_url_path if auth else None),
        bypass_strategy=(auth.bypass_strategy if auth else None),
        proxy_url=(auth.proxy_url if auth else None),
    )
    db.add(vendor)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A vendor with that name already exists.")

    for url in payload.target_urls:
        db.add(VendorTargetURL(vendor_id=vendor.id, url=url, enabled=True))

    # Create schedule row immediately so the scheduler knows when this vendor was last crawled.
    # last_enqueued_at = now means the 24h interval starts from creation, preventing
    # an immediate re-crawl on the next worker restart.
    from app.models.entities import ScheduledCrawl
    db.add(ScheduledCrawl(
        vendor_id=vendor.id,
        interval_hours=24,
        enabled=True,
        last_enqueued_at=datetime.utcnow(),
    ))
    db.commit()

    crawl_enqueued = False
    crawl_error = None
    if payload.enabled:
        try:
            enqueue_vendor_crawl(vendor.id)
            crawl_enqueued = True
        except Exception as exc:
            crawl_error = str(exc)

    return {"vendor_id": vendor.id, "crawl_enqueued": crawl_enqueued, "crawl_error": crawl_error}


@router.get("/admin/vendors")
def list_vendors(db: Session = Depends(get_db)):
    rows = db.query(Vendor).order_by(Vendor.created_at.desc()).all()
    return [
        {
            "id": v.id,
            "name": v.name,
            "base_url": v.base_url,
            "affiliate_template": v.affiliate_template,
            "enabled": v.enabled,
            "logo_url": v.logo_url,
            "country": v.country,
            "shipping_info": v.shipping_info,
            "coupon_code": v.coupon_code,
            "payment_methods": v.payment_methods,
            "rating": v.rating,
            "review_count": v.review_count,
            "founded_year": v.founded_year,
            "product_count": v.product_count,
            "bypass_strategy": v.bypass_strategy,
            "has_login": bool(v.login_email),
            "wc_consumer_key": v.wc_consumer_key,
            "wc_consumer_secret": v.wc_consumer_secret,
            "wc_api_url": v.wc_api_url,
            "login_email": v.login_email,
            "login_url_path": v.login_url_path,
            "created_at": v.created_at,
        }
        for v in rows
    ]


@router.get("/admin/vendors/{vendor_id}/scrape-config")
def get_vendor_scrape_config(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    return {
        "vendor_id": vendor.id,
        "name": vendor.name,
        "scrape_config": {
            "product_link_selector": vendor.product_link_selector,
            "product_link_pattern": vendor.product_link_pattern,
            "price_selector": vendor.price_selector,
            "price_attr": vendor.price_attr,
            "name_selector": vendor.name_selector,
            "dosage_selector": vendor.dosage_selector,
            "dosage_attribute": vendor.dosage_attribute,
            "popup_close_selector": vendor.popup_close_selector,
            "max_discovered_urls": vendor.max_discovered_urls,
            "max_discovery_pages": vendor.max_discovery_pages,
        },
    }


@router.patch("/admin/vendors/{vendor_id}/scrape-config")
def update_vendor_scrape_config(vendor_id: int, payload: VendorScrapeConfigPatch, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(vendor, key, value)
    db.commit()

    return {"ok": True, "updated_fields": list(updates.keys())}


@router.post("/admin/vendors/{vendor_id}/selector-test")
def test_vendor_selector(vendor_id: int, payload: VendorSelectorTestIn, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    status_code, html, error = fetch_page(payload.url)
    if error or not html:
        return {
            "ok": False,
            "status_code": status_code,
            "error": error or "empty_response",
            "result": None,
        }

    soup = BeautifulSoup(html, "html.parser")
    price_selector = payload.price_selector if payload.price_selector is not None else vendor.price_selector
    price_attr = payload.price_attr if payload.price_attr is not None else vendor.price_attr
    name_selector = payload.name_selector if payload.name_selector is not None else vendor.name_selector

    price_text = None
    selected_html = None
    if price_selector:
        node = soup.select_one(price_selector)
        if node:
            selected_html = str(node)[:400]
            if price_attr and node.has_attr(price_attr):
                price_text = str(node.get(price_attr, "")).strip()
            else:
                price_text = node.get_text(" ", strip=True)

    if not price_text:
        price_text = soup.get_text(" ", strip=True)[:5000]

    price, currency = parse_price_from_text(price_text)

    name = None
    if name_selector:
        n = soup.select_one(name_selector)
        if n:
            name = n.get_text(" ", strip=True) or None
    if not name:
        name = soup.title.string.strip() if soup.title and soup.title.string else None

    return {
        "ok": price is not None,
        "status_code": status_code,
        "error": None if price is not None else "price_not_found",
        "result": {
            "url": payload.url,
            "price": price,
            "currency": currency,
            "name": name,
            "price_selector_used": price_selector,
            "price_attr_used": price_attr,
            "name_selector_used": name_selector,
            "matched_node_preview": selected_html,
        },
    }


@router.post("/admin/vendors/{vendor_id}/targets")
def add_target_url(vendor_id: int, payload: TargetURLCreate, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    exists = (
        db.query(VendorTargetURL)
        .filter(VendorTargetURL.vendor_id == vendor_id, VendorTargetURL.url == payload.url)
        .first()
    )
    if exists:
        exists.enabled = payload.enabled
    else:
        db.add(VendorTargetURL(vendor_id=vendor_id, url=payload.url, enabled=payload.enabled))
    db.commit()

    if payload.enabled and vendor.enabled:
        enqueue_vendor_crawl(vendor_id)

    return {"ok": True}


@router.post("/admin/vendors/{vendor_id}/targets/import")
def import_target_urls(vendor_id: int, payload: TargetURLBulkImport, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    inserted = 0
    for url in payload.urls:
        url = url.strip()
        if not url:
            continue
        exists = (
            db.query(VendorTargetURL)
            .filter(VendorTargetURL.vendor_id == vendor_id, VendorTargetURL.url == url)
            .first()
        )
        if exists:
            exists.enabled = payload.enabled
        else:
            db.add(VendorTargetURL(vendor_id=vendor_id, url=url, enabled=payload.enabled))
            inserted += 1

    db.commit()
    if payload.crawl_now and payload.enabled and vendor.enabled:
        enqueue_vendor_crawl(vendor_id)

    return {"ok": True, "inserted": inserted, "total_urls": len(payload.urls)}


@router.patch("/admin/targets/{target_id}")
def toggle_target(target_id: int, enabled: bool, db: Session = Depends(get_db)):
    target = db.query(VendorTargetURL).filter(VendorTargetURL.id == target_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target URL not found")

    target.enabled = enabled
    db.commit()
    if enabled:
        enqueue_vendor_crawl(target.vendor_id)
    return {"ok": True, "enabled": enabled}


@router.delete("/admin/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    product = db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    listing_ids = [
        r[0] for r in db.query(VendorListing.id).filter(VendorListing.canonical_product_id == product_id).all()
    ]
    if listing_ids:
        db.query(PriceHistory).filter(PriceHistory.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(ManualPriceOverride).filter(ManualPriceOverride.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(CrawlLog).filter(CrawlLog.listing_id.in_(listing_ids)).delete(synchronize_session=False)
    db.query(VendorListing).filter(VendorListing.canonical_product_id == product_id).delete(synchronize_session=False)
    db.query(ProductTag).filter(ProductTag.canonical_product_id == product_id).delete(synchronize_session=False)
    db.delete(product)
    db.commit()
    return {"ok": True}


@router.post("/admin/listings/{listing_id}/manual-price")
def manual_price(listing_id: int, payload: ManualPriceIn, db: Session = Depends(get_db)):
    listing = db.query(VendorListing).filter(VendorListing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    set_manual_price(
        db,
        listing_id=listing_id,
        price=payload.price,
        currency=payload.currency,
        note=payload.note,
        created_by=payload.created_by,
    )
    db.commit()
    return {"ok": True}


@router.get("/admin/manual-listings")
def list_manual_listings(db: Session = Depends(get_db)):
    rows = (
        db.query(VendorListing, Vendor, CanonicalProduct)
        .join(Vendor, Vendor.id == VendorListing.vendor_id)
        .outerjoin(CanonicalProduct, CanonicalProduct.id == VendorListing.canonical_product_id)
        .filter(VendorListing.is_manual.is_(True))
        .order_by(VendorListing.id.desc())
        .all()
    )
    result = []
    for l, v, p in rows:
        tag_rows = db.query(ProductTag.tag).filter(ProductTag.canonical_product_id == l.canonical_product_id).all() if l.canonical_product_id else []
        result.append({
            "id": l.id,
            "product_name": p.name if p else l.vendor_product_name,
            "product_id": l.canonical_product_id,
            "vendor_id": v.id,
            "vendor_name": v.name,
            "price": l.last_price,
            "currency": l.currency or "USD",
            "in_stock": l.in_stock,
            "amount_mg": l.amount_mg,
            "amount_unit": l.amount_unit,
            "url": l.url if not l.url.startswith("manual://") else None,
            "category": p.category if p else None,
            "description": p.description if p else None,
            "tags": [r.tag for r in tag_rows],
        })
    return result


@router.post("/admin/manual-listings")
def create_manual_listing(payload: ManualListingCreate, db: Session = Depends(get_db)):
    from app.services.product_mapper import resolve_or_create_canonical_product

    vendor = db.query(Vendor).filter(Vendor.id == payload.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    product = resolve_or_create_canonical_product(db, payload.product_name)

    if payload.category:
        product.category = payload.category
    if payload.description:
        product.description = payload.description

    for tag in payload.tags:
        tag = tag.strip()
        if tag:
            exists = db.query(ProductTag).filter(
                ProductTag.canonical_product_id == product.id,
                ProductTag.tag == tag,
            ).first()
            if not exists:
                db.add(ProductTag(canonical_product_id=product.id, tag=tag, source="admin"))

    normalized = normalize_product_name(payload.product_name)
    listing_url = payload.url or f"manual://{payload.vendor_id}/{normalized}"

    price_per_mg = None
    if payload.price and payload.amount_mg and payload.amount_mg > 0:
        price_per_mg = payload.price / payload.amount_mg

    existing = db.query(VendorListing).filter(
        VendorListing.vendor_id == payload.vendor_id,
        VendorListing.url == listing_url,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="A manual listing for this product+vendor already exists.")

    listing = VendorListing(
        vendor_id=payload.vendor_id,
        canonical_product_id=product.id,
        vendor_product_name=payload.product_name,
        url=listing_url,
        last_price=payload.price,
        currency=payload.currency,
        in_stock=payload.in_stock,
        amount_mg=payload.amount_mg,
        amount_unit=payload.amount_unit,
        price_per_mg=price_per_mg,
        last_status="manual",
        last_fetched_at=datetime.utcnow(),
        is_manual=True,
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return {"ok": True, "id": listing.id, "product_id": product.id}


@router.put("/admin/manual-listings/{listing_id}")
def update_manual_listing(listing_id: int, payload: ManualListingUpdate, db: Session = Depends(get_db)):
    listing = db.query(VendorListing).filter(
        VendorListing.id == listing_id,
        VendorListing.is_manual.is_(True),
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Manual listing not found")

    if payload.price is not None:
        listing.last_price = payload.price
    if payload.currency is not None:
        listing.currency = payload.currency
    if payload.in_stock is not None:
        listing.in_stock = payload.in_stock
    if payload.amount_mg is not None:
        listing.amount_mg = payload.amount_mg
    if payload.amount_unit is not None:
        listing.amount_unit = payload.amount_unit
    if payload.url is not None:
        listing.url = payload.url or f"manual://{listing.vendor_id}/{normalize_product_name(listing.vendor_product_name or '')}"

    price = payload.price if payload.price is not None else listing.last_price
    amount = payload.amount_mg if payload.amount_mg is not None else listing.amount_mg
    if price and amount and amount > 0:
        listing.price_per_mg = price / amount

    listing.last_fetched_at = datetime.utcnow()

    if listing.canonical_product_id:
        product = db.query(CanonicalProduct).filter(CanonicalProduct.id == listing.canonical_product_id).first()
        if product:
            if payload.category is not None:
                product.category = payload.category
            if payload.description is not None:
                product.description = payload.description
            if payload.tags is not None:
                db.query(ProductTag).filter(
                    ProductTag.canonical_product_id == product.id,
                    ProductTag.source == "admin",
                ).delete()
                for tag in payload.tags:
                    tag = tag.strip()
                    if tag:
                        exists = db.query(ProductTag).filter(
                            ProductTag.canonical_product_id == product.id,
                            ProductTag.tag == tag,
                        ).first()
                        if not exists:
                            db.add(ProductTag(canonical_product_id=product.id, tag=tag, source="admin"))

    db.commit()
    return {"ok": True}


@router.delete("/admin/manual-listings/{listing_id}")
def delete_manual_listing(listing_id: int, db: Session = Depends(get_db)):
    listing = db.query(VendorListing).filter(
        VendorListing.id == listing_id,
        VendorListing.is_manual.is_(True),
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Manual listing not found")
    db.delete(listing)
    db.commit()
    return {"ok": True}


@router.post("/admin/listings/{listing_id}/crawl")
def crawl_listing_now(listing_id: int):
    enqueue_listing_crawl(listing_id)
    return {"ok": True}


@router.post("/admin/vendors/{vendor_id}/crawl")
def crawl_vendor_now(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    try:
        enqueue_vendor_crawl(vendor_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis unavailable — crawl not queued: {exc}")

    # Update schedule's last_enqueued_at so the monitoring page shows when the crawl was triggered
    sched = db.query(ScheduledCrawl).filter(ScheduledCrawl.vendor_id == vendor_id).first()
    if sched:
        sched.last_enqueued_at = datetime.utcnow()
        db.commit()

    return {"ok": True, "message": f"Crawl job queued for vendor '{vendor.name}'"}


@router.get("/admin/worker-status")
def worker_status():
    from app.services.queue import redis_client
    from app.workers.runner import WORKER_HEARTBEAT_KEY, QUEUE_KEY
    try:
        r = redis_client()
        heartbeat = r.get(WORKER_HEARTBEAT_KEY)
        queue_depth = r.llen(QUEUE_KEY)
        return {
            "ok": True,
            "worker_alive": heartbeat is not None,
            "last_heartbeat": heartbeat,
            "queue_depth": queue_depth,
        }
    except Exception as e:
        return {"ok": False, "worker_alive": False, "error": str(e), "queue_depth": 0}


@router.get("/dashboard/crawl-status", response_model=list[CrawlStatusView])
def crawl_status(db: Session = Depends(get_db)):
    rows = db.query(VendorListing, Vendor).join(Vendor, Vendor.id == VendorListing.vendor_id).all()
    return [
        CrawlStatusView(
            listing_id=l.id,
            vendor=v.name,
            url=l.url,
            last_status=l.last_status,
            last_error=l.last_error,
            blocked_count=l.blocked_count,
            last_fetched_at=l.last_fetched_at,
            in_stock=l.in_stock,
            amount_mg=l.amount_mg,
            amount_unit=l.amount_unit,
            price_per_mg=l.price_per_mg,
        )
        for l, v in rows
    ]


@router.get("/dashboard/alerts")
def alerts(db: Session = Depends(get_db)):
    rows = db.query(Alert).order_by(Alert.created_at.desc()).limit(200).all()
    return [
        {
            "id": r.id,
            "vendor_id": r.vendor_id,
            "severity": r.severity,
            "message": r.message,
            "resolved": r.resolved,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/stats")
def public_stats(db: Session = Depends(get_db)):
    vendor_count = db.query(Vendor).filter(Vendor.enabled.is_(True)).count()
    product_count = (
        db.query(VendorListing.canonical_product_id)
        .join(Vendor, Vendor.id == VendorListing.vendor_id)
        .filter(Vendor.enabled.is_(True), VendorListing.last_price.isnot(None))
        .distinct()
        .count()
    )
    return {
        "vendor_count": vendor_count,
        "product_count": product_count,
    }


@router.get("/products")
def list_all_products(db: Session = Depends(get_db)):
    import json as _json, re as _re

    _DOSAGE_SPLIT_RE = _re.compile(r'\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml)\b', _re.IGNORECASE)

    def _normalize_dosage(label: str) -> str:
        """Normalize '6 MG' / '6mg' / '6 mg' / '6-mg' / '20mgsinglevial' → '6 mg'."""
        s = _re.sub(r'\s+', '', label).lower()
        # Remove hyphen between digit and unit: WC slugs use "10-mg" → "10mg"
        s = _re.sub(r'(\d)-([a-z])', r'\1\2', s)
        # Strip trailing non-dosage text: "20mgsinglevial" → "20mg"
        # Matches the dosage prefix (digits + unit) and discards everything after.
        m = _re.match(r'^(\d+(?:\.\d+)?(?:mg|mcg|ug|g|iu|ml))', s)
        if m:
            s = m.group(1)
        # Insert space between number and unit for display: "6mg" → "6 mg"
        return _re.sub(r'(\d)([a-z])', r'\1 \2', s)

    def _split_dosage(label: str) -> list:
        matches = _DOSAGE_SPLIT_RE.findall(label)
        # When ≥1 dosage token found, normalise each token (not the whole label).
        # This prevents "10 mg single vial" → "10 mgsinglevial" when the full
        # label is passed through _normalize_dosage wholesale.
        if matches:
            return [_normalize_dosage(m) for m in matches]
        return [_normalize_dosage(label)]

    def _dosage_sort_key(d: str) -> float:
        m = _re.search(r"(\d+(?:\.\d+)?)", d)
        return float(m.group(1)) if m else 0

    all_products = db.query(CanonicalProduct).filter(CanonicalProduct.is_visible == True).all()

    # Group products by alias (admin display name) — admin rename controls grouping
    from collections import defaultdict as _defaultdict
    key_groups: dict[str, list[CanonicalProduct]] = _defaultdict(list)
    for p in all_products:
        key_groups[normalize_product_name(p.alias or p.name)].append(p)

    result = []
    for _key, group_products in key_groups.items():
        primary = min(group_products, key=lambda p: (p.category is None and p.description is None, p.id))
        group_ids = [p.id for p in group_products]

        listings = (
            db.query(VendorListing, Vendor)
            .join(Vendor, Vendor.id == VendorListing.vendor_id)
            .filter(
                VendorListing.canonical_product_id.in_(group_ids),
                VendorListing.last_price.isnot(None),
                Vendor.enabled.is_(True),
            )
            .order_by(VendorListing.last_price.asc())
            .all()
        )
        # Precompute current prices and previous prices for all listings in one batch
        all_listing_ids = [l.id for l, v in listings]
        curr_price_map = {
            l.id: (_manual_override(db, l.id).price if _manual_override(db, l.id) else l.last_price)
            for l, v in listings
        }
        prev_price_map = _get_previous_prices(db, all_listing_ids, curr_price_map)

        top3 = []
        for l, v in listings[:3]:
            price = curr_price_map.get(l.id)
            top3.append({
                "vendor": v.name,
                "logo_url": v.logo_url,
                "coupon_code": v.coupon_code,
                "country": v.country,
                "in_stock": l.in_stock if l.in_stock is not None else (l.last_status == "ok"),
                "price": price,
                "previous_price": prev_price_map.get(l.id),
                "currency": l.currency or "USD",
                "listing_id": l.id,
                "product_name": l.vendor_product_name,
                "amount_mg": l.amount_mg,
                "amount_unit": l.amount_unit,
                "price_per_mg": l.price_per_mg,
                "link": l.affiliate_url or build_affiliate_link(l.url, v.affiliate_template),
            })
        prices = [x["price"] for x in top3 if x.get("price") is not None]

        tag_rows = db.query(ProductTag.tag).filter(ProductTag.canonical_product_id.in_(group_ids)).all()
        tags = list({r.tag for r in tag_rows})

        # Build per-dosage vendor lists (cheapest per vendor for card summary)
        # dosage_map: label -> {vendor_name -> cheapest vendor_entry}
        dosage_map: dict[str, dict[str, dict]] = {}

        # Load ListingVariant records for all listings (per-dosage prices)
        listing_variants_map: dict[int, list] = {}
        if all_listing_ids:
            all_variants = db.query(ListingVariant).filter(ListingVariant.listing_id.in_(all_listing_ids)).all()
            for lv in all_variants:
                listing_variants_map.setdefault(lv.listing_id, []).append(lv)

        for l, v in listings:
            base_price = curr_price_map.get(l.id)
            base_entry = {
                "vendor": v.name,
                "logo_url": v.logo_url,
                "coupon_code": v.coupon_code,
                "country": v.country,
                "in_stock": l.in_stock if l.in_stock is not None else (l.last_status == "ok"),
                "currency": l.currency or "USD",
                "listing_id": l.id,
                "product_name": l.vendor_product_name,
                "link": l.affiliate_url or build_affiliate_link(l.url, v.affiliate_template),
            }
            lv_list = listing_variants_map.get(l.id, [])
            # Build maps from normalized dosage label -> variant price / stock
            lv_price_map: dict[str, float | None] = {}
            lv_stock_map: dict[str, bool | None] = {}
            for lv in lv_list:
                amt = lv.dosage
                unit = (lv.unit or "mg").lower()
                lbl = f"{int(amt)} {unit}" if amt == int(amt) else f"{amt} {unit}"
                key = _normalize_dosage(lbl)
                lv_price_map[key] = lv.price
                lv_stock_map[key] = lv.in_stock

            labels: list[str] = []
            # When dose_locked, the admin has overridden the dosage —
            # use amount_mg directly so stale variant_amounts can't
            # keep the listing grouped under the old scraped dose.
            if l.dose_locked and l.amount_mg is not None:
                unit = (l.amount_unit or "mg").lower()
                amt = l.amount_mg
                labels.append(f"{int(amt)} {unit}" if amt == int(amt) else f"{amt} {unit}")
            elif lv_list:
                # Real per-variant rows exist — render exactly those, ignoring
                # variant_amounts. Page attribute terms (e.g. genpeptide listing
                # 10/15 mg as terms but only selling 6/12/24/30/48/50) leak
                # phantom dose cards otherwise.
                for lv in sorted(lv_list, key=lambda x: x.dosage):
                    amt = lv.dosage
                    unit = (lv.unit or "mg").lower()
                    labels.append(f"{int(amt)} {unit}" if amt == int(amt) else f"{amt} {unit}")
            elif l.variant_amounts:
                try:
                    for raw_d in _json.loads(l.variant_amounts):
                        for d in _split_dosage(str(raw_d)):
                            if d:
                                labels.append(d)
                except Exception:
                    pass
            if not labels and l.amount_mg is not None:
                unit = (l.amount_unit or "mg").lower()
                amt = l.amount_mg
                labels.append(f"{int(amt)} {unit}" if amt == int(amt) else f"{amt} {unit}")

            for lbl in labels:
                norm_lbl = _normalize_dosage(lbl)
                # Use per-variant price if available, fall back to listing price
                var_price = lv_price_map.get(norm_lbl, base_price)
                price = var_price if var_price is not None else base_price
                # Use per-variant stock if recorded, otherwise inherit listing-level
                var_stock = lv_stock_map.get(norm_lbl)
                in_stock = var_stock if var_stock is not None else base_entry["in_stock"]
                amt_match = _re.search(r'(\d+(?:\.\d+)?)', lbl)
                amt_mg = float(amt_match.group(1)) if amt_match else l.amount_mg
                vendor_entry = {
                    **base_entry,
                    "in_stock": in_stock,
                    "price": price,
                    "previous_price": prev_price_map.get(l.id),
                    "amount_mg": amt_mg,
                    "amount_unit": (l.amount_unit or "mg").lower(),
                    "price_per_mg": (price / amt_mg) if (price and amt_mg) else l.price_per_mg,
                }
                if lbl not in dosage_map:
                    dosage_map[lbl] = {}
                prev = dosage_map[lbl].get(v.name)
                if prev is None or (price is not None and (prev["price"] is None or price < prev["price"])):
                    dosage_map[lbl][v.name] = vendor_entry

        available_dosages = [
            {
                "label": lbl,
                "vendors": sorted(
                    dosage_map[lbl].values(),
                    key=lambda x: (x["price"] is None, x["price"] or 0),
                ),
            }
            for lbl in sorted(dosage_map.keys(), key=_dosage_sort_key)
        ]

        category = next((p.category for p in group_products if p.category), None)
        description = next((p.description for p in group_products if p.description), None)

        # Canonical product click-through URL: cheapest vendor's product link.
        # Without this the frontend has no reliable per-card link and falls
        # back to the site home page.
        product_url = top3[0]["link"] if top3 else None

        result.append({
            "id": primary.id,
            "name": primary.alias or primary.name,
            "category": category,
            "description": description,
            "tags": tags,
            "available_dosages": available_dosages,
            "vendor_count": len(listings),
            "min_price": min(prices) if prices else None,
            "product_url": product_url,
            "top_vendors": top3,
        })

    result.sort(key=lambda x: x["name"])
    return result


@router.get("/vendors")
def list_vendors_public(db: Session = Depends(get_db)):
    vendors = db.query(Vendor).filter(Vendor.enabled.is_(True)).order_by(Vendor.name).all()
    result = []
    for v in vendors:
        product_count = (
            db.query(VendorListing)
            .filter(
                VendorListing.vendor_id == v.id,
                VendorListing.canonical_product_id.isnot(None),
            )
            .count()
        )
        result.append({
            "id": v.id,
            "name": v.name,
            "base_url": v.base_url,
            "logo_url": v.logo_url,
            "country": v.country,
            "shipping_info": v.shipping_info,
            "coupon_code": v.coupon_code,
            "payment_methods": v.payment_methods,
            "rating": v.rating,
            "review_count": v.review_count,
            "founded_year": v.founded_year,
            "product_count": product_count,
        })
    return result


@router.get("/products/{product_id}/prices")
def product_prices(product_id: int, db: Session = Depends(get_db)):
    product = db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Find all products with same alias — groups across vendor-specific naming
    alias_key = normalize_product_name(product.alias or product.name)
    group_ids = [
        p.id for p in db.query(CanonicalProduct).all()
        if normalize_product_name(p.alias or p.name) == alias_key
    ]

    rows = (
        db.query(VendorListing, Vendor)
        .join(Vendor, Vendor.id == VendorListing.vendor_id)
        .filter(VendorListing.canonical_product_id.in_(group_ids), Vendor.enabled.is_(True))
        .all()
    )

    # Expand listings with variants into one entry per variant/dosage
    import re as _re
    _AMT_RE = _re.compile(r'(\d+(?:\.\d+)?)\s*(mg|mcg|ug|g|iu|ml)\b', _re.IGNORECASE)

    result = []
    for l, v in rows:
        base = _effective_price_payload(db, l, v, product.name)
        variants = base.get("variants") or []
        if variants:
            for var in variants:
                entry = dict(base)
                entry["amount_mg"] = var["dosage"]
                entry["amount_unit"] = var.get("unit") or "mg"
                if var.get("price") is not None:
                    entry["effective_price"] = var["price"]
                    entry["price_min"] = var["price"]
                    entry["price_max"] = var["price"]
                    if var["dosage"]:
                        entry["price_per_mg"] = var["price"] / var["dosage"]
                if var.get("in_stock") is not None:
                    entry["in_stock"] = var["in_stock"]
                result.append(entry)
        else:
            # No ListingVariant records — try expanding from variant_amounts
            # When dose_locked, skip stale variant_amounts and use amount_mg directly
            raw_va = base.get("variant_amounts_raw") if not l.dose_locked else None
            parsed_amounts = []
            if raw_va:
                try:
                    for label in json.loads(raw_va):
                        m = _AMT_RE.search(str(label))
                        if m:
                            parsed_amounts.append((float(m.group(1)), m.group(2).lower(), str(label)))
                except Exception:
                    pass
            if len(parsed_amounts) >= 2:
                # Sort by dosage value so we can pair with price_min/price_max
                parsed_amounts.sort(key=lambda x: x[0])
                p_min = base.get("price_min") or base.get("effective_price")
                p_max = base.get("price_max") or base.get("effective_price")
                n = len(parsed_amounts)
                for i, (amt, unit, label) in enumerate(parsed_amounts):
                    entry = dict(base)
                    entry["amount_mg"] = amt
                    entry["amount_unit"] = unit
                    entry["variant_label"] = label
                    # Interpolate price between min and max based on position
                    if p_min is not None and p_max is not None and n > 1:
                        price = p_min + (p_max - p_min) * i / (n - 1)
                        entry["effective_price"] = round(price, 2)
                    if entry.get("effective_price") and amt:
                        entry["price_per_mg"] = entry["effective_price"] / amt
                    result.append(entry)
            else:
                result.append(base)
    return result


@router.post("/admin/products")
def create_product(payload: CanonicalProductCreate, db: Session = Depends(get_db)):
    normalized = normalize_product_name(payload.name)
    existing = db.query(CanonicalProduct).filter(CanonicalProduct.normalized_key == normalized).first()
    if existing:
        raise HTTPException(status_code=409, detail="A product with that normalized key already exists.")
    product = CanonicalProduct(name=payload.name, normalized_key=normalized)
    db.add(product)
    db.commit()
    db.refresh(product)
    return {"id": product.id, "name": product.name, "normalized_key": product.normalized_key}


@router.get("/admin/product-meta")
def product_meta(db: Session = Depends(get_db)):
    """Return all existing categories and tags for auto-complete in admin UI."""
    categories = (
        db.query(CanonicalProduct.category)
        .filter(CanonicalProduct.category.isnot(None), CanonicalProduct.category != "")
        .distinct()
        .all()
    )
    tags = db.query(ProductTag.tag).distinct().all()
    return {
        "categories": sorted({r[0] for r in categories}),
        "tags": sorted({r[0] for r in tags}),
    }


@router.get("/admin/products")
def list_products(db: Session = Depends(get_db)):
    from sqlalchemy import func

    products = db.query(CanonicalProduct).order_by(CanonicalProduct.name).all()
    result = []
    for p in products:
        all_listings = db.query(VendorListing).filter(VendorListing.canonical_product_id == p.id).all()
        listing_ids = [l.id for l in all_listings]
        tag_rows = db.query(ProductTag.tag).filter(ProductTag.canonical_product_id == p.id).all()

        # Distinct vendor IDs
        vendor_ids = list({l.vendor_id for l in all_listings})

        # Price range: prefer listing-level price_min/price_max, fall back to last_price
        all_mins = [l.price_min for l in all_listings if l.price_min is not None]
        all_maxs = [l.price_max for l in all_listings if l.price_max is not None]
        if not all_mins:
            all_mins = [l.last_price for l in all_listings if l.last_price is not None]
            all_maxs = all_mins[:]
        price_min = min(all_mins) if all_mins else None
        price_max = max(all_maxs) if all_maxs else None

        # Dosages from wp_listing_variants, fallback to listing amount_mg
        dosage_set: set[str] = set()
        if listing_ids:
            variants = db.query(ListingVariant).filter(ListingVariant.listing_id.in_(listing_ids)).all()
            for v in variants:
                val = int(v.dosage) if v.dosage == int(v.dosage) else v.dosage
                dosage_set.add(f"{val}{(v.unit or 'mg').lower()}")
        if not dosage_set:
            for l in all_listings:
                if l.amount_mg is not None:
                    unit = (l.amount_unit or "mg").lower()
                    amt = l.amount_mg
                    val = int(amt) if amt == int(amt) else amt
                    dosage_set.add(f"{val}{unit}")
        dosages = sorted(dosage_set)

        # In-stock: True if ANY listing reports in_stock
        in_stock = None
        for l in all_listings:
            if l.in_stock is True:
                in_stock = True
                break
            if l.in_stock is not None:
                in_stock = l.in_stock  # False — but keep looking for a True

        # First listing URL as the product page link
        product_url = None
        for l in all_listings:
            if l.url:
                product_url = l.url
                break

        result.append({
            "id": p.id,
            "name": p.alias or p.name,
            "original_name": p.name,
            "category": p.category,
            "description": p.description,
            "status": p.status,
            "is_visible": p.is_visible,
            "tags": [r.tag for r in tag_rows],
            "listing_count": len(all_listings),
            "vendor_ids": vendor_ids,
            "price_min": price_min,
            "price_max": price_max,
            "dosages": dosages,
            "in_stock": in_stock,
            "product_url": product_url,
            "created_at": p.created_at,
        })
    return result


@router.get("/admin/listings")
def list_listings(unmapped: bool = False, db: Session = Depends(get_db)):
    q = db.query(VendorListing, Vendor).join(Vendor, Vendor.id == VendorListing.vendor_id)
    if unmapped:
        q = q.filter(VendorListing.canonical_product_id.is_(None))
    rows = q.order_by(VendorListing.id.desc()).limit(500).all()
    return [
        {
            "id": l.id,
            "vendor_id": l.vendor_id,
            "vendor": v.name,
            "vendor_product_name": l.vendor_product_name,
            "url": l.url,
            "last_price": l.last_price,
            "currency": l.currency,
            "canonical_product_id": l.canonical_product_id,
            "last_status": l.last_status,
        }
        for l, v in rows
    ]


@router.patch("/admin/products/{product_id}")
def update_product(product_id: int, payload: CanonicalProductPatch, db: Session = Depends(get_db)):
    product = db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    dumped = payload.model_dump(exclude_unset=True)
    if "name" in dumped and dumped["name"]:
        product.alias = dumped["name"]  # admin rename sets alias; name/normalized_key stay stable for crawler
    if "category" in dumped:
        product.category = dumped["category"] or None
    if "description" in dumped:
        product.description = dumped["description"] or None
    if "status" in dumped and dumped["status"] in ("approved", "unreviewed"):
        product.status = dumped["status"]
    if "is_visible" in dumped and dumped["is_visible"] is not None:
        product.is_visible = dumped["is_visible"]
    if "tags" in dumped and dumped["tags"] is not None:
        # Replace all admin-sourced tags
        db.query(ProductTag).filter(
            ProductTag.canonical_product_id == product_id,
            ProductTag.source == "admin",
        ).delete()
        for tag in dumped["tags"]:
            tag = tag.strip()
            if tag and not db.query(ProductTag).filter(
                ProductTag.canonical_product_id == product_id,
                ProductTag.tag == tag,
            ).first():
                db.add(ProductTag(canonical_product_id=product_id, tag=tag, source="admin"))
    db.commit()
    return {"ok": True}


@router.patch("/admin/listings/{listing_id}")
def patch_listing(listing_id: int, payload: ListingPatch, db: Session = Depends(get_db)):
    listing = db.query(VendorListing).filter(VendorListing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if payload.in_stock is not None:
        listing.in_stock = payload.in_stock

    # Variant-level dose save: create/update a ListingVariant record.
    # When variant_label encodes a dose different from amount_mg, this is a
    # merge — drop the stale ListingVariant at the old dose and prune the
    # matching label from variant_amounts so the frontend stops rendering it.
    if payload.variant_label is not None and payload.amount_mg is not None:
        unit = payload.amount_unit or "mg"

        old_match = re.search(r'(\d+(?:\.\d+)?)', str(payload.variant_label))
        old_dose = float(old_match.group(1)) if old_match else None

        if old_dose is not None and abs(old_dose - payload.amount_mg) > 1e-9:
            db.query(ListingVariant).filter(
                ListingVariant.listing_id == listing_id,
                ListingVariant.dosage == old_dose,
                ListingVariant.unit == unit,
            ).delete(synchronize_session=False)

            if listing.variant_amounts:
                try:
                    raw_va = json.loads(listing.variant_amounts)
                    if isinstance(raw_va, list):
                        target = _normalize_variant_label(payload.variant_label)
                        pruned = [e for e in raw_va if _normalize_variant_label(str(e)) != target]
                        listing.variant_amounts = json.dumps(pruned) if pruned else None
                except Exception:
                    pass

        existing = (
            db.query(ListingVariant)
            .filter(ListingVariant.listing_id == listing_id,
                    ListingVariant.dosage == payload.amount_mg,
                    ListingVariant.unit == unit)
            .first()
        )
        if not existing:
            db.add(ListingVariant(
                listing_id=listing_id,
                dosage=payload.amount_mg,
                unit=unit,
                price=None,
            ))
        listing.dose_locked = True
    else:
        if payload.amount_mg is not None:
            listing.amount_mg = payload.amount_mg
            listing.dose_locked = True
            if listing.last_price and payload.amount_mg > 0:
                listing.price_per_mg = listing.last_price / payload.amount_mg
            # Update variant_amounts so the frontend dosage grouping reflects the override
            import json as _json
            unit = (payload.amount_unit or listing.amount_unit or "mg").lower()
            amt = payload.amount_mg
            lbl = f"{int(amt)} {unit}" if amt == int(amt) else f"{amt} {unit}"
            listing.variant_amounts = _json.dumps([lbl])
        if payload.amount_unit is not None:
            listing.amount_unit = payload.amount_unit
            listing.dose_locked = True

    if payload.vendor_product_name is not None:
        listing.vendor_product_name = payload.vendor_product_name
        canonical = resolve_or_create_canonical_product(db, payload.vendor_product_name)
        listing.canonical_product_id = canonical.id
    db.commit()
    return {"ok": True}


@router.delete("/admin/listings/{listing_id}/variant-amounts")
def clear_variant_amounts(listing_id: int, db: Session = Depends(get_db)):
    """Clear stale variant_amounts on a listing so dosage grouping uses amount_mg."""
    listing = db.query(VendorListing).filter(VendorListing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing.variant_amounts = None
    db.commit()
    return {"ok": True, "listing_id": listing_id}


@router.patch("/admin/vendors/{vendor_id}/meta")
def update_vendor_meta(vendor_id: int, payload: VendorMetaPatch, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(vendor, key, value)
    db.commit()
    return {"ok": True}


@router.patch("/admin/vendors/{vendor_id}")
def patch_vendor_basic(vendor_id: int, payload: VendorBasicPatch, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(vendor, key, value)
    db.commit()
    return {"ok": True}


@router.delete("/admin/vendors/{vendor_id}")
def delete_vendor(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Collect listing IDs and their canonical product IDs
    listings = db.query(VendorListing.id, VendorListing.canonical_product_id).filter(
        VendorListing.vendor_id == vendor_id
    ).all()
    listing_ids = [r[0] for r in listings]
    canonical_ids = list({r[1] for r in listings if r[1] is not None})

    # Delete listing-level child data
    if listing_ids:
        db.query(ListingVariant).filter(ListingVariant.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(PriceHistory).filter(PriceHistory.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(ManualPriceOverride).filter(ManualPriceOverride.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(CrawlLog).filter(CrawlLog.listing_id.in_(listing_ids)).delete(synchronize_session=False)
    db.query(VendorListing).filter(VendorListing.vendor_id == vendor_id).delete(synchronize_session=False)

    # Delete vendor-level data
    db.query(VendorTargetURL).filter(VendorTargetURL.vendor_id == vendor_id).delete(synchronize_session=False)
    db.query(ScheduledCrawl).filter(ScheduledCrawl.vendor_id == vendor_id).delete(synchronize_session=False)
    db.query(Alert).filter(Alert.vendor_id == vendor_id).delete(synchronize_session=False)
    db.query(CrawlLog).filter(CrawlLog.vendor_id == vendor_id).delete(synchronize_session=False)
    db.query(VendorSession).filter(VendorSession.vendor_id == vendor_id).delete(synchronize_session=False)
    db.delete(vendor)

    # Clean up ALL orphaned canonical products (no listings from any vendor)
    orphan_ids = [
        r[0] for r in db.query(CanonicalProduct.id).outerjoin(
            VendorListing, VendorListing.canonical_product_id == CanonicalProduct.id
        ).filter(VendorListing.id == None).all()
    ]
    deleted_products = 0
    if orphan_ids:
        db.query(ProductTag).filter(ProductTag.canonical_product_id.in_(orphan_ids)).delete(synchronize_session=False)
        db.query(CanonicalProduct).filter(CanonicalProduct.id.in_(orphan_ids)).delete(synchronize_session=False)
        deleted_products = len(orphan_ids)

    db.commit()
    return {"ok": True, "deleted_listings": len(listing_ids), "deleted_products": deleted_products}


@router.patch("/admin/listings/{listing_id}/canonical")
def assign_canonical(listing_id: int, payload: ListingCanonicalPatch, db: Session = Depends(get_db)):
    listing = db.query(VendorListing).filter(VendorListing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    product = db.query(CanonicalProduct).filter(CanonicalProduct.id == payload.canonical_product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    listing.canonical_product_id = payload.canonical_product_id
    db.commit()
    return {"ok": True}


@router.get("/products/search")
def search_products(q: str, db: Session = Depends(get_db)):
    items = (
        db.query(CanonicalProduct)
        .filter(CanonicalProduct.name.ilike(f"%{q}%"))
        .order_by(CanonicalProduct.name.asc())
        .limit(50)
        .all()
    )
    return [{"id": p.id, "name": p.name} for p in items]


# ─── Scheduled Crawls ────────────────────────────────────────────────────────

@router.post("/admin/schedules")
def create_schedule(payload: ScheduledCrawlCreate, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == payload.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    exists = db.query(ScheduledCrawl).filter(ScheduledCrawl.vendor_id == payload.vendor_id).first()
    if exists:
        raise HTTPException(status_code=409, detail="A schedule already exists for this vendor. Use PATCH to update it.")
    row = ScheduledCrawl(
        vendor_id=payload.vendor_id,
        interval_hours=payload.interval_hours,
        enabled=payload.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "vendor_id": row.vendor_id, "interval_hours": row.interval_hours, "enabled": row.enabled}


@router.get("/admin/schedules")
def list_schedules(db: Session = Depends(get_db)):
    rows = db.query(ScheduledCrawl).order_by(ScheduledCrawl.vendor_id).all()
    return [
        {
            "id": r.id,
            "vendor_id": r.vendor_id,
            "interval_hours": r.interval_hours,
            "enabled": r.enabled,
            "last_enqueued_at": r.last_enqueued_at,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.patch("/admin/schedules/{schedule_id}")
def update_schedule(schedule_id: int, payload: ScheduledCrawlPatch, db: Session = Depends(get_db)):
    row = db.query(ScheduledCrawl).filter(ScheduledCrawl.id == schedule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    return {"ok": True}


@router.delete("/admin/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)):
    row = db.query(ScheduledCrawl).filter(ScheduledCrawl.id == schedule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ─── Per-Vendor Crawl Summary ─────────────────────────────────────────────────

@router.get("/admin/crawl-summary")
def vendor_crawl_summary(db: Session = Depends(get_db)):
    """Per-vendor crawl results: success/error/blocked counts from recent CrawlLog entries."""
    from sqlalchemy import func, case

    # Aggregate CrawlLog by vendor — only listing-level logs (listing_id IS NOT NULL)
    rows = (
        db.query(
            CrawlLog.vendor_id,
            func.count(CrawlLog.id).label("total"),
            func.sum(case((CrawlLog.status == "ok", 1), else_=0)).label("ok_count"),
            func.sum(case((CrawlLog.status == "error", 1), else_=0)).label("error_count"),
            func.sum(case((CrawlLog.status == "blocked", 1), else_=0)).label("blocked_count"),
            func.max(CrawlLog.created_at).label("last_crawl_at"),
        )
        .filter(CrawlLog.listing_id.isnot(None))
        .group_by(CrawlLog.vendor_id)
        .all()
    )

    result = {}
    for r in rows:
        vid = r.vendor_id
        # Get the most recent error/blocked message for this vendor
        last_error_log = (
            db.query(CrawlLog.status, CrawlLog.message, CrawlLog.created_at)
            .filter(
                CrawlLog.vendor_id == vid,
                CrawlLog.listing_id.isnot(None),
                CrawlLog.status.in_(["error", "blocked"]),
            )
            .order_by(CrawlLog.created_at.desc())
            .first()
        )
        result[str(vid)] = {
            "vendor_id": vid,
            "total": int(r.total or 0),
            "ok": int(r.ok_count or 0),
            "error": int(r.error_count or 0),
            "blocked": int(r.blocked_count or 0),
            "last_crawl_at": r.last_crawl_at.isoformat() if r.last_crawl_at else None,
            "last_error_status": last_error_log.status if last_error_log else None,
            "last_error_message": last_error_log.message if last_error_log else None,
            "last_error_at": last_error_log.created_at.isoformat() if last_error_log else None,
        }

    return result


# ─── Product Tags ─────────────────────────────────────────────────────────────

@router.get("/admin/products/{product_id}/tags")
def list_product_tags(product_id: int, db: Session = Depends(get_db)):
    tags = db.query(ProductTag).filter(ProductTag.canonical_product_id == product_id).all()
    return [{"id": t.id, "tag": t.tag, "source": t.source, "created_at": t.created_at} for t in tags]


@router.post("/admin/products/{product_id}/tags")
def add_product_tag(product_id: int, payload: ProductTagIn, db: Session = Depends(get_db)):
    product = db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    exists = db.query(ProductTag).filter(
        ProductTag.canonical_product_id == product_id,
        ProductTag.tag == payload.tag,
    ).first()
    if exists:
        return {"ok": True, "id": exists.id, "created": False}
    tag = ProductTag(canonical_product_id=product_id, tag=payload.tag, source=payload.source)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return {"ok": True, "id": tag.id, "created": True}


@router.delete("/admin/products/{product_id}/tags/{tag_id}")
def delete_product_tag(product_id: int, tag_id: int, db: Session = Depends(get_db)):
    tag = db.query(ProductTag).filter(
        ProductTag.id == tag_id,
        ProductTag.canonical_product_id == product_id,
    ).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    db.delete(tag)
    db.commit()
    return {"ok": True}


# ─── Vendor Auth ──────────────────────────────────────────────────────────────

@router.post("/admin/vendors/{vendor_id}/auth")
def update_vendor_auth(vendor_id: int, db: Session = Depends(get_db), wc_consumer_key: str | None = None, wc_consumer_secret: str | None = None, wc_api_url: str | None = None, login_email: str | None = None, login_password: str | None = None, login_url_path: str | None = None, bypass_strategy: str | None = None, proxy_url: str | None = None):
    """Update login/API credentials for a vendor."""
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if wc_consumer_key is not None:
        vendor.wc_consumer_key = wc_consumer_key
    if wc_consumer_secret is not None:
        vendor.wc_consumer_secret = wc_consumer_secret
    if wc_api_url is not None:
        vendor.wc_api_url = wc_api_url or None
    if login_email is not None:
        vendor.login_email = login_email
    if login_password is not None:
        from app.services.crypto import encrypt_password
        vendor.login_password_enc = encrypt_password(login_password)
    if login_url_path is not None:
        vendor.login_url_path = login_url_path
    if bypass_strategy is not None:
        vendor.bypass_strategy = bypass_strategy
    if proxy_url is not None:
        vendor.proxy_url = proxy_url
    db.commit()

    # Invalidate any existing session so next crawl triggers fresh login
    from app.scraper.session_manager import invalidate_session
    invalidate_session(db, vendor_id)

    return {"ok": True}


@router.delete("/admin/vendors/{vendor_id}/session")
def invalidate_vendor_session(vendor_id: int, db: Session = Depends(get_db)):
    """Force re-login on the next crawl by invalidating the stored session."""
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    from app.scraper.session_manager import invalidate_session
    invalidate_session(db, vendor_id)
    return {"ok": True}


@router.get("/stream/prices")
def stream_prices():
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async def event_gen():
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe("price_events")
        try:
            while True:
                msg = pubsub.get_message(timeout=1.0)
                if msg and msg.get("type") == "message":
                    payload = msg.get("data")
                    if not isinstance(payload, str):
                        payload = json.dumps(payload)
                    yield f"event: price_update\ndata: {payload}\n\n"
                else:
                    yield ": ping\n\n"
                    await asyncio.sleep(10)
        finally:
            pubsub.close()

    return StreamingResponse(event_gen(), media_type="text/event-stream")
