"""Pay-per-hour console power: a CHIP payment turns a Tapo P110 on, and it
switches off again when the paid time runs out."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import segno
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import admin, chip, config, db
from .core import (check_payment_methods, credit_if_paid, duration, enforce_loop, hourly_sen, label,
                   load_config, log, options, plug_online, spawn, state, stations, templates)

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
async def station_page(request: Request, station: str, paid: int = 0, failed: int = 0, error: int = 0):
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
        "notice": "paid" if paid else "failed" if failed else "error" if error else None,
    })


@app.post("/s/{station}/pay")
async def pay(station: str, minutes: int = Form(...)):
    _require_station(station)
    amount = options.get(minutes)
    if amount is None:
        raise HTTPException(400, "Invalid play time")
    name = f"{label(station)} playtime - {duration(minutes)}"
    try:
        purchase = await chip.create_purchase(station, minutes, amount, name)
    except Exception as e:
        log.error("%s: CHIP purchase creation failed: %r", station, e)
        return RedirectResponse(f"/s/{station}?error=1", status_code=303)
    db.add_purchase(purchase["id"], station, minutes, amount)
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
    if not await chip.verify_callback(body, request.headers.get("X-Signature", "")):
        raise HTTPException(401, "Bad signature")
    purchase_id = json.loads(body).get("id")
    if purchase_id:
        # Raises if CHIP's API is unreachable -> non-2xx -> CHIP retries later.
        await credit_if_paid(purchase_id)
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "plugs": plug_online}
