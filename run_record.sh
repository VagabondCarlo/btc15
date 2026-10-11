#!/bin/bash
# Record the live order book. Trades nothing. Ctrl-C to stop.
cd "$(dirname "$0")"
[ -f keys.env ] && set -a && . ./keys.env && set +a
mkdir -p data
exec env PYTHONPATH=. ./.venv/bin/python live_record.py --db data/live.db "$@"
