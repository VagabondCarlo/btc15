#!/bin/bash
# One-time setup on a Raspberry Pi (Pi OS Bookworm or newer, 64-bit).
# Run from the repo root:  bash deploy/pi-setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== checking the clock =="
# This matters more here than anywhere else. A Pi has no battery-backed
# clock, so it boots thinking it is 1970 until the network comes up. The
# settlement average is taken over exact one-second slots, and signed API
# requests are rejected if the timestamp is more than 30 seconds out.
sudo timedatectl set-ntp true
timedatectl status | grep -E "System clock|NTP service"

echo "== packages =="
sudo apt-get update -qq
# numpy from apt is a prebuilt wheel. Installing it with pip compiles from
# source on a Pi and takes the better part of an hour.
sudo apt-get install -y python3-venv python3-numpy sqlite3

echo "== virtualenv =="
python3 -m venv --system-site-packages .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q requests cryptography pytest
./.venv/bin/python -c "import numpy; print('numpy', numpy.__version__)"

echo "== tests =="
PYTHONPATH=. ./.venv/bin/python -m pytest tests/ -q

echo
echo "Setup done. To run under systemd:"
echo "  sudo cp deploy/*.service /etc/systemd/system/"
echo "  sudo sed -i \"s|/home/pi/btc15|$(pwd)|\" /etc/systemd/system/btc15-*.service"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now btc15-recorder"
echo "  journalctl -u btc15-recorder -f"
