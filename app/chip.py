"""Minimal CHIP Collect client: create/fetch purchases and verify callback signatures."""

import base64
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config

# Statuses that mean the money arrived. A purchase moves paid -> cleared -> settled,
# so a delayed callback retry may see a later one.
PAID_STATUSES = {"paid", "cleared", "settled"}

_http = httpx.AsyncClient(
    base_url=config.CHIP_API,
    headers={"Authorization": f"Bearer {config.CHIP_SECRET_KEY}"},
    timeout=20,
)
_public_key = None
_public_key_fetched_at = 0.0


async def create_purchase(station: str, minutes: int, amount_sen: int, product_name: str) -> dict:
    body = {
        "brand_id": config.CHIP_BRAND_ID,
        "client": {"email": config.CHIP_CLIENT_EMAIL},
        "purchase": {
            "currency": "MYR",
            "products": [{"name": product_name, "price": amount_sen}],
        },
        "reference": f"{station}-{minutes}m",
        "success_callback": f"{config.PUBLIC_URL}/chip/callback",
        "success_redirect": f"{config.PUBLIC_URL}/s/{station}?paid=1",
        "failure_redirect": f"{config.PUBLIC_URL}/s/{station}?failed=1",
    }
    r = await _http.post("/purchases/", json=body)
    r.raise_for_status()
    return r.json()


async def get_purchase(purchase_id: str) -> dict:
    r = await _http.get(f"/purchases/{purchase_id}/")
    r.raise_for_status()
    return r.json()


async def _load_public_key(refresh: bool = False):
    global _public_key, _public_key_fetched_at
    # Refresh at most every 10 min so junk requests can't hammer CHIP's API.
    stale = time.time() - _public_key_fetched_at > 600
    if _public_key is None or (refresh and stale):
        r = await _http.get("/public_key/")
        r.raise_for_status()
        # Body is a JSON string containing the PEM.
        _public_key = serialization.load_pem_public_key(r.json().encode())
        _public_key_fetched_at = time.time()
    return _public_key


def _verify_with(key, body: bytes, signature: bytes) -> bool:
    try:
        key.verify(signature, body, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


async def verify_callback(body: bytes, signature_b64: str) -> bool:
    """X-Signature = base64 RSA PKCS#1 v1.5 / SHA-256 over the raw body."""
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        return False
    if _verify_with(await _load_public_key(), body, signature):
        return True
    # Key may have been rotated; retry once with a fresh copy.
    return _verify_with(await _load_public_key(refresh=True), body, signature)
