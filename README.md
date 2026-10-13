# Playtime

Pay-per-hour console power. A customer scans the QR code at a station, pays with CHIP
(DuitNow QR, FPX, e-wallet or card), and the station's Tapo P110 plug switches on.
It switches off when the paid time runs out.

```
QR code ──> /s/switch1 (pay page + countdown)
               │ Pay RM10
               ▼
          CHIP checkout ──paid──> POST /chip/callback (signature checked)
                                       │ adds 1 h to switch1
                                       ▼
                 every 15 s: plug ON while time is left, OFF otherwise
```

## How it stays correct

- **CHIP is the source of truth.** A callback only triggers a check; the app re-fetches the
  purchase from CHIP and credits it only if its status is paid and the amount matches.
- **Each payment is credited once,** even though CHIP may send the same callback twice.
- **The paid end time is stored in SQLite,** so a restart or power cut doesn't lose sessions.
- **The app checks every plug every 15 s** and turns it on or off to match the paid time.
  That also undoes someone pressing the plug's physical button.
- **The customer doesn't have to wait for the callback.** When they return from checkout,
  the page asks CHIP directly whether they paid.

## Setup

1. **Start it:** double-click **`start.bat`** (on a Raspberry Pi: `./start.sh`). The first
   run sets up Python and creates `.env` for you to fill in:
   - CHIP secret key and brand ID: CHIP dashboard → Developers. Start with the **test** key.
   - `ADMIN_TOKEN`: a long password for the admin panel.
2. **Public address:** CHIP has to reach `/chip/callback` over https with no port number.
   While `PUBLIC_URL` is empty or a `trycloudflare.com` address, `start.bat` also starts a free
   Cloudflare quick tunnel and writes its address into `PUBLIC_URL`. That address changes every
   start, so it's for testing. For real use, set up a named Cloudflare Tunnel on your own
   domain and put that address in `PUBLIC_URL`. `start.bat` then starts only the server.
   (`python run.py --tunnel` / `--no-tunnel` overrides the choice.)
3. **Tapo:** in the Tapo app, turn on **Me → Third-Party Services → Third-Party Compatibility**,
   and give each plug a fixed IP (DHCP reservation) in your router. Then in the admin panel:
   - **Settings → Tapo account:** your Tapo app email and password. They're tested on a plug
     before saving, and stored in `playtime.db` on this PC (never shown in the browser again).
   - **Stations:** add a station per console with its plug's IP. **Check** shows which plug
     answers at that IP, so you don't switch off the wrong one.
   - Set `PLUG_DRY_RUN=false` in `.env` and restart to switch real plugs.
4. **Show the QR code:** either print the cards from `/print`, or put a tablet or monitor at
   each station showing `/d/<station>`. The display shows a big QR code while the
   station is free. When someone pays it switches to a big countdown by itself, and goes back
   to the QR code when time is up. Tap the screen once to go full screen. It keeps the screen
   awake where the browser allows it.

   Don't use the console's TV for this: its power is cut when time runs out.

## Admin panel

Open `/admin` and log in with the `ADMIN_TOKEN` password. It shows:

- **Revenue:** today (vs yesterday), last 7 days, this month, plus a daily chart.
- **Stations:** live countdown and plug status for each station, with **+15 min / +30 min /
  +1 hr** free time and **Stop** (tap twice to confirm). **Edit** renames a station or changes
  its plug IP; **+ Add station** adds one. A station's link (`/s/<id>`, `/d/<id>`) never
  changes when you rename it, so printed QR codes keep working.
- **Settings:** the Tapo account the plugs are switched with.
- **Unconfirmed checkouts:** for when a customer says "I paid but it didn't start".
  **Recheck** asks CHIP again and starts their time if the payment went through.
- **Recent activity:** payments, plus every free-time and stop action by staff.

The login lasts 30 days on that device. Changing `ADMIN_TOKEN` logs everyone out.

## Try it without hardware or CHIP keys

```
uvicorn app.main:app --env-file .env.demo
```

Plugs are simulated and the admin password is `demo`. The Pay button won't work with
the placeholder keys, so start a timer from `/admin` instead.

## Pages and endpoints

| Path | What |
|---|---|
| `/` | All stations and their status |
| `/s/{station}` | QR landing page: pick hours, pay, live countdown |
| `/d/{station}` | Display screen for a tablet/monitor at the station: big QR when free, big countdown once paid |
| `/print` | Printable QR cards |
| `/admin` | Staff admin panel |
| `/api/stations/{station}` | Station state as JSON (used by the countdown) |
| `POST /chip/callback` | CHIP success callback |
| `POST /admin/stations/{station}/add?minutes=N` | Free time (admin login or header `X-Admin-Token`) |
| `POST /admin/stations/{station}/stop` | End a session now |
| `POST /admin/purchases/{id}/recheck` | Ask CHIP again whether a checkout was paid |
| `/healthz` | Plug reachability |

## Hardware notes

- **Put the TV on the plug, not just the dock.** The Switch has its own battery, so cutting
  only the dock's power doesn't stop play. A power strip with the TV and dock on the P110
  works. Secure the console so it can't be undocked and played handheld.
- The PC running this must be on the **same network as the plugs**, and should be on 24/7
  (a mini PC or Raspberry Pi is ideal).
- If a plug stops responding, see the `tapo` library's
  [troubleshooting guide](https://github.com/mihai-dinculescu/tapo/blob/main/TROUBLESHOOTING.md).

## Tests

```
pytest
```
