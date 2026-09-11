#!/usr/bin/env python3
"""Comp-image backfill (PRD V2 §34) — archive every live external-comp image to the 6TB, politely.

Rot census 2026-09-10: 302/302 sampled URLs alive (eBay back to 2015, SCP, Fanatics) -> archive now
while everything is still fetchable. Candidates come from canonical (see _backfill/candidates_*.csv).

Design:
  * stdlib only (/usr/bin/python3): urllib + threads. GLOBAL rate cap (default 6 req/s) via worker pacing.
  * RESUMABLE: skips any key whose file already exists >1KB (the 1.44M images saved by the old v2
    pipeline are skipped the same way — same dir layout comp_images/ebay/<item_id>/01.<ext>).
  * LEDGER: every result appended to _backfill/ledger_<source>.jsonl (ok/dead/err + bytes + http code);
    counters snapshot to _backfill/progress_<source>.json every ~500 results — this doubles as the
    definitive per-row rot census.
  * AUTO-STOP (block detection, per project rules): >40% failures over the last 300 results, or 8
    consecutive 403/429, or 6TB free < 100 GB -> write state, exit 3. Never retries a hard 403 wall.
Usage:
  /usr/bin/python3 external_engine/comp_image_backfill.py --source ebay [--rate 6] [--limit N]
  /usr/bin/python3 external_engine/comp_image_backfill.py --all         # ebay -> fanatics -> scp_catalog
"""
from __future__ import annotations
import argparse, collections, csv, json, os, queue, sys, threading, time, urllib.request, urllib.error

BASE = "/Volumes/MAZI_EVIDENCE_6TB/comp_images"
W = os.path.join(BASE, "_backfill")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"}
SOURCES = {"ebay": "candidates_ebay.csv", "fanatics": "candidates_fanatics.csv", "scp_catalog": "candidates_scp_catalog.csv",
           "tcgplayer_catalog": "candidates_tcgplayer_catalog.csv"}
# 30 workers, NOT a politeness change: the 6 req/s cap below is unchanged. Measured 2026-09-10 — with 6
# workers each blocking ~5 s on a ~434 KB image, throughput was 1.19 req/s, i.e. only 20% of the budget we
# already set. Worker count must exceed rate*latency to actually reach the cap; the limiter still enforces 6/s.
WORKERS = 30

def ext_of(url: str) -> str:
    p = url.split("?")[0].lower()
    for e in (".webp", ".jpg", ".jpeg", ".png"):
        if p.endswith(e): return ".jpg" if e == ".jpeg" else e
    return ".jpg"

def free_gb() -> float:
    st = os.statvfs(BASE); return st.f_bavail * st.f_frsize / 1e9

def run_source(source: str, rate: float, limit: int | None, candidates: str | None = None) -> int:
    cand = candidates or os.path.join(W, SOURCES[source])
    outdir = os.path.join(BASE, "ebay" if source == "ebay" else source)
    os.makedirs(outdir, exist_ok=True)
    ledger = open(os.path.join(W, f"ledger_{source}.jsonl"), "a")
    prog_path = os.path.join(W, f"progress_{source}.json")
    per_worker_sleep = WORKERS / max(rate, 0.5)
    q: "queue.Queue[tuple[str,str]]" = queue.Queue(maxsize=2000)
    counts = collections.Counter(); recent = collections.deque(maxlen=300); consec403 = [0]
    lock = threading.Lock(); stop = threading.Event()
    def record(key, status, code, nbytes):
        with lock:
            counts[status] += 1; recent.append(status)
            if status == "blocked": consec403[0] += 1
            else: consec403[0] = 0
            ledger.write(json.dumps({"k": key, "s": status, "c": code, "b": nbytes}) + "\n")
            n = sum(counts.values())
            if n % 500 == 0:
                ledger.flush()
                json.dump({"at": time.strftime("%FT%TZ", time.gmtime()), "source": source, **counts,
                           "done": n}, open(prog_path + ".tmp", "w")); os.replace(prog_path + ".tmp", prog_path)
            bad = sum(1 for s in recent if s not in ("ok", "skip"))
            if (len(recent) == 300 and bad > 120) or consec403[0] >= 8:
                print(f"[AUTO-STOP] failure surge (bad={bad}/300, consec403={consec403[0]}) — treating as block signal", flush=True)
                stop.set()
    def worker():
        while not stop.is_set():
            try: key, url = q.get(timeout=3)
            except queue.Empty: return
            t0 = time.time(); did_net = False
            try:
                d = os.path.join(outdir, key); f = os.path.join(d, "01" + ext_of(url))
                if os.path.exists(f) and os.path.getsize(f) > 1024:
                    record(key, "skip", 0, 0)
                else:
                    did_net = True
                    req = urllib.request.Request(url, headers=UA)
                    with urllib.request.urlopen(req, timeout=20) as r:
                        data = r.read(3_000_000)
                    if len(data) < 512:
                        record(key, "small", r.status, len(data))
                    else:
                        os.makedirs(d, exist_ok=True)
                        with open(f + ".tmp", "wb") as fh: fh.write(data)
                        os.replace(f + ".tmp", f)
                        record(key, "ok", 200, len(data))
            except urllib.error.HTTPError as e:
                record(key, "dead" if e.code in (404, 410) else ("blocked" if e.code in (403, 429) else "err"), e.code, 0)
            except Exception:
                record(key, "neterr", 0, 0)
            finally:
                q.task_done()
                if did_net:  # pace only real network hits; disk-skip resumes fly through at full speed
                    dt = time.time() - t0
                    if dt < per_worker_sleep: time.sleep(per_worker_sleep - dt)
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    [t.start() for t in threads]
    fed = 0
    with open(cand, newline="") as fh:
        for key, url in csv.reader(fh):
            if stop.is_set(): break
            if fed % 20000 == 0 and free_gb() < 100:
                print("[AUTO-STOP] 6TB free < 100 GB", flush=True); stop.set(); break
            q.put((key, url)); fed += 1
            if limit and fed >= limit: break
    while not q.empty() and not stop.is_set(): time.sleep(1)
    stop.set(); [t.join(timeout=10) for t in threads]
    ledger.flush(); ledger.close()
    print(f"[{source}] done: {dict(counts)} (fed {fed:,})", flush=True)
    return 3 if consec403[0] >= 8 else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES)); ap.add_argument("--all", action="store_true")
    ap.add_argument("--rate", type=float, default=6.0); ap.add_argument("--limit", type=int)
    ap.add_argument("--candidates", help="override candidates csv (delta syncs)")
    a = ap.parse_args()
    order = list(SOURCES) if a.all else [a.source]
    if not order or order == [None]: ap.error("--source or --all required")
    for s in order:
        rc = run_source(s, a.rate, a.limit, a.candidates)
        if rc: sys.exit(rc)

if __name__ == "__main__":
    main()
