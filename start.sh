#!/bin/sh
# Start Playtime on Linux / Raspberry Pi (server, plus a quick tunnel if you have no domain yet).
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Python..."
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt || exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - fill in your CHIP keys, then run this again."
  exit 1
fi

exec .venv/bin/python run.py "$@"
