from datetime import datetime
from pydantic import BaseModel, Field


class VendorScrapeConfig(BaseModel):
    product_link_selector: str | None = None
    product_link_pattern: str | None = None
    price_selector: str | None = None
    price_attr: str | None = None
    name_selector: str | None = None
    dosage_selector: str | None = None
    dosage_attribute: str | None = None
    popup_close_selector: str | None = None
    max_discovered_urls: int = 120
    max_discovery_pages: int = 8


class VendorAuthConfig(BaseModel):
    """Login credentials, WC API credentials, and anti-bot settings for a vendor."""
    # WooCommerce REST API (highest-priority source)
    wc_consumer_key: str | None = None
    wc_consumer_secret: str | None = None
    # Public WooCommerce API (admin-entered base URL, no auth required)
    wc_api_url: str | None = None
    # Browser login fallback
    login_email: str | None = None
    login_password: str | None = None   # plaintext; service layer encrypts before storing
    login_url_path: str | None = None   # e.g. "/my-account" (defaults to "/my-account")
    bypass_strategy: str | None = None  # "none"|"playwright_stealth"|"capsolver_hcaptcha"|"capsolver_recaptcha"|"capsolver_cloudflare"
    proxy_url: str | None = None


class VendorCreate(BaseModel):
    name: str
    base_url: str
    affiliate_template: str | None = None
    enabled: bool = True
    target_urls: list[str] = Field(default_factory=list)
    crawl_now: bool = True
    scrape_config: VendorScrapeConfig | None = None
    # New: display metadata and auth
    logo_url: str | None = None
    country: str | None = None
    shipping_info: str | None = None
    coupon_code: str | None = None
    payment_methods: list[str] | None = None
    founded_year: int | None = None
    auth: VendorAuthConfig | None = None


class VendorScrapeConfigPatch(BaseModel):
    product_link_selector: str | None = None
    product_link_pattern: str | None = None
    price_selector: str | None = None
    price_attr: str | None = None
    name_selector: str | None = None
    dosage_selector: str | None = None
    dosage_attribute: str | None = None
    popup_close_selector: str | None = None
    max_discovered_urls: int | None = None
    max_discovery_pages: int | None = None


class VendorSelectorTestIn(BaseModel):
    url: str
    price_selector: str | None = None
    price_attr: str | None = None
    name_selector: str | None = None


class TargetURLCreate(BaseModel):
    url: str
    enabled: bool = True


class TargetURLBulkImport(BaseModel):
    urls: list[str] = Field(default_factory=list)
    enabled: bool = True
    crawl_now: bool = True


class ManualPriceIn(BaseModel):
    price: float
    currency: str = "USD"
    note: str | None = None
    created_by: str = "admin"


class MerchantPriceView(BaseModel):
    vendor: str
    listing_id: int
    product: str
    effective_price: float | None
    currency: str | None
    last_fetched_at: datetime | None
    source: str
    link: str


class CrawlStatusView(BaseModel):
    listing_id: int
    vendor: str
    url: str
    last_status: str
    last_error: str | None
    blocked_count: int
    last_fetched_at: datetime | None
    in_stock: bool | None = None
    amount_mg: float | None = None
    amount_unit: str | None = None
    price_per_mg: float | None = None


class ManualListingCreate(BaseModel):
    product_name: str
    vendor_id: int
    price: float
    currency: str = "USD"
    in_stock: bool = True
    amount_mg: float | None = None
    amount_unit: str | None = "mg"
    url: str | None = None          # optional product page URL for "Buy" link
    category: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class ManualListingUpdate(BaseModel):
    price: float | None = None
    currency: str | None = None
    in_stock: bool | None = None
    amount_mg: float | None = None
    amount_unit: str | None = None
    url: str | None = None
    category: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class CanonicalProductCreate(BaseModel):
    name: str


class CanonicalProductPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    description: str | None = None
    status: str | None = None  # "approved" or "unreviewed"
    is_visible: bool | None = None
    tags: list[str] | None = None  # replaces all admin tags when set


class ListingPatch(BaseModel):
    in_stock: bool | None = None
    amount_mg: float | None = None
    amount_unit: str | None = None
    vendor_product_name: str | None = None
    variant_label: str | None = None  # When set, create/update a ListingVariant instead of listing-level dose


class VendorBasicPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    enabled: bool | None = None


class VendorMetaPatch(BaseModel):
    logo_url: str | None = None
    country: str | None = None
    shipping_info: str | None = None
    coupon_code: str | None = None
    payment_methods: list[str] | None = None
    rating: float | None = None
    review_count: int | None = None
    founded_year: int | None = None
    bypass_strategy: str | None = None
    proxy_url: str | None = None
    affiliate_template: str | None = None


class ListingCanonicalPatch(BaseModel):
    canonical_product_id: int


class ScheduledCrawlCreate(BaseModel):
    vendor_id: int
    interval_hours: int = 24
    enabled: bool = True


class ScheduledCrawlPatch(BaseModel):
    interval_hours: int | None = None
    enabled: bool | None = None


class ProductTagIn(BaseModel):
    tag: str
    source: str = "admin"
