#!/bin/bash
# image_backfill_healer.sh — keeps the comp-image backfill alive.
#
# The card-photo pass is a ~15 h job that was running only as a nohup from an operator shell. Nothing
# owned it: a crash, an OOM kill or a reboot at hour 10 would have left it stopped at whatever percent
# it reached, silently, until a human noticed. Every other long job in this lane is supervised; this
# one was the exception. Run as a supervisor HEALER so it inherits the keep-alive->ssh->supervisor
# chain (and with it Full Disk Access to the 6TB, which a launchd GUI agent does not have).
#
# Idempotent by construction:
#   * exits immediately if ANY comp_image_backfill is already running (the downloader is single-threaded
#     per source by design; a second pool on this spinning disk thrashes -- 0.51/s vs 6.0/s measured)
#   * exits if the source is already stamped complete
#   * otherwise relaunches; comp_image_backfill is resumable and skips existing files without a request
# Only a clean exit 0 stamps completion. Exit 3 means the auto-stop fired (failure surge / 403 wall /
# disk full) -- that must NOT be recorded as done, so the next tick retries it.
set -u
# Every source that is not yet stamped complete, in priority order -- not one hardcoded source.
# This was SRC=scp_catalog, which went complete on 2026-09-14; from then on the healer exited on the
# stamp every tick and kept NOTHING alive. On 2026-09-20 the eBay job tripped AUTO-STOP on a network
# failure surge, aborted cleanly as designed, and then sat dead for 4 hours because no keeper covered
# it. A guard that only watches the one job already finished is not a guard.
SOURCES="${MAZI_IMAGE_BACKFILL_SOURCES:-ebay fanatics scp_catalog tcgplayer_catalog}"
# rate/workers match the tuned foreground config so a crash-relaunch does not silently revert to the
# old slow defaults. The downloader paces itself down for the canonical refresh and for live-capture
# load, so a high ceiling here is a ceiling, not a commitment.
RATE="${MAZI_IMAGE_BACKFILL_RATE:-25}"
WORKERS="${MAZI_IMAGE_BACKFILL_WORKERS:-32}"
ROOT="${MAZI_PROJECT_ROOT:-$HOME/whatnot-sniper}"
W=/Volumes/MAZI_EVIDENCE_6TB/comp_images/_backfill
LOG="$ROOT/logs/image_backfill_healer.log"
LOCK="$W/healer.pid"
ts() { date -u +%FT%TZ; }

[ -d "$W" ] || exit 0                       # 6TB not mounted (or no FDA) -> nothing to do, not an error

# single instance, same lesson as the SCP watchdog: a slow pass must never multiply into a herd
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then exit 0; fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# one downloader at a time: two pools on one spindle thrash (0.51/s vs 6.0/s measured)
if pgrep -f "comp_image_backfill.py" >/dev/null 2>&1; then exit 0; fi

cd "$ROOT" || exit 0
for SRC in $SOURCES; do
  STAMP="$W/complete_${SRC}.stamp"
  [ -f "$STAMP" ] && continue
  CAND="$W/candidates_${SRC}_remaining.csv"
  [ -f "$CAND" ] || CAND="$W/candidates_${SRC}.csv"
  [ -f "$CAND" ] || continue
  echo "[$(ts)] nothing running and $SRC not complete -> relaunching (rate $RATE, workers $WORKERS)" >> "$LOG"
  nohup bash -c '/usr/bin/caffeinate -is /usr/bin/python3 -u "'"$ROOT"'/external_engine/comp_image_backfill.py" \
      --source "'"$SRC"'" --rate "'"$RATE"'" --workers "'"$WORKERS"'" --candidates "'"$CAND"'" >> "'"$LOG"'" 2>&1 \
    && { touch "'"$STAMP"'"; echo "[$(date -u +%FT%TZ)] '"$SRC"' COMPLETE" >> "'"$LOG"'"; }' >/dev/null 2>&1 &
  exit 0          # started one; next tick handles the next source
done
exit 0
