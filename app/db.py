"""SQLite state. A station's `ends_at` is the single source of truth for whether
its plug should be on, so the server can restart at any time without losing sessions."""

import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id          TEXT PRIMARY KEY,   -- CHIP purchase id
    station     TEXT NOT NULL,
    hours       INTEGER NOT NULL,   -- legacy (whole hours only); use minutes
    amount_sen  INTEGER NOT NULL,
    status      TEXT NOT NULL,      -- 'created' | 'paid'
    created_at  REAL NOT NULL,
    paid_at     REAL,
    minutes     INTEGER,
    promo_code  TEXT,               -- promo code used, if any
    discount_sen INTEGER NOT NULL DEFAULT 0,
    sandbox     INTEGER NOT NULL DEFAULT 0  -- 1 = CHIP test-mode checkout (no real money)
);
CREATE TABLE IF NOT EXISTS stations (
    station     TEXT PRIMARY KEY,
    started_at  REAL NOT NULL DEFAULT 0,
    ends_at     REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS staff_actions (
    id          INTEGER PRIMARY KEY,
    station     TEXT NOT NULL,
    action      TEXT NOT NULL,      -- 'add' | 'stop' | 'settings' ('ip' in older rows)
    minutes     INTEGER,
    at          REAL NOT NULL,
    detail      TEXT
);
-- Managed in the admin panel. `id` is in the QR code URLs (/s/<id>) so it never
-- changes; `name` is what customers see and can be renamed freely.
CREATE TABLE IF NOT EXISTS station_list (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    ip          TEXT NOT NULL,
    position    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
-- Uses are counted from `purchases`, so a code removed and created again starts at zero.
CREATE TABLE IF NOT EXISTS promo_codes (
    code        TEXT PRIMARY KEY,   -- upper case letters and digits
    percent_off INTEGER NOT NULL DEFAULT 0,  -- exactly one of percent_off / sen_off is set
    sen_off     INTEGER NOT NULL DEFAULT 0,
    max_uses    INTEGER,            -- NULL = no limit
    created_at  REAL NOT NULL
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init():
    with _conn() as c:
        c.executescript(SCHEMA)
        # Databases from before 30-minute options: add `minutes` and fill it from `hours`.
        if "minutes" not in _columns(c, "purchases"):
            c.execute("ALTER TABLE purchases ADD COLUMN minutes INTEGER")
        c.execute("UPDATE purchases SET minutes = hours * 60 WHERE minutes IS NULL")
        for column, kind in (("promo_code", "TEXT"), ("discount_sen", "INTEGER NOT NULL DEFAULT 0"),
                             ("sandbox", "INTEGER NOT NULL DEFAULT 0")):
            if column not in _columns(c, "purchases"):
                c.execute(f"ALTER TABLE purchases ADD COLUMN {column} {kind}")
        if "detail" not in _columns(c, "staff_actions"):
            c.execute("ALTER TABLE staff_actions ADD COLUMN detail TEXT")


def _columns(c, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


# --- Stations & settings ------------------------------------------------------

def seed_stations(seed: dict[str, str], name_for):
    """First run only: create the stations listed in STATIONS in .env."""
    with _conn() as c:
        if c.execute("SELECT 1 FROM station_list LIMIT 1").fetchone():
            return
        # IPs saved by an earlier version of the admin panel.
        overrides = {}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'station_ips'").fetchone():
            overrides = {r["station"]: r["ip"] for r in c.execute("SELECT station, ip FROM station_ips")}
        for position, (sid, ip) in enumerate(seed.items()):
            c.execute(
                "INSERT INTO station_list (id, name, ip, position) VALUES (?, ?, ?, ?)",
                (sid, name_for(sid), overrides.get(sid, ip), position),
            )


def station_list() -> dict[str, dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM station_list ORDER BY position, id")
        return {r["id"]: {"name": r["name"], "ip": r["ip"]} for r in rows}


def station_id_used(sid: str) -> bool:
    """True if the id belongs to a station now or in the past (payments keep old ids)."""
    with _conn() as c:
        return any(c.execute(q, (sid,)).fetchone() for q in (
            "SELECT 1 FROM station_list WHERE id = ?",
            "SELECT 1 FROM purchases WHERE station = ?",
            "SELECT 1 FROM staff_actions WHERE station = ?",
        ))


def add_station(sid: str, name: str, ip: str):
    with _conn() as c:
        position = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM station_list").fetchone()[0]
        c.execute("INSERT INTO station_list (id, name, ip, position) VALUES (?, ?, ?, ?)",
                  (sid, name, ip, position))


def update_station(sid: str, name: str, ip: str):
    with _conn() as c:
        c.execute("UPDATE station_list SET name = ?, ip = ? WHERE id = ?", (name, ip, sid))


def remove_station(sid: str):
    with _conn() as c:
        c.execute("DELETE FROM station_list WHERE id = ?", (sid,))
        c.execute("DELETE FROM stations WHERE station = ?", (sid,))


def get_setting(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                  " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --- Promo codes ------------------------------------------------------------------
# A use is a paid checkout with the code, made since the code was created. Sandbox and
# production are counted apart, so test payments don't use up a real code.

def promo_codes():
    """All promo codes, newest first, with their production `uses` and sandbox `test_uses`."""
    with _conn() as c:
        return c.execute(
            "SELECT p.*, COALESCE(SUM(u.sandbox = 0), 0) AS uses, COALESCE(SUM(u.sandbox = 1), 0) AS test_uses"
            " FROM promo_codes p LEFT JOIN purchases u ON u.promo_code = p.code"
            "  AND u.created_at >= p.created_at AND u.status = 'paid'"
            " GROUP BY p.code ORDER BY p.created_at DESC"
        ).fetchall()


def get_promo(code: str, sandbox: bool, station: str, hold_seconds: float):
    """The promo code with, in this mode, its `uses` and `held`: checkouts with it still open
    at other stations, whose customers may be paying right now. None if there's no such code."""
    with _conn() as c:
        return c.execute(
            "SELECT p.*, COALESCE(SUM(u.status = 'paid'), 0) AS uses,"
            " COALESCE(SUM(u.status = 'created' AND u.created_at > ? AND u.station != ?), 0) AS held"
            " FROM promo_codes p LEFT JOIN purchases u ON u.promo_code = p.code"
            "  AND u.created_at >= p.created_at AND u.sandbox = ?"
            " WHERE p.code = ? GROUP BY p.code",
            (time.time() - hold_seconds, station, int(sandbox), code),
        ).fetchone()


def add_promo(code: str, percent_off: int, sen_off: int, max_uses: int | None):
    with _conn() as c:
        c.execute("INSERT INTO promo_codes (code, percent_off, sen_off, max_uses, created_at)"
                  " VALUES (?, ?, ?, ?, ?)", (code, percent_off, sen_off, max_uses, time.time()))


def remove_promo(code: str) -> bool:
    with _conn() as c:
        return c.execute("DELETE FROM promo_codes WHERE code = ?", (code,)).rowcount > 0


# --- Purchases & sessions -------------------------------------------------------


def add_purchase(purchase_id: str, station: str, minutes: int, amount_sen: int, sandbox: bool,
                 promo_code: str | None = None, discount_sen: int = 0):
    with _conn() as c:
        c.execute(
            "INSERT INTO purchases (id, station, hours, minutes, amount_sen, status, created_at,"
            " promo_code, discount_sen, sandbox) VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, ?)",
            (purchase_id, station, minutes // 60, minutes, amount_sen, time.time(),
             promo_code, discount_sen, int(sandbox)),
        )


def get_purchase(purchase_id: str):
    with _conn() as c:
        return c.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()


def recent_unpaid(station: str | None = None, max_age_seconds: int = 1800, limit: int = 5):
    """Checkouts started but not (yet) confirmed paid; station=None means all stations."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM purchases WHERE status = 'created' AND created_at > ?"
            " AND (? IS NULL OR station = ?) ORDER BY created_at DESC LIMIT ?",
            (time.time() - max_age_seconds, station, station, limit),
        ).fetchall()


def paid_since(ts: float):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM purchases WHERE status = 'paid' AND paid_at >= ? ORDER BY paid_at DESC",
            (ts,),
        ).fetchall()


def log_staff_action(station: str, action: str, minutes: int | None = None, detail: str | None = None):
    with _conn() as c:
        c.execute(
            "INSERT INTO staff_actions (station, action, minutes, at, detail) VALUES (?, ?, ?, ?, ?)",
            (station, action, minutes, time.time(), detail),
        )


def recent_staff_actions(limit: int):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM staff_actions ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()


def _session(c, station: str) -> tuple[float, float]:
    row = c.execute(
        "SELECT started_at, ends_at FROM stations WHERE station = ?", (station,)
    ).fetchone()
    return (row["started_at"], row["ends_at"]) if row else (0.0, 0.0)


def _add_time(c, station: str, seconds: float) -> float:
    """Start a new session (plus warm-up time for the TV to turn on), or extend the
    running one from its current end."""
    now = time.time()
    started_at, ends_at = _session(c, station)
    if ends_at <= now:
        started_at = ends_at = now
        seconds += config.WARMUP_SECONDS
    new_end = ends_at + seconds
    c.execute(
        "INSERT INTO stations (station, started_at, ends_at) VALUES (?, ?, ?)"
        " ON CONFLICT(station) DO UPDATE SET started_at = excluded.started_at,"
        " ends_at = excluded.ends_at",
        (station, started_at, new_end),
    )
    return new_end


def session(station: str) -> tuple[float, float]:
    """(started_at, ends_at) as unix timestamps; ends_at <= now means idle."""
    with _conn() as c:
        return _session(c, station)


def add_time(station: str, seconds: float) -> float:
    with _conn() as c:
        return _add_time(c, station, seconds)


def stop(station: str):
    with _conn() as c:
        c.execute("UPDATE stations SET ends_at = 0 WHERE station = ?", (station,))


def mark_paid_and_credit(purchase_id: str) -> float | None:
    """Atomically mark a purchase paid and add its time. Returns the new end
    time, or None if it was already credited (CHIP may deliver callbacks twice)."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute(
                "SELECT * FROM purchases WHERE id = ? AND status = 'created'", (purchase_id,)
            ).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return None
            c.execute(
                "UPDATE purchases SET status = 'paid', paid_at = ? WHERE id = ?",
                (time.time(), purchase_id),
            )
            new_end = _add_time(c, row["station"], row["minutes"] * 60)
            c.execute("COMMIT")
            return new_end
        except Exception:
            c.execute("ROLLBACK")
            raise
