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

TWO DOORS (2026-10-01, "lock down the mini image server")
---------------------------------------------------------
Until 2026-10-01 one listener on 0.0.0.0:8510 served the WHOLE archive (~5.9M photos: 4.27M eBay
seller photos, 1.2M Fanatics, 425K SCP) to anyone, because the Mini publishes it through Tailscale
Funnel at https://stavross-mac-mini.tail9fccf8.ts.net. eBay item ids are public and guessable. The
site only ever shows ~398K of those pictures. So the server now has two doors in one process:

  PUBLIC door    127.0.0.1:8510 (default). Tailscale Serve/Funnel proxies here and ONLY here, so
                 everything that arrives through the hostname -- public visitors and tailnet devices
                 alike -- is treated as public. Answers:
                   GET/HEAD /img/<source>/<key>  only when the key is on the PUBLIC ALLOW-LIST
                                                 (every image key beta_catalog.image_small/_large
                                                 references, built by tools/build_image_allowlist.py,
                                                 plus an operator "staged" file), or the source is a
                                                 curated lot-photo source (lotphoto_goldin,
                                                 lotphoto_fanatics -- the whole folder, ~198 files; the
                                                 catalog-art apply verifies the served sha256 BEFORE the
                                                 beta row exists, so they must be reachable first).
                   GET/HEAD /healthz             {"ok": true|false} and nothing else.
                 Everything else -- unlisted keys, the bulk ebay/fanatics/tcgplayer archives, /card/,
                 /r, the usage page -- gets ONE identical 404 (same status, body and headers as a
                 real miss), decided in memory BEFORE any disk access. So the public side can't be
                 used to learn which keys exist, and random keys no longer cost 6TB seeks.
                 Lot photos are NEVER served from or copied to the SSD cache (UNCACHED_SOURCES), on
                 either door, and get a 1 h Cache-Control instead of "immutable": deleting the 6TB
                 file takes a lot photo down at once, and a lot photo re-fetched under the same key
                 is served (and sha256-checked) from the new file, never from a stale copy.

  INTERNAL door  <tailnet IP>:8512 (default "tailnet:8512": the Mini's own 100.64/10 and
                 fd7a:115c:a1e0::/48 addresses, read from ifconfig). The full, previous behaviour:
                 every source, /card/, /r, usage, the full /healthz JSON. It is NEVER behind
                 Serve/Funnel; OTHER tailnet devices reach it at
                 http://stavross-mac-mini.tail9fccf8.ts.net:8512 (MagicDNS + WireGuard). A connection is
                 refused unless its SOURCE ADDRESS is in the internal allow (default: the tailnet ranges
                 only) AND is not one of this machine's own addresses. Both halves matter: Serve/Funnel
                 (and anything else running on the Mini) connects from 127.0.0.1 when its target is
                 localhost, but from the Mini's OWN tailnet address when its target is
                 http://<tailnet-ip>:8512 or http://<hostname>:8512. Refusing the Mini's own addresses
                 (re-read from ifconfig every 30 s, plus every address the door binds) means a Serve or
                 Funnel pointed at this port by mistake -- by any of those targets -- still cannot
                 publish the archive. Tools on the Mini itself read the files directly instead.
                 (Loopback is only ever allowed if --internal-allow names a loopback range: tests.)

Access is decided by which socket a connection arrived on plus its TCP source address -- never by a
request header. Tailscale's identity headers (Tailscale-User-Login, Tailscale-Funnel-Request) are
only LOGGED as present/absent, because it has not been verified on this box that tailscaled strips
client-sent copies. A spoofed header changes nothing.

ALLOW-LIST FILES (see tools/build_image_allowlist.py and docs/IMAGE_SERVER_RUNBOOK.md)
  ~/mazi_local_evidence/image_allowlist/public_allowlist.txt   (MAZI_IMAGE_ALLOWLIST)
      "# mazi-image-allowlist v1" / "<source>/<key>" lines / "# end count=N". Loaded at start,
      re-read when it changes (polled), and a file that fails to parse is IGNORED: the last good
      list stays in force.
  ~/mazi_local_evidence/image_allowlist/public_allowlist.last_good.txt
      written by THIS server after every successful load (byte copy of what it parsed). The
      supervisor restarts this process often; if public_allowlist.txt is missing or does not parse
      at a restart, the server loads this copy instead and says so loudly (FALLBACK) in the log.
      Only if neither loads does the public door fail CLOSED for the bulk sources (ebay, fanatics,
      tcgplayer_catalog); scp_catalog and the lot photos stay up so the site does not go blank.
  ~/mazi_local_evidence/image_allowlist/staged.txt             (MAZI_IMAGE_ALLOWLIST_STAGED)
      optional, hand-edited: "<source>/<key>" or full ".../img/<source>/<key>" URLs, one per line.
      For keys about to be published (e.g. the VeeFriends apply HEADs the public URL BEFORE it
      writes the beta row). Staged keys do NOT expire: a staged key stays public until it is
      removed from this file, even if its beta row is withdrawn. The builder reports staged keys
      that are already listed (safe to drop; --prune-staged drops them) and those that are not in
      the beta. Deleting the file un-stages everything. A staged file that does not parse keeps the
      last good staged set, also across restarts (staged.last_good.txt).
  ~/mazi_local_evidence/image_allowlist/mode                   (MAZI_IMAGE_PUBLIC_MODE_FILE)
      optional: one word, "enforce" or "observe". Polled with the lists, so the public door can be
      switched without restarting anything (the supervisor starts this server with no arguments).
      No file = the --public-mode default (enforce). Anything else in the file = enforce, logged.

Endpoints (GET/HEAD only -- there is no write path; write verbs get 405 on both doors):
  /                      usage                                         (internal)
  /healthz               json: per-source counts, disk free, allow-list (internal; public: ok only)
  /img/<source>/<key>    the image; extension resolved automatically   (public: allow-listed only)
  /r?u=<image_url>       302 to the right /img/ path for a pricecharting/SCP image URL (internal)
  /card/<console>/<prod> the card's raw reference photo by SportsCardsPro slug           (internal)

Serving:
  /usr/bin/python3 external_engine/comp_image_server.py
      [--public-bind 127.0.0.1:8510] [--internal-bind tailnet:8512|off|HOST:PORT[,..]]
      [--internal-allow 100.64.0.0/10,fd7a:115c:a1e0::/48] [--allowlist PATH] [--staged PATH]
      [--public-mode enforce|observe] [--mode-file PATH|''] [--access-log PATH|-]
  observe serves the public door like the internal one but logs what enforce WOULD have denied
  (decision=would_deny:...). Enforce is the default; the mode file overrides it while it exists.

TAKEDOWN (full runbook: docs/IMAGE_SERVER_RUNBOOK.md): remove the picture from the beta, rebuild the
list (the builder refuses a big shrink without --allow-shrink), remove the key from staged.txt if it
is there, and for a lot photo delete its 6TB folder (lot photos are never cached, so that is
immediate). Check the access log for "deny:unlisted" on the key.

