"""Start Playtime: the web server, plus a Cloudflare quick tunnel while there's no
permanent public address yet (PUBLIC_URL in .env is empty, localhost, or a
*.trycloudflare.com address). The tunnel's new address is written into PUBLIC_URL.

    python run.py              # decide automatically
    python run.py --tunnel     # always start a quick tunnel
    python run.py --no-tunnel  # server only (e.g. you have your own domain)
"""

import argparse
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def read_public_url(env: Path = ENV) -> str:
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("PUBLIC_URL="):
            return line.split("=", 1)[1].strip()
    return ""


def write_public_url(url: str, env: Path = ENV):
    lines = env.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("PUBLIC_URL="):
            lines[i] = f"PUBLIC_URL={url}\n"
            break
    else:
        lines.append(f"\nPUBLIC_URL={url}\n")
    env.write_text("".join(lines), encoding="utf-8")


def needs_quick_tunnel(url: str) -> bool:
    return not url or "localhost" in url or "127.0.0.1" in url or "trycloudflare.com" in url


def find_cloudflared() -> str:
    for candidate in (shutil.which("cloudflared"),
                      r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                      r"C:\Program Files\cloudflared\cloudflared.exe"):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("cloudflared isn't installed. On Windows: winget install --id Cloudflare.cloudflared")


def start_quick_tunnel(port: int) -> tuple[subprocess.Popen, str]:
    print("Starting Cloudflare quick tunnel...")
    proc = subprocess.Popen(
        [find_cloudflared(), "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    for line in proc.stdout:
        match = QUICK_URL.search(line)
        if match:
            # Keep reading its output in the background so the pipe never fills up.
            threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
            return proc, match.group(0)
    sys.exit("cloudflared stopped before giving a tunnel address. Is the internet connection up?")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tunnel", action="store_true", help="always start a quick tunnel")
    group.add_argument("--no-tunnel", action="store_true", help="server only")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if not ENV.exists():
        sys.exit("No .env file yet: copy .env.example to .env and fill it in.")

    url = read_public_url()
    if url and "://" not in url:
        url = "https://" + url  # same as the app does
    tunnel = None
    if args.tunnel or (not args.no_tunnel and needs_quick_tunnel(url)):
        tunnel, url = start_quick_tunnel(args.port)
        write_public_url(url)

    print()
    print(f"  Public address : {url}")
    print(f"  Admin panel    : http://localhost:{args.port}/admin   (or {url}/admin)")
    print(f"  QR codes       : {url}/print")
    if tunnel:
        print()
        print("  Quick tunnel: this address changes every time you start. Reopen the station")
        print("  screens (/d/...) with the new address, and don't print QR codes from it.")
    print()
    print("  Press Ctrl+C to stop.")
    print()

    try:
        # A fresh process, so the app reads the PUBLIC_URL just written to .env.
        subprocess.run([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(args.port)], cwd=BASE)
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel:
            tunnel.terminate()


if __name__ == "__main__":
    main()
