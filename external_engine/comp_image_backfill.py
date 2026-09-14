#!/usr/bin/env python3
"""Comp-image backfill (PRD V2 §34) — archive every live external-comp image to the 6TB, politely.

Rot census 2026-09-10: 302/302 sampled URLs alive (eBay back to 2015, SCP, Fanatics) -> archive now
while everything is still fetchable. Candidates come from canonical (see _backfill/candidates_*.csv).

CORRECTION 2026-09-13: that census was too small to be trusted per-source. A host-level check of the
fanatics candidates found 401,985 of 1,455,961 rows (27.6%) on origins that are already gone — see
DEAD_HOSTS. Rot is NOT uniform across sources; sample by host, not by row count, before claiming a
source is archivable. The remaining 72.4% are alive and downloadable.

KNOWN CEILING — the flat <source>/<key>/ layout (measured 2026-09-14, and the binding constraint once
connection reuse landed). One directory per image, all siblings under one parent, costs:

    mkdir+write into a near-empty directory                      0.2 ms
    mkdir+write into scp_catalog/  (~120,000 siblings)       1,424   ms      ~6,200x
    mkdir+write into ebay/         (~1.4M siblings, IDLE)  >12,000   ms      (probe timed out)

The eBay directory has no writer at all, so this is directory SIZE, not lock contention between
workers. Two consequences: throughput DEGRADES as a source fills (scp_catalog gets slower on its way
from 120K to 338K), and the 1.4M-entry eBay store is already far past the knee — relevant before the
4.2M-image eBay backfill is resumed.

The fix is sharding the key (e.g. <source>/<key[:2]>/<key>/) so no directory holds more than a few
thousand entries. NOT done here: the flat path is the convention Neon/8504 and the front-end
integration resolve against, so changing it is an interface decision, plus a migration of the 1.5M
images already on disk. Raising --workers does not help; the cost is per-insert, not per-request.

OPERATIONAL COROLLARY — never `ls`, `find` or `du` these directories while a job is running. Reading
them is as expensive as writing them, and it competes for the same spindle: a single `find` over ebay/
cut this backfill from 10 req/s to 2.2, and a forgotten background `ls` of scp_catalog/ held it there
(both self-inflicted, 2026-09-14). Progress is already published cheaply and should be read from
_backfill/progress_<source>.json (~100 bytes, rewritten every 500 results) and ledger_<source>.jsonl.
Counting files by listing the directory is the expensive way to learn a number the job already tells you.

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
import argparse, collections, csv, http.client, json, os, queue, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request

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
REFRESH_BACKOFF = 2.0
# Tried 10x on the theory that image writes were starving the concurrent refresh of the spindle. The
# experiment refuted it: at 8 req/s the refresh got ~3% CPU, and after throttling to 2.5 req/s it got
# 1.3% -- SLOWER. Backing off bought nothing, because the refresh does not contend with us at all:
#   iostat 2026-09-14 -- disk6 (the 6TB, where images are written) 20-143 tps ... idle
#                        disk0 (internal, Chrome + Spotlight + DuckDB spill) 8,000-9,200 tps ... pinned
# The images land on the quiet disk. What is saturated is the internal one, which this job barely
# touches. So deference here is close to pure cost; 2x is a small hedge against being wrong a third
# time, not a belief that it helps. Measure disk6 before ever raising it again.


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
# Raised from 28/20 after the same measurement. System load here is dominated by Chrome (the capture
# bots) and Spotlight on the INTERNAL disk; this job's work is network plus writes to the external 6TB,
# so it barely moves the number it was being judged by. At 28 it throttled itself almost permanently for
# contention it was not part of. Kept as a genuine emergency brake — bot_manager sheds capture bots above
# ~36 sustained, so back off near there, not far below it.
LOAD_BACKOFF_ON = 34.0
LOAD_BACKOFF_OFF = 26.0
# SETTLED BY EXPERIMENT (2026-09-14), after two wrong guesses in both directions. SIGSTOP'd this job
# for 75 s and watched, which is the test that should have been run first:
#     downloader running   load 38.8   fleet 1/2 target
#     downloader FROZEN    load 22.7   fleet 2/2 target
# So this job contributes ~16 points of load and is exactly what pushes the box over bot_manager's 36
# shed threshold: without it the machine sits ~23-25, comfortably clear. Correlation had been argued
# both ways from watching load move; freezing the process settled it in 75 seconds.
#
# WHY it costs that much is the flat-directory ceiling above: macOS load counts threads in
# uninterruptible disk wait, and every image spends ~1.4 s blocked on a directory insert. N images/s
# therefore costs ~1.4N load, whatever the worker count. Backing off trades throughput for load at a
# fixed rate; only sharding removes the trade. That makes the layout fix a fleet-health matter, not
# just a speed one.
#
# 34/26: back off just under the fleet's threshold, release with real headroom. Live auctions are
# unrepeatable and this backfill is not, so the backfill yields.
#
# Earlier note, kept because the reasoning still holds for why a threshold must not sit too low: load
# on this box swings 16 -> 68 within a minute purely from Chrome renderers starting and stopping for the capture
# bots. Any threshold near that band throttles this job continuously on a signal it does not drive, and
# both 28 and 45 did exactly that while the fleet sat happily at 2/2 target and the 6TB sat idle.
# bot_manager already has its OWN governor and sheds its own bots when it needs to; a second guard here,
# keyed on a number this job barely moves, is redundant cost rather than safety. 80 is above anything
# observed except the genuine 2026-09-13 crisis (89), so it now fires only in a real emergency.
# If this job ever does need to defer, the honest signal is fleet-target shortfall or disk6 saturation —
# not system load. Measure before lowering these again.


def live_capture_pressure(backed_off: bool) -> bool:
    """Whether to keep giving the machine to the live-capture fleet, given our current state."""
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        return False
    return load1 > LOAD_BACKOFF_OFF if backed_off else load1 >= LOAD_BACKOFF_ON


class KeepAlive:
    """One reusable connection per host, per worker.

    urllib.request.urlopen opens a fresh TCP+TLS connection for EVERY call. For ~10KB catalog
    thumbnails the handshake dominates the transfer. Measured 2026-09-14: 16 workers managed 5.5 req/s
    against an allowed 12.5, i.e. ~2.9 s per 10KB image, with the 6TB idle (75-128 tps) and the pace
    ceiling not the binding constraint — the time was going into per-request setup, not transfer.
    Every SCP catalog image is on one host, so a kept-alive connection removes that setup per image.
    """

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.conns: dict = {}

    def _close(self, ck) -> None:
        c = self.conns.pop(ck, None)
        if c is not None:
            try: c.close()
            except Exception: pass

    def get(self, url: str, headers: dict, max_bytes: int = 3_000_000, _hops: int = 3):
        parts = urllib.parse.urlsplit(url)
        ck = (parts.scheme, parts.netloc)
        conn = self.conns.get(ck)
        if conn is None:
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = cls(parts.netloc, timeout=self.timeout)
            self.conns[ck] = conn
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        try:
            conn.request("GET", path, headers={**headers, "Connection": "keep-alive"})
            r = conn.getresponse()
            data = r.read(max_bytes)
            if len(data) >= max_bytes:
                self._close(ck)          # oversized body: cheaper to drop the connection than drain it
            else:
                r.read()                 # a response must be fully consumed before the socket is reusable
            status, loc = r.status, r.getheader("Location")
        except Exception:
            self._close(ck)              # a half-used connection is poison; next call reconnects
            raise
        if status in (301, 302, 303, 307, 308) and loc and _hops > 0:
            return self.get(urllib.parse.urljoin(url, loc), headers, max_bytes, _hops - 1)
        return status, data

    def close_all(self) -> None:
        for ck in list(self.conns):
            self._close(ck)


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
        http_get = KeepAlive(timeout=20)
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
                    status, data = http_get.get(url, UA)
                    if status in (404, 410):
                        record(key, "dead", status, 0)
                    elif status in (403, 429):
                        record(key, "blocked", status, 0)
                    elif status != 200:
                        record(key, "err", status, 0)
                    elif len(data) < 512:
                        record(key, "small", status, len(data))
                    else:
                        os.makedirs(d, exist_ok=True)
                        with open(f + ".tmp", "wb") as fh: fh.write(data)
                        os.replace(f + ".tmp", f)
                        record(key, "ok", status, len(data))
            except Exception:
                record(key, "neterr", 0, 0)
            finally:
                q.task_done()
                if did_net:  # pace only real network hits; disk-skip resumes fly through at full speed
                    dt = time.time() - t0
                    if dt < pace[0]: time.sleep(pace[0] - dt)
        http_get.close_all()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    [t.start() for t in threads]
    fed = 0; dead_host = 0
    with open(cand, newline="") as fh:
        for key, url in csv.reader(fh):
            if stop.is_set(): break
            if host_of(url) in DEAD_HOSTS:
                dead_host += 1; continue  # origin is gone — skip without spending a request or a failure slot
            # every 200, not 2000: the check is two syscalls, but at a paced 8 req/s a 2000-item interval
            # is ~4 minutes of reaction time — long enough for load to sit over bot_manager's shed
            # threshold and cost live capture bots before we noticed. Cheap checks should be frequent.
            if fed % 200 == 0:
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
