import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve files against the project folder, not wherever the server was started from.
BASE_DIR = Path(__file__).resolve().parent.parent
# PLAYTIME_ENV_FILE picks another settings file, so the demo and tests never read your real .env.
load_dotenv(BASE_DIR / os.environ.get("PLAYTIME_ENV_FILE", ".env"))


def _parse_stations(raw: str) -> dict[str, str]:
    """'switch1=192.168.1.50,switch2=192.168.1.51' -> {'switch1': '192.168.1.50', ...}"""
    stations = {}
    for item in raw.split(","):
        if item.strip():
            name, ip = item.split("=", 1)
            stations[name.strip()] = ip.strip()
    return stations


SHOP_NAME = os.environ.get("SHOP_NAME", "Playtime")

CHIP_API = "https://gate.chip-in.asia/api/v1"
CHIP_SECRET_KEY = os.environ["CHIP_SECRET_KEY"]
CHIP_BRAND_ID = os.environ["CHIP_BRAND_ID"]
# CHIP requires a client email on every purchase; receipts go here.
CHIP_CLIENT_EMAIL = os.environ["CHIP_CLIENT_EMAIL"]
# Optional: open checkout on one payment method, e.g. "duitnow_qr" or "fpx".
CHIP_PREFERRED_METHOD = os.environ.get("CHIP_PREFERRED_METHOD", "")
# Optional: only offer these payment methods, comma separated, e.g. "duitnow_qr,fpx,razer_tng"
# (codes from CHIP's GET /payment_methods/). Empty = everything your CHIP account offers.
CHIP_PAYMENT_METHODS = [m.strip().lower() for m in os.environ.get("CHIP_PAYMENT_METHOD", "").split(",") if m.strip()]

# Public https URL CHIP can reach (CHIP rejects URLs with an explicit port).
# "play.example.com" is accepted too: https:// is added, since QR codes and CHIP need it.
PUBLIC_URL = os.environ["PUBLIC_URL"].strip().rstrip("/")
if PUBLIC_URL and "://" not in PUBLIC_URL:
    PUBLIC_URL = "https://" + PUBLIC_URL

TAPO_USERNAME = os.environ.get("TAPO_USERNAME", "")
TAPO_PASSWORD = os.environ.get("TAPO_PASSWORD", "")
# true = pretend to switch plugs (for trying the flow without hardware).
PLUG_DRY_RUN = os.environ.get("PLUG_DRY_RUN", "false").lower() == "true"
# Optional: stations to create on the very first run. Normally you add them in the admin panel.
STATIONS = _parse_stations(os.environ.get("STATIONS", ""))

PRICE_PER_HOUR_SEN = int(os.environ.get("PRICE_PER_HOUR_SEN", "1000"))  # RM10.00, shown as "per hour"
MAX_HOURS = int(os.environ.get("MAX_HOURS", "3"))


def parse_options(raw: str) -> dict[int, int]:
    """'30:500,60:1000' -> {30: 500, 60: 1000} (minutes -> price in sen)"""
    options = {}
    for item in raw.split(","):
        if item.strip():
            minutes, sen = item.split(":")
            options[int(minutes)] = int(sen)
    return dict(sorted(options.items()))


# What customers can buy, as minutes:price_in_sen (prices set in the admin panel override
# this). Default: 30 min at half the hourly
# price, then 1..MAX_HOURS hours at the hourly price.
PLAY_OPTIONS = parse_options(os.environ.get("PLAY_OPTIONS") or ",".join(
    [f"30:{PRICE_PER_HOUR_SEN // 2}"]
    + [f"{h * 60}:{h * PRICE_PER_HOUR_SEN}" for h in range(1, MAX_HOURS + 1)]
))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "15"))
# Admin panel password (also accepted as the X-Admin-Token header). Empty disables /admin.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# For "today" / "this month" in the admin stats. Malaysia is UTC+8 with no DST.
UTC_OFFSET_HOURS = float(os.environ.get("UTC_OFFSET_HOURS", "8"))
DB_PATH = str(BASE_DIR / os.environ.get("DB_PATH", "playtime.db"))  # absolute paths kept as-is
