#!/usr/bin/env python3
"""Read-only HTTP server for the comp-image archive on the 6TB.

The images have been landing correctly for a while; nothing could reach them. This closes that gap
without a copy step, so a photo is servable the moment the backfill writes it.

Deliberately more than a plain static mount, because two things about the archive are awkward and
both are cheap to solve here rather than in every client:

  * extensions differ by source -- eBay serves WebP, everything else JPEG. /img/ finds whichever
    01.* exists, so callers never have to guess.
  * SCP images are keyed by sha1(image_url)[:20], not by the sale's item id. /r does that hash
    server-side, so a client with a canonical row needs no crypto at all.

Endpoints (GET/HEAD only -- there is no write path):
  /                      usage
  /healthz               json: per-source counts, disk free
  /img/<source>/<key>    the image; extension resolved automatically
  /r?u=<image_url>       302 to the right /img/ path for a pricecharting/SCP image URL

Serving:
  /usr/bin/python3 external_engine/comp_image_server.py [--port 8510] [--host 0.0.0.0]

EXPOSURE -- READ THIS BEFORE CHANGING ANYTHING. As of 2026-09-22 this is INTERNET-FACING: the
Mini publishes it through Tailscale Funnel at https://stavross-mac-mini.tail9fccf8.ts.net, because
Vercel cannot reach a LAN address and the front end needs the photos. It used to say "LAN only, do
not put it on a public interface"; that is no longer true and pretending otherwise is how a wrong
assumption gets built on.

What that means in practice: there is NO authentication, so everything reachable here is public to
anyone who learns the hostname. That is acceptable only because the surface is deliberately tiny --
GET/HEAD on archived card photos, keys that must match KEY_RE, paths that must resolve inside ROOT,
and every write verb answered 405. Keep it that way. Do not add an endpoint that lists keys, reads
an arbitrary path, echoes a query back, or accepts a body. `tailscale funnel reset` takes it back
off the internet.

TCC: the 6TB is unreadable to launchd GUI agents. Run it from the supervisor (which is started
through ssh localhost and inherits Full Disk Access), not from a plain LaunchAgent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("MAZI_COMP_IMAGES_ROOT", "/Volumes/MAZI_EVIDENCE_6TB/comp_images")
SOURCES = ("scp_catalog", "ebay", "fanatics", "tcgplayer_catalog")
# keys are hashes or marketplace item ids; anything with a separator in it is not one of ours
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
EXTS = (".jpg", ".webp", ".png", ".jpeg")
CTYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".png": "image/png"}
SCP_HOST_HINT = "pricecharting.com"

# ---------------------------------------------------------------------------------------------
# READ-THROUGH SSD CACHE
#
# The archive lives on a 6TB spinning disk that is also the write target of a 32-worker downloader.
# Measured 2026-09-22 over the public Funnel URL, cold: 2.9-11.1 s per image. The same images once
# in the page cache: ~0.22 s. The cost is seek, not bandwidth -- the SMALLEST files were the SLOWEST
# (an 11KB SCP jpg took 7.5 s; a 534KB eBay webp took 2.9 s), which is the signature of a head
# moving across 4.9M directories rather than of a link running out of throughput.
#
# So: serve from the 6TB once, keep a copy on the internal SSD, serve every later hit from there.
# A front end re-requests the same popular cards constantly, so the hit rate is what matters and a
# modest cache covers it. This is a pure accelerator -- the 6TB stays the only source of truth, and
# every failure path here falls back to serving directly rather than erroring.
CACHE_DIR = os.environ.get("MAZI_IMAGE_CACHE_DIR", os.path.expanduser("~/.cache/mazi_comp_images"))
CACHE_MAX_GB = float(os.environ.get("MAZI_IMAGE_CACHE_MAX_GB", "20"))
CACHE_ENABLED = CACHE_DIR.lower() not in ("", "off", "none", "0")
CACHE_MIN_FREE_GB = 8.0   # never fill the boot volume; below this we serve but stop caching
_CSTATS = {"hit": 0, "miss": 0, "write": 0, "evicted": 0, "bytes": 0, "writable": True}


# ---------------------------------------------------------------------------------------------
# CARD LOOKUP BY SLUG
#
# The front end identifies a comp's card by raw->>'slug' -- the SportsCardsPro console/product slug,
# e.g. basketball-cards-1996-topps-chrome/kobe-bryant-138 (proven 2026-09-22 from its read-only
# role's query shapes). It does NOT have image keys, and deriving one means shipping it a 96MB map.
# So: accept the slug it already holds and resolve it here. /card/<console>/<product> serves the
# card's single RAW reference photo (the SCP catalogue image: no slab, no grade -- verified by eye
# across sources), so one URL per MAZI id works for every grade of the card.
#
# Surface stays tiny on purpose: exact lookup of a strictly-shaped slug, no listing, no prefix
# search, no echo. The map is the slug-keyed refmap built from the SCP catalogue (393,161 cards).
# SSD copy first: a 96MB read off the 6TB while the downloader owns the spindle took >9s on the
# first try (the whole endpoint answered 503 meanwhile). The 6TB original is the fallback and the
# source of truth; refresh the SSD copy whenever the refmap is rebuilt.
SLUG_MAP_CANDIDATES = tuple(p for p in (
    os.environ.get("MAZI_CARD_SLUG_MAP", ""),
    os.path.expanduser("~/.cache/mazi_card_map/card_reference_images_by_slug.csv"),
    os.path.join(ROOT, "_backfill", "refmap", "card_reference_images_by_slug.csv"),
) if p)
SLUG_SEG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,118}$")
_SLUG_MAP: dict = {"map": None, "error": None, "at": 0.0, "path": None}


def _load_slug_map() -> None:
    """Parse the refmap once, off-thread; publish the dict atomically when complete.

    Catches EVERYTHING, not just OSError: a malformed row raising inside a daemon thread would
    otherwise kill the loader silently and leave /card/ answering 503 forever with error=None,
    which is exactly what the first smoke test looked like."""
    import csv
    t0 = time.time()
    for path in SLUG_MAP_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            m: dict = {}
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    slug, key = (row.get("slug") or "").strip().lower(), (row.get("image_key") or "").strip()
                    if slug and key and KEY_RE.match(key):
                        m[slug] = key
            _SLUG_MAP.update(map=m, at=time.time(), error=None, path=path)
            sys.stderr.write(f"card slug map loaded: {len(m):,} cards from {path} in {time.time()-t0:.1f}s\n")
            return
        except Exception as e:  # noqa: BLE001 -- see docstring
            _SLUG_MAP["error"] = f"{path}: {type(e).__name__}: {e}"
            sys.stderr.write(f"card slug map failed on {path}: {e}\n")
    if _SLUG_MAP["error"] is None:
        _SLUG_MAP["error"] = "no map file found at any of: " + ", ".join(SLUG_MAP_CANDIDATES)
    sys.stderr.write(f"card slug map NOT loaded: {_SLUG_MAP['error']}\n")


def card_image_key(console: str, product: str) -> str | None:
    m = _SLUG_MAP["map"]
    if m is None:
        return None
    console, product = (console or "").lower(), (product or "").lower()
    if not (SLUG_SEG_RE.match(console) and SLUG_SEG_RE.match(product)):
        return None
    return m.get(f"{console}/{product}")


def cache_base(source: str, key: str) -> str:
    """Sharded two levels, same reason as the archive itself: a flat directory of millions of
    siblings is where the last performance cliff was. One file per key here (not a directory),
    so the fan-out is a quarter of the archive's."""
    k = (key or "").lower()
    return os.path.join(CACHE_DIR, source, k[:2] or "__", k[2:4] or "__", key)


