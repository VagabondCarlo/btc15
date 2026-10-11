#!/bin/bash
# Run on the Pi after setup. Proves the install works end to end, read-only.
# Places no orders and needs no API keys.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
ok(){ echo "  PASS  $1"; }
no(){ echo "  FAIL  $1"; fail=1; }

echo "btc15 install check"
echo

V=$(./.venv/bin/python -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
[ -n "$V" ] && ok "python $V in the venv" || no "venv missing: run deploy/pi-setup.sh"

./.venv/bin/python -c 'import numpy,requests,cryptography' 2>/dev/null \
  && ok "numpy, requests, cryptography import" || no "a dependency is missing"

SKEW=$(PYTHONPATH=. ./.venv/bin/python -c \
  'from btc15.state import clock_skew; print("%.1f"%clock_skew())' 2>/dev/null)
if [ -n "$SKEW" ]; then
  A=${SKEW#-}
  awk "BEGIN{exit !($A < 20)}" && ok "clock within ${SKEW}s of the venue" \
    || no "clock is ${SKEW}s out. sudo timedatectl set-ntp true"
else
  no "could not reach the venue to check the clock"
fi

PYTHONPATH=. ./.venv/bin/python - <<'PY' && ok "live market data reachable" || no "cannot reach gateway.polymarket.us"
import sys, time
from datetime import datetime, timezone
from btc15.gateway import Gateway, WINDOW_SECS, event_slug, parse_terms
try:
    g = Gateway()
    ws = int(time.time()) // WINDOW_SECS * WINDOW_SECS
    t = parse_terms(g.event(event_slug(datetime.fromtimestamp(ws, timezone.utc))))
    assert t and t["market_slug"]
    c = g.index_candles(int(time.time()) - 10, int(time.time()))
    assert c
except Exception as e:
    print(f"        {e}", file=sys.stderr); sys.exit(1)
PY

PYTHONPATH=. ./.venv/bin/python - <<'PY' && ok "settlement reconstruction matches the venue" || no "reconstruction does not match"
import sys, time
from datetime import datetime, timezone
from btc15.gateway import Gateway, WINDOW_SECS, event_slug, parse_terms, twap_at
try:
    g = Gateway()
    ws = (int(time.time()) // WINDOW_SECS - 1) * WINDOW_SECS
    t = parse_terms(g.event(event_slug(datetime.fromtimestamp(ws, timezone.utc))))
    pr = g.index_range(ws - 70, ws + 5)
    ours, n = twap_at(pr, ws)
    assert n == 60 and abs(ours - t["price_to_beat"]) < 0.05, \
        f"ours {ours} vs theirs {t['price_to_beat']} from {n} prints"
except Exception as e:
    print(f"        {e}", file=sys.stderr); sys.exit(1)
PY

PYTHONPATH=. ./.venv/bin/python -m pytest tests/ -q >/dev/null 2>&1 \
  && ok "all tests pass" || no "tests fail: run pytest tests/ -q to see why"

if [ -f keys.env ]; then
  P=$(stat -c %a keys.env 2>/dev/null || stat -f %Lp keys.env 2>/dev/null)
  [ "$P" = "600" ] && ok "keys.env is owner-only" \
    || no "keys.env is mode $P. run: chmod 600 keys.env"
else
  echo "  ----  keys.env not present yet (fine until you go live)"
fi

echo
if [ $fail -eq 0 ]; then
  echo "Ready. Start the recorder with:"
  echo "  sudo systemctl enable --now btc15-recorder"
  echo "Then paper-trade with:"
  echo "  ./run_bot.sh --balance 10 --bankroll 5"
else
  echo "Fix the failures above before going further."
fi
exit $fail
