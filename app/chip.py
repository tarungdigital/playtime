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

_http = httpx.AsyncClient(base_url=config.CHIP_API, timeout=20)
# Per mode (sandbox True/False): CHIP's public key for callbacks, and when it was fetched.
_public_keys: dict[bool, object] = {}
_public_keys_fetched_at: dict[bool, float] = {}


def secret_key(sandbox: bool) -> str:
    return config.CHIP_SANDBOX_SECRET_KEY if sandbox else config.CHIP_SECRET_KEY


def _auth(sandbox: bool) -> dict:
    return {"Authorization": f"Bearer {secret_key(sandbox)}"}


async def create_purchase(station: str, minutes: int, amount_sen: int, product_name: str,
                          sandbox: bool, client_name: str) -> dict:
    body = {
        "brand_id": config.CHIP_BRAND_ID,
        # The station's name, so the checkout doesn't ask the customer for theirs.
        "client": {"email": config.CHIP_CLIENT_EMAIL, "full_name": client_name},
        "purchase": {
            "currency": "MYR",
            "products": [{"name": product_name, "price": amount_sen}],
        },
        "reference": f"{station}-{minutes}m",
        "success_callback": f"{config.PUBLIC_URL}/chip/callback",
        "success_redirect": f"{config.PUBLIC_URL}/s/{station}?paid=1",
        "failure_redirect": f"{config.PUBLIC_URL}/s/{station}?failed=1",
    }
    if config.CHIP_PAYMENT_METHODS:
        body["payment_method_whitelist"] = config.CHIP_PAYMENT_METHODS
    r = await _http.post("/purchases/", json=body, headers=_auth(sandbox))
    r.raise_for_status()
    return r.json()


async def available_methods(sandbox: bool) -> list[str]:
    """Payment method codes this CHIP account can offer in MYR. Also tests the key."""
    # Without an amount CHIP lists only cards: FPX, DuitNow QR and e-wallets have minimums.
    # RM10 is above all of them.
    params = {"brand_id": config.CHIP_BRAND_ID, "currency": "MYR", "amount": 1000}
    r = await _http.get("/payment_methods/", params=params, headers=_auth(sandbox))
    r.raise_for_status()
    return r.json().get("available_payment_methods", [])


async def get_purchase(purchase_id: str, sandbox: bool) -> dict:
    r = await _http.get(f"/purchases/{purchase_id}/", headers=_auth(sandbox))
    r.raise_for_status()
    return r.json()


async def _load_public_key(sandbox: bool, refresh: bool = False):
    # Refresh at most every 10 min so junk requests can't hammer CHIP's API.
    stale = time.time() - _public_keys_fetched_at.get(sandbox, 0) > 600
    if sandbox not in _public_keys or (refresh and stale):
        r = await _http.get("/public_key/", headers=_auth(sandbox))
        r.raise_for_status()
        # Body is a JSON string containing the PEM.
        _public_keys[sandbox] = serialization.load_pem_public_key(r.json().encode())
        _public_keys_fetched_at[sandbox] = time.time()
    return _public_keys[sandbox]


def _verify_with(key, body: bytes, signature: bytes) -> bool:
    try:
        key.verify(signature, body, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


async def verify_callback(body: bytes, signature_b64: str, sandbox: bool) -> bool:
    """X-Signature = base64 RSA PKCS#1 v1.5 / SHA-256 over the raw body, signed with the
    key of the mode (sandbox or production) the purchase was made in."""
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        return False
    if _verify_with(await _load_public_key(sandbox), body, signature):
        return True
    # Key may have been rotated; retry once with a fresh copy.
    return _verify_with(await _load_public_key(sandbox, refresh=True), body, signature)
