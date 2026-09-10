from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, sourced from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Cloudflare
    cloudflare_api_token: str = Field(alias="CLOUDFLARE_API_TOKEN")

    # Database
    database_url: str = Field(alias="DATABASE_URL")

    # Internal API auth (checked against a header on requests from AloBot)
    internal_api_shared_secret: str = Field(alias="INTERNAL_API_SHARED_SECRET")

    # Misc
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
