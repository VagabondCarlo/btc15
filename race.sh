#!/bin/bash
# Run both builds in paper at once on the same live market, separate books.
#   ./race.sh start    launch both
#   ./race.sh status   compare them
#   ./race.sh stop     halt both
cd "$(dirname "$0")"
mkdir -p data
BAL=${BAL:-10}
BANK=${BANK:-5}

case "${1:-status}" in
  start)
    for p in turnover income; do
      pkill -f "btc15_bot.py --db data/race_$p.db" 2>/dev/null
    done
    sleep 1
    for p in turnover income; do
      PYTHONPATH=. nohup ./.venv/bin/python btc15_bot.py \
        --db "data/race_$p.db" --profile "$p" \
        --balance "$BAL" --bankroll "$BANK" \
        > "data/race_$p.log" 2>&1 &
      echo "started $p (pid $!)"
    done
    echo "both running in paper. ./race.sh status to compare."
    ;;
  stop)
    for p in turnover income; do pkill -f "btc15_bot.py --db data/race_$p.db"; done
    echo "stopped"
    ;;
  status)
    PYTHONPATH=. ./.venv/bin/python - <<'PY'
import sqlite3, os
print(f"{'build':>10}{'running':>9}{'decisions':>11}{'trades':>8}"
      f"{'wins':>6}{'balance':>10}{'equity':>9}")
print("-"*64)
for p in ("turnover","income"):
    db=f"data/race_{p}.db"
    if not os.path.exists(db):
        print(f"{p:>10}{'no':>9}{'-':>11}"); continue
    c=sqlite3.connect(db); c.row_factory=sqlite3.Row
    d=c.execute("select count(*) n from decisions").fetchone()["n"]
    o=c.execute("""select count(*) n, sum(pnl>0) w, sum(pnl) p
                   from orders where outcome is not null""").fetchone()
    st=c.execute("select balance,risk_json from bot_state where id=1").fetchone()
    import json
    eq=json.loads(st["risk_json"]).get("equity",0) if st else 0
    run=os.popen(f"pgrep -f 'btc15_bot.py --db {db}' | head -1").read().strip()
    print(f"{p:>10}{'yes' if run else 'no':>9}{d:>11}{o['n'] or 0:>8}"
          f"{o['w'] or 0:>6}{st['balance'] if st else 0:>10.2f}{eq:>9.2f}")
print("\nlogs: data/race_turnover.log  data/race_income.log")
PY
    ;;
esac
