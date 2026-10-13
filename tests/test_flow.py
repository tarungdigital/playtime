"""End-to-end flow with CHIP's API faked and plugs in dry-run mode."""

import base64
import json
import os
import tempfile
import time
from urllib.parse import urlsplit

os.environ.update({
    "PLAYTIME_ENV_FILE": "no-such-env-file",  # never read the real .env
    "CHIP_SECRET_KEY": "test", "CHIP_BRAND_ID": "brand", "CHIP_CLIENT_EMAIL": "t@example.com",
    "PUBLIC_URL": "https://play.example.com", "PLUG_DRY_RUN": "true",
    "STATIONS": "switch1=10.0.0.1,switch2=10.0.0.2", "ADMIN_TOKEN": "secret",
    "CHECK_INTERVAL_SECONDS": "3600",
    "DB_PATH": os.path.join(tempfile.mkdtemp(), "test.db"),
})

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app import chip, config, core, db, main, plug

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
IP = "10.0.0.1"


@pytest.fixture
def chip_purchases(monkeypatch):
    """Fake CHIP: purchase id -> purchase object."""
    purchases = {}
    counter = iter(range(1, 1000))

    async def create_purchase(station, minutes, amount_sen, product_name):
        pid = f"p{next(counter)}"
        purchases[pid] = {"id": pid, "status": "created", "purchase": {"total": amount_sen}}
        return {"id": pid, "checkout_url": f"https://gate.chip-in.asia/p/{pid}/"}

    async def get_purchase(pid):
        return purchases[pid]

    monkeypatch.setattr(chip, "create_purchase", create_purchase)
    monkeypatch.setattr(chip, "get_purchase", get_purchase)
    monkeypatch.setattr(chip, "_public_key", KEY.public_key())
    monkeypatch.setattr(chip, "_public_key_fetched_at", time.time())
    return purchases


@pytest.fixture
def client(chip_purchases):
    if os.path.exists(os.environ["DB_PATH"]):
        os.remove(os.environ["DB_PATH"])
    plug._dry_run_state.clear()
    plug.set_credentials(config.TAPO_USERNAME, config.TAPO_PASSWORD)
    with TestClient(main.app) as c:  # startup seeds stations from STATIONS into the fresh DB
        yield c


def buy(client, chip_purchases, minutes=60, station="switch1"):
    r = client.post(f"/s/{station}/pay", data={"minutes": minutes}, follow_redirects=False)
    assert r.status_code == 303
    return urlsplit(r.headers["location"]).path.rstrip("/").split("/")[-1]


def send_callback(client, purchase, sign=True):
    body = json.dumps({**purchase, "event_type": "purchase.paid"}).encode()
    sig = KEY.sign(body, padding.PKCS1v15(), hashes.SHA256()) if sign else b"junk"
    return client.post("/chip/callback", content=body,
                       headers={"X-Signature": base64.b64encode(sig).decode()})


def run_sync(client, station="switch1"):
    client.portal.call(core.sync_plug, station)


def remaining(client, station="switch1"):
    return client.get(f"/api/stations/{station}").json()["remaining_seconds"]


def test_pay_redirects_to_chip_checkout(client, chip_purchases):
    pid = buy(client, chip_purchases, minutes=120)
    assert db.get_purchase(pid)["amount_sen"] == 2000
    assert db.get_purchase(pid)["status"] == "created"


def test_pay_rejects_unknown_options(client):
    assert client.post("/s/switch1/pay", data={"minutes": 45}).status_code == 400
    assert client.post("/s/switch1/pay", data={"minutes": 540}).status_code == 400
    assert client.post("/s/nope/pay", data={"minutes": 60}).status_code == 404


def test_half_hour_option_costs_rm5_and_adds_30_minutes(client, chip_purchases):
    pid = buy(client, chip_purchases, minutes=30)
    assert db.get_purchase(pid)["amount_sen"] == 500
    chip_purchases[pid]["status"] = "paid"
    send_callback(client, chip_purchases[pid])
    assert 1790 < remaining(client) <= 1800
    page = client.get("/s/switch1").text
    assert "30 min" in page and "RM5" in page


