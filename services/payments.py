from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from .config import (
    ARTCO_BASE_URL,
    REQUEST_TIMEOUT,
    STRIPE_APPLICATION_FEE_BPS,
    STRIPE_CHECKOUT_CATALOG,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
)

STRIPE_API = "https://api.stripe.com/v1"


class PaymentConfigurationError(RuntimeError):
    pass


class PaymentUnavailable(RuntimeError):
    pass


class CheckoutSessionNotFound(RuntimeError):
    pass


def _require_secret() -> str:
    if not STRIPE_SECRET_KEY:
        raise PaymentUnavailable("Stripe Checkout is not configured.")
    return STRIPE_SECRET_KEY


def checkout_catalog_entry(artwork_id: str) -> dict[str, Any] | None:
    value = STRIPE_CHECKOUT_CATALOG.get(artwork_id)
    return value if isinstance(value, dict) else None


async def create_checkout_session(artwork_id: str, success_url: str | None = None, cancel_url: str | None = None) -> dict[str, Any]:
    """Create a Stripe-hosted Checkout Session from server-side catalog data.

    The browser supplies only the artwork ID. Price IDs and seller account IDs
    come from ARTCO_STRIPE_CHECKOUT_CATALOG, preventing client-side price tampering.
    """
    secret = _require_secret()
    catalog = checkout_catalog_entry(artwork_id)
    if not catalog:
        raise PaymentUnavailable("This artwork is not configured for ArtCo Checkout.")
    price_id = str(catalog.get("price_id") or "").strip()
    if not price_id:
        raise PaymentConfigurationError("Checkout catalog entry is missing a Stripe Price ID.")

    success = success_url or f"{ARTCO_BASE_URL.rstrip('/')}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel = cancel_url or f"{ARTCO_BASE_URL.rstrip('/')}/checkout/cancel"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        price_resp = await client.get(
            f"{STRIPE_API}/prices/{price_id}",
            auth=(secret, ""),
        )
        price_resp.raise_for_status()
        price = price_resp.json()
        if not price.get("active") or not price.get("unit_amount") or not price.get("currency"):
            raise PaymentConfigurationError("The configured Stripe price is not active or does not have a fixed amount.")

        form = {
            "mode": "payment",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "success_url": success,
            "cancel_url": cancel,
            "client_reference_id": artwork_id,
            "metadata[artwork_id]": artwork_id,
        }

        connected_account = str(catalog.get("connected_account_id") or "").strip()
        if connected_account:
            form["payment_intent_data[transfer_data][destination]"] = connected_account
            if STRIPE_APPLICATION_FEE_BPS:
                application_fee = round(int(price["unit_amount"]) * STRIPE_APPLICATION_FEE_BPS / 10_000)
                if application_fee > 0:
                    form["payment_intent_data[application_fee_amount]"] = str(application_fee)

        response = await client.post(
            f"{STRIPE_API}/checkout/sessions",
            data=form,
            auth=(secret, ""),
        )
        response.raise_for_status()
        return response.json()


async def retrieve_checkout_session(session_id: str) -> dict[str, Any]:
    """Retrieve payment status directly from Stripe; never infer payment from a redirect."""
    session_id = str(session_id or "").strip()
    if not session_id.startswith("cs_") or len(session_id) > 255:
        raise CheckoutSessionNotFound("A valid Stripe Checkout Session ID is required.")
    secret = _require_secret()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(f"{STRIPE_API}/checkout/sessions/{session_id}", auth=(secret, ""))
        if response.status_code == 404:
            raise CheckoutSessionNotFound("That checkout session could not be found.")
        response.raise_for_status()
        session = response.json()
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    return {
        "session_id": session.get("id"),
        "status": session.get("status"),
        "payment_status": session.get("payment_status"),
        "artwork_id": metadata.get("artwork_id") or session.get("client_reference_id"),
        "amount_total": session.get("amount_total"),
        "currency": session.get("currency"),
        "created": session.get("created"),
        "expires_at": session.get("expires_at"),
    }


def verify_webhook_signature(payload: bytes, signature_header: str) -> bool:
    if not STRIPE_WEBHOOK_SECRET:
        return False
    timestamp = None
    signatures: list[str] = []
    for part in signature_header.split(","):
        key, _, value = part.partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not signatures:
        return False
    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - timestamp_int) > 300:
        return False
    signed = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, value) for value in signatures)


def parse_webhook(payload: bytes) -> dict[str, Any]:
    return json.loads(payload.decode("utf-8"))
