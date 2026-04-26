-- Migration: Store COA / spec-sheet data extracted from product images & PDFs

CREATE TABLE IF NOT EXISTS wp_coa_documents (
    id INT AUTO_INCREMENT PRIMARY KEY,
    listing_id INT NOT NULL,
    source_url VARCHAR(2048) NOT NULL,
    source_type VARCHAR(16) NOT NULL,        -- 'pdf' | 'image'
    source_hash VARCHAR(64) NOT NULL,        -- sha256 of the bytes (dedup key)
    extractor VARCHAR(32) NOT NULL,          -- 'pdf_text' | 'tesseract' | 'vision_llm'
    purity_pct FLOAT NULL,
    content_mg FLOAT NULL,
    content_unit VARCHAR(16) NULL,
    molecular_weight FLOAT NULL,
    sequence VARCHAR(512) NULL,
    raw_text TEXT NULL,
    confidence FLOAT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_coa_listing_source (listing_id, source_hash),
    INDEX idx_coa_listing (listing_id)
) ENGINE=MyISAM DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
