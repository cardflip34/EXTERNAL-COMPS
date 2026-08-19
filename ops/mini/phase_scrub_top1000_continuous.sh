#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/stavrosaimini/whatnot-sniper"
STORE="/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store"
LOG_DIR="$STORE/logs"
LOCK="$STORE/.phase_top1000_continuous.lock"
PYTHON="/usr/bin/python3"
MAX_PER_PLAYER="${MAZI_PHASE_MAX_PER_PLAYER:-10000}"
SLEEP_SECONDS="${MAZI_PHASE_SLEEP_SECONDS:-15}"

mkdir -p "$LOG_DIR"
stamp="$(date -u +%Y%m%d_%H%M%S)"
log="$LOG_DIR/phase_top1000_continuous_${stamp}.log"
if ! { : >> "$log"; } 2>/dev/null; then
  LOG_DIR="$HOME/Library/Logs/mazi_external_comps"
  mkdir -p "$LOG_DIR"
  log="$LOG_DIR/phase_top1000_continuous_${stamp}.log"
fi
exec >> "$log" 2>&1

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] phase_top1000_continuous_start host=$(hostname) store=$STORE"

if [ ! -d "$STORE" ]; then
  echo "store_missing path=$STORE"
  exit 0
fi

if ! mkdir "$LOCK" 2>/dev/null; then
  if pgrep -f 'run_player_scrub_phase.py --phase|ebay_player_scraper.py --player' >/dev/null 2>&1; then
    echo "lock_busy path=$LOCK active_process=1"
    exit 0
  fi
  now_epoch="$(date +%s)"
  lock_epoch="$(stat -f %m "$LOCK" 2>/dev/null || echo "$now_epoch")"
  lock_age="$((now_epoch - lock_epoch))"
  if [ "$lock_age" -lt 1800 ]; then
    echo "lock_busy path=$LOCK active_process=0 age_seconds=$lock_age"
    exit 0
  fi
  echo "stale_lock_recovered path=$LOCK age_seconds=$lock_age"
  rm -rf "$LOCK"
  if ! mkdir "$LOCK" 2>/dev/null; then
    echo "lock_busy_after_stale_recovery path=$LOCK"
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

if pgrep -f 'run_player_scrub_phase.py --phase' >/dev/null 2>&1 || pgrep -f 'ebay_player_scraper.py --player' >/dev/null 2>&1; then
  echo "phase_lane_active"
  pgrep -fl 'run_player_scrub_phase.py --phase|ebay_player_scraper.py --player' || true
  exit 0
fi

phase="$("$PYTHON" - <<'PY'
import json

ledger = "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store/player_scrub_phase_ledger.json"
with open(ledger) as f:
    entries = json.load(f).get("entries", [])

for phase in range(1, 11):
    rows = [e for e in entries if int(e.get("phase") or 0) == phase]
    actionable = [
        e for e in rows
        if e.get("status") in ("pending", "failed_retry", "running")
    ]
    if actionable:
        print(phase)
        break
PY
)"

if [ -z "$phase" ]; then
  echo "all_top1000_phases_complete"
  exit 0
fi

cd "$ROOT"
export MAZI_EBAY_SCRUB_STORE="$STORE"
export MAZI_EBAY_DETAIL_SPEC=0

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] phase_${phase}_resume_start max_per_player=$MAX_PER_PLAYER sleep=$SLEEP_SECONDS"
"$PYTHON" -u run_player_scrub_phase.py --phase "$phase" --pending-only --max-per-player "$MAX_PER_PLAYER" --sleep "$SLEEP_SECONDS"
rc=$?
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] phase_${phase}_resume_finished rc=$rc"
exit "$rc"
