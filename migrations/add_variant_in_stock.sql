-- Migration: Add per-variant in_stock to wp_listing_variants.
-- Without this, every dosage of a variable WC product inherits the parent
-- product's stock_status, hiding sold-out individual doses.

SET @col_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'wp_listing_variants'
      AND COLUMN_NAME = 'in_stock'
);
SET @sql = IF(@col_exists = 0,
    'ALTER TABLE wp_listing_variants ADD COLUMN in_stock BOOLEAN NULL',
    'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
