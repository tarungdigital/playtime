"""Shared logic: keeping plugs in line with paid time, and crediting payments."""

import asyncio
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import chip, config, db, plug

log = logging.getLogger("playtime")

# id -> {"name", "ip"}. Loaded from the database at startup; the admin panel edits it.
stations: dict[str, dict] = {}
# minutes -> price in sen, in order: what customers can buy. Also edited in the admin panel.
options: dict[int, int] = {}
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
plug_online: dict[str, bool] = {}  # missing = not checked yet
plug_on: dict[str, bool | None] = {}
_background: set[asyncio.Task] = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def rm(sen: int) -> str:
    """1000 -> 'RM10', 1050 -> 'RM10.50'"""
    return f"RM{sen // 100}" if sen % 100 == 0 else f"RM{sen / 100:.2f}"


def pretty(station_id: str) -> str:
    return station_id.replace("-", " ").replace("_", " ").title()


def label(station: str) -> str:
    """The station's display name (ids of removed stations still read sensibly)."""
    return stations[station]["name"] if station in stations else pretty(station)


def duration(minutes: int) -> str:
    """30 -> '30 min', 60 -> '1 hour', 120 -> '2 hours', 90 -> '1 hr 30 min'"""
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} min"
    if not m:
        return f"{h} hour{'s' if h > 1 else ''}"
    return f"{h} hr {m} min"


METHOD_NAMES = {
    "duitnow_qr": "DuitNow QR", "dnqr": "DuitNow QR", "fpx": "FPX", "fpx_b2b1": "FPX Business",
    "razer_tng": "Touch 'n Go", "razer_grabpay": "GrabPay", "razer_shopeepay": "ShopeePay",
    "shopee_pay": "ShopeePay", "razer_maybankqr": "Maybank QR", "razer_atome": "Atome",
    "visa": "Visa", "mastercard": "Mastercard", "maestro": "Maestro",
    "mpgs_apple_pay": "Apple Pay", "mpgs_google_pay": "Google Pay", "crypto_coin": "Crypto",
}


def payment_methods() -> list[str]:
    """Names of the payment methods customers can use, for the pages to show."""
    if not config.CHIP_PAYMENT_METHODS:
        return ["DuitNow QR", "FPX", "e-wallets", "cards"]
    names = [METHOD_NAMES.get(code, code) for code in config.CHIP_PAYMENT_METHODS]
    return list(dict.fromkeys(names))  # dnqr + duitnow_qr -> one "DuitNow QR"


def sandbox() -> bool:
    """True while new checkouts use CHIP's sandbox (test payments, no real money).
    Switched in the admin panel; until then, sandbox only if there's no production key."""
    saved = db.get_setting("chip_mode")
    return saved == "sandbox" if saved else not config.CHIP_SECRET_KEY


def set_sandbox(on: bool):
    db.set_setting("chip_mode", "sandbox" if on else "production")


async def check_payment_methods():
    """Startup: warn about a missing CHIP key, and about CHIP_PAYMENT_METHOD codes this
    CHIP account doesn't offer, since CHIP would then refuse to create purchases."""
    if not chip.secret_key(sandbox()):
        log.warning("no CHIP %s key in .env, so customers can't pay. Add it, or switch mode in the admin panel.",
                    "CHIP_SANDBOX_SECRET_KEY" if sandbox() else "CHIP_SECRET_KEY")
        return
    wanted = config.CHIP_PAYMENT_METHODS + ([config.CHIP_PREFERRED_METHOD] if config.CHIP_PREFERRED_METHOD else [])
    if not wanted:
        return
    try:
        available = await chip.available_methods(sandbox())
    except Exception as e:
        log.warning("couldn't check payment methods with CHIP: %r", e)
        return
    unknown = [m for m in wanted if m not in available]
    if unknown:
        log.warning("payment methods not offered by your CHIP account: %s. Available: %s",
                    ", ".join(unknown), ", ".join(available))
    if config.CHIP_PAYMENT_METHODS and config.CHIP_PREFERRED_METHOD not in ("", *config.CHIP_PAYMENT_METHODS):
        log.warning("CHIP_PREFERRED_METHOD=%s isn't in CHIP_PAYMENT_METHOD", config.CHIP_PREFERRED_METHOD)


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["payment_methods"] = payment_methods
templates.env.globals["sandbox"] = sandbox
templates.env.filters["rm"] = rm
templates.env.filters["label"] = label
templates.env.filters["duration"] = duration


async def sync_plug(station: str):
    """Make the plug match the paid session: on while time remains, off otherwise.
    This also undoes anyone pressing the plug's physical button."""
    async with _locks[station]:
        if station not in stations:  # removed while this was queued
            return
        ip = stations[station]["ip"]
        try:
            want_on = db.session(station)[1] > time.time()
            is_on = await asyncio.wait_for(plug.is_on(ip), 15)
            if is_on != want_on:
                await asyncio.wait_for(plug.set_power(ip, want_on), 15)
                log.info("%s: plug turned %s", station, "ON" if want_on else "OFF")
            plug_online[station] = True
            plug_on[station] = want_on
        except Exception as e:
            plug_online[station] = False
            plug_on[station] = None
            log.warning("%s: can't reach plug at %s: %r", station, ip, e)


