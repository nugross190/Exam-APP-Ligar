"""Database engine, session factory, and FastAPI dependency.

Reads DATABASE_URL from env; falls back to a local sqlite file for dev.
For Postgres production deployments on Railway, DATABASE_URL is injected
automatically by the Railway PG plugin.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///./hadir_exam.db",
)

# Railway sometimes hands out 'postgres://...' URIs; SQLAlchemy 2.x wants
# 'postgresql://...'. Normalize it.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

_engine_kwargs = {"future": True}
if DATABASE_URL.startswith("sqlite"):
    # SQLite needs this when used across threads (FastAPI request handlers)
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, future=True,
)


def get_db():
    """FastAPI dependency: yields a session, closes on request exit."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def utcnow() -> datetime:
    """Naive UTC datetime — same semantics as the deprecated
    `datetime.utcnow()`, but without the DeprecationWarning on 3.12+.

    Naive (tzinfo=None) on purpose: every DateTime column in models.py
    is naive too, so mixing aware/naive datetimes in comparisons would
    raise. If we ever migrate columns to `DateTime(timezone=True)`,
    drop the .replace() and propagate the tz-aware values everywhere.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