def cached_file(source: str, key: str) -> str | None:
    """Cache hit path. Four stats on an SSD is microseconds; it is never worth an index."""
    if not CACHE_ENABLED:
        return None
    base = cache_base(source, key)
    for ext in EXTS:
        p = base + ext
        if os.path.isfile(p):
            return p
    return None


def _cache_sweep() -> None:
    """Hold the cache under budget, oldest-access-first, and clear abandoned .tmp files.

    Walks only the cache (bounded, on SSD) -- never the archive. Deleting here can only ever cost a
    re-read from the 6TB, so this is deliberately blunt: on any error it gives up quietly rather
    than risking a half-applied policy."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        st = os.statvfs(CACHE_DIR)
        free_gb = st.f_bavail * st.f_frsize / 1e9
        _CSTATS["writable"] = free_gb > CACHE_MIN_FREE_GB
        budget = CACHE_MAX_GB * 1e9
        files, total, now = [], 0, time.time()
        for dirpath, _, names in os.walk(CACHE_DIR):
            for n in names:
                p = os.path.join(dirpath, n)
                try:
                    stt = os.stat(p)
                except OSError:
                    continue
                if n.endswith(".tmp"):
                    # a partial write whose request died; nothing will ever claim it
                    if now - stt.st_mtime > 3600:
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
                    continue
                files.append((stt.st_atime, stt.st_size, p))
                total += stt.st_size
        _CSTATS["bytes"] = total
        if total <= budget:
            return
        files.sort()                       # least-recently-accessed first
        for _, sz, p in files:
            if total <= budget * 0.9:      # trim to 90% so this does not run every sweep
                break
            try:
                os.unlink(p)
                total -= sz
                _CSTATS["evicted"] += 1
            except OSError:
                pass
        _CSTATS["bytes"] = total
    except OSError:
        pass


def _cache_sweeper() -> None:
    while True:
        _cache_sweep()
        time.sleep(1800)



def image_path(source: str, key: str) -> str | None:
    """Absolute path to this key's image, or None. Rejects anything that escapes ROOT.

    Storage is sharded (<source>/<k0:2>/<k2:4>/<key>/) as of 2026-09-17 because a flat directory of
    1.4M siblings made each new write take >12 s. Existing images were NOT moved, so both layouts are
    live: sharded is checked first since that is where everything new lands, flat second so the ~1.5M
    already on disk keep resolving. Callers -- including the front end -- see one stable URL either way,
    which is the whole reason the layout could change without anyone having to be told."""
    if source not in SOURCES or not KEY_RE.match(key or ""):
        return None
    k = key.lower()
    candidates = (
        os.path.join(ROOT, source, k[:2] or "__", k[2:4] or "__", key),   # sharded
        os.path.join(ROOT, source, key),                                   # legacy flat
    )
    root_real = os.path.realpath(ROOT) + os.sep
    for base in candidates:
        base = os.path.realpath(base)
        if not base.startswith(root_real):
            continue                     # belt and braces: KEY_RE already bars separators
        for ext in EXTS:
            p = base + os.sep + "01" + ext
            if os.path.isfile(p):
                return p
    return None


def scp_key(image_url: str) -> str:
    return hashlib.sha1(image_url.encode()).hexdigest()[:20]


_COUNTS_CACHE: dict = {"at": 0.0, "val": None}
COUNTS_TTL = 1800.0  # 30 min. The ledgers are 150MB+ and growing; this read is genuinely expensive
                     # and the numbers move by a few thousand an hour, so freshness costs more than
                     # it is worth.
COUNTS_FILE = os.path.join(ROOT, "_backfill", "server_counts_cache.json")


def _refresh_counts_async() -> None:
    """Recompute in the background so /healthz never blocks on it."""
    if _COUNTS_CACHE.get("refreshing"):
        return
    _COUNTS_CACHE["refreshing"] = True
    def run():
        try:
            _compute_counts()
        finally:
            _COUNTS_CACHE["refreshing"] = False
    threading.Thread(target=run, daemon=True).start()


def _load_cache_from_disk() -> None:
    """Counts survive a restart. Recomputing on every start meant a 150MB ledger read racing the
    downloader for the same spindle -- measured 2026-09-22: image serving went from 8ms to 17.6s
    while that ran. The server restarts often (it is supervised); the read must not."""
    try:
        with open(COUNTS_FILE) as f:
            d = json.load(f)
        if isinstance(d.get("val"), dict):
            _COUNTS_CACHE["at"], _COUNTS_CACHE["val"] = d.get("at", 0.0), d["val"]
    except (OSError, ValueError):
        pass


def counts(ttl: float = COUNTS_TTL) -> dict:
    """Per-source image counts read from the backfill ledgers -- never by listing the directories,
    which hold up to 1.4M entries apiece and are as expensive to read as to write.

    Cached, because the ledgers are ~15MB and growing: uncached this took 21 s per call, which makes
    /healthz useless for the polling it exists for. Image serving never touches this path."""
    now = time.time()
    if _COUNTS_CACHE["val"] is None:
        # Cold: never block a request on the ledger read. Kick it off and answer honestly.
        _refresh_counts_async()
        return {"status": "counting", "note": "ledger scan in progress; retry shortly"}
    if _COUNTS_CACHE["val"] is not None:
        # STALE-WHILE-REVALIDATE. Always answer from cache, refresh behind it. A blocking recompute
        # made /healthz time out entirely once the ledgers passed ~40MB (observed 2026-09-20: >60s,
        # while image serving stayed at 8ms). A status endpoint that hangs is worse than one that is
        # five minutes out of date -- it reads as "the server is down".
        if now - _COUNTS_CACHE["at"] >= ttl:
            _refresh_counts_async()
        return _COUNTS_CACHE["val"]
    return _compute_counts()


def _compute_counts() -> dict:
    W = os.path.join(ROOT, "_backfill")
    out = {}
    for src in SOURCES:
        p = os.path.join(W, f"ledger_{src}.jsonl")
        have = set()
        try:
            with open(p, errors="ignore") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    k, s = r.get("k"), r.get("s")
                    if s in ("ok", "skip"):
                        have.add(k)
                    elif s:
                        have.discard(k)
        except OSError:
            pass
        out[src] = len(have)
    _COUNTS_CACHE["at"], _COUNTS_CACHE["val"] = time.time(), out
    try:
        tmp = COUNTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"at": _COUNTS_CACHE["at"], "val": out}, f)
        os.replace(tmp, COUNTS_FILE)
    except OSError:
        pass
    return out


USAGE = """comp image server

  /healthz                 per-source counts and disk free
  /img/<source>/<key>      the image (extension resolved for you)
  /r?u=<image_url>         302 to the image for a pricecharting/SCP url
  /card/<console>/<product>  the card's raw reference photo, by its SportsCardsPro slug

