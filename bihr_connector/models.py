"""SQLAlchemy ORM models for BIHR catalog and sync state."""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Boolean, DateTime, Text, Float, Integer, JSON
from sqlalchemy.orm import declarative_base, Mapped, mapped_column

Base = declarative_base()


class Brand(Base):
    """BIHR brand dictionary derived from product catalogs."""

    __tablename__ = "brands"

    brand_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    brand_name: Mapped[str] = mapped_column(String(255))
    previous_brand_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by `refresh_active_brand` (scheduler.py) each time this brand's
    # PartNumbers+Prices are pulled -- lets `run_cycle` gate incremental
    # refresh on its own cadence (`INCREMENTAL_REFRESH_HOURS`), the same way
    # `catalog_due` gates the four bulk catalogs on theirs. NULL means never
    # incrementally refreshed yet (always due).
    last_incremental_refresh_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Product(Base):
    """BIHR product catalog records."""

    __tablename__ = "products"

    part_number: Mapped[str] = mapped_column(String(50), primary_key=True)
    old_part_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    supplier_part_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    bar_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_discontinued: Mapped[bool] = mapped_column(Boolean, default=False)
    brand_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    brand_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    group_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    family_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    family_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    segment_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    segment_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text)
    raw_hash: Mapped[str] = mapped_column(String(64))
    is_present: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_run_id: Mapped[str] = mapped_column(String(36))


class Price(Base):
    """BIHR price records."""

    __tablename__ = "prices"

    part_number: Mapped[str] = mapped_column(String(50), primary_key=True)
    dealer_price_excl_taxes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    recommended_retail_price_excl_taxes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    recommended_retail_price_incl_taxes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_currently_discounted: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text)
    raw_hash: Mapped[str] = mapped_column(String(64))
    is_present: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_run_id: Mapped[str] = mapped_column(String(36))


class Stock(Base):
    """BIHR stock records."""

    __tablename__ = "stocks"

    part_number: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    forecast_availability: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    update_datetime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text)
    raw_hash: Mapped[str] = mapped_column(String(64))
    is_present: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_run_id: Mapped[str] = mapped_column(String(36))


class SalesCategory(Base):
    """BIHR sales categories."""

    __tablename__ = "sales_categories"

    category_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text)
    raw_hash: Mapped[str] = mapped_column(String(64))
    is_present: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_run_id: Mapped[str] = mapped_column(String(36))


class SyncState(Base):
    """Sync state and watermarks for each catalog type."""

    __tablename__ = "sync_state"

    catalog_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    watermark_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_full_started_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_full_completed_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_incremental_started_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_incremental_completed_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    last_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncRun(Base):
    """Record of each sync run (bulk or per-brand)."""

    __tablename__ = "sync_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    catalog_type: Mapped[str] = mapped_column(String(50))
    mode: Mapped[str] = mapped_column(String(20))  # "full" or "incremental"
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_raw: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rows_unique: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rows_new: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rows_modified: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rows_unchanged: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50))
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BrandMatchCandidate(Base):
    """Cross-reference of BIHR brands against Reference Inteligencia's manufacturers."""

    __tablename__ = "brand_match_candidates"

    brand_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    matched_manufacturer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    matched_manufacturer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    master_product_count: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectorStatus(Base):
    """Single-row-per-connector sync health snapshot.

    Written by this connector only (see connector_status.py's recorder
    helpers). A separate project, connectors-hub (a different repo),
    will later read this table read-only to show sync health on a
    dashboard -- that consumer has no bearing on this model's shape.
    """

    __tablename__ = "connector_status"

    connector_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    scheduler_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    brands_total: Mapped[int] = mapped_column(Integer, default=0)
    brands_active: Mapped[int] = mapped_column(Integer, default=0)
    brand_match_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_incremental_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    catalogs: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
