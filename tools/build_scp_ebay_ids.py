#!/usr/bin/env python3
"""build_scp_ebay_ids.py -- local index of the eBay item ids already present as SportsCardsPro rows (raw.ledger_anchor =
'ebay-<id>'), so a direct eBay SPORTS row can be skipped when SCP already holds that sale. Read-only on the SCP store;
writes ~/mazi_ebay_sweep/scp_ebay_ids.sqlite. Incremental: remembers the byte offset it reached.
  taskpolicy -b /usr/bin/python3 -u tools/build_scp_ebay_ids.py
"""
import os, re, sqlite3, time
SRC = "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/scp_broad/scp_broad_comps.jsonl"
DB = os.path.expanduser("~/mazi_ebay_sweep/scp_ebay_ids.sqlite")
ANCHOR = re.compile(rb'"ledger_anchor": "ebay-(\d{9,14})"')
c = sqlite3.connect(DB)
c.execute("CREATE TABLE IF NOT EXISTS ids (item_id TEXT PRIMARY KEY) WITHOUT ROWID")
c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
off = int((c.execute("SELECT v FROM meta WHERE k='offset'").fetchone() or ["0"])[0])
size, t0, n = os.path.getsize(SRC), time.time(), 0
with open(SRC, "rb") as f:
    f.seek(off)
    while True:
        buf = f.read(64 << 20)
        if not buf:
            break
        cut = buf.rfind(b"\n") + 1 or len(buf)
        ids = [(m.group(1).decode(),) for m in ANCHOR.finditer(buf[:cut])]
        c.executemany("INSERT OR IGNORE INTO ids VALUES (?)", ids); n += len(ids)
        off += cut; f.seek(off)
        c.execute("INSERT OR REPLACE INTO meta VALUES ('offset', ?)", (str(off),)); c.commit()
        print(f"{off / size:6.1%}  +{n:,} anchors  {time.time() - t0:.0f}s", flush=True)
print("done:", c.execute("SELECT count(*) FROM ids").fetchone()[0], "distinct eBay ids in SCP rows")
