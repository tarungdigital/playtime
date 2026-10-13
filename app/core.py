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


async def check_payment_methods():
    """Startup: warn about CHIP_PAYMENT_METHOD codes this CHIP account doesn't offer,
    since CHIP would then refuse to create purchases."""
    wanted = config.CHIP_PAYMENT_METHODS + ([config.CHIP_PREFERRED_METHOD] if config.CHIP_PREFERRED_METHOD else [])
    if not wanted:
        return
    try:
        available = await chip.available_methods()
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
    purchase = await chip.get_purchase(purchase_id)
    if purchase.get("status") not in chip.PAID_STATUSES:
        return False
    total = purchase.get("purchase", {}).get("total")
    if total != row["amount_sen"]:
        log.error("purchase %s: paid %s sen, expected %s sen", purchase_id, total, row["amount_sen"])
        return False
    if db.mark_paid_and_credit(purchase_id) is None:
        return False
    log.info("%s: +%d min from purchase %s", row["station"], row["minutes"], purchase_id)
    spawn(sync_plug(row["station"]))
    return True
