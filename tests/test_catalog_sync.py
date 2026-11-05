"""Tests for catalog sync and database schema."""

from bihr_connector.models import Brand


def test_schema_round_trip(db_session):
    """Test that we can create and retrieve a Brand record."""
    db_session.add(Brand(brand_code="1035", brand_name="BARKBUSTERS", is_active=False))
    db_session.commit()
    assert db_session.get(Brand, "1035").brand_name == "BARKBUSTERS"
