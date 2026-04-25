from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, SmallInteger, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CanonicalProduct(Base):
    __tablename__ = "wp_canonical_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # original vendor product name, never changed by admin
    alias: Mapped[str | None] = mapped_column(String(255), nullable=True)  # admin display name; falls back to name if null
    normalized_key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unreviewed")  # "unreviewed" | "approved"
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProductTag(Base):
    __tablename__ = "wp_product_tags"
    __table_args__ = (Index("uq_product_tag", "canonical_product_id", "tag", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_product_id: Mapped[int] = mapped_column(ForeignKey("wp_canonical_products.id"), nullable=False, index=True)
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="crawler")  # "crawler" | "admin"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Vendor(Base):
    __tablename__ = "wp_vendors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    affiliate_template: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Per-vendor crawl/extract hints
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)  # "woocommerce"|"shopify"|"bigcommerce"|"custom" — auto-detected if blank
    product_link_selector: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_link_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    price_selector: Mapped[str | None] = mapped_column(String(255), nullable=True)
    price_attr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name_selector: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dosage_selector: Mapped[str | None] = mapped_column(String(255), nullable=True)  # CSS selector for dosage/variant elements
    dosage_attribute: Mapped[str | None] = mapped_column(String(128), nullable=True)  # data-attribute_name value e.g. "attribute_mg"
    popup_close_selector: Mapped[str | None] = mapped_column(String(255), nullable=True)  # CSS selector to dismiss modal/popup
    max_discovered_urls: Mapped[int] = mapped_column(Integer, default=120)
    max_discovery_pages: Mapped[int] = mapped_column(Integer, default=8)

    # Display metadata (admin-entered or scraped)
    logo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    shipping_info: Mapped[str | None] = mapped_column(String(255), nullable=True)
    coupon_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_methods: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # e.g. ["credit_card","crypto","check"]
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trustpilot_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    founded_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    product_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # last known live count

    # WooCommerce REST API credentials (highest-priority data source)
    wc_consumer_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    wc_consumer_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Public WooCommerce API URL (admin-entered base URL for unauthenticated WC API)
    wc_api_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Login credentials for sites requiring authentication
    login_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    login_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet-encrypted
    login_url_path: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g. "/my-account"

    # Anti-bot strategy: "none" | "playwright_stealth" | "capsolver_hcaptcha" | "capsolver_recaptcha" | "capsolver_cloudflare"
    bypass_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proxy_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class VendorSession(Base):
    """Stores live authenticated browser session (cookies) for vendors that require login."""
    __tablename__ = "wp_vendor_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("wp_vendors.id"), nullable=False, unique=True, index=True)
    cookies_json: Mapped[str | None] = mapped_column(Text(16777215), nullable=True)  # MEDIUMTEXT
    storage_json: Mapped[str | None] = mapped_column(Text, nullable=True)            # localStorage snapshot
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VendorTargetURL(Base):
    __tablename__ = "wp_vendor_target_urls"
    __table_args__ = (Index("uq_vendor_target_url", "vendor_id", "url", unique=True, mysql_length={"url": 191}),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("wp_vendors.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class VendorListing(Base):
    __tablename__ = "wp_vendor_listings"
    __table_args__ = (Index("uq_vendor_listing_url", "vendor_id", "url", unique=True, mysql_length={"url": 191}),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("wp_vendors.id"), nullable=False, index=True)
    canonical_product_id: Mapped[int | None] = mapped_column(ForeignKey("wp_canonical_products.id"), nullable=True, index=True)
    vendor_product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    affiliate_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # lowest variant price (for sorting)
    price_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(64), default="never_fetched")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_count: Mapped[int] = mapped_column(Integer, default=0)

    # Enriched product data (populated by crawler)
    in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    amount_mg: Mapped[float | None] = mapped_column(Float, nullable=True)    # numeric dosage value
    amount_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "mg" | "mcg" | "IU" | "mL" | "g"
    price_per_mg: Mapped[float | None] = mapped_column(Float, nullable=True)    # price / amount_mg (in base unit)
    sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    variant_amounts: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array e.g. ["5 mg","10 mg"]
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)  # True for admin-entered listings
    dose_locked: Mapped[bool] = mapped_column(Boolean, default=False)  # True = admin-set dose, skip on re-scrape


class ListingVariant(Base):
    __tablename__ = "wp_listing_variants"
    __table_args__ = (Index("uq_listing_variant", "listing_id", "dosage", "unit", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("wp_vendor_listings.id"), nullable=False, index=True)
    dosage: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), default="mg")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)


class ManualPriceOverride(Base):
    __tablename__ = "wp_manual_price_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("wp_vendor_listings.id"), nullable=False, index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="admin")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PriceHistory(Base):
    __tablename__ = "wp_price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("wp_vendor_listings.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CrawlLog(Base):
    __tablename__ = "wp_crawl_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("wp_vendor_listings.id"), nullable=True, index=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("wp_vendors.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Alert(Base):
    __tablename__ = "wp_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("wp_vendors.id"), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), default="warning")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ScheduledCrawl(Base):
    """Controls automatic periodic crawling per vendor."""
    __tablename__ = "wp_scheduled_crawls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("wp_vendors.id"), nullable=False, index=True)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BrokenLinkRun(Base):
    """One row per scheduled front-page broken-link audit."""
    __tablename__ = "wp_broken_link_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    frontend_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_links: Mapped[int] = mapped_column(Integer, default=0)
    broken_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="running")  # running|done|error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BrokenLinkCheck(Base):
    """One row per product link found on the front page (upserted each run)."""
    __tablename__ = "wp_broken_link_checks"
    __table_args__ = (Index("uq_broken_link_url", "url", unique=True, mysql_length={"url": 191}),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("wp_broken_link_runs.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    final_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_broken: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
