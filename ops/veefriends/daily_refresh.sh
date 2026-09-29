#!/bin/bash
# daily_refresh.sh -- VeeFriends external comps, once a day (Andy approved 2026-09-27).
#   1. eBay: signed-in VeeFriends sold pull since 2 days ago (attaches to the Chrome that sign_in.sh opened)
#   2. import the new rows into Neon (dedup-safe: ON CONFLICT DO NOTHING)
#   3. publish: re-link every VeeFriends sale (incl. Fanatics, which the sources_supervisor refreshes every 6 h)
# Touches only the home dir + Neon (launchd cannot read the 6TB volume).
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
LOG=$HOME/mazi_veefriends/logs/daily_refresh.log
LOCK=$HOME/mazi_veefriends/.daily_refresh.lock
ts() { date -u +%FT%TZ; }
mkdir "$LOCK" 2>/dev/null || { echo "[$(ts)] another run holds $LOCK -- skip" >> "$LOG"; exit 0; }
trap 'rmdir "$LOCK"' EXIT
cd "$HOME/whatnot-sniper" || exit 1
set -a; . ./.env.external_comps_bridge; set +a
export MAZI_DB_NO_POOL=1 PYTHONPATH=$HOME/whatnot-sniper
FLOOR=$(date -v-4d +%F)   # eBay indexes sold items late; the import dedups
echo "[$(ts)] start floor=$FLOOR" >> "$LOG"
if curl -s --max-time 5 http://127.0.0.1:9333/json/version >/dev/null; then
  /usr/bin/python3 tools/ebay_veefriends_pilot.py --operator-approved --floor "$FLOOR" --max-pages 12 >> "$LOG" 2>&1; rc=$?
  echo "[$(ts)] ebay pull rc=$rc" >> "$LOG"
  F=$(ls -t "$HOME"/mazi_veefriends/ebay_pilot/sold_2026*.jsonl 2>/dev/null | head -1)
  if [ "$rc" -eq 0 ] && [ -n "$F" ]; then
    /usr/bin/python3 tools/import_ebay_veefriends_pilot.py --file "$F" --commit >> "$LOG" 2>&1; rc=$?
    echo "[$(ts)] import rc=$rc" >> "$LOG"
  fi
else
  echo "[$(ts)] WARNING: signed-in eBay Chrome is not open (127.0.0.1:9333) -- eBay step skipped; run ~/mazi_ebay_session/sign_in.sh" >> "$LOG"
fi
/usr/bin/python3 tools/veefriends_link.py publish --since 2025-01-01 >> "$LOG" 2>&1; rc=$?
echo "[$(ts)] publish rc=$rc -- done" >> "$LOG"
