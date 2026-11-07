#!/usr/bin/env bash
# Android (Termux) – start the app, then open http://127.0.0.1:7860 in Chrome
cd "$(dirname "$0")"
command -v termux-wake-lock >/dev/null && termux-wake-lock
pip install -q -U yt-dlp gallery-dl || true
export LOCAL_MODE=1 HOST=127.0.0.1
python app.py
