#!/usr/bin/env python3
"""Comp-image backfill (PRD V2 §34) — archive every live external-comp image to the 6TB, politely.

Rot census 2026-09-10: 302/302 sampled URLs alive (eBay back to 2015, SCP, Fanatics) -> archive now
while everything is still fetchable. Candidates come from canonical (see _backfill/candidates_*.csv).

CORRECTION 2026-09-13: that census was too small to be trusted per-source. A host-level check of the
fanatics candidates found 401,985 of 1,455,961 rows (27.6%) on origins that are already gone — see
DEAD_HOSTS. Rot is NOT uniform across sources; sample by host, not by row count, before claiming a
source is archivable. The remaining 72.4% are alive and downloadable.

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
import argparse, collections, csv, json, os, queue, subprocess, sys, threading, time, urllib.request, urllib.error

BASE = "/Volumes/MAZI_EVIDENCE_6TB/comp_images"
W = os.path.join(BASE, "_backfill")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"}
SOURCES = {"ebay": "candidates_ebay.csv", "fanatics": "candidates_fanatics.csv", "scp_catalog": "candidates_scp_catalog.csv",
           "tcgplayer_catalog": "candidates_tcgplayer_catalog.csv"}
# 8 workers. NOT a politeness figure — a DISK figure. Measured 2026-09-12: the 6TB is a spinning HDD
# shared with the SCP scrub and the DuckDB canonical refresh; 30 concurrent small-file writers caused seek
# thrashing and throughput FELL to 0.51/s (below the 6-worker 1.19/s). On HDD, concurrency past a small
# number is negative — for the ~434KB eBay listing photos that figure was measured against. Small catalog
# thumbnails (SCP images are ~10KB) are a different shape and take more concurrency before the spindle is
# the limit, so this is only a DEFAULT now: see --workers. Contention with the refresh is handled by
# REFRESH_BACKOFF (pace down, never stop).
WORKERS = 8

# Hosts that are broken at the ORIGIN, verified 2026-09-13 — never worth a request:
#   host.jwcinc.net              NXDOMAIN (domain gone; 32,536 fanatics rows, all pre-2014)
#   dw7591lwb84er.cloudfront.net HTTP 500 from Fanatics' own image-fetcher role, which lacks
#                                s3:ListBucket on the pwccauctions bucket (369,449 rows)
# These must be dropped at the FEEDER, not attempted and recorded: the candidates files are
# time-ordered, so dead hosts arrive in contiguous runs. A run of them pushes the rolling
# failure ratio past the 40%/300 auto-stop and halts an otherwise healthy job — which reads
# exactly like a block. 72% of the fanatics candidates are alive and downloadable; only these
# two hosts are not.
DEAD_HOSTS = frozenset({"host.jwcinc.net", "dw7591lwb84er.cloudfront.net"})


def host_of(url: str) -> str:
    try:
        return url.split("/")[2].lower()
    except IndexError:
        return ""


def ext_of(url: str) -> str:
    p = url.split("?")[0].lower()
    for e in (".webp", ".jpg", ".jpeg", ".png"):
        if p.endswith(e): return ".jpg" if e == ".jpeg" else e
    return ".jpg"

REFRESH_LOCK = os.path.expanduser("~/mazi_local_evidence/canonical_refresh.lock")

def refresh_running() -> bool:
    """True while the 6-hourly canonical refresh holds its lock — it saturates the same spindle."""
    try:
        pid = open(REFRESH_LOCK).read().strip()
        if not pid.isdigit():
            return False
        return subprocess.run(["/bin/kill", "-0", pid], capture_output=True).returncode == 0
    except OSError:
        return False


# SLOW DOWN for the canonical refresh; never STOP for it. The original design blocked outright while the
# refresh held its lock, which failed twice in opposite directions:
#   1. unbounded, it starved completely — ~15 h of continuous waiting produced ZERO images (2026-09-13);
#   2. capped at 20 min, it still only managed a ~6% duty cycle, because the feeder re-entered the wait
#      every 2000 candidates: ~80 s of work per 20 min of waiting against a 1 h 20 m refresh. Measured 0/s.
# Pacing instead of blocking keeps both jobs moving, and it retires an entire bug class: nothing waits on
# a lock any more, so image_sync (which runs INSIDE the refresh, holding that very lock) can no longer
# deadlock against its own child. --no-yield is kept for callers that want no refresh deference at all.
REFRESH_BACKOFF = 3.0


# The Whatnot live-capture fleet shares this Mini, and bot_manager sheds capture bots once load stays
# above ~36. A live auction happens once; this backfill is historical and resumable, so it must always
# be the thing that gives way. Observed 2026-09-13: fleet at 0/2 bots with load 60-90 while this ran at
# 6 req/s. Stay well clear of the shed threshold.
CAPTURE_BACKOFF = 4.0   # multiply the per-request pause by this while the fleet needs the machine
# Hysteresis band, not a single threshold. Backing off merely because run_bot.py EXISTS was far too
# blunt: measured 2026-09-14 with 2 bots happily capturing at load 10 while this sat throttled to
# 2 req/s for no reason. What actually matters is headroom under bot_manager's ~36 shed threshold, so
# back off approaching it and only resume once load has fallen well clear — the gap is what stops us
# oscillating against our own contribution to the number.
LOAD_BACKOFF_ON = 28.0
LOAD_BACKOFF_OFF = 20.0


def live_capture_pressure(backed_off: bool) -> bool:
    """Whether to keep giving the machine to the live-capture fleet, given our current state."""
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        return False
    return load1 > LOAD_BACKOFF_OFF if backed_off else load1 >= LOAD_BACKOFF_ON


def free_gb() -> float:
    st = os.statvfs(BASE); return st.f_bavail * st.f_frsize / 1e9

def run_source(source: str, rate: float, limit: int | None, candidates: str | None = None, do_yield: bool = True,
               workers: int = WORKERS) -> int:
    cand = candidates or os.path.join(W, SOURCES[source])
    outdir = os.path.join(BASE, "ebay" if source == "ebay" else source)
    os.makedirs(outdir, exist_ok=True)
    ledger = open(os.path.join(W, f"ledger_{source}.jsonl"), "a")
    prog_path = os.path.join(W, f"progress_{source}.json")
    base_sleep = workers / max(rate, 0.5)
    pace = [base_sleep]      # mutable so the feeder can re-pace live workers without a restart
    cap_state = [False]      # capture-backoff latch, kept separate so refresh pacing cannot confuse its hysteresis
    q: "queue.Queue[tuple[str,str]]" = queue.Queue(maxsize=2000)
    counts = collections.Counter(); recent = collections.deque(maxlen=300); consec403 = [0]
    lock = threading.Lock(); stop = threading.Event(); feed_done = threading.Event()
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
        # An empty queue does NOT mean the run is over — the feeder pauses for minutes at a time while
        # the canonical refresh owns the spindle. Exiting here strands the feeder on a full queue with
        # no consumers (observed 2026-09-13: eBay run alive 45 min, 1 thread, zero bytes). Only
        # feed_done/stop may end a worker.
        while not stop.is_set():
            try: key, url = q.get(timeout=3)
            except queue.Empty:
                if feed_done.is_set(): return
                continue
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
                    if dt < pace[0]: time.sleep(pace[0] - dt)
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    [t.start() for t in threads]
    fed = 0; dead_host = 0
    with open(cand, newline="") as fh:
        for key, url in csv.reader(fh):
            if stop.is_set(): break
            if host_of(url) in DEAD_HOSTS:
                dead_host += 1; continue  # origin is gone — skip without spending a request or a failure slot
            if fed % 2000 == 0:
                cap_state[0] = live_capture_pressure(cap_state[0])
                refresh = do_yield and refresh_running()
                mult = max(REFRESH_BACKOFF if refresh else 1.0, CAPTURE_BACKOFF if cap_state[0] else 1.0)
                want = base_sleep * mult
                if want != pace[0]:
                    why = "canonical refresh" if refresh and mult == REFRESH_BACKOFF else (
                          "live capture" if cap_state[0] else "clear")
                    print(f"[pace] {why}: {rate / mult:.1f} req/s (load {os.getloadavg()[0]:.1f})", flush=True)
                    pace[0] = want
            if fed % 20000 == 0 and free_gb() < 100:
                print("[AUTO-STOP] 6TB free < 100 GB", flush=True); stop.set(); break
            while not stop.is_set():  # never block forever: a full queue with no live worker is a bug, not backpressure
                try:
                    q.put((key, url), timeout=30); break
                except queue.Full:
                    if not any(t.is_alive() for t in threads):
                        print("[ABORT] queue full but every worker is dead — exiting instead of hanging", flush=True)
                        stop.set()
            if stop.is_set(): break
            fed += 1
            if limit and fed >= limit: break
    feed_done.set()
    while not q.empty() and not stop.is_set(): time.sleep(1)
    stop.set(); [t.join(timeout=10) for t in threads]
    ledger.flush(); ledger.close()
    print(f"[{source}] done: {dict(counts)} (fed {fed:,}, dead_host_skipped {dead_host:,})", flush=True)
    return 3 if consec403[0] >= 8 else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES)); ap.add_argument("--all", action="store_true")
    ap.add_argument("--rate", type=float, default=6.0); ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"concurrent fetchers (default {WORKERS}; tuned for ~434KB eBay images — small "
                         "catalog thumbnails can take more before the spindle is the limit)")
    ap.add_argument("--candidates", help="override candidates csv (delta syncs)")
    ap.add_argument("--no-yield", action="store_true",
                    help="do not pause for the canonical refresh (REQUIRED when invoked by image_sync, "
                         "which runs inside the refresh and holds its lock)")
    a = ap.parse_args()
    order = list(SOURCES) if a.all else [a.source]
    if not order or order == [None]: ap.error("--source or --all required")
    for s in order:
        rc = run_source(s, a.rate, a.limit, a.candidates, do_yield=not a.no_yield, workers=a.workers)
        if rc: sys.exit(rc)

if __name__ == "__main__":
    main()
