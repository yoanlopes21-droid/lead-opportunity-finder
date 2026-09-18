from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from decimal import Decimal
from typing import Optional

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "Lead Opportunity Finder"
    api_prefix: str = "/api/v1"
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'lead_opportunity_finder.sqlite3'}"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    france_travail_client_id: Optional[str] = None
    france_travail_client_secret: Optional[str] = None
    france_travail_token_url: str = (
        "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire"
    )
    france_travail_scope: str = "api_offresdemploiv2 o2dsoffre"
    france_travail_offers_url: str = (
        "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
    )
    france_travail_timeout_seconds: float = 10.0
    societe_com_api_token: Optional[SecretStr] = None
    societe_com_api_url: str = "https://api.societe.com/api/v1"
    societe_com_timeout_seconds: float = 10.0
    societe_com_requests_per_second: float = 1.0
    societe_com_contact_ttl_days: int = 30
    societe_com_directors_ttl_days: int = 90
    brave_search_api_key: Optional[SecretStr] = None
    brave_search_api_url: str = "https://api.search.brave.com/res/v1/web/search"
    brave_search_timeout_seconds: float = 10.0
    brave_search_requests_per_second: float = 1.0
    brave_search_monthly_request_budget: int = 1000
    brave_search_default_run_hard_cap: int = 40
    brave_search_estimated_price_per_1000_usd: Decimal = Decimal("5.0")
    brave_search_estimated_monthly_free_credit_usd: Decimal = Decimal("5.0")
    official_web_fetch_timeout_seconds: float = 10.0
    official_web_fetch_requests_per_second: float = 1.0
    official_web_max_response_bytes: int = 1_048_576

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="LEAD_FINDER_",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
