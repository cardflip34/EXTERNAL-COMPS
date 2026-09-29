#!/bin/bash
# Read-only production snapshot for the current driver cycle.
#
# MEASUREMENT NOTE: do NOT derive a block rate from log lines. scp_broad_scrub logs
# EVERY blocked card but only every 50th success (`processed % 50 == 0`), so a
# log-line ratio over-reports blocking by ~50x (it read 41% when the truth was 4%).
# Successes come from the STATE file (blocked cards are never marked done), blocks
# from the BLOCKED lines.
D=$HOME/mazi_scp_broad
LAST=$(grep -n "=== attempt" "$D/scp_broad_run.log" | tail -1 | cut -d: -f1)
tail -n +"$LAST" "$D/scp_broad_run.log" > /tmp/prod_now.txt
BLK=$(grep -c BLOCKED /tmp/prod_now.txt)
LIVE=$(python3 -c "import json;print(len(json.load(open('$D/scp_live_state.json'))['done_ids']))" 2>/dev/null || echo 0)
HIST=$(python3 -c "import json;print(len(json.load(open('$D/scp_broad_state.json'))['done_ids']))" 2>/dev/null || echo 0)
echo "cycle_started: $(head -1 /tmp/prod_now.txt)"
echo "live_cards_done=$LIVE  hist_cards_done=$HIST  blocked_this_cycle=$BLK"
awk -v s="$LIVE" -v b="$BLK" 'BEGIN{ if (s+b>0) printf "TRUE block_rate=%.1f%% (blocks/(successes+blocks))\n", 100*b/(s+b) }'
echo -n "local_rows: "; wc -l < "$D/scp_broad_comps.jsonl"
# NB: match ' kept=' with the leading space so the per-run summary line's
# 'rows_kept=NNNN' is NOT counted as a per-card yield (it inflated mean to 974
# against a median of 30).
grep -oE ' kept=[0-9]+' /tmp/prod_now.txt | cut -d= -f2 | python3 -c "
import sys
v=[int(x) for x in sys.stdin]
print(f'yield(sampled): n={len(v)} mean={sum(v)/len(v):.0f} rows/card median={sorted(v)[len(v)//2]}' if v else 'yield: (none yet)')"
