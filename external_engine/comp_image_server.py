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

LAN ONLY. Binding 0.0.0.0 exposes it to the local network, which is the point; it is NOT
internet-facing unless someone forwards a port, and it should not be. Nothing here authenticates,
so do not put it on a public interface.

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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("MAZI_COMP_IMAGES_ROOT", "/Volumes/MAZI_EVIDENCE_6TB/comp_images")
SOURCES = ("scp_catalog", "ebay", "fanatics", "tcgplayer_catalog")
# keys are hashes or marketplace item ids; anything with a separator in it is not one of ours
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
EXTS = (".jpg", ".webp", ".png", ".jpeg")
CTYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".png": "image/png"}
SCP_HOST_HINT = "pricecharting.com"


def image_path(source: str, key: str) -> str | None:
    """Absolute path to <source>/<key>/01.<ext>, or None. Rejects anything that escapes ROOT."""
    if source not in SOURCES or not KEY_RE.match(key or ""):
        return None
    base = os.path.realpath(os.path.join(ROOT, source, key))
    if not base.startswith(os.path.realpath(ROOT) + os.sep):
        return None                      # belt and braces: KEY_RE already bars separators
    for ext in EXTS:
        p = base + os.sep + "01" + ext
        if os.path.isfile(p):
            return p
    return None


def scp_key(image_url: str) -> str:
    return hashlib.sha1(image_url.encode()).hexdigest()[:20]


def counts() -> dict:
    """Per-source image counts read from the backfill ledgers -- never by listing the directories,
    which hold up to 1.4M entries apiece and are as expensive to read as to write."""
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
    return out


USAGE = """comp image server

  /healthz                 per-source counts and disk free
  /img/<source>/<key>      the image (extension resolved for you)
  /r?u=<image_url>         302 to the image for a pricecharting/SCP url

sources: {sources}

examples
  /img/scp_catalog/3108997300ae3e33f70d
  /img/ebay/206162825097
  /r?u=https%3A//storage.googleapis.com/images.pricecharting.com/abc/240.jpg

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

    def _send_file(self, path: str) -> None:
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
            if self.command != "HEAD":
                with open(path, "rb") as f:
                    while chunk := f.read(256 * 1024):
                        self.wfile.write(chunk)
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

        if parts[0] == "img" and len(parts) == 3:
            p = image_path(parts[1], parts[2])
            if p:
                return self._send_file(p)
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
    sys.stderr.write(f"comp image server on http://{a.host}:{a.port}  root={ROOT}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
