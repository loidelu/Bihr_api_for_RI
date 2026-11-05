"""SQLAlchemy engine and session management."""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import Session, sessionmaker

from bihr_connector.config import get_settings

# Module-level engine (singleton per process)
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine() -> None:
    """Initialize the engine and session factory."""
    global _engine, _SessionLocal
    settings = get_settings()
    _engine = create_engine(settings.DATABASE_URL, echo=False)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def get_engine() -> Engine:
    """Get the SQLAlchemy engine, initializing if needed."""
    global _engine
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager for database sessions."""
    global _SessionLocal
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()
