"""Check that this machine can control a Tapo plug, outside the app.

    python check_plug.py 192.168.1.50          # show status
    python check_plug.py 192.168.1.50 on|off   # switch it

Uses the Tapo login saved in the admin panel (or TAPO_USERNAME / TAPO_PASSWORD in .env).
"""

import asyncio
import sys

from tapo import ApiClient

from app import config, db


async def main(ip: str, action: str | None):
    db.init()
    username = db.get_setting("tapo_username") or config.TAPO_USERNAME
    password = db.get_setting("tapo_password") or config.TAPO_PASSWORD
    if not (username and password):
        sys.exit("No Tapo login yet: add it in the admin panel under Settings > Tapo account.")
    device = await ApiClient(username, password).p110(ip)
    if action == "on":
        await device.on()
    elif action == "off":
        await device.off()
    info = await device.get_device_info()
    print(f"{info.nickname} @ {ip}: {'ON' if info.device_on else 'OFF'}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or (len(sys.argv) > 2 and sys.argv[2] not in ("on", "off")):
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
