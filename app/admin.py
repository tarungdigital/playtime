"""Staff admin panel: live stations, free time / stop, revenue and activity."""

import asyncio
import hashlib
import hmac
import ipaddress
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import chip, config, db, plug
from .core import (EWALLETS, add_station, checkout_methods, clean_code, credit_if_paid, duration, label,
                   method_names, offered_methods, options, plug_on, promo_label, remove_station, rm, sandbox,
                   set_options, set_sandbox, spawn, state, stations, sync_plug, templates, update_station)

router = APIRouter(prefix="/admin")

COOKIE = "playtime_admin"
TZ = timezone(timedelta(hours=config.UTC_OFFSET_HOURS))


# --- Auth -------------------------------------------------------------------

def _session_value() -> str:
    # Derived from the password, so changing ADMIN_TOKEN logs everyone out.
    return hmac.new(config.ADMIN_TOKEN.encode(), b"playtime-admin-session", hashlib.sha256).hexdigest()


def is_admin(request: Request) -> bool:
    if not config.ADMIN_TOKEN:
        return False
    token = request.headers.get("x-admin-token", "")
    if token and hmac.compare_digest(token, config.ADMIN_TOKEN):
        return True
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie, _session_value())


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(403, "Not logged in")


def _require_station(station: str):
    if station not in stations:
        raise HTTPException(404, "Unknown station")


@router.get("")
async def dashboard(request: Request):
    if not is_admin(request):
        return templates.TemplateResponse(request, "admin_login.html", {
            "shop_name": config.SHOP_NAME, "enabled": bool(config.ADMIN_TOKEN), "error": False,
        })
    return templates.TemplateResponse(request, "admin.html", {
        "shop_name": config.SHOP_NAME, "summary": summary_data(),
    })


@router.post("/login")
async def login(request: Request, password: str = Form("")):
    if config.ADMIN_TOKEN and hmac.compare_digest(password, config.ADMIN_TOKEN):
        response = RedirectResponse("/admin", status_code=303)
        https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, _session_value(), max_age=30 * 86400,
                            httponly=True, samesite="lax", secure=https)
        return response
    await asyncio.sleep(1)  # slow down password guessing
    return templates.TemplateResponse(request, "admin_login.html", {
        "shop_name": config.SHOP_NAME, "enabled": bool(config.ADMIN_TOKEN), "error": True,
    }, status_code=401)


@router.post("/logout")
async def logout():
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(COOKIE)
    return response


# --- Data -------------------------------------------------------------------

