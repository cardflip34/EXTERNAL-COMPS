#!/bin/bash
# scp_health_check.sh — read-only health check for the dual-lane SCP engine.
# Prints ONE alert line per problem, or nothing at all when healthy. Designed to be
# driven by a remote Monitor over ssh: keeping the logic HERE (not in the monitor's
# shell) avoids local shell-quoting quirks -- e.g. zsh does not word-split unquoted
# `set -- $OUT`, which silently broke the previous monitor.
D=$HOME/mazi_scp_broad

LAST=$(grep -n "=== attempt" "$D/scp_broad_run.log" 2>/dev/null | tail -1 | cut -d: -f1)
[ -z "$LAST" ] && LAST=1
BLK=$(tail -n +"$LAST" "$D/scp_broad_run.log" 2>/dev/null | grep -c BLOCKED)
LIVE=$(python3 -c "import json;print(len(json.load(open('$D/scp_live_state.json'))['done_ids']))" 2>/dev/null || echo 0)
# Match the REAL process (an interpreter running the script), never a shell or tool whose
# command line merely MENTIONS the name. 2026-09-26: `pgrep -f bridge_scp_broad_to_neon.py` matched
# an agent's `sed ... bridge_scp_broad_to_neon.py`, so a dead bridge was not relaunched; and the old
# stall-kill `pkill -9 -f scp_broad_scrub.py` would SIGKILL any shell that mentioned the scrub.
BRIDGE_RE='^[^ ]*[Pp]ython[^ ]* ([^ ]*/)?tools/bridge_scp_broad_to_neon\.py( |$)'
DRIVER_RE='^(/bin/)?bash [^ ]*/run_scp_broad\.sh$'
SCRUB_RE='^[^ ]*[Pp]ython[^ ]* ([^ ]*/)?tools/scp_broad_scrub\.py( |$)'
DRV=$(pgrep -f "$DRIVER_RE" | wc -l | tr -d ' ')
BRG=$(pgrep -f "$BRIDGE_RE" | wc -l | tr -d ' ')
AGE=$(python3 -c "
import json,datetime
t=json.load(open('$D/heartbeat.json'))['ts'].rstrip('Z')
print(int((datetime.datetime.utcnow()-datetime.datetime.fromisoformat(t)).total_seconds()//60))
" 2>/dev/null || echo 999)

# TRUE block rate = blocks/(successes+blocks). NEVER derive this from log lines:
# scp_broad_scrub logs every block but only every 50th success, which over-reports
# blocking by ~50x (read 41% when the truth was 4%).
TOT=$((LIVE + BLK))
if [ "$TOT" -ge 60 ]; then
  R=$((100 * BLK / TOT))
  if [ "$R" -ge 30 ]; then
    echo "ALERT block rate ${R}% (${BLK} blocks / ${TOT} attempts) — CHECK TRANSPORT FIRST (jina 403 looks identical to an SCP block) before assuming eviction"
  fi
fi
[ "$DRV" -eq 0 ] && echo "ALERT run_scp_broad.sh driver DOWN (watchdog should relaunch within 5min)"
[ "$BRG" -eq 0 ] && echo "ALERT bridge_scp_broad_to_neon DOWN (rows accumulating locally, not landing in Neon)"
[ "$AGE" -ge 25 ] && echo "ALERT watchdog heartbeat ${AGE}min old (launchd agent may be unloaded)"
exit 0
