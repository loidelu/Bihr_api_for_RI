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
    # Read-only connection string for Reference Inteligencia's database,
    # used only by `match-brands`. Provisioning the role and setting this
    # value in .env is a deployment-time action (see Task 6 brief, Step 0)
    # -- never set a default here, and never put the real value in
    # .env.example.
    RI_DATABASE_URL: str = os.environ.get("RI_DATABASE_URL", "")
    # Schema RI's tables live in on that database. The real production
    # value is "ri" (see ADR 0005) -- overridable for tests or other
    # deployments where RI's tables sit in a different schema.
    RI_SCHEMA: str = os.environ.get("RI_SCHEMA", "ri")
    # How often an active brand's incremental refresh (PartNumbers+Prices,
    # scheduler.refresh_active_brand) may run, in hours. Per the plan's
    # preflight ruling: deliberately stricter than the bulk catalog
    # cadences (Stocks' 6h is the tightest) but not unbounded -- default 2h.
    INCREMENTAL_REFRESH_HOURS: int = int(os.environ.get("INCREMENTAL_REFRESH_HOURS", "2"))


def get_settings() -> Settings:
    """Get the current settings."""
    return Settings()
