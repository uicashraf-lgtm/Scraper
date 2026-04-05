-- Migration: Add listing variants table, price_min/price_max to listings, status to canonical products

-- 1. Add status column to wp_canonical_products (skip if already exists)
SET @col_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'wp_canonical_products' AND COLUMN_NAME = 'status');
SET @sql = IF(@col_exists = 0, 'ALTER TABLE wp_canonical_products ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT ''unreviewed''', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2. Add price_min to wp_vendor_listings
SET @col_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'wp_vendor_listings' AND COLUMN_NAME = 'price_min');
SET @sql = IF(@col_exists = 0, 'ALTER TABLE wp_vendor_listings ADD COLUMN price_min FLOAT NULL', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3. Add price_max to wp_vendor_listings
SET @col_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'wp_vendor_listings' AND COLUMN_NAME = 'price_max');
SET @sql = IF(@col_exists = 0, 'ALTER TABLE wp_vendor_listings ADD COLUMN price_max FLOAT NULL', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 4. Create wp_listing_variants table (MyISAM to match existing tables — no FK support)
CREATE TABLE IF NOT EXISTS wp_listing_variants (
    id INT AUTO_INCREMENT PRIMARY KEY,
    listing_id INT NOT NULL,
    dosage FLOAT NOT NULL,
    unit VARCHAR(16) NOT NULL DEFAULT 'mg',
    price FLOAT NULL,
    UNIQUE KEY uq_listing_variant (listing_id, dosage, unit),
    INDEX idx_variant_listing (listing_id)
) ENGINE=MyISAM DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. Backfill price_min/price_max from existing last_price
UPDATE wp_vendor_listings
SET price_min = last_price, price_max = last_price
WHERE last_price IS NOT NULL AND price_min IS NULL;