def summary_data() -> dict:
    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)

    paid = db.paid_since(min(yesterday, week_start, month_start).timestamp())

    def total(since: datetime, until: datetime | None = None):
        """Sales: sandbox payments are tests, and 100% promo codes are free play, not money."""
        rows = [p for p in paid if not p["sandbox"] and p["amount_sen"] and p["paid_at"] >= since.timestamp()
                and (until is None or p["paid_at"] < until.timestamp())]
        return sum(p["amount_sen"] for p in rows), len(rows), sum(p["minutes"] for p in rows)

    today_sen, today_count, today_minutes = total(today)
    days = []
    for i in range(7):
        start = week_start + timedelta(days=i)
        days.append({
            "date": start.strftime("%Y-%m-%d"),
            "label": "Today" if i == 6 else start.strftime("%a"),
            "long": start.strftime("%a, %d %b"),
            "sen": total(start, start + timedelta(days=1))[0],
        })

    station_rows = [
        {**state(s), "label": label(s), "ip": cfg["ip"], "plug_on": plug_on.get(s)}
        for s, cfg in stations.items()
    ]
    username, password = plug.credentials()
    test = sandbox()
    methods = checkout_methods(test)

    activity = [
        {"at": p["paid_at"], "kind": "payment", "station": p["station"], "minutes": p["minutes"],
         "sen": p["amount_sen"], "sandbox": bool(p["sandbox"]), "promo": p["promo_code"],
         "discount_sen": p["discount_sen"]}
        for p in paid[:25]
    ] + [
        {"at": a["at"], "kind": a["action"], "station": a["station"],
         "minutes": a["minutes"], "detail": a["detail"]}
        for a in db.recent_staff_actions(25)
    ]
    activity.sort(key=lambda a: a["at"], reverse=True)

    pending = [
        {"id": p["id"], "station": p["station"], "minutes": p["minutes"],
         "sen": p["amount_sen"], "created_at": p["created_at"], "sandbox": bool(p["sandbox"])}
        for p in db.recent_unpaid(max_age_seconds=3600, limit=10)
    ]

    return {
        "now": time.time(),
        "stations": station_rows,
        "prices": [{"minutes": m, "sen": sen} for m, sen in options.items()],
        # The password itself never leaves the server.
        "tapo": {"username": username, "password_set": bool(password), "simulated": config.PLUG_DRY_RUN},
        # Which keys are in .env, never the keys themselves.
        "payments": {
            "sandbox": test, "production_key": bool(config.CHIP_SECRET_KEY),
            "sandbox_key": bool(config.CHIP_SANDBOX_SECRET_KEY),
            # What customers can pay with (None until CHIP has been asked), and whether that
            # includes e-wallets: "on", "excluded" by CHIP_PAYMENT_METHOD, or "off" at CHIP.
            "methods": None if methods is None else method_names(methods),
            "ewallets": None if methods is None else "on" if EWALLETS & set(methods)
            else "excluded" if EWALLETS & set(offered_methods[test]) else "off",
        },
        "promos": [
            {"code": p["code"], "label": promo_label(p), "max_uses": p["max_uses"],
             "uses": p["uses"], "test_uses": p["test_uses"]}
            for p in db.promo_codes()
        ],
        "stats": {
            "today_sen": today_sen,
            "today_count": today_count,
            "today_minutes": today_minutes,
            "yesterday_sen": total(yesterday, today)[0],
            "week_sen": total(week_start)[0],
            "month_sen": total(month_start)[0],
            "month_label": now.strftime("%B"),
        },
        "days": days,
        "activity": activity[:25],
        "pending": pending,
    }


@router.get("/api/summary", dependencies=[Depends(require_admin)])
async def summary():
    return summary_data()


# --- Actions ----------------------------------------------------------------

@router.post("/stations/{station}/add", dependencies=[Depends(require_admin)])
async def add_time(station: str, minutes: int = 60):
    """Give free time, e.g. to make up for a fault."""
    _require_station(station)
    if not 1 <= minutes <= 600:
        raise HTTPException(400, "minutes must be 1-600")
    db.add_time(station, minutes * 60)
    db.log_staff_action(station, "add", minutes)
    await sync_plug(station)
    return state(station)


@router.post("/stations/{station}/stop", dependencies=[Depends(require_admin)])
async def stop(station: str):
    _require_station(station)
    db.stop(station)
    db.log_staff_action(station, "stop")
    await sync_plug(station)
    return state(station)


class PlugAddress(BaseModel):
    ip: str


class StationIn(BaseModel):
    name: str
    ip: str
    force: bool = False  # save even if no plug answers (e.g. it's unplugged right now)


class TapoAccount(BaseModel):
    username: str
    password: str = ""  # empty = keep the saved one
    force: bool = False


def _valid_name(raw: str, station: str | None = None) -> str:
    name = " ".join(raw.split())
    if not 1 <= len(name) <= 40:
        raise HTTPException(400, "Give the station a name (up to 40 characters).")
    for other, cfg in stations.items():
        if other != station and cfg["name"].casefold() == name.casefold():
            raise HTTPException(400, f"There's already a station called {cfg['name']}.")
    return name


def _valid_ip(raw: str) -> str:
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        raise HTTPException(400, "That isn't a valid IP address. It should look like 192.168.1.50.")


async def _identify(ip: str) -> dict:
    try:
        return await asyncio.wait_for(plug.identify(ip), 12)
    except Exception:
        raise HTTPException(409, f"No Tapo plug answered at {ip}. Check the address and that "
                                 "the plug is on the same Wi-Fi.")


@router.post("/plugs/check", dependencies=[Depends(require_admin)])
async def check_plug(body: PlugAddress):
    """Which plug is at this IP? Lets staff confirm before pointing a station at it."""
    return await _identify(_valid_ip(body.ip))


