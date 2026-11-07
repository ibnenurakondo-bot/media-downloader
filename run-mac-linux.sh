#!/usr/bin/env bash
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Install Python 3.10+ first (https://www.python.org/downloads/)"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
echo "Installing / updating packages (first run takes a minute)..."
python -m pip install -q -U pip
python -m pip install -q -U -r requirements-local.txt
export LOCAL_MODE=1 OPEN_BROWSER=1 HOST=127.0.0.1
read -r -p "Use from your phone too (same Wi-Fi)? [y/N]: " PHONE
if [ "$PHONE" = "y" ] || [ "$PHONE" = "Y" ]; then
  export HOST=0.0.0.0
  read -r -p "Set a password for the app (or press Enter to skip): " ACCESS_CODE
  export ACCESS_CODE
fi
python app.py
