#!/bin/bash
# whatnot_link_healer.sh — keeps every trusted Whatnot sale linked to a permanent MAZI catalog id.
#
# The capture pipeline identifies a card (Gemini) but never assigns the catalog card_id the front end
# needs, so nothing we record can be published (measured 2026-09-24: 0 of 50,444 trusted sales had
# one). Until linking lives inside the identification step itself, this healer closes the gap on
# the Mini: every tick it links whatever is new, with the same scorer and the same provenance rules
# as the one-off backfill, and writes only to whatnot_catalog_links. Human decisions are never
# touched. Run as a supervisor HEALER (inherits the ssh-localhost chain and its access to the 6TB).
#
# Idempotent: single instance, --only-unlinked, exits quietly when there is nothing to do or when the
# beta password / bridge env is not present (not configured is not an error).
set -u
ROOT="${MAZI_PROJECT_ROOT:-$HOME/whatnot-sniper}"
LOG="$ROOT/logs/whatnot_link_healer.log"
LOCK="$ROOT/logs/.whatnot_link_healer.pid"
OUT="/Volumes/MAZI_EVIDENCE_6TB/comp_images/_backfill/refmap/whatnot_links_latest.csv"
ts() { date -u +%FT%TZ; }

cd "$ROOT" || exit 0
[ -f "$HOME/private/beta-password" ] || exit 0
[ -f ./.env.external_comps_bridge ] || exit 0
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then exit 0; fi
# one linker at a time, whoever started it: an operator's manual pass and the healer must never
# both page the beta catalog at once (it runs on Small compute)
pgrep -f "tools/link_whatnot_to_catalog.py" >/dev/null 2>&1 && exit 0
# At most one pass per MIN_GAP_S. The supervisor fires this every ~24 min (every_ticks=24 at
# TICK_S=60, not the ~2 h its comment says) and a pass takes ~25 min, so passes ran back to back.
# Each one re-scores every unlinked sale against ~4.9M catalog cards: 74.6% of the beta DB's
# execution time on 2026-09-26, which slowed public search. Operator-approved 2026-09-26.
STAMP="$ROOT/logs/.whatnot_link_healer.last_start"
MIN_GAP_S="${MAZI_LINK_HEALER_MIN_GAP_S:-6600}"
if [ -f "$STAMP" ] && [ $(( $(date +%s) - $(stat -f %m "$STAMP") )) -lt "$MIN_GAP_S" ]; then exit 0; fi
echo $$ > "$LOCK"; trap 'rm -f "$LOCK"' EXIT
touch "$STAMP"

set -a; . ./.env.external_comps_bridge; set +a
echo "[$(ts)] tick" >> "$LOG"
/usr/bin/caffeinate -is nice -n 10 /usr/bin/python3 -u tools/link_whatnot_to_catalog.py \
    --only-unlinked --apply --out "$OUT" >> "$LOG" 2>&1
rc=$?   # capture BEFORE $(ts): a command substitution inside the echo resets $? (masked 30 h of failures)
echo "[$(ts)] rc=$rc" >> "$LOG"
exit 0
