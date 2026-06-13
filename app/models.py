"""ORM models for TinyAnim.

Two tables:

* ``GlobalStat``         — a single cumulative counter row (id == 1) powering the
                           "X MB saved across N files" dashboard headline.
* ``OptimizationRecord`` — one row per processed file (audit / analytics trail).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String
from sqlalchemy.sql import func

from .database import Base


class GlobalStat(Base):
    __tablename__ = "global_stats"

    id = Column(Integer, primary_key=True)
    total_files = Column(Integer, nullable=False, default=0)
    total_original_bytes = Column(BigInteger, nullable=False, default=0)
    total_optimized_bytes = Column(BigInteger, nullable=False, default=0)
    total_saved_bytes = Column(BigInteger, nullable=False, default=0)


class OptimizationRecord(Base):
    __tablename__ = "optimization_records"

    id = Column(Integer, primary_key=True)
    file_id = Column(String(32), index=True, nullable=False)
    file_type = Column(String(16), nullable=False)
    original_bytes = Column(Integer, nullable=False)
    optimized_bytes = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """A billable API credential. The raw key is shown once at creation and only
    its SHA-256 hash is ever persisted."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    key_hash = Column(String(64), unique=True, index=True, nullable=False)
    label = Column(String(120), nullable=False, default="")
    plan = Column(String(32), nullable=False, default="free")
    used_this_period = Column(Integer, nullable=False, default=0)
    period_start = Column(DateTime, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    # Stripe linkage (populated by the webhook on checkout / subscription events).
    stripe_customer_id = Column(String(80), index=True, nullable=True)
    stripe_subscription_id = Column(String(80), index=True, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ProcessedEvent(Base):
    """Stripe event ids we've already handled — guarantees webhook idempotency
    even when Stripe re-delivers the same event."""

    __tablename__ = "processed_events"

    event_id = Column(String(80), primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
