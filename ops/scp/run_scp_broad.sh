#!/bin/bash
# Dual-lane SCP driver — resumable, watchdog-managed.
#
#   LANE B (LIVE):  re-sweeps the TOP-VOLUME cards for the post-eBay-block window
#                   (2026-07-01 -> now). These cards are already marked done in
#                   Lane A's state, so without this lane the highest-selling cards
#                   in the catalog would never yield another live sale. Its state
#                   RESETS when a full sweep completes, so it rolls continuously.
#   LANE A (HIST):  marches the 1M-card backlog for history. Floor lowered to 2015
#                   (eBay has ~0 pre-2026 rows) and the eBay-owned band
#                   [2026-01-01, 2026-07-01) EXCLUDED -- eBay holds 3.13M rows there
#                   and the cross-source dup guard is INERT for broad rows.
#
# The two lanes run SEQUENTIALLY in this one process: they can never contend on the
# SCP politeness mutex (a blocked lane would poll it every 30s and waste throughput).
# Both append to the SAME jsonl, so the single existing bridge drains both; the
# idempotent source_item_id + ON CONFLICT make re-sweeps safe by construction.
# TRANSPORT NOTE 2026-07-30 (v2): back to JINA, now AUTHENTICATED.
# The 55% "block rate" was never SCP -- r.jina.ai allows 20 RPM anonymous and we ran
# ~50/min for ~12 days, so its HTTP 403 was arithmetic. A free API key lifts that to
# 500 RPM. Jina is preferred over direct playwright because it (a) shields Mini's
# residential IP from SCP's Cloudflare, (b) spawns no chromium, so Mini's load stays
# low, and (c) is marginally faster (~3.2s vs ~3.7s/card). playwright remains the
# automatic fallback inside fetch_scp_html if jina ever fails again.
cd ~/whatnot-sniper || exit 1
export PYTHONPATH=$HOME/whatnot-sniper
export MAZI_SCP_POLITENESS_LOCK=$HOME/mazi_scp_broad/.scp_politeness.lock
# Authenticated Jina: 20 RPM anonymous -> 500 RPM with this (free) key. Exported from
# a 0600 file into the ENV only -- never a command line -- so `ps` cannot expose it.
D=~/mazi_scp_broad
LOG=$D/scp_broad_run.log
# 2026-09-25: the key had been answering HTTP 402 (quota used up) for days; every keyed fetch came
# back "BLOCKED" and cost a 30 s pause. Probe the key once per driver start: a dead key means run
# anonymous (r.jina.ai allows 20 RPM without a key) at a pace that stays under that ceiling. Drop a
# fresh key in $D/.jina_key and the next driver start picks it up automatically.
SLEEP=3.5                                  # anonymous pace: ~9 cards/min incl. fallbacks < 20 RPM
if [ -r "$D/.jina_key" ]; then
  KEY=$(tr -d '\n' < "$D/.jina_key")
  CODE=$(curl -s -o /dev/null -m 25 -w '%{http_code}' -H "Authorization: Bearer $KEY" "https://r.jina.ai/https://example.com/")
  case "$CODE" in
    200) export MAZI_JINA_API_KEY="$KEY"; SLEEP=1.0
         echo "[jina] key accepted (HTTP 200) -> authenticated, sleep ${SLEEP}s  $(date -u +%FT%TZ)" >> "$LOG" ;;
    *)   echo "[jina] key REJECTED (HTTP $CODE) -> anonymous 20 RPM, sleep ${SLEEP}s; put a fresh key in $D/.jina_key  $(date -u +%FT%TZ)" >> "$LOG" ;;
  esac
  unset KEY
else
  echo "[jina] no key file -> anonymous 20 RPM, sleep ${SLEEP}s  $(date -u +%FT%TZ)" >> "$LOG"
fi
TARGETS=$D/targets.jsonl
STATE=$D/scp_broad_state.json
OUT=$D/scp_broad_comps.jsonl
LOG=$D/scp_broad_run.log

