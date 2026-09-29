#!/usr/bin/env python3
"""ring_report.py -- cards scraped and BLOCKED rate per refresh tier, from scp_broad_run.log.

Each scrub invocation ends with 'processed=N resolved=R fallbacks=F blocked=B' followed by
'out=... state=<file> total_done=T'; the state file names the tier. Invocations are dated
by the newest timestamp printed before them.

  /usr/bin/python3 ~/mazi_scp_broad/ring_report.py [--since 2026-09-26T07:08Z] [--until ...]
"""
import argparse, os, re
from collections import defaultdict

LOG = os.path.expanduser("~/mazi_scp_broad/scp_broad_run.log")
TS = re.compile(r"(20\d\d-\d\d-\d\dT\d\d:\d\d(?::\d\d)?Z?)")
SUM = re.compile(r"^processed=(\d+) resolved=(\d+) fallbacks=(\d+) blocked=(\d+)")
ST = re.compile(r"state=\S*/(scp_\S+?)\.json")
NAMES = {"scp_broad_state": "HIST (lane A)", "scp_live_state": "live = A+B by need (weekly)",
         "scp_ring_A_state": "A daily", "scp_ring_C_state": "C monthly",
         "scp_ring_D_state": "D quarterly", "scp_ring_dormant_state": "dormant 180d"}

ap = argparse.ArgumentParser()
ap.add_argument("--since", default="0000")
ap.add_argument("--until", default="9999")
a = ap.parse_args()
last_ts, pending = "", None
agg = defaultdict(lambda: [0, 0, 0])            # invocations, processed, blocked
for line in open(LOG, errors="ignore"):
    m = TS.search(line)
    if m:
        last_ts = m.group(1)
    s = SUM.match(line)
    if s:
        pending = (int(s.group(1)), int(s.group(4)), last_ts)
        continue
    st = ST.search(line)
    if st and pending:
        n, b, ts = pending
        pending = None
        if a.since <= ts < a.until:
            k = NAMES.get(st.group(1), st.group(1))
            agg[k][0] += 1; agg[k][1] += n; agg[k][2] += b
print(f"window {a.since} .. {a.until}   (a chunk still running is not counted)")
print(f"{'lane / tier':30s} {'runs':>5s} {'cards':>7s} {'blocked':>8s} {'rate':>6s}")
tot = [0, 0, 0]
for k, (r, n, b) in sorted(agg.items()):
    print(f"{k:30s} {r:5d} {n:7,d} {b:8,d} {100 * b / max(n, 1):5.1f}%")
    tot = [tot[0] + r, tot[1] + n, tot[2] + b]
print(f"{'TOTAL':30s} {tot[0]:5d} {tot[1]:7,d} {tot[2]:8,d} {100 * tot[2] / max(tot[1], 1):5.1f}%")
