"""API-key issuance, authentication and quota metering.

Keys are random 256-bit tokens prefixed ``tinyanim_``. Only the SHA-256 hash is
stored, so a database leak never exposes usable credentials. Quota is a rolling
30-day window reset lazily on first use after the period elapses.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .database import get_db
from .models import ApiKey
from .plans import get_plan

KEY_PREFIX = "tinyanim_"
_PERIOD = timedelta(days=30)


# --------------------------------------------------------------------------- #
#  Key helpers
# --------------------------------------------------------------------------- #
def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract_key(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _utcnow() -> datetime:
    # Naive UTC, to stay comparable with datetimes SQLite reads back as naive.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _maybe_reset_period(key: ApiKey) -> None:
    now = _utcnow()
    if key.period_start is None or (now - key.period_start) > _PERIOD:
        key.period_start = now
        key.used_this_period = 0


# --------------------------------------------------------------------------- #
#  Dependencies
# --------------------------------------------------------------------------- #
def optional_api_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Optional[ApiKey]:
    """Return the authenticated key, or ``None`` for anonymous requests.

    A *present but invalid* key is rejected (401); absence is allowed."""
    raw = _extract_key(authorization, x_api_key)
    if not raw:
        return None
    key = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(raw)).first()
    if key is None or not key.active:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key.")
    return key


def require_api_key(key: Optional[ApiKey] = Depends(optional_api_key)) -> ApiKey:
    if key is None:
        raise HTTPException(
            status_code=401,
            detail="An API key is required. Send it as 'Authorization: Bearer <key>'.",
        )
    return key


# --------------------------------------------------------------------------- #
#  Quota
# --------------------------------------------------------------------------- #
def quota_remaining(key: ApiKey) -> int:
    plan = get_plan(key.plan)
    return max(0, plan.monthly_quota - (key.used_this_period or 0))


def consume_quota(db: Session, key: ApiKey, count: int) -> None:
    """Reserve ``count`` optimizations against the key, or raise 402."""
    _maybe_reset_period(key)
    plan = get_plan(key.plan)
    if (key.used_this_period or 0) + count > plan.monthly_quota:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Monthly quota of {plan.monthly_quota} exceeded for the "
                f"'{plan.name}' plan. Upgrade to continue."
            ),
        )
    key.used_this_period = (key.used_this_period or 0) + count
    db.add(key)
    db.commit()


# --------------------------------------------------------------------------- #
#  Admin auth (key issuance)
# --------------------------------------------------------------------------- #
def verify_admin(admin_token: str, configured: Optional[str]) -> None:
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="Key issuance is disabled (TINYANIM_ADMIN_TOKEN is not set).",
        )
    if not admin_token or not hmac.compare_digest(admin_token, configured):
        raise HTTPException(status_code=403, detail="Invalid admin token.")
