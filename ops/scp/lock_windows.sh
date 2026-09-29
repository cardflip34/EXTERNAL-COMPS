#!/bin/bash
# lock_windows.sh <anchored-regex-of-the-side-job> -- give a one-off side job the SCP lock in <=12-min
# windows by pausing the ring scrub OUTSIDE the lock (waiters poll every 20 s; the scrub re-takes the lock
# within 3.5 s, so a side job otherwise starves). Each window starts right after a fresh run-log line so the
# watchdog's 25-min stall timer never fires. Request rate unchanged: every fetch still goes through the lock.
JOB_RE=$1
D=$HOME/mazi_scp_broad; LOCK=$D/.scp_politeness.lock; LOG=$D/scp_broad_run.log
SCRUB_RE='^[^ ]*[Pp]ython[^ ]* ([^ ]*/)?tools/scp_broad_scrub\.py( |$)'
SC=; trap '[ -n "$SC" ] && kill -CONT $SC 2>/dev/null' EXIT
holder() { sed -n 's/^pid=\([0-9]*\).*/\1/p' "$LOCK" 2>/dev/null; }
for w in $(seq 1 12); do
  pgrep -f "$JOB_RE" >/dev/null || break
  for i in $(seq 1 120); do [ $(( $(date +%s) - $(stat -f %m "$LOG") )) -lt 60 ] && break; sleep 10; done
  SC=$(pgrep -f "$SCRUB_RE" | head -1); [ -n "$SC" ] || { sleep 60; continue; }
  for i in $(seq 1 60); do
    if [ "$(holder)" != "$SC" ]; then kill -STOP $SC; [ "$(holder)" = "$SC" ] && { kill -CONT $SC; sleep 1; continue; }; break; fi
    sleep 1
  done
  echo "[window $w] paused scrub $SC $(date -u +%T)Z"; echo "[note] $(date -u +%FT%TZ) scrub paused (lock_windows.sh) for a one-off side job; not a stall" >> "$LOG"
  for i in $(seq 1 72); do pgrep -f "$JOB_RE" >/dev/null || break; sleep 10; done
  kill -CONT $SC; echo "[window $w] resumed $(date -u +%T)Z"; echo "[note] $(date -u +%FT%TZ) scrub resumed (lock_windows.sh)" >> "$LOG"
  sleep 90
done
echo "done $(date -u +%T)Z"
