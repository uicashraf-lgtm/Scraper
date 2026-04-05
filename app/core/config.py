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

    # Credential encryption key (32+ char string; stored in .env)
    secret_key: str = "changeme-please-set-in-dotenv-32c"

    # CapSolver API key for CAPTCHA solving (optional; set in .env to enable)
    capsolver_api_key: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
