"""Pay-per-hour console power: a CHIP payment turns a Tapo P110 on, and it
switches off again when the paid time runs out."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import segno
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import admin, chip, config, db
from .core import (PromoError, check_payment_methods, credit_free, credit_if_paid, duration, enforce_loop,
                   hourly_sen, label, load_config, log, options, plug_online, promo_label, promo_price, sandbox,
                   spawn, state, stations, templates, usable_promo)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    load_config()
    spawn(check_payment_methods())
    task = asyncio.create_task(enforce_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(admin.router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
# One checkout with a promo code at a time, so two customers can't both take its last use.
_promo_lock = asyncio.Lock()


def _require_station(station: str):
    if station not in stations:
        raise HTTPException(404, "Unknown station")


# --- Customer pages ---------------------------------------------------------

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "shop_name": config.SHOP_NAME,
        "stations": [state(s) for s in stations],
    })


@app.get("/s/{station}")
async def station_page(request: Request, station: str, paid: int = 0, failed: int = 0, error: int = 0,
                       badpromo: int = 0, free: int = 0):
    """Landing page for the printed QR code: pay, then watch the countdown."""
    _require_station(station)
    if paid:
        # Don't wait for CHIP's callback: check this station's recent checkouts directly.
        for row in db.recent_unpaid(station):
            try:
                await asyncio.wait_for(credit_if_paid(row["id"]), 8)
            except Exception as e:
                log.warning("could not check purchase %s: %r", row["id"], e)
    return templates.TemplateResponse(request, "station.html", {
        "shop_name": config.SHOP_NAME,
        "station": station,
        "state": state(station),
        "price_sen": hourly_sen(),
        "options": options,
        "default_minutes": 60 if 60 in options else next(iter(options)),
        "notice": ("paid" if paid else "failed" if failed else "error" if error
                   else "badpromo" if badpromo else "free" if free else None),
    })


@app.post("/s/{station}/promo")
async def check_promo(station: str, code: str = Form("")):
    """The pay page's Apply button: what each option costs with this code."""
    _require_station(station)
    try:
        promo = usable_promo(code, station)
    except PromoError as e:
        await asyncio.sleep(1)  # slow down guessing codes
        raise HTTPException(400, str(e))
    return {"code": promo["code"], "label": promo_label(promo),
            "prices": {m: promo_price(promo, sen) for m, sen in options.items()}}


@app.post("/s/{station}/pay")
async def pay(station: str, minutes: int = Form(...), promo: str = Form("")):
    _require_station(station)
    price = options.get(minutes)
    if price is None:
        raise HTTPException(400, "Invalid play time")
    if not promo.strip():
        return await _checkout(station, minutes, price)
    async with _promo_lock:
        try:
            code = usable_promo(promo, station)
        except PromoError:  # e.g. its last use went while the customer was choosing
            return RedirectResponse(f"/s/{station}?badpromo=1", status_code=303)
        return await _checkout(station, minutes, price, code)


async def _checkout(station: str, minutes: int, price: int, promo=None):
    """Create the CHIP purchase and send the customer to its checkout page."""
    amount = promo_price(promo, price) if promo else price
    test = sandbox()
    if amount == 0:  # a code covering the whole price: nothing to pay
        credit_free(station, minutes, price, promo, test)
        return RedirectResponse(f"/s/{station}?free=1", status_code=303)
    name = f"{label(station)} playtime - {duration(minutes)}"
    if promo:
        name += f" (promo {promo['code']})"
    try:
        purchase = await chip.create_purchase(station, minutes, amount, name, test, label(station))
    except Exception as e:
        log.error("%s: CHIP purchase creation failed: %r", station, e)
        return RedirectResponse(f"/s/{station}?error=1", status_code=303)
    db.add_purchase(purchase["id"], station, minutes, amount, test,
                    promo["code"] if promo else None, price - amount)
    url = purchase["checkout_url"]
    if config.CHIP_PREFERRED_METHOD:
        url += ("&" if "?" in url else "?") + "preferred=" + config.CHIP_PREFERRED_METHOD
    return RedirectResponse(url, status_code=303)


@app.get("/api/stations/{station}")
async def station_state(station: str):
    _require_station(station)
    return state(station)


def _pay_url(station: str) -> str:
    return f"{config.PUBLIC_URL}/s/{station}"


def _qr_svg(url: str) -> str:
    return segno.make(url, error="m").svg_inline(omitsize=True)


@app.get("/station/{station}")
async def old_display_url(station: str):
    return RedirectResponse(f"/d/{station}", status_code=301)


@app.get("/d/{station}")
async def station_display(request: Request, station: str):
    """Full-screen display for a tablet/monitor at the station: shows the QR code,
    switches to a big countdown once someone pays, and back when time is up."""
    _require_station(station)
    url = _pay_url(station)
    return templates.TemplateResponse(request, "display.html", {
        "shop_name": config.SHOP_NAME,
        "station": station,
        "state": state(station),
        "price_sen": hourly_sen(),
        "qr_svg": _qr_svg(url),
        "pay_url": url,
        "warmup_seconds": config.WARMUP_SECONDS,
    })


@app.get("/print")
async def print_qr(request: Request):
    """Printable QR cards, one per station."""
    cards = [{"station": s, "url": _pay_url(s), "svg": _qr_svg(_pay_url(s))} for s in stations]
    return templates.TemplateResponse(request, "print.html", {
        "shop_name": config.SHOP_NAME, "cards": cards, "price_sen": hourly_sen(),
    })


# --- CHIP callback ----------------------------------------------------------

@app.post("/chip/callback")
async def chip_callback(request: Request):
    body = await request.body()  # raw bytes: the signature covers them exactly
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Not JSON")
    purchase_id = data.get("id") if isinstance(data, dict) else None
    # Checked with the key of the mode the purchase was made in, even if it's been switched since.
    row = db.get_purchase(purchase_id) if isinstance(purchase_id, str) else None
    test = bool(row["sandbox"]) if row else sandbox()
    if not await chip.verify_callback(body, request.headers.get("X-Signature", ""), test):
        raise HTTPException(401, "Bad signature")
    if row:
        # Raises if CHIP's API is unreachable -> non-2xx -> CHIP retries later.
        await credit_if_paid(purchase_id)
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "plugs": plug_online}