# --- REFRESH ROTATION (2026-09-26, approved by Andy) ---------------------------------
# SCP keeps only the latest ~30 sales per grade bucket, so a card must be re-read before
# its busiest bucket refills. build_refresh_tiers.py sorts cards by 120-day activity:
#   A daily | live_targets.jsonl (A+B "by need") weekly | C monthly | D quarterly | dormant 180 d
# A ring sweeps its file in chunks. Once a pass is complete, the next one starts only after
# the interval has passed since the previous pass BEGAN -- or at 1.5x the interval, which
# gives up on cards that stayed blocked. Chunks stop the big tiers starving the others.
# Every fetch still goes through acquire_scp_budget(), so the request rate is unchanged.
# Measured pace ~350 cards/h incl. block backoffs => a full cycle is at most ~20 h.
LIVE_TARGETS=$D/live_targets.jsonl      # A+B by need (was: top 5,000 by volume, until 2026-09-26)
LIVE_STATE=$D/scp_live_state.json       # kept, so the in-flight live sweep carries on
HIST_CHUNK=${HIST_CHUNK:-2000}          # history per cycle (2500 -> 2000 to make room for the rings)
# Jan-Jun 2026 band (plan 1.7 4b): kept in a ROLLING SIDE FILE the bridge never reads. It goes to
# Neon later as its own import (run 9). Canary passed on cached replay 2026-09-26 before arming.
BAND_OUT=/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/scp_broad/scp_band_2026h1.jsonl
BAND_ARGS="--band-out $BAND_OUT --band-from 2026-01-01 --band-to 2026-07-01"

ring() {  # ring NAME TARGETS EVERY_H CHUNK [STATE]
  local name=$1 targets=$2 every_h=$3 chunk=$4 st=${5:-$D/scp_ring_$1_state.json}
  local began=$D/scp_ring_$1.began total done
  [ -s "$targets" ] || { echo "[ring $name] no targets file $targets -- skipped" >> "$LOG"; return 0; }
  total=$(wc -l < "$targets" | tr -d ' ')
  # Count only ids in THIS file: after a list swap the state holds ids of dropped cards,
  # and len(done_ids) would call the pass complete before every card was read.
  done=$(python3 -c 'import json,sys
try: seen=set(json.load(open(sys.argv[1]))["done_ids"])
except Exception: seen=set()
print(sum(1 for l in open(sys.argv[2]) if l.strip() and str(json.loads(l).get("id","")) in seen))' "$st" "$targets" 2>/dev/null || echo 0)
  if [ ! -f "$began" ] \
     || { [ "$done" -ge "$total" ] && [ -n "$(find "$began" -mmin +$((every_h * 60)) 2>/dev/null)" ]; } \
     || [ -n "$(find "$began" -mmin +$((every_h * 90)) 2>/dev/null)" ]; then
    rm -f "$st"; touch "$began"; done=0
    echo "[ring $name] new pass: $total cards, every ${every_h}h  $(date -u +%FT%TZ)" >> "$LOG"
  fi
  [ "$done" -ge "$total" ] && return 0    # pass complete; the next one is not due yet
  echo "[ring $name] $done/$total done, chunk $chunk  $(date -u +%FT%TZ)" >> "$LOG"
  python3 tools/scp_broad_scrub.py --targets "$targets" --out "$OUT" --state "$st" \
    --floor 2026-07-01 --ceiling 2099-01-01 $BAND_ARGS \
    --limit "$chunk" --prefer jina --sleep "$SLEEP" --block-backoff 60 --block-backoff-max 900 >> "$LOG" 2>&1
}

for attempt in $(seq 1 4000); do
  TOTAL=$(wc -l < "$TARGETS")   # recomputed each pass: targets.jsonl can be EXTENDED live
  DONE=$(python3 -c "import json;print(len(json.load(open('$STATE'))['done_ids']))" 2>/dev/null || echo 0)
  echo "=== attempt $attempt: hist $DONE/$TOTAL  $(date -u +%FT%TZ) ===" >> "$LOG"

  # --- REFRESH RINGS (replace the old continuous LANE B live sweep) --------------
  ring A       "$D/targets_tier_A_daily.jsonl"       24 1000
  ring live    "$LIVE_TARGETS"                       168 1500 "$LIVE_STATE"
  ring C       "$D/targets_tier_C_monthly.jsonl"    720 200
  ring D       "$D/targets_tier_D_quarterly.jsonl" 2160 1400
  ring dormant "$D/targets_tier_dormant.jsonl"     4320 1400
  ring A       "$D/targets_tier_A_daily.jsonl"       24 1000    # no-op unless a day has passed

  # --- LANE A: historical march ----------------------------------------------
  if [ "$DONE" -lt "$TOTAL" ]; then
    python3 tools/scp_broad_scrub.py --targets "$TARGETS" \
      --out "$OUT" --state "$STATE" \
      --floor 2015-01-01 --ceiling 2099-01-01 \
      --exclude-from 2026-01-01 --exclude-to 2026-07-01 $BAND_ARGS \
      --limit "$HIST_CHUNK" --prefer jina --sleep "$SLEEP" --block-backoff 60 --block-backoff-max 900 >> "$LOG" 2>&1
  else
    echo "[hist] backlog complete ($DONE/$TOTAL) — live lane continues" >> "$LOG"
  fi
  sleep 5
done
echo "=== driver exit $(date -u +%FT%TZ) ===" >> "$LOG"
