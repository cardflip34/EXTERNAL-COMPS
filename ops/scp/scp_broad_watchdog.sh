#!/bin/bash
# scp_broad_watchdog.sh — reboot-durable keepalive for the broad-SCP 2025 engine.
# Run by launchd (com.mazi.scp-broad-watchdog, StartInterval 300 + RunAtLoad).
# SAFE under launchd: everything here writes ONLY to the home dir (no 6TB/TCC).
# Relaunches (a) the scrape driver and (b) the rolling Neon bridge if dead, and
# writes a heartbeat JSON each pass. When all targets are done the driver exits
# immediately on relaunch (DONE>=TOTAL check) — the watchdog then just heartbeats.
D=$HOME/mazi_scp_broad
LOG=$D/watchdog.log
HB=$D/heartbeat.json
ts() { date -u +%FT%TZ; }

DRIVER_ALIVE=1
BRIDGE_ALIVE=1

# --- driver keepalive ---------------------------------------------------------
# STALL DETECTOR (added 2026-08-21): a live-but-wedged scrubber is the "loaded-but-dead"
# failure mode -- on 2026-08-21 scp_broad_scrub.py burned 89% CPU for 7h with zero log output
# while the driver sat blocked on it. Process-exists is NOT health; PROGRESS is. If the run log
# has not advanced in STALL_MIN minutes while a scrubber is alive, kill the scrubber; the driver
# loop then spawns the next chunk on its own.
STALL_MIN=${SCP_STALL_MIN:-25}
if pgrep -f "scp_broad_scrub.py" >/dev/null 2>&1 && [ -f "$D/scp_broad_run.log" ]; then
  LOG_AGE_MIN=$(( ( $(date +%s) - $(stat -f %m "$D/scp_broad_run.log") ) / 60 ))
  if [ "$LOG_AGE_MIN" -ge "$STALL_MIN" ]; then
    echo "[$(ts)] STALLED: run log idle ${LOG_AGE_MIN}m (>=${STALL_MIN}m) with scrubber alive -> killing scrubber" >> "$LOG"
    pkill -9 -f "scp_broad_scrub.py" 2>/dev/null
    sleep 2
  fi
fi

if ! pgrep -f "run_scp_broad.sh" >/dev/null 2>&1; then
  DRIVER_ALIVE=0
  echo "[$(ts)] driver DEAD -> relaunch" >> "$LOG"
  cd "$HOME/whatnot-sniper" && nohup bash "$D/run_scp_broad.sh" >/dev/null 2>&1 &
fi

# --- bridge keepalive ---------------------------------------------------------
if ! pgrep -f "bridge_scp_broad_to_neon.py" >/dev/null 2>&1; then
  BRIDGE_ALIVE=0
  echo "[$(ts)] bridge DEAD -> relaunch" >> "$LOG"
  cd "$HOME/whatnot-sniper" && nohup bash -c 'set -a && . ./.env.external_comps_bridge 2>/dev/null && set +a && export MAZI_DB_NO_POOL=1 PYTHONPATH=$HOME/whatnot-sniper && python3 tools/bridge_scp_broad_to_neon.py --jsonl '"$D"'/scp_broad_comps.jsonl --loop --interval 600 >> '"$D"'/scp_broad_bridge.log 2>&1' >/dev/null 2>&1 &
fi

# --- heartbeat -----------------------------------------------------------------
DONE=$(/usr/bin/python3 -c "import json;print(len(json.load(open('$D/scp_broad_state.json'))['done_ids']))" 2>/dev/null || echo 0)
LIVE_DONE=$(/usr/bin/python3 -c "import json;print(len(json.load(open('$D/scp_live_state.json'))['done_ids']))" 2>/dev/null || echo 0)
ROWS=$(wc -l < "$D/scp_broad_comps.jsonl" 2>/dev/null | tr -d ' ' || echo 0)
# NB: grep -c prints "0" AND exits 1 on no-match, so no "|| echo 0" (double-print).
BLOCKED=$(grep -c "BLOCKED" "$D/scp_broad_run.log" 2>/dev/null); BLOCKED=${BLOCKED:-0}
BRIDGED=$(/usr/bin/python3 -c "import json;print(json.load(open('$D/scp_broad_comps.bridge_state.json')).get('inserted',0))" 2>/dev/null || echo 0)
cat > "$HB" <<EOF
{"ts":"$(ts)","driver_alive_at_check":$DRIVER_ALIVE,"bridge_alive_at_check":$BRIDGE_ALIVE,
 "cards_done":$DONE,"live_cards_done":$LIVE_DONE,"local_rows":$ROWS,"blocked_total":$BLOCKED,"bridge_inserted_this_run":$BRIDGED}
EOF