sources: {sources}

examples
  /img/scp_catalog/3108997300ae3e33f70d
  /img/ebay/206162825097
  /r?u=https%3A//storage.googleapis.com/images.pricecharting.com/abc/240.jpg
  /card/basketball-cards-1996-topps-chrome/kobe-bryant-138

read-only; GET and HEAD only.
""".format(sources=", ".join(SOURCES))


class Handler(BaseHTTPRequestHandler):
    server_version = "MaziCompImages/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")   # images only, no credentials, LAN only
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: str, cache_to: str | None = None) -> None:
        """Stream the file; if cache_to is given, tee it to the SSD on the way past.

        Teeing rather than re-reading matters: the 6TB read is the expensive part, and we already
        have the bytes in hand. The copy is only committed (atomic rename) after a COMPLETE send, so
        a client that disconnects mid-image can never leave a truncated file to be served forever."""
        ext = os.path.splitext(path)[1].lower()
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", CTYPE.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(size))
            self.send_header("Access-Control-Allow-Origin", "*")
            # archived originals never change in place; a new photo gets a new key
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            if self.command == "HEAD":
                return
            tmp = cf = None
            if cache_to and _CSTATS["writable"]:
                try:
                    os.makedirs(os.path.dirname(cache_to), exist_ok=True)
                    tmp = f"{cache_to}.{os.getpid()}.{threading.get_ident()}.tmp"
                    cf = open(tmp, "wb")
                except OSError:
                    tmp = cf = None          # caching is best-effort; serving is not
            try:
                with open(path, "rb") as f:
                    while chunk := f.read(256 * 1024):
                        self.wfile.write(chunk)
                        if cf:
                            cf.write(chunk)
                if cf:
                    cf.close()
                    cf = None
                    os.replace(tmp, cache_to)      # only now is it a complete, servable copy
                    tmp = None
                    _CSTATS["write"] += 1
                    # keep the running total honest between sweeps -- otherwise /healthz reports an
                    # empty cache for up to 30 min after it has filled, which reads as "not working"
                    _CSTATS["bytes"] += size
            finally:
                if cf:
                    try:
                        cf.close()
                    except OSError:
                        pass
                if tmp:
                    try:
                        os.unlink(tmp)             # partial send -> never promote it
                    except OSError:
                        pass
        except OSError:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        u = urllib.parse.urlsplit(self.path)
        parts = [p for p in u.path.split("/") if p]

        if not parts:
            return self._send(200, USAGE.encode(), "text/plain; charset=utf-8")

        if parts == ["healthz"]:
            body = json.dumps({
                "ok": os.path.isdir(ROOT),
                "root": ROOT,
                "images": counts(),
                "disk_free_gb": round(os.statvfs(ROOT).f_bavail * os.statvfs(ROOT).f_frsize / 1e9, 1),
                "card_map": {"cards": len(_SLUG_MAP["map"]) if _SLUG_MAP["map"] is not None else None,
                             "loaded": _SLUG_MAP["map"] is not None, "path": _SLUG_MAP["path"],
                             "error": _SLUG_MAP["error"]},
                "cache": {"enabled": CACHE_ENABLED, "dir": CACHE_DIR, "max_gb": CACHE_MAX_GB,
                          "used_gb": round(_CSTATS["bytes"] / 1e9, 2), "writable": _CSTATS["writable"],
                          "hit": _CSTATS["hit"], "miss": _CSTATS["miss"],
                          "written": _CSTATS["write"], "evicted": _CSTATS["evicted"]},
            }, indent=1).encode()
            return self._send(200, body, "application/json", {"Cache-Control": "no-store"})

        if parts[0] == "r":
            url = urllib.parse.parse_qs(u.query).get("u", [""])[0]
            if not url.startswith("http"):
                return self._send(400, b"pass ?u=<image_url>\n", "text/plain; charset=utf-8")
            if SCP_HOST_HINT not in url:
                return self._send(422, b"only pricecharting/SCP urls are derivable; other sources are "
                                       b"keyed by item id, use /img/<source>/<item_id>\n",
                                  "text/plain; charset=utf-8")
            return self._send(302, b"", "text/plain; charset=utf-8",
                              {"Location": f"/img/scp_catalog/{scp_key(url)}", "Cache-Control": "no-store"})

        if parts[0] == "card" and len(parts) == 3:
            if _SLUG_MAP["map"] is None:
                return self._send(503, b"card map still loading; retry shortly\n",
                                  "text/plain; charset=utf-8", {"Retry-After": "10", "Cache-Control": "no-store"})
            key = card_image_key(parts[1], parts[2])
            if not key:
                return self._send(404, b"no reference image for that card slug\n", "text/plain; charset=utf-8")
            hit = cached_file("scp_catalog", key)
            if hit:
                _CSTATS["hit"] += 1
                return self._send_file(hit)
            p = image_path("scp_catalog", key)
            if p:
                _CSTATS["miss"] += 1
                return self._send_file(p, cache_to=cache_base("scp_catalog", key) + os.path.splitext(p)[1].lower())
            return self._send(404, b"card known but its image is not in the archive\n", "text/plain; charset=utf-8")

        if parts[0] == "img" and len(parts) == 3:
            source, key = parts[1], parts[2]
            hit = cached_file(source, key) if (source in SOURCES and KEY_RE.match(key or "")) else None
            if hit:
                _CSTATS["hit"] += 1
                return self._send_file(hit)                 # SSD; already the fast copy
            p = image_path(source, key)
            if p:
                _CSTATS["miss"] += 1
                ext = os.path.splitext(p)[1].lower()
                return self._send_file(p, cache_to=cache_base(source, key) + ext)
            return self._send(404, b"no image for that source/key\n", "text/plain; charset=utf-8")

        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        self._send(405, b"read-only\n", "text/plain; charset=utf-8", {"Allow": "GET, HEAD"})

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("MAZI_IMAGE_SERVER_PORT", 8510)))
    ap.add_argument("--host", default=os.environ.get("MAZI_IMAGE_SERVER_HOST", "0.0.0.0"))
    a = ap.parse_args()
    if not os.path.isdir(ROOT):
        sys.stderr.write(f"image root not readable: {ROOT}\n"
                         "(on this Mini that usually means TCC -- run from the supervisor/ssh, "
                         "not a LaunchAgent)\n")
        return 2
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    # Warm the counts cache off-thread. Cold it is a ~26 s ledger read, and the first person to hit
    # /healthz should not conclude the server is hung. Images never wait on this.
    _load_cache_from_disk()          # instant if a previous run left counts behind
    threading.Thread(target=counts, daemon=True).start()
    threading.Thread(target=_load_slug_map, daemon=True).start()   # ~3s; /card/ answers 503 until done
    if CACHE_ENABLED:
        _cache_sweep()                                                  # set `writable` before serving
        threading.Thread(target=_cache_sweeper, daemon=True).start()
        sys.stderr.write(f"ssd cache {CACHE_DIR} max={CACHE_MAX_GB}GB writable={_CSTATS['writable']}\n")
    sys.stderr.write(f"comp image server on http://{a.host}:{a.port}  root={ROOT}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
