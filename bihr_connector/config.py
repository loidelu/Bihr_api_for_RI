"""Configuration settings for the BIHR connector."""

import os
from dataclasses import dataclass


@dataclass
class Settings:
    """Settings loaded from environment variables."""

    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://localhost/bihr_connector"
    )
    BIHR_USERNAME: str = os.environ.get("BIHR_USERNAME", "")
    BIHR_PASSWORD: str = os.environ.get("BIHR_PASSWORD", "")
    BIHR_BASE_URL: str = os.environ.get("BIHR_BASE_URL", "https://api.bihr.net")


def get_settings() -> Settings:
    """Get the current settings."""
    return Settings()
