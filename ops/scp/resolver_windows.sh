#!/bin/bash
# resolver_windows.sh (2026-09-26, one-off) -- the slug resolver loses almost every race for the
# SCP lock to the driver's scrub (waiters poll every 20 s; the scrub re-takes it within 3.5 s).
# Give it the lock in <=12-minute windows by pausing the scrub OUTSIDE the lock. Each window
# starts right after a fresh run-log line, so the watchdog's 25-min stall timer never fires.
# The request rate is unchanged: the resolver goes through acquire_scp_budget() like everyone else.
D=$HOME/mazi_scp_broad; LOCK=$D/.scp_politeness.lock; LOG=$D/scp_broad_run.log
SC=
trap '[ -n "$SC" ] && kill -CONT $SC 2>/dev/null' EXIT
holder() { sed -n 's/^pid=\([0-9]*\).*/\1/p' "$LOCK" 2>/dev/null; }
RERUN=0
for w in $(seq 1 8); do
  if ! pgrep -f resolve_slug_targets.py >/dev/null; then
    [ $RERUN -eq 1 ] && break
    RERUN=1   # first pass done: retry the misses once with the patched console fallback
    cd "$HOME/whatnot-sniper" && MAZI_SCP_POLITENESS_LOCK=$LOCK PYTHONPATH=$HOME/whatnot-sniper nohup /usr/bin/python3 \
      $D/resolve_slug_targets.py --slugs $D/hot_missing_slugs_20260926.jsonl --targets $D/targets.jsonl \
      --out $D/hot_missing_targets_20260926.jsonl >> $D/resolve_hot_missing.log 2>&1 < /dev/null &
    echo "[rerun] started $(date -u +%T)Z"; sleep 5
  fi
  for i in $(seq 1 120); do [ $(( $(date +%s) - $(stat -f %m "$LOG") )) -lt 60 ] && break; sleep 10; done
  SC=$(pgrep -f 'Python tools/scp_broad_scrub.py --targets' | head -1); [ -n "$SC" ] || { sleep 60; continue; }
  for i in $(seq 1 60); do
    if [ "$(holder)" != "$SC" ]; then
      kill -STOP $SC
      [ "$(holder)" = "$SC" ] && { kill -CONT $SC; sleep 1; continue; }
      break
    fi
    sleep 1
  done
  echo "[window $w] paused scrub $SC $(date -u +%T)Z"
  for i in $(seq 1 72); do pgrep -f resolve_slug_targets.py >/dev/null || break; sleep 10; done
  kill -CONT $SC; echo "[window $w] resumed $(date -u +%T)Z"
  sleep 90
done
echo "done $(date -u +%T)Z"
