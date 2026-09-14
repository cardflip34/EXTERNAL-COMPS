#!/bin/bash
# Canonical refresh — keeps external_market_canonical_v1 tracking the live scrubs.
#
# WHY: the scrapers run continuously, but canonicalization was a MANUAL step. On 2026-08-25 that
# drift was measured at 4 days / ~2.4M rows (SCP local 8.89M vs canonical 6.49M). Scrapers healthy,
# store stale — a silent freshness failure. This makes the step automatic.
#
# Idempotent: multi_source_ingest OVERWRITES each source partition from its current snapshot, so a
# re-run is always safe and always converges on the live data. Single-instance via flock-style lock.
# Invoked by the V2 supervisor as a governed healer (skipped when resource is RED).
set -uo pipefail
ROOT="$HOME/whatnot-sniper"
PY="$ROOT/.venv_extcomps/bin/python"
LOG="$ROOT/logs/canonical_refresh.log"
LOCK="$HOME/mazi_local_evidence/canonical_refresh.lock"
MIN_INTERVAL_H=${CANONICAL_REFRESH_MIN_H:-6}
mkdir -p "$(dirname "$LOCK")" "$ROOT/logs"

ts() { date -u +%FT%TZ; }

# single instance
if [ -s "$LOCK" ]; then
  PID="$(tr -dc '0-9' < "$LOCK")"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then echo "[$(ts)] already running pid=$PID" >> "$LOG"; exit 0; fi
fi

# rate limit: don't rebuild more often than MIN_INTERVAL_H
STAMP="$HOME/mazi_local_evidence/canonical_refresh.last"
if [ -f "$STAMP" ]; then
  AGE_H=$(( ( $(date +%s) - $(stat -f %m "$STAMP") ) / 3600 ))
  [ "$AGE_H" -lt "$MIN_INTERVAL_H" ] && exit 0
fi

echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT
echo "[$(ts)] canonical refresh START" >> "$LOG"
cd "$ROOT" || exit 1
# Neon → staging export first, so canonical ingests LIVE data for Neon-only pipelines (fanatics; see
# neon_source_export.py). Uses system python (psycopg); non-fatal — on failure ingest reuses prior staging.
set -a; . ./.env.external_comps_bridge 2>/dev/null; set +a
/usr/bin/python3 -u external_engine/neon_source_export.py --source fanatics >> "$LOG" 2>&1 || echo "[$(ts)] WARN fanatics export failed; using existing staging" >> "$LOG"
if nice -n 12 "$PY" -u external_engine/multi_source_ingest.py --memory-limit 1200MB >> "$LOG" 2>&1; then
  nice -n 12 "$PY" -u external_engine/classifier_v1.py --memory-limit 1200MB >> "$LOG" 2>&1
  nice -n 12 "$PY" external_engine/canonical_writer.py rebuild-index >> "$LOG" 2>&1
  nice -n 12 "$PY" -u external_engine/freshness_report.py >> "$LOG" 2>&1
  nice -n 12 "$PY" -u external_engine/image_sync_incremental.py >> "$LOG" 2>&1
  touch "$STAMP"
  echo "[$(ts)] canonical refresh OK" >> "$LOG"
else
  echo "[$(ts)] canonical refresh FAILED (ingest rc!=0) — store left as-is" >> "$LOG"
fi