def _valid_free_ip(raw: str, station: str | None = None) -> str:
    ip = _valid_ip(raw)
    for other, cfg in stations.items():
        if other != station and cfg["ip"] == ip:
            raise HTTPException(400, f"{cfg['name']} already uses {ip}.")
    return ip


# A wrong IP could be someone else's plug (the fridge!), which a station would then
# switch off, so a plug must answer at a new IP unless staff explicitly override.

@router.post("/stations", dependencies=[Depends(require_admin)])
async def create_station(body: StationIn):
    name, ip = _valid_name(body.name), _valid_free_ip(body.ip)
    found = None if body.force else await _identify(ip)
    station = add_station(name, ip)
    db.log_staff_action(station, "settings", detail=f"added {name} (plug {ip})")
    await sync_plug(station)
    return {"station": station, "name": name, "ip": ip, "plug": found}


@router.post("/stations/{station}", dependencies=[Depends(require_admin)])
async def edit_station(station: str, body: StationIn):
    """Rename a station and/or point it at a different plug. Its id (in the QR code
    links) stays the same, so printed codes keep working."""
    _require_station(station)
    old = dict(stations[station])
    name, ip = _valid_name(body.name, station), _valid_free_ip(body.ip, station)
    found = None
    if ip != old["ip"] and not body.force:
        found = await _identify(ip)
    await update_station(station, name, ip)
    changes = []
    if name != old["name"]:
        changes.append(f"renamed {old['name']} → {name}")
    if ip != old["ip"]:
        changes.append(f"plug IP {old['ip']} → {ip}")
        await sync_plug(station)
    if changes:
        db.log_staff_action(station, "settings", detail="; ".join(changes))
    return {"station": station, "name": name, "ip": ip, "plug": found}


@router.post("/stations/{station}/remove", dependencies=[Depends(require_admin)])
async def delete_station(station: str):
    _require_station(station)
    if state(station)["active"]:
        raise HTTPException(400, "Stop this station's session before removing it.")
    name = label(station)
    await remove_station(station)
    db.log_staff_action(station, "settings", detail=f"removed {name}")
    return {"ok": True}


@router.post("/tapo-account", dependencies=[Depends(require_admin)])
async def set_tapo_account(body: TapoAccount):
    """The Tapo app login the plugs are controlled with. Tested on a plug before saving."""
    username = body.username.strip()
    password = body.password or plug.credentials()[1]
    if not username or not password:
        raise HTTPException(400, "Enter the email and password you use for the Tapo app.")
    tested = None
    if not config.PLUG_DRY_RUN and not body.force and stations:
        ip = next(iter(stations.values()))["ip"]
        try:
            found = await asyncio.wait_for(plug.identify(ip, (username, password)), 15)
        except Exception as e:
            raise HTTPException(409, f"Couldn't log in to the plug at {ip} with these details "
                                     f"({str(e)[:120] or type(e).__name__}). Both email and password "
                                     "are case-sensitive. If the plug is just offline, save anyway.")
        tested = f"Logged in to “{found['nickname']}” at {ip}"
    db.set_setting("tapo_username", username)
    db.set_setting("tapo_password", password)
    plug.set_credentials(username, password)
    db.log_staff_action("", "settings", detail=f"Tapo login set to {username}")
    for s in list(stations):
        spawn(sync_plug(s))
    return {"username": username, "tested": tested, "simulated": config.PLUG_DRY_RUN}


class PriceOption(BaseModel):
    minutes: int
    sen: int


class Prices(BaseModel):
    options: list[PriceOption]


@router.post("/prices", dependencies=[Depends(require_admin)])
async def set_prices(body: Prices):
    """The play-time options on the pay page. Checkouts already started keep their price."""
    new = {}
    for o in body.options:
        if not 5 <= o.minutes <= 600:
            raise HTTPException(400, "Each option must be between 5 minutes and 10 hours.")
        if not 100 <= o.sen <= 100_000:
            raise HTTPException(400, "Prices must be between RM1 and RM1,000.")
        if o.minutes in new:
            raise HTTPException(400, f"{duration(o.minutes)} is listed twice.")
        new[o.minutes] = o.sen
    if not 1 <= len(new) <= 6:
        raise HTTPException(400, "Have between 1 and 6 options.")
    old = dict(options)
    set_options(new)
    changes = [f"{duration(m)} {rm(old[m])} → {rm(sen)}" for m, sen in options.items() if m in old and old[m] != sen]
    changes += [f"added {duration(m)} {rm(sen)}" for m, sen in options.items() if m not in old]
    changes += [f"removed {duration(m)}" for m in old if m not in options]
    if changes:
        db.log_staff_action("", "settings", detail="prices: " + "; ".join(changes))
    return {"prices": [{"minutes": m, "sen": sen} for m, sen in options.items()]}


