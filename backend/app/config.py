"""Runtime configuration. Everything comes from environment variables (see .env.example)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://metricon:metricon@localhost:5433/metricon"
    # Login is OFF by default: anyone who can reach the URL gets full (administrator) access. Set REQUIRE_LOGIN=true to bring the
    # sign-in page, accounts and roles back; nothing else changes.
    require_login: bool = False
    session_secret: str = "dev-only-change-me"
    session_cookie_secure: bool = False
    session_max_age_hours: int = 12
    app_timezone: str = "Australia/Sydney"

    # Providers. Blank credentials leave the module visibly "not connected".
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_base_url: str = "https://api.dataforseo.com"
    anthropic_api_key: str = ""
    claude_model: str = ""  # deliberately no default: must be a supported model chosen by the operator
    anthropic_base_url: str = ""

    # Collection behaviour
    user_agent: str = "MetriconMarketIntelBot/1.0 (+private market monitoring; contact: set FETCH_CONTACT)"
    fetch_contact: str = ""
    fetch_timeout_s: float = 25.0
    fetch_concurrency: int = 4
    fetch_per_host_delay_s: float = 2.0
    fetch_max_bytes: int = 15_000_000
    store_raw_snapshots: bool = True
    raw_snapshot_max_bytes: int = 3_000_000
    stale_after_hours: int = 72
    # Optional allowlist credential agreed with a site owner (e.g. Metricon IT), sent only to the listed hosts.
    fetch_extra_headers_json: str = ""
    fetch_extra_headers_hosts: str = "metricon.com.au"
    allow_browser_fetch: bool = False  # Playwright only where a source is flagged fetch_mode=browser

    # Cost / volume limits (admins can override via app_setting rows)
    dataforseo_daily_usd_limit: float = 2.0
    dataforseo_monthly_usd_limit: float = 40.0
    dataforseo_max_requests_per_job: int = 40
    dataforseo_est_cost_per_explore: float = 0.011  # DataForSEO list price for one Live Explore task (public pricing page); an ESTIMATE - reported cost is stored separately
    anthropic_max_calls_per_job: int = 60
    anthropic_daily_call_limit: int = 300
    trend_cache_hours: int = 144  # weekly cadence: serve stored data inside this window
    news_max_items_per_query: int = 25

    # Optional private object storage for large originals (not required)
    object_storage_url: str = ""

    seed_admin_email: str = ""
    seed_admin_password: str = ""

    @property
    def anthropic_ready(self) -> bool:
        return bool(self.anthropic_api_key and self.claude_model)

    @property
    def dataforseo_ready(self) -> bool:
        return bool(self.dataforseo_login and self.dataforseo_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
