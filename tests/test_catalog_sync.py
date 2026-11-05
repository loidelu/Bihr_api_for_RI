"""Tests for catalog sync and database schema."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from bihr_connector.models import Base, Brand


@pytest.fixture
def db_session():
    """Create a test database session."""
    # Use the test database URL
    import os
    test_db_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://bihr:bihr_test_pw@localhost:55432/bihr_connector_test"
    )

    engine = create_engine(test_db_url, echo=False)

    # Create all tables
    Base.metadata.create_all(engine)

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    # Clean up
    session.rollback()
    session.close()

    # Drop all tables after test
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_schema_round_trip(db_session):
    """Test that we can create and retrieve a Brand record."""
    db_session.add(Brand(brand_code="1035", brand_name="BARKBUSTERS", is_active=False))
    db_session.commit()
    assert db_session.get(Brand, "1035").brand_name == "BARKBUSTERS"
