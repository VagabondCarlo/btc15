#!/bin/bash
# Paper by default. LIVE=1 ./run_bot.sh for real orders (asks to confirm).
cd "$(dirname "$0")"
[ -f keys.env ] && set -a && . ./keys.env && set +a
mkdir -p data
exec env PYTHONPATH=. ./.venv/bin/python btc15_bot.py "$@"