Logging: lifecycle and allow-list events go to stderr (the supervisor's comp_image_server.out).
Requests go to a rotating access log (~/Library/Logs/mazi_external_comps/comp_image_access.log,
20 MB x 5): door, mode, decision, verb, status, bytes, path, a coarse peer class
(loopback/tailnet/lan/other -- never the address), header PRESENCE flags for Tailscale-User-Login
(tsu) and Tailscale-Funnel-Request (tsf) -- never values --, a user-agent family and the referer's
host only.

TCC: the 6TB is unreadable to launchd GUI agents. Run it from the supervisor (which is started
through ssh localhost and inherits Full Disk Access), not from a plain LaunchAgent.
`tailscale funnel reset` takes the public door off the internet entirely.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("MAZI_COMP_IMAGES_ROOT", "/Volumes/MAZI_EVIDENCE_6TB/comp_images")
# lotphoto_*: the front photo of a headline sale's own Goldin / Fanatics lot (operator exception 2026-10-01, CLAUDE.md
# Hard Gates) -- display art only, one folder per house so a house can be removed in one step; tools/lotphoto_fetch.py
SOURCES = ("scp_catalog", "ebay", "fanatics", "tcgplayer_catalog", "lotphoto_goldin", "lotphoto_fanatics")
# keys are hashes or marketplace item ids; anything with a separator in it is not one of ours
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
EXTS = (".jpg", ".webp", ".png", ".jpeg")
CTYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".png": "image/png"}
SCP_HOST_HINT = "pricecharting.com"


def valid_key(key: str | None) -> bool:
    """KEY_RE, and never a dots-only key ('.', '..'): those are path components, not ids."""
    return bool(key) and bool(KEY_RE.match(key)) and key.strip(".") != ""


# ---------------------------------------------------------------------------------------------
# PUBLIC ALLOW-LIST
#
# What the public door may serve. Built from the beta by tools/build_image_allowlist.py (every
# /img/<source>/<key> that beta_catalog.image_small / image_large points at on our host), plus an
# optional hand-edited staged file. Held in memory as {source: frozenset(keys)} (~400K keys, ~45 MB).
ALLOWLIST_MAGIC = "# mazi-image-allowlist v1"
ALLOWLIST_DIR = os.path.expanduser("~/mazi_local_evidence/image_allowlist")
ALLOWLIST_PATH = os.environ.get("MAZI_IMAGE_ALLOWLIST", os.path.join(ALLOWLIST_DIR, "public_allowlist.txt"))
STAGED_PATH = os.environ.get("MAZI_IMAGE_ALLOWLIST_STAGED", os.path.join(ALLOWLIST_DIR, "staged.txt"))
MODE_PATH = os.environ.get("MAZI_IMAGE_PUBLIC_MODE_FILE", os.path.join(ALLOWLIST_DIR, "mode"))
ALLOWLIST_POLL_S = float(os.environ.get("MAZI_IMAGE_ALLOWLIST_POLL_S", "30"))
ALLOWLIST_MAX_BYTES = 256 * 1024 * 1024
PUBLIC_MODES = ("enforce", "observe")
# curated, small, reviewed one by one: the whole folder is public (see module docstring)
PUBLIC_WHOLE_SOURCES = ("lotphoto_goldin", "lotphoto_fanatics")
# never read from or written to the SSD cache, on either door: a whole-folder-public source is taken down by
# deleting its 6TB file, and a cached copy would outlive that (and would be what a re-fetch's sha256 check saw)
UNCACHED_SOURCES = PUBLIC_WHOLE_SOURCES
UNCACHED_CACHE_CONTROL = "public, max-age=3600"     # can be taken down / re-fetched under the same key
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
# if NO list has ever loaded: these stay public so the site does not go blank; everything else 404s
NO_LIST_OPEN_SOURCES = ("scp_catalog",)
_END_RE = re.compile(r"^# end count=(\d+)$")
_META_RE = re.compile(r"^#\s*([a-z_]+)=(\S*)")
_IMG_PATH_RE = re.compile(r"^(?:https?://[^/\s]+)?/img/([A-Za-z0-9_]+)/([^/?#\s]+)/?$")


class AllowListError(ValueError):
    pass


def _entry(source: str, key: str, where: str) -> tuple:
    if source not in SOURCES:
        raise AllowListError(f"{where}: unknown source {source!r}")
    if not valid_key(key):
        raise AllowListError(f"{where}: invalid key")
    return source, key


def parse_allowlist(src) -> tuple[dict, dict]:
    """src: the file's text, or any iterable of its lines (an open file streams it).
    -> ({source: frozenset(keys)}, meta). Strict: a missing magic line, a missing or wrong
    '# end count=N' trailer (= a truncated or half-written file), anything after the trailer, or any
    malformed entry rejects the WHOLE file, so a bad build can never half-apply."""
    lines = src.splitlines() if isinstance(src, str) else src
    sets: dict = {s: set() for s in SOURCES}
    meta: dict = {}
    n, end, first = 0, None, True
    for i, raw in enumerate(lines, start=1):
        ln = raw.strip()
        if first:
            if ln != ALLOWLIST_MAGIC:
                raise AllowListError("missing header line %r" % ALLOWLIST_MAGIC)
            first = False
            continue
        if not ln:
            continue
        if end is not None:
            raise AllowListError(f"line {i}: content after the '# end' trailer")
        if ln.startswith("#"):
            m = _END_RE.match(ln)
            if m:
                end = int(m.group(1))
                continue
            mm = _META_RE.match(ln)
            if mm:
                meta[mm.group(1)] = mm.group(2)
            continue
        source, sep, key = ln.partition("/")
        if not sep:
            raise AllowListError(f"line {i}: not <source>/<key>")
        s, k = _entry(source, key, f"line {i}")
        sets[s].add(k)
        n += 1
    if first:
        raise AllowListError("empty file")
    if end is None:
        raise AllowListError("missing '# end count=N' trailer (truncated file?)")
    if n != end:
        raise AllowListError(f"trailer says {end} entries, file has {n}")
    return {s: frozenset(v) for s, v in sets.items()}, meta


def parse_staged(text: str) -> dict:
    """Lenient format for the hand-edited staged file: '<source>/<key>' or a URL ending in
    /img/<source>/<key>; blank lines and #comments ignored. Any malformed line rejects the file."""
    sets: dict = {s: set() for s in SOURCES}
    for i, raw in enumerate(text.splitlines(), start=1):
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        m = _IMG_PATH_RE.match(ln)
        if m:
            source, key = m.group(1), m.group(2)
        else:
            source, sep, key = ln.partition("/")
            if not sep:
                raise AllowListError(f"staged line {i}: not <source>/<key> or an /img/ URL")
        s, k = _entry(source, key, f"staged line {i}")
        sets[s].add(k)
    return {s: frozenset(v) for s, v in sets.items() if v}


def format_allowlist(entries: dict, meta: dict | None = None) -> str:
    """The on-disk form parse_allowlist() accepts. Sorted, so two builds of the same data diff clean."""
    out = [ALLOWLIST_MAGIC]
    for k, v in (meta or {}).items():
        out.append(f"# {k}={v}")
    n = 0
    for source in SOURCES:
        for key in sorted(entries.get(source) or ()):
            _entry(source, key, "format")
            out.append(f"{source}/{key}")
            n += 1
    out.append(f"# end count={n}")
    return "\n".join(out) + "\n"


