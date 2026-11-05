"""Pytest configuration and shared fixtures."""

import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from bihr_connector.models import Base


@pytest.fixture
def db_session():
    """Create a test database session with a real Postgres connection."""
    test_db_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://bihr:bihr_test_pw@localhost:55432/bihr_connector_test"
    )

    engine = create_engine(test_db_url, echo=False)

    # Create all tables
    Base.metadata.create_all(engine)

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()

    yield session

    # Clean up
    session.rollback()
    session.close()

    # Drop all tables after test
    Base.metadata.drop_all(engine)
    engine.dispose()