class PaymentMode(BaseModel):
    sandbox: bool


@router.post("/payment-mode", dependencies=[Depends(require_admin)])
async def set_payment_mode(body: PaymentMode):
    """Switch new checkouts between CHIP's sandbox (test payments) and production. Checkouts
    already started finish in the mode they began in."""
    name, env = ("sandbox", "CHIP_SANDBOX_SECRET_KEY") if body.sandbox else ("production", "CHIP_SECRET_KEY")
    if not chip.secret_key(body.sandbox):
        raise HTTPException(400, f"There's no {name} key yet. Add {env} to .env and restart.")
    try:  # a wrong key would stop every customer paying, so check CHIP accepts it first
        offered_methods[body.sandbox] = await asyncio.wait_for(chip.available_methods(body.sandbox), 15)
    except Exception as e:
        raise HTTPException(409, f"CHIP didn't accept the {name} key in .env "
                                 f"({str(e)[:120] or type(e).__name__}). Check {env} and CHIP_BRAND_ID.")
    if body.sandbox != sandbox():
        set_sandbox(body.sandbox)
        db.log_staff_action("", "settings", detail=f"payments switched to {name}")
    return {"sandbox": sandbox()}


class PromoIn(BaseModel):
    code: str
    percent_off: int = 0
    sen_off: int = 0
    max_uses: int | None = None  # None = no limit


@router.post("/promos", dependencies=[Depends(require_admin)])
async def create_promo(body: PromoIn):
    code = clean_code(body.code)
    if not re.fullmatch(r"[A-Z0-9]{3,20}", code):
        raise HTTPException(400, "Codes are 3 to 20 letters and numbers, e.g. WELCOME10.")
    if bool(body.percent_off) == bool(body.sen_off):
        raise HTTPException(400, "Give the discount as a percentage or an amount in RM.")
    if body.percent_off and not 1 <= body.percent_off <= 100:
        raise HTTPException(400, "A percentage discount must be between 1% and 100%.")
    if body.sen_off and not 10 <= body.sen_off <= 100_000:
        raise HTTPException(400, "An RM discount must be between RM0.10 and RM1,000.")
    if body.max_uses is not None and not 1 <= body.max_uses <= 100_000:
        raise HTTPException(400, "Uses must be between 1 and 100,000, or blank for no limit.")
    if any(p["code"] == code for p in db.promo_codes()):
        raise HTTPException(400, f"There's already a code {code}.")
    db.add_promo(code, body.percent_off, body.sen_off, body.max_uses)
    discount = promo_label({"percent_off": body.percent_off, "sen_off": body.sen_off})
    uses = f"{body.max_uses} use{'s' if body.max_uses != 1 else ''}" if body.max_uses else "no limit"
    db.log_staff_action("", "settings", detail=f"promo code {code} added ({discount}, {uses})")
    return {"code": code, "label": discount}


@router.post("/promos/{code}/remove", dependencies=[Depends(require_admin)])
async def delete_promo(code: str):
    """Customers can't use it any more; checkouts already started with it keep their price."""
    if not db.remove_promo(code):
        raise HTTPException(404, "Unknown promo code")
    db.log_staff_action("", "settings", detail=f"promo code {code} removed")
    return {"ok": True}


@router.post("/purchases/{purchase_id}/recheck", dependencies=[Depends(require_admin)])
async def recheck(purchase_id: str):
    """For "I paid but nothing happened": ask CHIP again about this checkout."""
    if db.get_purchase(purchase_id) is None:
        raise HTTPException(404, "Unknown purchase")
    try:
        credited = await asyncio.wait_for(credit_if_paid(purchase_id), 15)
    except Exception:
        raise HTTPException(502, "Couldn't reach CHIP. Try again in a moment.")
    return {"credited": credited}
