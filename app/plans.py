"""Subscription plan definitions.

These map 1:1 to what you'd sell. ``price_usd`` is display-only; wiring real
billing is a matter of pointing a Stripe price/webhook at the ``name`` field and
flipping an ``ApiKey.plan`` on ``checkout.session.completed``. Nothing else in
the engine needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_quota: int      # optimizations per 30-day period
    batch_max_files: int    # 0 == batch endpoint disabled
    max_upload_mb: int      # per-file size cap
    price_usd: int          # display only


PLANS: dict[str, Plan] = {
    "free": Plan("free", monthly_quota=50, batch_max_files=0, max_upload_mb=10, price_usd=0),
    "pro": Plan("pro", monthly_quota=10_000, batch_max_files=50, max_upload_mb=25, price_usd=19),
    "business": Plan("business", monthly_quota=100_000, batch_max_files=200, max_upload_mb=50, price_usd=99),
}

DEFAULT_PLAN = "free"


def get_plan(name: str) -> Plan:
    return PLANS.get(name, PLANS[DEFAULT_PLAN])
