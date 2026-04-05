-- Add dose_locked flag to wp_vendor_listings
-- When TRUE, the scraper will not overwrite amount_mg / amount_unit on re-scrape.
ALTER TABLE wp_vendor_listings
    ADD COLUMN dose_locked TINYINT(1) NOT NULL DEFAULT 0
    AFTER is_manual;
