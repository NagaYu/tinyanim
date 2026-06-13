"""SQLAlchemy engine / session wiring backed by a local SQLite file.

SQLite keeps the whole product self-hostable as a single process with zero
external infrastructure — perfect for a high-margin micro-SaaS.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent

# On Render/Railway, point TINYANIM_DATA_DIR at a mounted persistent disk so the
# SQLite database (and the cumulative stats it holds) survives restarts/deploys.
DATA_DIR = Path(os.environ.get("TINYANIM_DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "tinyanim.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    # FastAPI may touch the connection from worker threads.
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def init_db() -> None:
    """Create tables and guarantee the singleton stats row exists."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(engine)

    with SessionLocal() as session:
        stat = session.get(models.GlobalStat, 1)
        if stat is None:
            session.add(models.GlobalStat(id=1))
            session.commit()


def get_db():
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