def test_bad_signature_rejected(client, chip_purchases):
    pid = buy(client, chip_purchases)
    chip_purchases[pid]["status"] = "paid"
    assert send_callback(client, chip_purchases[pid], sign=False).status_code == 401
    assert remaining(client) == 0


def test_paid_callback_credits_once_and_turns_plug_on(client, chip_purchases):
    pid = buy(client, chip_purchases)
    chip_purchases[pid]["status"] = "paid"
    assert send_callback(client, chip_purchases[pid]).status_code == 200
    assert send_callback(client, chip_purchases[pid]).status_code == 200  # duplicate delivery
    assert 3590 < remaining(client) <= 3600
    run_sync(client)
    assert plug._dry_run_state[IP] is True


def test_unpaid_callback_ignored(client, chip_purchases):
    pid = buy(client, chip_purchases)
    # Signed, but CHIP's API says it isn't paid (e.g. a purchase.created event).
    assert send_callback(client, chip_purchases[pid]).status_code == 200
    assert remaining(client) == 0


def test_amount_mismatch_not_credited(client, chip_purchases):
    pid = buy(client, chip_purchases, minutes=180)
    chip_purchases[pid].update(status="paid", purchase={"total": 1000})
    send_callback(client, chip_purchases[pid])
    assert remaining(client) == 0


def test_second_purchase_extends_running_session(client, chip_purchases):
    for minutes in (60, 120):
        pid = buy(client, chip_purchases, minutes=minutes)
        chip_purchases[pid]["status"] = "paid"
        send_callback(client, chip_purchases[pid])
    state = client.get("/api/stations/switch1").json()
    assert 3 * 3600 - 10 < state["remaining_seconds"] <= 3 * 3600
    assert state["total_seconds"] == pytest.approx(3 * 3600, abs=2)


def test_return_from_checkout_credits_without_callback(client, chip_purchases):
    pid = buy(client, chip_purchases)
    chip_purchases[pid]["status"] = "paid"
    assert client.get("/s/switch1?paid=1").status_code == 200
    assert remaining(client) > 3500


def test_plug_forced_off_without_session(client):
    plug._dry_run_state[IP] = True  # someone pressed the plug's button
    run_sync(client)
    assert plug._dry_run_state[IP] is False


def test_plug_turns_off_when_time_runs_out(client, chip_purchases):
    client.post("/admin/stations/switch1/add?minutes=1", headers={"X-Admin-Token": "secret"})
    assert plug._dry_run_state[IP] is True
    with db._conn() as c:
        c.execute("UPDATE stations SET ends_at = ? WHERE station = 'switch1'", (time.time() - 1,))
    run_sync(client)
    assert plug._dry_run_state[IP] is False


def test_admin_requires_token(client):
    assert client.post("/admin/stations/switch1/add").status_code == 403
    assert client.post("/admin/stations/switch1/add", headers={"X-Admin-Token": "wrong"}).status_code == 403
    r = client.post("/admin/stations/switch1/stop", headers={"X-Admin-Token": "secret"})
    assert r.status_code == 200 and r.json()["active"] is False