def _file_sig(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _check_size(path: str) -> None:
    if os.path.getsize(path) > ALLOWLIST_MAX_BYTES:
        raise AllowListError(f"file larger than {ALLOWLIST_MAX_BYTES} bytes")


def _read_bytes(path: str) -> tuple[bytes, tuple]:
    """-> (content, sig). The sig comes from the SAME open file descriptor the bytes were read from, so it
    always describes exactly what was parsed (the builder may replace the file between a stat and an open)."""
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        if st.st_size > ALLOWLIST_MAX_BYTES:
            raise AllowListError(f"file larger than {ALLOWLIST_MAX_BYTES} bytes")
        data = f.read(ALLOWLIST_MAX_BYTES + 1)
    if len(data) > ALLOWLIST_MAX_BYTES:
        raise AllowListError(f"file larger than {ALLOWLIST_MAX_BYTES} bytes")
    return data, (st.st_mtime_ns, st.st_size, st.st_ino)


def _read_text(path: str) -> str:
    return _read_bytes(path)[0].decode("utf-8")


def load_allowlist_file(path: str) -> tuple[dict, dict]:
    _check_size(path)
    with open(path, encoding="utf-8") as f:
        return parse_allowlist(f)


def last_good_path_for(path: str) -> str:
    """public_allowlist.txt -> public_allowlist.last_good.txt (staged.txt -> staged.last_good.txt)."""
    root, ext = os.path.splitext(path)
    return f"{root}.last_good{ext or '.txt'}"


def _write_atomic_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(path) or "."
    tmp = os.path.join(d, f".{os.path.basename(path)}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class AllowList:
    """The public allow-list plus the staged file, reloaded when either changes on disk.

    Readers take one reference to the current dict and never see a half-built one (the swap is a
    single attribute assignment). A reload that fails leaves the previous list in force -- in memory
    while this process lives, and across restarts through the *.last_good.txt copies written after
    every successful load."""

    def __init__(self, path: str = ALLOWLIST_PATH, staged_path: str | None = STAGED_PATH,
                 log=None, last_good_path: str | None = "", staged_last_good_path: str | None = "") -> None:
        # last_good_path: "" = derive from path (public_allowlist.last_good.txt); None = no on-disk copy
        self.path, self.staged_path = path, staged_path
        self.last_good_path = last_good_path_for(path) if last_good_path == "" else last_good_path
        self.staged_last_good_path = (last_good_path_for(staged_path) if staged_last_good_path == "" and staged_path
                                      else (staged_last_good_path or None))
        self._main: dict | None = None          # None = never loaded
        self._staged: dict = {}
        self._sig_main = None
        self._sig_staged = "init"                # first reload always looks at the staged file
        self._sha_last_good = self._sha_staged_last_good = None
        self._log = log or (lambda msg: sys.stderr.write(msg + "\n"))
        self._lock = threading.Lock()
        self.status: dict = {"loaded": False, "path": path, "staged_path": staged_path,
                             "counts": None, "total": 0, "meta": {}, "loaded_at": None,
                             "loaded_from": None, "last_good_path": self.last_good_path,
                             "last_error": None, "last_error_at": None, "staged_total": 0,
                             "staged_error": None, "staged_loaded_from": None}

    # -- queries ---------------------------------------------------------------------------------
    def loaded(self) -> bool:
        return self._main is not None

    def contains(self, source: str, key: str) -> bool:
        m = self._main
        if m is not None and key in m.get(source, ()):
            return True
        return key in self._staged.get(source, ())

    # -- loading ---------------------------------------------------------------------------------
    def maybe_reload(self) -> None:
        with self._lock:
            self._reload_main()
            self._reload_staged()

    def _reload_main(self) -> None:
        sig = _file_sig(self.path)
        if sig is None:
            if self._sig_main != "missing":
                self._sig_main = "missing"
                if self._main is None:
                    self._fail(f"allow-list file not found: {self.path}")
                else:
                    self._fail(f"allow-list file disappeared: {self.path}; keeping the last good list "
                               f"({self.status['total']:,} keys)")
            return
        if sig == self._sig_main:
            return
        try:
            data, sig = _read_bytes(self.path)
            sets, meta = parse_allowlist(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, AllowListError) as e:
            self._sig_main = sig                 # do not re-parse the same bad file every poll
            keep = (f"keeping the last good list ({self.status['total']:,} keys, built_at="
                    f"{self.status['meta'].get('built_at', '?')})" if self._main is not None else
                    "no list loaded yet in this process")
            self._fail(f"allow-list reload FAILED ({type(e).__name__}: {e}); {keep}")
            return
        was_fallback = self.status["loaded_from"] == "last_good"
        self._sig_main = sig
        self._install(sets, meta, "main", self.path)
        if was_fallback:
            self._log(f"allow-list FALLBACK OVER: {self.path} loads again")
        # only now, with the bytes proven good, does this become the copy a restart falls back to
        self._sha_last_good = self._save_copy(self.last_good_path, data, self._sha_last_good)

    def _install(self, sets: dict, meta: dict, loaded_from: str, path: str) -> None:
        self._main = sets
        counts = {s: len(v) for s, v in sets.items() if v}
        total = sum(counts.values())
        self.status.update(loaded=True, counts=counts, total=total, meta=meta, loaded_from=loaded_from,
                           loaded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if loaded_from == "main":
            self.status["last_error"] = None
        self._log(f"public allow-list loaded: {total:,} keys "
                  f"({', '.join(f'{s} {n:,}' for s, n in counts.items())}) built_at={meta.get('built_at', '?')} "
                  f"from {path}")

    def _load_last_good(self) -> bool:
        """Only used while no list is in memory (i.e. right after a restart): the on-disk copy of the last list
        this server accepted."""
        p = self.last_good_path
        if not p:
            return False
        try:
            data, _ = _read_bytes(p)
            sets, meta = parse_allowlist(data.decode("utf-8"))
        except FileNotFoundError:
            self._log(f"no last-good allow-list copy at {p}")
            return False
        except (OSError, UnicodeDecodeError, AllowListError) as e:
            self._log(f"last-good allow-list copy {p} is unusable too ({type(e).__name__}: {e})")
            return False
        self._install(sets, meta, "last_good", p)
        self._sha_last_good = hashlib.sha256(data).hexdigest()
        self._log(f"!!! PUBLIC ALLOW-LIST FALLBACK: {self.path} is missing or bad, so the public door is serving "
                  f"the LAST GOOD copy {p} (built_at={meta.get('built_at', '?')}). Fix or rebuild "
                  f"{os.path.basename(self.path)}; the server switches back as soon as it loads.")
        return True

    def _save_copy(self, path: str | None, data: bytes, known_sha: str | None) -> str | None:
        """Atomically write data to path unless it already holds exactly that. Best effort: a failure here never
        touches the in-memory list. -> the sha256 now on disk (or known_sha if the write failed)."""
        if not path:
            return known_sha
        h = hashlib.sha256(data).hexdigest()
        if h == known_sha:
            return h
        try:
            old, _ = _read_bytes(path)
            if hashlib.sha256(old).hexdigest() == h:
                return h
        except (OSError, AllowListError):
            pass
        try:
            _write_atomic_bytes(path, data)
            return h
        except OSError as e:
            self._log(f"could not write the last-good copy {path} ({type(e).__name__}: {e}); the list in memory "
                      "is unaffected, but a restart could not fall back to it")
            return known_sha

    def _reload_staged(self) -> None:
        if not self.staged_path:
            return
        sig = _file_sig(self.staged_path)
        if sig == self._sig_staged:
            return
        if sig is None:
            if self._staged:
                self._log(f"staged allow-list removed: {self.staged_path}; 0 staged keys")
            self._staged, self._sig_staged = {}, None
            self.status.update(staged_total=0, staged_error=None, staged_loaded_from=None)
            # deleting staged.txt un-stages everything, and that has to survive a restart too
            p = self.staged_last_good_path
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError as e:
                    self._log(f"could not remove {p} ({e}); a later bad staged.txt could fall back to it")
            self._sha_staged_last_good = None
            return
        try:
            data, sig = _read_bytes(self.staged_path)
            staged = parse_staged(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, AllowListError) as e:
            self._sig_staged = sig
            self.status["staged_error"] = f"{type(e).__name__}: {e}"
            self._log(f"staged allow-list reload FAILED ({type(e).__name__}: {e})")
            if self.status["staged_loaded_from"] is None and self._load_staged_last_good():
                return
            self._log(f"keeping the last good staged list ({self.status['staged_total']:,} keys)")
            return
        self._staged, self._sig_staged = staged, sig
        total = sum(len(v) for v in staged.values())
        self.status.update(staged_total=total, staged_error=None, staged_loaded_from="staged")
        self._log(f"staged allow-list loaded: {total:,} keys from {self.staged_path}")
        self._sha_staged_last_good = self._save_copy(self.staged_last_good_path, data, self._sha_staged_last_good)

    def _load_staged_last_good(self) -> bool:
        p = self.staged_last_good_path
        if not p:
            return False
        try:
            data, _ = _read_bytes(p)
            staged = parse_staged(data.decode("utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, UnicodeDecodeError, AllowListError) as e:
            self._log(f"last-good staged copy {p} is unusable too ({type(e).__name__}: {e})")
            return False
        self._staged = staged
        self._sha_staged_last_good = hashlib.sha256(data).hexdigest()
        total = sum(len(v) for v in staged.values())
        self.status.update(staged_total=total, staged_loaded_from="last_good")
        self._log(f"!!! STAGED FALLBACK: {self.staged_path} does not parse; serving the last good staged copy {p} "
                  f"({total:,} keys) until it is fixed")
        return True

    def _fail(self, msg: str) -> None:
        self.status.update(last_error=msg, last_error_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self._log(msg)
        if self._main is None and not self._load_last_good():
            self._log("PUBLIC DOOR FAILING CLOSED for ebay, fanatics, tcgplayer_catalog (404 for every key) "
                      "until an allow-list loads; scp_catalog and lot photos stay public so the site does "
                      "not go blank")

    def poll_forever(self, every_s: float = ALLOWLIST_POLL_S) -> None:
        poll_forever([self], every_s, self._log)


class PublicMode:
    """enforce | observe for the public door. The --public-mode default, overridden while the mode file exists, so
    observe can be switched on and off without restarting the server (the supervisor starts it with no arguments).
    A mode file that cannot be read or holds anything else means ENFORCE (fail closed), logged loudly."""

    def __init__(self, default: str = "enforce", path: str | None = MODE_PATH, log=None) -> None:
        if default not in PUBLIC_MODES:
            raise ValueError(f"public mode must be one of {PUBLIC_MODES}, not {default!r}")
        self.default, self.path = default, (path or None)
        self._mode, self.source = default, "default"
        self._sig = "init"
        self._log = log or (lambda msg: sys.stderr.write(msg + "\n"))
        self._lock = threading.Lock()

    def current(self) -> str:
        return self._mode

    def maybe_reload(self) -> None:
        if not self.path:
            return
        with self._lock:
            sig = _file_sig(self.path)
            if sig == self._sig:
                return
            self._sig = sig
            if sig is None:
                new, source, why = self.default, "default", f"no mode file at {self.path}; --public-mode default"
            else:
                try:
                    with open(self.path, "rb") as f:
                        word = f.read(64).decode("utf-8").strip().lower()
                except (OSError, UnicodeDecodeError) as e:
                    word = f"<unreadable: {type(e).__name__}>"
                if word in PUBLIC_MODES:
                    new, source, why = word, "file", f"from {self.path}"
                else:
                    new, source = "enforce", "file_invalid"
                    why = (f"!!! {self.path} holds {word[:20]!r}, not one of {'/'.join(PUBLIC_MODES)}: "
                           "ENFORCING (fail closed)")
            self._mode, self.source = new, source
            self._log(f"PUBLIC door mode: {new} ({why})")


def poll_forever(objs, every_s: float = ALLOWLIST_POLL_S, log=None) -> None:
    """Re-check each object's files every every_s seconds. Never dies."""
    log = log or (lambda msg: sys.stderr.write(msg + "\n"))
    while True:
        time.sleep(every_s)
        for o in objs:
            try:
                o.maybe_reload()
            except Exception as e:  # noqa: BLE001 -- the poller must never die
                log(f"allow-list/mode poller error: {type(e).__name__}: {e}")


def public_decision(source: str, key: str, allow: AllowList) -> tuple[bool, str]:
    """May the PUBLIC door serve /img/<source>/<key>? -> (allowed, reason). Pure and in-memory: it
    runs before any disk access, so a denied key costs nothing and reveals nothing."""
    if source not in SOURCES or not valid_key(key):
        return False, "bad_request"
    if source in PUBLIC_WHOLE_SOURCES:
        return True, "curated_source"
    if allow.contains(source, key):
        return True, "listed"
    if allow.loaded():
        return False, "unlisted"
    if source in NO_LIST_OPEN_SOURCES:
        return True, "no_list_open_source"
    return False, "no_list_closed"


# ---------------------------------------------------------------------------------------------
# PEERS (internal door) -- decided on the TCP source address, never on a header
TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
DEFAULT_INTERNAL_ALLOW = "100.64.0.0/10,fd7a:115c:a1e0::/48"
DEFAULT_INTERNAL_BIND = "tailnet:8512"
DEFAULT_PUBLIC_BIND = "127.0.0.1:8510"


def _ip(host: str):
    try:
        ip = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def parse_nets(spec: str) -> tuple:
    return tuple(ipaddress.ip_network(p.strip(), strict=False) for p in (spec or "").split(",") if p.strip())


def peer_allowed(host: str, nets) -> bool:
    ip = _ip(host)
    return ip is not None and any(ip.version == n.version and ip in n for n in nets)


def peer_class(host: str) -> str:
    ip = _ip(host)
    if ip is None:
        return "other"
    if ip.is_loopback:
        return "loopback"
    if any(ip.version == n.version and ip in n for n in TAILNET_NETS):
        return "tailnet"
    if ip.is_private or ip.is_link_local:
        return "lan"
    return "other"


def local_addresses() -> list:
    """Every address on every interface of this machine (normalised strings), read from ifconfig. Tailscale
    puts the tailnet ones on a utun. [] if ifconfig cannot be run."""
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for m in re.finditer(r"\binet6?\s+([0-9A-Fa-f:.%a-z0-9]+)", out):
        ip = _ip(m.group(1))
        if ip is not None and str(ip) not in found:
            found.append(str(ip))
    return found


def is_tailnet(addr: str) -> bool:
    ip = _ip(addr)
    return ip is not None and any(ip.version == n.version and ip in n for n in TAILNET_NETS)


def tailnet_addresses(addrs: list | None = None) -> list:
    """This machine's tailnet addresses (from local_addresses(), or the list given)."""
    return [a for a in (local_addresses() if addrs is None else addrs) if is_tailnet(a)]


def norm_addrs(addrs) -> frozenset:
    """Addresses as the internal door compares them: parsed, scope dropped, v4-mapped v6 folded to v4."""
    out = set()
    for a in addrs:
        ip = _ip(str(a))
        if ip is not None:
            out.add(str(ip))
    return frozenset(out)


def nets_include_loopback(nets) -> bool:
    return any(lo in n for n in nets for lo in (ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1"))
               if lo.version == n.version)


def fmt_addr(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_bind(spec: str) -> list:
    """'127.0.0.1:8510' | '[fd7a::1]:8512' | 'tailnet:8512' | 'off' (comma-separated) -> [(host, port)]
    with host 'tailnet' left symbolic (resolved at bind time)."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or part.lower() in ("off", "none", "0"):
            continue
        if part.startswith("["):
            host, _, port = part[1:].partition("]:")
        else:
            host, _, port = part.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"bad bind spec {part!r}: want HOST:PORT, [V6]:PORT or tailnet:PORT")
        out.append((host, int(port)))
    return out


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


def paths_overlap(a: str, b: str) -> bool:
    """True if one path is the other or lies inside it (after resolving symlinks)."""
    ra, rb = os.path.realpath(a), os.path.realpath(b)
    return ra == rb or ra.startswith(rb.rstrip(os.sep) + os.sep) or rb.startswith(ra.rstrip(os.sep) + os.sep)


def cache_conflict() -> str | None:
    """Why the SSD cache must stay OFF, or None. The sweep DELETES files under CACHE_DIR (eviction, abandoned .tmp
    files, the lot-photo purge), so a CACHE_DIR that is, contains or sits inside the archive ROOT would delete
    originals. Checked at startup (caching is switched off) and again before every sweep."""
    if CACHE_ENABLED and paths_overlap(CACHE_DIR, ROOT):
        return f"MAZI_IMAGE_CACHE_DIR {CACHE_DIR} overlaps the archive root {ROOT}"
    return None


# ---------------------------------------------------------------------------------------------
# CARD LOOKUP BY SLUG  (internal door only since 2026-10-01: the site never builds /card/ URLs)
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
    """Cache hit path. Four stats on an SSD is microseconds; it is never worth an index.
    Never a hit for UNCACHED_SOURCES (lot photos): see the module docstring."""
    if not CACHE_ENABLED or source not in SOURCES or source in UNCACHED_SOURCES or not valid_key(key):
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
    if not CACHE_ENABLED or cache_conflict():
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _purge_uncached_copies()
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


def _purge_uncached_copies() -> int:
    """Remove SSD copies of UNCACHED_SOURCES left by servers from before 2026-10-02 (35 Goldin + 22 Fanatics
    lot photos on the Mini that day). They are never read any more; removing them means a lot photo whose 6TB
    file was deleted has no copy left anywhere on the box. Copies only: the 6TB originals are never touched
    (refused outright if CACHE_DIR overlaps ROOT)."""
    if not CACHE_ENABLED or cache_conflict():
        return 0
    n = 0
    for s in UNCACHED_SOURCES:
        d = os.path.join(CACHE_DIR, s)
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        for dirpath, _, names in os.walk(d):
            for name in names:
                try:
                    os.unlink(os.path.join(dirpath, name))
                    n += 1
                except OSError:
                    pass
    if n:
        _CSTATS["purged_uncached"] = _CSTATS.get("purged_uncached", 0) + n
        sys.stderr.write(f"ssd cache: removed {n} cached cop{'y' if n == 1 else 'ies'} of "
                         f"{'/'.join(UNCACHED_SOURCES)} (served from the 6TB only now)\n")
    return n


def _cache_sweeper() -> None:
    while True:
        _cache_sweep()
        time.sleep(1800)


def image_path(source: str, key: str) -> str | None:
    """Absolute path to this key's image, or None. Rejects anything that escapes ROOT/<source>.

    Storage is sharded (<source>/<k0:2>/<k2:4>/<key>/) as of 2026-09-17 because a flat directory of
    1.4M siblings made each new write take >12 s. Existing images were NOT moved, so both layouts are
    live: sharded is checked first since that is where everything new lands, flat second so the ~1.5M
    already on disk keep resolving. Callers -- including the front end -- see one stable URL either way,
    which is the whole reason the layout could change without anyone having to be told.

    Containment is per SOURCE (2026-10-01), not just per ROOT: the public door serves the curated
    lot-photo folders whole, so a stray symlink in one of them must not reach into ebay/ or fanatics/."""
    if source not in SOURCES or not valid_key(key):
        return None
    k = key.lower()
    src_root = os.path.join(ROOT, source)
    root_real = os.path.realpath(ROOT) + os.sep
    src_real = os.path.realpath(src_root) + os.sep
    candidates = (
        os.path.join(src_root, k[:2] or "__", k[2:4] or "__", key),   # sharded
        os.path.join(src_root, key),                                   # legacy flat
    )
    for base in candidates:
        base = os.path.realpath(base)
        if not (base.startswith(root_real) and base.startswith(src_real)):
            continue                     # belt and braces: KEY_RE already bars separators
        for ext in EXTS:
            p = base + os.sep + "01" + ext
            if os.path.isfile(p) and os.path.realpath(p).startswith(src_real):
                return p
    return None


def scp_key(image_url: str) -> str:
    return hashlib.sha1(image_url.encode()).hexdigest()[:20]


_COUNTS_CACHE: dict = {"at": 0.0, "val": None}
COUNTS_TTL = 1800.0  # 30 min. The ledgers are 150MB+ and growing; this read is genuinely expensive
                     # and the numbers move by a few thousand an hour, so freshness costs more than
                     # it is worth.


def _counts_file() -> str:
    return os.path.join(ROOT, "_backfill", "server_counts_cache.json")


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
        with open(_counts_file()) as f:
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
    # STALE-WHILE-REVALIDATE. Always answer from cache, refresh behind it. A blocking recompute
    # made /healthz time out entirely once the ledgers passed ~40MB (observed 2026-09-20: >60s,
    # while image serving stayed at 8ms). A status endpoint that hangs is worse than one that is
    # five minutes out of date -- it reads as "the server is down".
    if now - _COUNTS_CACHE["at"] >= ttl:
        _refresh_counts_async()
    return _COUNTS_CACHE["val"]


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
        cf = _counts_file()
        tmp = cf + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"at": _COUNTS_CACHE["at"], "val": out}, f)
        os.replace(tmp, cf)
    except OSError:
        pass
    return out


USAGE = """comp image server -- INTERNAL door (full archive; tailnet only)

  /healthz                 per-source counts, disk free, public allow-list status
  /img/<source>/<key>      the image (extension resolved for you)
  /r?u=<image_url>         302 to the image for a pricecharting/SCP url
  /card/<console>/<product>  the card's raw reference photo, by its SportsCardsPro slug

sources: {sources}

examples
  /img/scp_catalog/<20-hex sha1 of the pricecharting image url>
  /img/ebay/<ebay item id>
  /r?u=https%3A//storage.googleapis.com/images.pricecharting.com/abc/240.jpg
  /card/basketball-cards-1996-topps-chrome/kobe-bryant-138

The PUBLIC door (Serve/Funnel -> 127.0.0.1:8510) serves only allow-listed /img/ keys and /healthz.
read-only; GET and HEAD only.
""".format(sources=", ".join(SOURCES))


# ---------------------------------------------------------------------------------------------
# ACCESS LOG -- no client addresses, no header values
ACCESS_LOG_DEFAULT = os.path.expanduser("~/Library/Logs/mazi_external_comps/comp_image_access.log")
_ACCESS = logging.getLogger("mazi.comp_image_server.access")
_ACCESS.propagate = False
_ACCESS.setLevel(logging.INFO)

_UA_FAMILIES = (
    ("mazi-", "mazi"), ("googlebot", "googlebot"), ("google-inspectiontool", "googlebot"),
    ("googleother", "googlebot"), ("bingbot", "bingbot"), ("applebot", "applebot"),
    ("facebookexternalhit", "facebook"), ("meta-externalagent", "facebook"), ("twitterbot", "twitter"),
    ("slackbot", "slack"), ("slack-imgproxy", "slack"), ("discordbot", "discord"), ("whatsapp", "whatsapp"),
    ("telegrambot", "telegram"), ("linkedinbot", "linkedin"), ("vercel", "vercel"),
    ("python-urllib", "python"), ("python-requests", "python"), ("curl/", "curl"), ("wget", "wget"),
    ("undici", "node"), ("node-fetch", "node"), ("node", "node"), ("bot", "otherbot"),
    ("spider", "otherbot"), ("crawl", "otherbot"), ("mozilla", "browser"),
)


def ua_family(ua: str | None) -> str:
    u = (ua or "").lower()
    if not u:
        return "none"
    for needle, fam in _UA_FAMILIES:
        if needle in u:
            return fam
    return "other"


def referer_host(ref: str | None) -> str:
    if not ref:
        return "-"
    try:
        h = urllib.parse.urlsplit(ref).hostname or "-"
    except ValueError:
        return "?"
    return re.sub(r"[^a-z0-9.-]", "", h.lower())[:64] or "?"


def configure_access_log(dest: str | None) -> None:
    """'-' or '' -> stderr; a path -> rotating file (20 MB x 5). Falls back to stderr if the file
    cannot be opened, because losing the access log must never stop the server."""
    for h in list(_ACCESS.handlers):
        _ACCESS.removeHandler(h)
    handler: logging.Handler
    if dest and dest != "-":
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(dest, maxBytes=20 * 1024 * 1024, backupCount=5)
        except OSError as e:
            sys.stderr.write(f"access log {dest} unusable ({e}); logging requests to stderr\n")
            handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.StreamHandler(sys.stderr)
    fmt = logging.Formatter("%(asctime)sZ %(message)s", "%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime          # UTC, like every other MAZI log stamp
    handler.setFormatter(fmt)
    _ACCESS.addHandler(handler)


def _safe_path(p: str) -> str:
    return re.sub(r"[^\x21-\x7e]", "?", p or "")[:200]


# ---------------------------------------------------------------------------------------------
# HTTP
NOT_FOUND = (404, b"not found\n", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})


class DoorServer(ThreadingHTTPServer):
    """One listening socket = one door. door='public' serves the allow-list view; door='internal'
    serves everything but only to peers whose source address is in allowed_nets and is not one of this
    machine's own addresses (self_addrs; loopback is governed by allowed_nets alone)."""
    daemon_threads = True

    def __init__(self, addr, handler, *, door: str, allow: AllowList, public_mode="enforce",
                 allowed_nets=(), self_addrs=()) -> None:
        if door not in ("public", "internal"):
            raise ValueError(door)
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        self.door, self.allow, self.allowed_nets = door, allow, tuple(allowed_nets)
        # public_mode: "enforce" | "observe" (fixed), or a PublicMode that follows the mode file
        self.mode = public_mode if isinstance(public_mode, PublicMode) else PublicMode(public_mode, path=None)
        self.self_addrs = norm_addrs(self_addrs)
        self.refused = self.refused_self = 0
        super().__init__(addr, handler)
        # the address this door is bound to is always one of this machine's own (the keeper adds the rest)
        self.self_addrs = norm_addrs(list(self.self_addrs) + [self.server_address[0]])

    @property
    def public_mode(self) -> str:
        return self.mode.current()

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse-DNS getfqdn() we never use; skip it
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port

    def handle_error(self, request, client_address) -> None:
        """A client hanging up is routine (634 BrokenPipe tracebacks in the old log); anything else
        is printed -- but without the client address the base class would include."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)):
            return
        sys.stderr.write(f"request error on the {self.door} door ({peer_class(client_address[0])} peer):\n")
        traceback.print_exc()

    def verify_request(self, request, client_address) -> bool:
        if self.door != "internal":
            return True
        ip = _ip(client_address[0])
        if ip is not None and not ip.is_loopback and str(ip) in self.self_addrs:
            # A connection from this machine's own (tailnet) address: a Serve/Funnel or other local proxy pointed
            # at http://<tailnet-ip or hostname>:8512 would arrive exactly like this. Never the internal door.
            self.refused_self += 1
            if self.refused_self <= 20 or self.refused_self % 1000 == 0:
                sys.stderr.write("internal door refused a connection from one of THIS machine's own addresses "
                                 "(a Serve/Funnel or local proxy pointed at the internal door?) "
                                 f"(refused so far: {self.refused_self})\n")
            return False
        if peer_allowed(client_address[0], self.allowed_nets):
            return True
        self.refused += 1
        if self.refused <= 20 or self.refused % 1000 == 0:
            sys.stderr.write(f"internal door refused a {peer_class(client_address[0])} peer "
                             f"(refused so far: {self.refused})\n")
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "MaziCompImages/2.0"
    sys_version = ""                     # do not advertise the Python version
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------------------------------
    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def log_request(self, code="-", size="-"):
        pass                             # written once per request by _access(), with the decision

    def log_message(self, fmt: str, *args) -> None:
        # errors from the base class (bad request lines, timeouts). No client address, by design.
        _ACCESS.info("%s err %s", self._door_tag(), _safe_path(fmt % args))

    def _door_tag(self) -> str:
        return "pub" if getattr(self.server, "door", "public") == "public" else "int"

    def _access(self) -> None:
        h = self.headers
        _ACCESS.info(
            "%s %s %s %s %s %s %s peer=%s tsu=%d tsf=%d ua=%s ref=%s",
            self._door_tag(), getattr(self, "_mode", "-") if self.server.door == "public" else "-",
            self._decision, self.command, getattr(self, "_status", "-"), self._bytes,
            _safe_path(self.path), peer_class(self.client_address[0]),
            int(h is not None and "Tailscale-User-Login" in h), int(h is not None and "Tailscale-Funnel-Request" in h),
            ua_family(h.get("User-Agent") if h is not None else None),
            referer_host(h.get("Referer") if h is not None else None))

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self._bytes = len(body)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")   # images only, no credentials, no cookies
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self) -> None:
        """THE public 404: identical for an unlisted key, a key that does not exist, an unknown
        source and an unknown route, so the public side is not an existence oracle."""
        self._send(*NOT_FOUND)

    def _send_file(self, path: str, public: bool, cache_to: str | None = None,
                   cache_control: str = IMMUTABLE_CACHE_CONTROL) -> None:
        """Stream the file; if cache_to is given, tee it to the SSD on the way past.

        Teeing rather than re-reading matters: the 6TB read is the expensive part, and we already
        have the bytes in hand. The copy is only committed (atomic rename) after a COMPLETE send, so
        a client that disconnects mid-image can never leave a truncated file to be served forever.
        The file is opened BEFORE the 200 goes out, so a vanished file is a clean 404; a failure
        after the headers (usually the client hanging up) just closes the connection -- there is no
        second response to send, and no traceback to write."""
        ext = os.path.splitext(path)[1].lower()
        try:
            f = open(path, "rb")
            size = os.fstat(f.fileno()).st_size
        except OSError:
            return self._miss(public, "image unreadable")
        with f:
            self._bytes = size
            self.send_response(200)
            self.send_header("Content-Type", CTYPE.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(size))
            self.send_header("Access-Control-Allow-Origin", "*")
            # archived originals never change in place (a new photo gets a new key); lot photos can be taken
            # down or re-fetched under the same key, so they get UNCACHED_CACHE_CONTROL
            self.send_header("Cache-Control", cache_control)
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
            except OSError:
                self.close_connection = True       # client went away mid-body (BrokenPipe etc.)
                self._decision += ":aborted"
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

    def _miss(self, public: bool, why: str) -> None:
        if public:
            return self._not_found()
        return self._send(404, (why + "\n").encode(), "text/plain; charset=utf-8")

    def _serve_img(self, source: str, key: str, public: bool) -> None:
        if source not in SOURCES or not valid_key(key):
            return self._miss(public, "no image for that source/key")
        if source in UNCACHED_SOURCES:
            # the 6TB file is the only copy ever served: deleting it is the takedown, and a re-fetch under the same
            # key is what the next request (and the catalog-art apply's sha256 check) sees
            p = image_path(source, key)
            if p:
                _CSTATS["direct"] = _CSTATS.get("direct", 0) + 1
                return self._send_file(p, public, cache_control=UNCACHED_CACHE_CONTROL)
            return self._miss(public, "no image for that source/key")
        hit = cached_file(source, key)
        if hit:
            _CSTATS["hit"] += 1
            return self._send_file(hit, public)                 # SSD; already the fast copy
        p = image_path(source, key)
        if p:
            _CSTATS["miss"] += 1
            # no tee when caching is off: cache_base() of an "off" CACHE_DIR is a RELATIVE path
            cache_to = cache_base(source, key) + os.path.splitext(p)[1].lower() if CACHE_ENABLED else None
            return self._send_file(p, public, cache_to=cache_to)
        return self._miss(public, "no image for that source/key")

    # -- verbs -----------------------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self._decision, self._bytes, self._status = "-", 0, "-"
        self._mode = self.server.public_mode     # read once: the mode file can flip it mid-request
        try:
            u = urllib.parse.urlsplit(self.path)
            parts = [p for p in u.path.split("/") if p]
            if self.server.door == "public":
                self._public(u, parts)
            else:
                self._decision = "internal"
                self._internal(u, parts)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True         # the client hung up; nothing left to answer
            self._decision += ":aborted"
        finally:
            self._access()

    def _public(self, u, parts) -> None:
        if parts == ["healthz"]:
            self._decision = "health"
            body = json.dumps({"ok": os.path.isdir(ROOT)}).encode()
            return self._send(200, body, "application/json", {"Cache-Control": "no-store"})
        observe = self._mode == "observe"
        if len(parts) == 3 and parts[0] == "img":
            source, key = parts[1], parts[2]
            ok, why = public_decision(source, key, self.server.allow)
            if ok:
                self._decision = "allow:" + why
                return self._serve_img(source, key, public=True)
            if observe:
                self._decision = "would_deny:" + why
                return self._serve_img(source, key, public=False)
            self._decision = "deny:" + why
            return self._not_found()
        if observe:
            self._decision = "would_deny:route"
            return self._internal(u, parts)
        self._decision = "deny:route"
        return self._not_found()

    def _internal(self, u, parts) -> None:
        if not parts:
            return self._send(200, USAGE.encode(), "text/plain; charset=utf-8")

        if parts == ["healthz"]:
            st = os.statvfs(ROOT) if os.path.isdir(ROOT) else None
            body = json.dumps({
                "ok": os.path.isdir(ROOT),
                "root": ROOT,
                "images": counts(),
                "disk_free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1) if st else None,
                "card_map": {"cards": len(_SLUG_MAP["map"]) if _SLUG_MAP["map"] is not None else None,
                             "loaded": _SLUG_MAP["map"] is not None, "path": _SLUG_MAP["path"],
                             "error": _SLUG_MAP["error"]},
                "cache": {"enabled": CACHE_ENABLED, "dir": CACHE_DIR, "max_gb": CACHE_MAX_GB,
                          "used_gb": round(_CSTATS["bytes"] / 1e9, 2), "writable": _CSTATS["writable"],
                          "hit": _CSTATS["hit"], "miss": _CSTATS["miss"],
                          "written": _CSTATS["write"], "evicted": _CSTATS["evicted"],
                          "uncached_sources": list(UNCACHED_SOURCES), "direct": _CSTATS.get("direct", 0),
                          "purged_uncached": _CSTATS.get("purged_uncached", 0)},
                "public": {"mode": self.server.public_mode, "mode_source": self.server.mode.source,
                           "mode_file": self.server.mode.path, "whole_sources": list(PUBLIC_WHOLE_SOURCES),
                           "no_list_open_sources": list(NO_LIST_OPEN_SOURCES),
                           "allowlist": self.server.allow.status},
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
            return self._serve_img("scp_catalog", key, public=False)

        if parts[0] == "img" and len(parts) == 3:
            return self._serve_img(parts[1], parts[2], public=False)

        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        self._decision, self._bytes, self._status = "write_verb", 0, "-"
        self._mode = self.server.public_mode
        self.close_connection = True     # never read a request body: drop the connection instead
        try:
            self._send(405, b"read-only\n", "text/plain; charset=utf-8", {"Allow": "GET, HEAD"})
        finally:
            self._access()

    do_PUT = do_DELETE = do_PATCH = do_POST


# ---------------------------------------------------------------------------------------------
# STARTUP
def _serve_in_thread(srv: DoorServer, name: str) -> threading.Thread:
    t = threading.Thread(target=srv.serve_forever, name=name, daemon=True)
    t.start()
    return t


def _internal_door_keeper(specs: list, allow: AllowList, nets, public_mode, retry_s: float = 30.0) -> None:
    """Bind every internal address, retrying the ones that fail (e.g. Tailscale not up yet at boot), and keep
    every internal door's list of this machine's own addresses current (re-read from ifconfig each pass, plus
    every address the doors bind), so a connection from the Mini to itself is never let in.
    The public door never waits on this."""
    bound: dict = {}
    warned: set = set()
    while True:
        local = local_addresses()
        want = []
        for host, port in specs:
            if host == "tailnet":
                addrs = tailnet_addresses(local)
                if not addrs and ("tailnet", port) not in warned:
                    warned.add(("tailnet", port))
                    sys.stderr.write("internal door: no tailnet address on any interface yet; retrying every "
                                     f"{retry_s:.0f}s\n")
                want += [(a, port) for a in addrs]
            else:
                want.append((host, port))
        own = norm_addrs(list(local) + [h for h, _ in want] + [h for h, _ in bound])
        for srv in bound.values():
            srv.self_addrs = own                 # one attribute swap; verify_request reads it once
        for addr in want:
            if addr in bound:
                continue
            try:
                srv = DoorServer(addr, Handler, door="internal", allow=allow, public_mode=public_mode,
                                 allowed_nets=nets, self_addrs=own)
            except OSError as e:
                if addr not in warned:
                    warned.add(addr)
                    sys.stderr.write(f"internal door: cannot bind {fmt_addr(*addr)} ({e}); retrying\n")
                continue
            bound[addr] = srv
            _serve_in_thread(srv, f"internal-{fmt_addr(*addr)}")
            sys.stderr.write(f"INTERNAL door (full archive) on {fmt_addr(*addr)}  peers allowed: "
                             f"{', '.join(str(n) for n in nets) or 'NONE'}, minus this machine's own "
                             f"{len(own)} addresses\n")
        time.sleep(retry_s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="comp image server (public allow-list door + internal door)")
    ap.add_argument("--public-bind", default=os.environ.get("MAZI_IMAGE_PUBLIC_BIND", DEFAULT_PUBLIC_BIND),
                    help="HOST:PORT for the public (Serve/Funnel) door; default %(default)s")
    ap.add_argument("--internal-bind", default=os.environ.get("MAZI_IMAGE_INTERNAL_BIND", DEFAULT_INTERNAL_BIND),
                    help="tailnet:PORT | HOST:PORT[,...] | off; default %(default)s")
    ap.add_argument("--internal-allow", default=os.environ.get("MAZI_IMAGE_INTERNAL_ALLOW", DEFAULT_INTERNAL_ALLOW),
                    help="CIDRs whose TCP source address may use the internal door; default %(default)s")
    ap.add_argument("--allowlist", default=ALLOWLIST_PATH)
    ap.add_argument("--staged", default=STAGED_PATH, help="optional staged keys file ('' to disable)")
    ap.add_argument("--public-mode", choices=PUBLIC_MODES,
                    default=os.environ.get("MAZI_IMAGE_PUBLIC_MODE", "enforce"),
                    help="default mode of the public door when no mode file exists; default %(default)s")
    ap.add_argument("--mode-file", default=MODE_PATH,
                    help="file holding 'enforce' or 'observe', polled; overrides --public-mode while it exists "
                         "('' to disable); default %(default)s")
    ap.add_argument("--access-log", default=os.environ.get("MAZI_IMAGE_ACCESS_LOG", ACCESS_LOG_DEFAULT),
                    help="rotating request log path, or '-' for stderr")
    # legacy flags (pre-2026-10-01 single listener): they now set the PUBLIC door
    ap.add_argument("--host", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if not os.path.isdir(ROOT):
        sys.stderr.write(f"image root not readable: {ROOT}\n"
                         "(on this Mini that usually means TCC -- run from the supervisor/ssh, "
                         "not a LaunchAgent)\n")
        return 2
    pub = parse_bind(a.public_bind)
    if len(pub) != 1 or pub[0][0] == "tailnet":
        sys.stderr.write("--public-bind must be exactly one HOST:PORT\n")
        return 2
    pub_host, pub_port = pub[0]
    if a.host:
        pub_host = a.host
    if a.port:
        pub_port = a.port
    internal_specs = parse_bind(a.internal_bind)
    nets = parse_nets(a.internal_allow)
    if internal_specs and nets_include_loopback(nets):
        sys.stderr.write("!!! --internal-allow includes LOOPBACK: anything on this machine -- including a Serve/Funnel "
                         "proxy pointed at the internal door -- can read the whole archive. Tests only.\n")
    configure_access_log(a.access_log)
    global CACHE_ENABLED
    why = cache_conflict()
    if why:
        CACHE_ENABLED = False
        sys.stderr.write(f"!!! SSD cache DISABLED: {why} (the cache sweep deletes files; it must never touch "
                         "the archive). Serving straight from the 6TB.\n")
    try:
        mode = PublicMode(a.public_mode, a.mode_file or None)
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 2
    mode.maybe_reload()                  # synchronous, like the list

    allow = AllowList(a.allowlist, a.staged or None)
    allow.maybe_reload()                 # synchronous: the first request already sees the list

    srv = DoorServer((pub_host, pub_port), Handler, door="public", allow=allow, public_mode=mode)
    # Warm the counts cache off-thread. Cold it is a ~26 s ledger read, and the first person to hit
    # /healthz should not conclude the server is hung. Images never wait on this.
    _load_cache_from_disk()          # instant if a previous run left counts behind
    threading.Thread(target=counts, daemon=True).start()
    threading.Thread(target=_load_slug_map, daemon=True).start()   # ~3s; /card/ answers 503 until done
    threading.Thread(target=poll_forever, args=([allow, mode],), daemon=True, name="allowlist-poll").start()
    if CACHE_ENABLED:
        _cache_sweep()                                                  # set `writable` before serving
        threading.Thread(target=_cache_sweeper, daemon=True).start()
        sys.stderr.write(f"ssd cache {CACHE_DIR} max={CACHE_MAX_GB}GB writable={_CSTATS['writable']}\n")
    if internal_specs:
        threading.Thread(target=_internal_door_keeper, args=(internal_specs, allow, nets, mode),
                         daemon=True, name="internal-door").start()
    else:
        sys.stderr.write("internal door: off\n")
    sys.stderr.write(f"PUBLIC door ({mode.current()}, {mode.source}) on http://{fmt_addr(pub_host, pub_port)}  "
                     f"root={ROOT}  allowlist={a.allowlist}  mode_file={a.mode_file or 'off'}  "
                     f"access_log={a.access_log}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
