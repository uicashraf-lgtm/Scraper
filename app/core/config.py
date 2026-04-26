from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "dev"
    database_url: str = "mysql+pymysql://root:@localhost:3306/peptide_db"
    redis_url: str = "redis://redis-12792.c11.us-east-1-2.ec2.cloud.redislabs.com:12792/0"
    block_alert_threshold: int = 3
    scraper_user_agent: str = "PeptiPricesBot/1.0"

    # Scheduler settings
    scheduler_interval_hours: int = 24
    scheduler_poll_seconds: int = 3600

    # Trustpilot rating refresh (per-vendor)
    trustpilot_refresh_hours: int = 72

    # Broken-link audit on the public front page
    # frontend_url: page to scrape for product/buy links (e.g. "https://mysite.com/")
    # If unset, the scheduled audit is skipped silently.
    frontend_url: str | None = None
    broken_link_check_interval_hours: int = 72  # every 3 days
    broken_link_request_timeout: float = 15.0
    broken_link_max_links: int = 1000

    # Credential encryption key (32+ char string; stored in .env)
    secret_key: str = "changeme-please-set-in-dotenv-32c"

    # CapSolver API key for CAPTCHA solving (optional; set in .env to enable)
    capsolver_api_key: str | None = None

    # COA / spec-sheet extraction from product PDFs and images (purity, mass, content).
    # Off by default — extraction downloads + OCRs each candidate document so it adds
    # noticeable latency and CPU cost per listing. Enable per-deploy when desired.
    coa_extraction_enabled: bool = False
    coa_max_documents_per_listing: int = 4

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
