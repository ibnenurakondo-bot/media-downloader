#!/usr/bin/env bash
# Android (Termux) one-time setup
set -e
cd "$(dirname "$0")"
pkg update -y
pkg install -y python ffmpeg
pip install -U -r requirements-termux.txt
echo
echo "Setup done. Start the app with:  bash termux-run.sh"