def load_config():
    """Startup: stations and the Tapo login come from the database. On the very
    first run, stations are copied from STATIONS in .env."""
    db.seed_stations(config.STATIONS, pretty)
    stations.clear()
    stations.update(db.station_list())
    saved = db.get_setting("play_options")
    options.clear()
    options.update(config.parse_options(saved) if saved else config.PLAY_OPTIONS)
    username, password = db.get_setting("tapo_username"), db.get_setting("tapo_password")
    if username and password:
        plug.set_credentials(username, password)


def set_options(new: dict[int, int]):
    new = dict(sorted(new.items()))
    db.set_setting("play_options", ",".join(f"{m}:{sen}" for m, sen in new.items()))
    options.clear()
    options.update(new)


# --- Promo codes ----------------------------------------------------------------

MIN_CHARGE_SEN = 100  # RM1: a discount never takes a checkout below this
# A checkout with a code holds one of its uses this long, in case that customer is still
# paying. Only against other stations, so the same customer can go back and try again.
PROMO_HOLD_SECONDS = 15 * 60


class PromoError(Exception):
    """Why a promo code can't be used, worded for customers."""


def clean_code(raw: str) -> str:
    """' save 20 ' -> 'SAVE20'"""
    return "".join(raw.split()).upper()


def promo_label(promo) -> str:
    """'20% off' or 'RM2 off'"""
    return f"{promo['percent_off']}% off" if promo["percent_off"] else f"{rm(promo['sen_off'])} off"


def promo_price(promo, sen: int) -> int:
    off = round(sen * promo["percent_off"] / 100) if promo["percent_off"] else promo["sen_off"]
    return max(sen - off, min(sen, MIN_CHARGE_SEN))


def usable_promo(raw: str, station: str):
    """The promo code if a customer at this station can use it now; PromoError if not."""
    promo = db.get_promo(clean_code(raw), sandbox(), station, PROMO_HOLD_SECONDS)
    if promo is None:
        raise PromoError("That code isn't valid. Check it and try again.")
    if promo["max_uses"] is not None:
        if promo["uses"] >= promo["max_uses"]:
            raise PromoError("Sorry, this code has been fully used.")
        if promo["uses"] + promo["held"] >= promo["max_uses"]:
            raise PromoError("Someone is paying with this code right now. Try again in a few minutes.")
    return promo


def hourly_sen() -> int:
    """The "RM10 per hour" shown on screens: the 1-hour price, or the best hourly rate."""
    if 60 in options:
        return options[60]
    return round(min(sen * 60 / m for m, sen in options.items()))


def new_station_id(name: str) -> str:
    """'Switch 3' -> 'switch-3'. Never reuses an old id, since payments refer to it."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "station"
    sid, n = base, 2
    while db.station_id_used(sid):
        sid, n = f"{base}-{n}", n + 1
    return sid


def add_station(name: str, ip: str) -> str:
    sid = new_station_id(name)
    db.add_station(sid, name, ip)
    stations[sid] = {"name": name, "ip": ip}
    return sid


async def update_station(station: str, name: str, ip: str):
    async with _locks[station]:
        old_ip = stations[station]["ip"]
        db.update_station(station, name, ip)
        stations[station] = {"name": name, "ip": ip}
        if ip != old_ip:
            plug.forget(old_ip)
            plug_online.pop(station, None)
            plug_on.pop(station, None)


async def remove_station(station: str):
    async with _locks[station]:
        ip = stations.pop(station)["ip"]
        db.remove_station(station)
        plug_online.pop(station, None)
        plug_on.pop(station, None)
    try:  # leave the plug off rather than stranded on
        await asyncio.wait_for(plug.set_power(ip, False), 15)
    except Exception as e:
        log.warning("%s: couldn't switch off plug %s while removing: %r", station, ip, e)
    plug.forget(ip)


async def enforce_loop():
    while True:
        await asyncio.gather(*(sync_plug(s) for s in list(stations)))
        await asyncio.sleep(config.CHECK_INTERVAL_SECONDS)


def state(station: str) -> dict:
    started_at, ends_at = db.session(station)
    remaining = max(0, int(ends_at - time.time()))
    return {
        "station": station,
        "active": remaining > 0,
        "remaining_seconds": remaining,
        "total_seconds": int(ends_at - started_at) if remaining else 0,
        "plug_online": plug_online.get(station),
    }


async def credit_if_paid(purchase_id: str) -> bool:
    """Ask CHIP (the source of truth) whether this purchase is paid; if so, add
    its time exactly once and switch the plug on."""
    row = db.get_purchase(purchase_id)
    if row is None or row["status"] == "paid":
        return False
    purchase = await chip.get_purchase(purchase_id, bool(row["sandbox"]))
    if purchase.get("status") not in chip.PAID_STATUSES:
        return False
    total = purchase.get("purchase", {}).get("total")
    if total != row["amount_sen"]:
        log.error("purchase %s: paid %s sen, expected %s sen", purchase_id, total, row["amount_sen"])
        return False
    if db.mark_paid_and_credit(purchase_id) is None:
        return False
    log.info("%s: +%d min from purchase %s%s", row["station"], row["minutes"], purchase_id,
             " (sandbox)" if row["sandbox"] else "")
    spawn(sync_plug(row["station"]))
    return True
