"""
One-time migration: adds new columns to existing tables and creates new tables.
Safe to run multiple times — each ALTER TABLE is wrapped in a check.
"""
import sys
from sqlalchemy import text
from app.db.session import engine
from app.models.entities import Base

def column_exists(conn, table, column):
    result = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :table AND COLUMN_NAME = :column"
    ), {"table": table, "column": column})
    return result.scalar() > 0

def add_column(conn, table, column, definition):
    if not column_exists(conn, table, column):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
        print(f"  + {table}.{column}")
    else:
        print(f"  = {table}.{column} (already exists)")

with engine.begin() as conn:
    print("-- wp_vendors --")
    add_column(conn, "wp_vendors", "platform",           "VARCHAR(32) NULL")
    add_column(conn, "wp_vendors", "payment_methods",    "JSON NULL")
    add_column(conn, "wp_vendors", "rating",             "FLOAT NULL")
    add_column(conn, "wp_vendors", "review_count",       "INT NULL")
    add_column(conn, "wp_vendors", "founded_year",       "SMALLINT NULL")
    add_column(conn, "wp_vendors", "product_count",      "INT NULL")
    add_column(conn, "wp_vendors", "login_email",        "VARCHAR(255) NULL")
    add_column(conn, "wp_vendors", "login_password_enc", "TEXT NULL")
    add_column(conn, "wp_vendors", "login_url_path",     "VARCHAR(255) NULL")
    add_column(conn, "wp_vendors", "bypass_strategy",    "VARCHAR(64) NULL")
    add_column(conn, "wp_vendors", "proxy_url",          "VARCHAR(512) NULL")
    add_column(conn, "wp_vendors", "dosage_selector",       "VARCHAR(255) NULL")
    add_column(conn, "wp_vendors", "dosage_attribute",      "VARCHAR(128) NULL")
    add_column(conn, "wp_vendors", "popup_close_selector",  "VARCHAR(255) NULL")
    add_column(conn, "wp_vendors", "wc_consumer_key",       "VARCHAR(255) NULL")
    add_column(conn, "wp_vendors", "wc_consumer_secret",    "TEXT NULL")
    add_column(conn, "wp_vendors", "wc_api_url",            "VARCHAR(1024) NULL")
    add_column(conn, "wp_vendors", "affiliate_template",    "VARCHAR(1024) NULL")
    add_column(conn, "wp_vendors", "trustpilot_checked_at", "DATETIME NULL")

    print("-- wp_vendor_listings --")
    add_column(conn, "wp_vendor_listings", "in_stock",         "BOOLEAN NULL")
    add_column(conn, "wp_vendor_listings", "amount_mg",        "FLOAT NULL")
    add_column(conn, "wp_vendor_listings", "amount_unit",      "VARCHAR(16) NULL")
    add_column(conn, "wp_vendor_listings", "price_per_mg",     "FLOAT NULL")
    add_column(conn, "wp_vendor_listings", "sku",              "VARCHAR(128) NULL")
    add_column(conn, "wp_vendor_listings", "variant_amounts",  "TEXT NULL")
    add_column(conn, "wp_vendor_listings", "is_manual",        "BOOLEAN NOT NULL DEFAULT FALSE")
    add_column(conn, "wp_vendor_listings", "affiliate_url",    "VARCHAR(2048) NULL")

    print("-- wp_product_aliases: drop unique constraint on alias_normalized (allow multiple products to share an alias) --")
    result = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'wp_product_aliases' AND INDEX_NAME = 'ix_wp_product_aliases_alias_normalized'"
    ))
    if result.scalar() > 0:
        conn.execute(text("ALTER TABLE wp_product_aliases DROP INDEX ix_wp_product_aliases_alias_normalized"))
        print("  - dropped unique index on wp_product_aliases.alias_normalized")
    else:
        print("  = unique index already dropped or not found")

    print("-- new tables --")

# create_all only creates tables that don't exist yet — safe to call
Base.metadata.create_all(bind=engine)
print("  + wp_product_tags, wp_vendor_sessions, wp_scheduled_crawls (created if missing)")
print("\nMigration complete.")
