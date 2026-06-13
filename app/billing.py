"""Stripe billing integration — webhook verification, event handling and
Checkout Session creation.

Deliberately dependency-free: Stripe's webhook signature scheme is plain
HMAC-SHA256, and Checkout Sessions are created with a single form-encoded HTTPS
POST via ``urllib``. No ``stripe`` SDK, nothing to keep up to date.

Configuration (env)
-------------------
* ``STRIPE_WEBHOOK_SECRET``  — ``whsec_…`` signing secret (required for /webhooks).
* ``STRIPE_SECRET_KEY``      — ``sk_…`` secret key (required for /checkout).
* ``STRIPE_PRICE_PRO``       — price id mapped to the ``pro`` plan.
* ``STRIPE_PRICE_BUSINESS``  — price id mapped to the ``business`` plan.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from sqlalchemy.orm import Session

from .models import ApiKey

STRIPE_API = "https://api.stripe.com/v1/checkout/sessions"
_SIGNATURE_TOLERANCE = 300  # seconds — rejects replayed/stale signatures


class BillingError(Exception):
    """Carries an HTTP status code up to the route layer."""

    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


# --------------------------------------------------------------------------- #
#  Plan <-> Stripe price mapping (read live so tests/env changes take effect)
# --------------------------------------------------------------------------- #
def plan_to_price(plan: str) -> str | None:
    return {
        "pro": os.environ.get("STRIPE_PRICE_PRO"),
        "business": os.environ.get("STRIPE_PRICE_BUSINESS"),
    }.get(plan)


def price_to_plan(price_id: str) -> str | None:
    for plan in ("pro", "business"):
        if plan_to_price(plan) == price_id:
            return plan
    return None


# --------------------------------------------------------------------------- #
#  Webhook signature verification (Stripe scheme, stdlib only)
# --------------------------------------------------------------------------- #
def verify_signature(
    payload: bytes,
    sig_header: str,
    secret: str | None,
    tolerance: int = _SIGNATURE_TOLERANCE,
) -> dict:
    """Validate the ``Stripe-Signature`` header and return the parsed event.

    Raises ``BillingError`` on any failure (missing config, bad/old signature)."""
    if not secret:
        raise BillingError("Webhook secret is not configured.", 503)
    if not sig_header:
        raise BillingError("Missing Stripe-Signature header.", 400)

    parts: dict[str, list[str]] = {}
    for item in sig_header.split(","):
        key, _, value = item.partition("=")
        if value:
            parts.setdefault(key.strip(), []).append(value.strip())

    try:
        timestamp = int(parts.get("t", [""])[0])
    except (ValueError, IndexError):
        raise BillingError("Malformed signature header.", 400)

    signatures = parts.get("v1", [])
    if not signatures:
        raise BillingError("No v1 signature present.", 400)

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise BillingError("Signature verification failed.", 400)

    if tolerance and abs(time.time() - timestamp) > tolerance:
        raise BillingError("Signature timestamp outside tolerance.", 400)

    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        raise BillingError("Invalid JSON payload.", 400)


# --------------------------------------------------------------------------- #
#  Event handling
# --------------------------------------------------------------------------- #
def handle_event(db: Session, event: dict) -> str:
    """Apply a (already-verified) Stripe event to the database.

    Returns a short status string for logging / the webhook response."""
    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if event_type == "checkout.session.completed":
        return _on_checkout_completed(db, obj)
    if event_type == "customer.subscription.updated":
        return _on_subscription_updated(db, obj)
    if event_type == "customer.subscription.deleted":
        return _on_subscription_deleted(db, obj)
    return "ignored"


def _on_checkout_completed(db: Session, session: dict) -> str:
    metadata = session.get("metadata") or {}
    key_hash = metadata.get("key_hash") or session.get("client_reference_id")
    if not key_hash:
        return "no_key_reference"

    key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()
    if key is None:
        return "key_not_found"

    key.active = True
    if metadata.get("plan"):
        key.plan = metadata["plan"]
    if session.get("customer"):
        key.stripe_customer_id = session["customer"]
    if session.get("subscription"):
        key.stripe_subscription_id = session["subscription"]
    db.add(key)
    db.commit()
    return "key_activated"


def _on_subscription_updated(db: Session, sub: dict) -> str:
    key = _find_by_subscription(db, sub.get("id"))
    if key is None:
        return "key_not_found"

    # Cancelled/unpaid subscriptions lose access; active ones re-map to the plan.
    status = sub.get("status")
    if status in {"canceled", "unpaid", "incomplete_expired"}:
        key.plan = "free"
    else:
        price_id = _first_price_id(sub)
        plan = price_to_plan(price_id) if price_id else None
        if plan:
            key.plan = plan
    db.add(key)
    db.commit()
    return f"plan={key.plan}"


def _on_subscription_deleted(db: Session, sub: dict) -> str:
    key = _find_by_subscription(db, sub.get("id"))
    if key is None:
        return "key_not_found"
    key.plan = "free"  # graceful downgrade — the key keeps working on the free tier
    db.add(key)
    db.commit()
    return "downgraded_to_free"


def _find_by_subscription(db: Session, subscription_id: str | None) -> ApiKey | None:
    if not subscription_id:
        return None
    return (
        db.query(ApiKey)
        .filter(ApiKey.stripe_subscription_id == subscription_id)
        .first()
    )


def _first_price_id(sub: dict) -> str | None:
    try:
        return sub["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return None


# --------------------------------------------------------------------------- #
#  Checkout Session creation
# --------------------------------------------------------------------------- #
def create_checkout_session(
    plan: str,
    key_hash: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
) -> str:
    """Create a Stripe Checkout Session and return its hosted URL."""
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        raise BillingError("Stripe is not configured (set STRIPE_SECRET_KEY).", 503)
    price = plan_to_price(plan)
    if not price:
        raise BillingError(f"No Stripe price configured for plan '{plan}'.", 503)

    fields = [
        ("mode", "subscription"),
        ("line_items[0][price]", price),
        ("line_items[0][quantity]", "1"),
        ("success_url", success_url),
        ("cancel_url", cancel_url),
        ("client_reference_id", key_hash),
        ("metadata[key_hash]", key_hash),
        ("metadata[plan]", plan),
    ]
    if customer_email:
        fields.append(("customer_email", customer_email))

    request = urllib.request.Request(
        STRIPE_API,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise BillingError(f"Stripe rejected the checkout request: {detail}", 502)
    except urllib.error.URLError as exc:
        raise BillingError(f"Could not reach Stripe: {exc.reason}", 502)

    url = data.get("url")
    if not url:
        raise BillingError("Stripe did not return a checkout URL.", 502)
    return url