def test_admin_login_and_dashboard(client):
    assert "Staff login" in client.get("/admin").text
    assert client.get("/admin/api/summary").status_code == 403
    assert client.post("/admin/login", data={"password": "nope"}).status_code == 401
    r = client.post("/admin/login", data={"password": "secret"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Revenue, last 7 days" in client.get("/admin").text  # cookie kept by the client
    assert client.post("/admin/stations/switch1/add?minutes=30").status_code == 200
    client.post("/admin/logout")
    assert client.get("/admin/api/summary").status_code == 403


def test_admin_summary_counts_payments_and_staff_actions(client, chip_purchases):
    for minutes in (60, 120):
        pid = buy(client, chip_purchases, minutes=minutes)
        chip_purchases[pid]["status"] = "paid"
        send_callback(client, chip_purchases[pid])
    buy(client, chip_purchases, station="switch2")  # abandoned checkout
    auth = {"X-Admin-Token": "secret"}
    client.post("/admin/stations/switch2/add?minutes=15", headers=auth)
    s = client.get("/admin/api/summary", headers=auth).json()
    assert s["stats"]["today_sen"] == 3000 and s["stats"]["today_count"] == 2
    assert s["stats"]["week_sen"] == 3000 and s["days"][-1]["sen"] == 3000
    assert [a["kind"] for a in s["activity"]] == ["add", "payment", "payment"]
    assert [p["station"] for p in s["pending"]] == ["switch2"]


def test_admin_recheck_credits_paid_checkout(client, chip_purchases):
    pid = buy(client, chip_purchases)
    chip_purchases[pid]["status"] = "paid"  # callback never arrived
    r = client.post(f"/admin/purchases/{pid}/recheck", headers={"X-Admin-Token": "secret"})
    assert r.json() == {"credited": True}
    assert remaining(client) > 3500


ADMIN = {"X-Admin-Token": "secret"}


def edit(client, station, **body):
    return client.post(f"/admin/stations/{station}", json=body, headers=ADMIN)


def test_admin_renames_station_and_changes_plug_ip(client):
    r = client.post("/admin/plugs/check", json={"ip": "10.0.0.9"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["nickname"] == "Simulated plug"
    r = edit(client, "switch1", name="  Front   TV ", ip=" 10.0.0.9 ")
    assert r.status_code == 200 and r.json()["name"] == "Front TV" and r.json()["ip"] == "10.0.0.9"
    assert core.stations["switch1"] == {"name": "Front TV", "ip": "10.0.0.9"}
    # The id (and so the printed QR code link) is unchanged; pages show the new name.
    assert "Front TV" in client.get("/s/switch1").text and "Front TV" in client.get("/d/switch1").text
    s = client.get("/admin/api/summary", headers=ADMIN).json()
    assert s["stations"][0]["label"] == "Front TV" and s["stations"][0]["ip"] == "10.0.0.9"
    assert s["activity"][0]["kind"] == "settings"
    assert s["activity"][0]["detail"] == "renamed Switch1 → Front TV; plug IP 10.0.0.1 → 10.0.0.9"


def test_admin_rejects_bad_names_and_ips(client, monkeypatch):
    assert edit(client, "switch1", name="Switch1", ip="not-an-ip").status_code == 400
    assert edit(client, "switch1", name="Switch1", ip="10.0.0.2").status_code == 400  # switch2's plug
    assert edit(client, "switch1", name="switch2", ip="10.0.0.1").status_code == 400  # switch2's name
    assert edit(client, "switch1", name="   ", ip="10.0.0.1").status_code == 400

    async def no_answer(ip, login=None):
        raise TimeoutError

    monkeypatch.setattr(plug, "identify", no_answer)
    assert edit(client, "switch1", name="Switch1", ip="10.0.0.9").status_code == 409
    assert core.stations["switch1"]["ip"] == "10.0.0.1"
    assert edit(client, "switch1", name="Switch1", ip="10.0.0.9", force=True).status_code == 200
    assert core.stations["switch1"]["ip"] == "10.0.0.9"


def test_admin_adds_and_removes_stations(client, chip_purchases):
    r = client.post("/admin/stations", json={"name": "Switch 3", "ip": "10.0.0.3"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["station"] == "switch-3"
    assert "Switch 3" in client.get("/s/switch-3").text
    assert [s["station"] for s in client.get("/admin/api/summary", headers=ADMIN).json()["stations"]] == \
        ["switch1", "switch2", "switch-3"]
    # A station with a session running can't be removed.
    client.post("/admin/stations/switch-3/add?minutes=10", headers=ADMIN)
    assert client.post("/admin/stations/switch-3/remove", headers=ADMIN).status_code == 400
    client.post("/admin/stations/switch-3/stop", headers=ADMIN)
    assert client.post("/admin/stations/switch-3/remove", headers=ADMIN).status_code == 200
    assert client.get("/s/switch-3").status_code == 404
    assert plug._dry_run_state["10.0.0.3"] is False
    # Ids are never reused, so old payments and logs can't get mixed up with a new station.
    r = client.post("/admin/stations", json={"name": "Switch 3", "ip": "10.0.0.3"}, headers=ADMIN)
    assert r.json()["station"] == "switch-3-2"


def test_stations_survive_restart(client):
    edit(client, "switch1", name="Front TV", ip="10.0.0.9")
    client.post("/admin/stations", json={"name": "Back TV", "ip": "10.0.0.4"}, headers=ADMIN)
    core.stations.clear()
    with TestClient(main.app):  # startup reloads stations from the database, not .env
        pass
    assert core.stations == {
        "switch1": {"name": "Front TV", "ip": "10.0.0.9"},
        "switch2": {"name": "Switch2", "ip": "10.0.0.2"},
        "back-tv": {"name": "Back TV", "ip": "10.0.0.4"},
    }


def test_tapo_login_saved_and_password_never_sent_to_browser(client):
    r = client.post("/admin/tapo-account", json={"username": "me@example.com", "password": "s3cret-Pw"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["simulated"] is True
    assert plug.credentials() == ("me@example.com", "s3cret-Pw")
    summary = client.get("/admin/api/summary", headers=ADMIN)
    assert summary.json()["tapo"] == {"username": "me@example.com", "password_set": True, "simulated": True}
    assert "s3cret-Pw" not in summary.text and "s3cret-Pw" not in client.get("/admin", headers=ADMIN).text
    # Blank password keeps the saved one; the login also survives a restart.
    client.post("/admin/tapo-account", json={"username": "new@example.com"}, headers=ADMIN)
    plug.set_credentials("", "")
    with TestClient(main.app):
        pass
    assert plug.credentials() == ("new@example.com", "s3cret-Pw")


def set_prices(client, *pairs):
    return client.post("/admin/prices", json={"options": [{"minutes": m, "sen": s} for m, s in pairs]},
                       headers=ADMIN)


def test_admin_sets_prices(client, chip_purchases):
    r = set_prices(client, (90, 1700), (30, 600), (60, 1200))
    assert r.status_code == 200
    assert r.json()["prices"] == [{"minutes": 30, "sen": 600}, {"minutes": 60, "sen": 1200},
                                  {"minutes": 90, "sen": 1700}]
    page = client.get("/s/switch1").text
    assert "1 hr 30 min" in page and "RM17" in page and "3 hours" not in page
    assert "RM12</b> per hour" in client.get("/d/switch1").text  # screens show the 1-hour price
    pid = buy(client, chip_purchases, minutes=90)
    assert db.get_purchase(pid)["amount_sen"] == 1700
    assert client.post("/s/switch1/pay", data={"minutes": 180}).status_code == 400  # removed option
    log = client.get("/admin/api/summary", headers=ADMIN).json()["activity"][0]
    assert log["detail"] == ("prices: 30 min RM5 → RM6; 1 hour RM10 → RM12; added 1 hr 30 min RM17; "
                             "removed 2 hours; removed 3 hours")
    core.options.clear()
    with TestClient(main.app):  # prices survive a restart
        pass
    assert core.options == {30: 600, 60: 1200, 90: 1700}


def test_admin_rejects_bad_prices(client):
    assert set_prices(client).status_code == 400  # none
    assert set_prices(client, (60, 50)).status_code == 400  # below RM1
    assert set_prices(client, (60, 1000), (60, 1200)).status_code == 400  # duplicate
    assert set_prices(client, *[(m, 1000) for m in (15, 30, 45, 60, 90, 120, 180)]).status_code == 400  # > 6
    assert set_prices(client, (2, 1000)).status_code == 400  # too short
    assert core.options == {30: 500, 60: 1000, 120: 2000, 180: 3000}


def test_pages_render(client):
    assert "Switch1" in client.get("/s/switch1").text
    assert client.get("/s/nope").status_code == 404
    assert "<svg" in client.get("/print").text
    assert "Switch2" in client.get("/").text
    display = client.get("/d/switch2").text
    assert "Scan to play" in display and display.count('class="segno"') == 2
    assert client.get("/d/nope").status_code == 404
    old = client.get("/station/switch2", follow_redirects=False)
    assert old.status_code == 301 and old.headers["location"] == "/d/switch2"
