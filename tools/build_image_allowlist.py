#!/usr/bin/env python3
"""build_image_allowlist.py -- the PUBLIC allow-list for external_engine/comp_image_server.py (2026-10-01).

READ-ONLY on the beta. It never writes the database; every chunk runs in a READ ONLY transaction.

The image server's public door (Tailscale Serve/Funnel) may only serve pictures the site actually shows. The
site shows exactly what beta_catalog.image_small / image_large point at, so this reads those two columns and
writes every /img/<source>/<key> on our image host to one file:

    # mazi-image-allowlist v1
    # built_at=2026-10-01T23:40:00Z
    # ...counts...
    scp_catalog/3108997300ae3e33f70d
    ebay/206162825097
    # end count=398231

The server re-reads the file when it changes (within ~30 s) and ignores a file that does not parse, keeping
its last good list, so a failed or partial build can never blank the site. The write here is atomic anyway
(tmp file, fsync, rename).

HOW IT READS THE BETA. beta_catalog is ~9.5M rows (6.2 GB heap) with no index on the image columns, so this is
a keyset walk over the primary key: find the card_id 50,000 rows ahead (index-only scan, ~0.3 s), then fetch
the image columns of that range with the picture filter (index range scan). ~190 chunks, a short pause
between them, a 60 s statement timeout on each. Measured 2026-10-01: ~0.3 s per 50K-row chunk.

SHRINK GUARD. The site loses pictures if this list loses keys, so the build refuses to replace a list with
one more than --max-shrink (default 10%) smaller, unless --allow-shrink. A big legitimate withdrawal is a
deliberate operator run with --allow-shrink.

Lot photos (lotphoto_goldin, lotphoto_fanatics) are public as whole folders on the server side; the ones the
beta references are listed here too, only so the counts are complete.

  python3 tools/build_image_allowlist.py                       # beta -> ~/mazi_local_evidence/image_allowlist/public_allowlist.txt
  python3 tools/build_image_allowlist.py --dry-run             # read + count, write nothing
  python3 tools/build_image_allowlist.py --from-urls FILE ...  # offline: build from text/CSV files of image URLs
  python3 tools/build_image_allowlist.py --check               # validate the current list file and print its counts

Scheduled by ops/launchd/com.mazi.image-allowlist.plist (every 6 h). Exit codes: 0 written (or unchanged),
2 usage/config, 3 refused by a guard (shrink / too few keys / unsupported routes), 4 database error.
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "external_engine"))
import comp_image_server as cis  # noqa: E402  (constants + the file format; importing starts nothing)

IMAGE_HOST = "stavross-mac-mini.tail9fccf8.ts.net"
# same beta as tools/export_veefriends_beta.py (BETA / BHOST); not imported because that module pulls in the
# VeeFriends stack
BETA = "jzxgtvxcuukxqkbwbuxg"
BHOST = "aws-0-us-west-1.pooler.supabase.com"
DEFAULT_PWFILE = Path(os.path.expanduser("~/private/beta-password"))

BOUND_SQL = ("SELECT card_id FROM public.beta_catalog WHERE card_id > %s "
             "ORDER BY card_id LIMIT 1 OFFSET %s")
RANGE_SQL = ("SELECT image_small, image_large FROM public.beta_catalog "
             "WHERE card_id > %s AND card_id <= %s AND (image_small IS NOT NULL OR image_large IS NOT NULL)")
TAIL_SQL = ("SELECT image_small, image_large FROM public.beta_catalog "
            "WHERE card_id > %s AND (image_small IS NOT NULL OR image_large IS NOT NULL)")
URL_RE = re.compile(r"https?://[^\s,\"'<>]+")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------------------- classify
def classify(url: str | None, host: str = IMAGE_HOST) -> tuple:
    """One stored picture URL -> ('ok', source, key) | (reason,). Only /img/<source>/<key> on our host is
    something the image server's public door has to answer for."""
    if not url:
        return ("empty",)
    try:
        from urllib.parse import urlsplit
        p = urlsplit(url.strip())
    except ValueError:
        return ("unparseable",)
    if (p.hostname or "").lower() != host.lower():
        return ("other_host",)
    parts = [x for x in p.path.split("/") if x]
    if len(parts) != 3 or parts[0] != "img":
        return ("our_host_other_route",)      # /card/..., /r?u=..., anything the public door will 404
    source, key = parts[1], parts[2]
    if source not in cis.SOURCES:
        return ("unknown_source",)
    if not cis.valid_key(key):
        return ("bad_key",)
    return ("ok", source, key)


def collect(rows, host: str = IMAGE_HOST) -> tuple[dict, collections.Counter]:
    """rows: iterable of tuples of URL columns (e.g. (image_small, image_large)).
    -> ({source: set(keys)}, Counter of outcomes per URL value)."""
    entries: dict = {s: set() for s in cis.SOURCES}
    stats: collections.Counter = collections.Counter()
    for row in rows:
        stats["rows"] += 1
        for url in row:
            c = classify(url, host)
            stats[c[0]] += 1
            if c[0] == "ok":
                entries[c[1]].add(c[2])
    return entries, stats


# ------------------------------------------------------------------------------------------- sources
def beta_rows(pwfile: Path, chunk: int, pause: float, statement_timeout_s: int):
    """Yield (image_small, image_large) for every beta_catalog row with a picture, chunk by chunk."""
    import psycopg
    if not pwfile.is_file() or pwfile.stat().st_mode & 0o077:
        raise SystemExit(f"{pwfile}: must exist with mode 600")
    conn = psycopg.connect(host=BHOST, port=5432, user=f"postgres.{BETA}", dbname="postgres",
                           password=pwfile.read_text().strip(), sslmode="require", connect_timeout=15,
                           application_name="mazi-image-allowlist", autocommit=True)
    try:
        conn.execute(f"SET statement_timeout = '{int(statement_timeout_s)}s'")
        conn.execute("SET default_transaction_read_only = on")
        conn.read_only = True                 # every transaction below is BEGIN READ ONLY
        yield from keyset_chunks(conn, chunk, pause)
    finally:
        conn.close()


def keyset_chunks(conn, chunk: int, pause: float):
    after, n_chunks, t0 = "", 0, time.time()
    while True:
        with conn.transaction():
            row = conn.execute(BOUND_SQL, (after, chunk - 1)).fetchone()
            hi = row[0] if row else None
            if hi is None:
                rows = conn.execute(TAIL_SQL, (after,)).fetchall()
            else:
                rows = conn.execute(RANGE_SQL, (after, hi)).fetchall()
        n_chunks += 1
        yield from rows
        if n_chunks % 20 == 0:
            log(f"  chunk {n_chunks} (through {str(hi)[:48]!r}) {time.time() - t0:.0f}s")
        if hi is None:
            log(f"  beta scan done: {n_chunks} chunks in {time.time() - t0:.0f}s")
            return
        after = hi
        if pause:
            time.sleep(pause)


def url_file_rows(paths):
    """Offline input: any text/CSV file; every http(s) URL in it is one value."""
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                for u in URL_RE.findall(line):
                    yield (u,)


# ------------------------------------------------------------------------------------------- output
def existing_count(path: Path) -> int | None:
    """The trailer count of the current list (cheap: reads the tail), or None if there is none/it is bad."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
    except OSError:
        return None
    m = re.match(r"^# end count=(\d+)$", tail[-1].strip()) if tail else None
    return int(m.group(1)) if m else None


def write_atomic(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    data = text.encode("utf-8")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    # re-parse what we are about to install with the SERVER's parser: never publish a file it would reject
    cis.parse_allowlist(tmp.read_text(encoding="utf-8"))
    os.replace(tmp, path)
    return hashlib.sha256(data).hexdigest()


def build(rows, out: Path, *, host: str, source_label: str, max_shrink: float, allow_shrink: bool,
          min_keys: int, dry_run: bool, fail_on_other_routes: bool) -> tuple[int, dict]:
    t0 = time.time()
    entries, stats = collect(rows, host)
    counts = {s: len(v) for s, v in entries.items() if v}
    total = sum(counts.values())
    summary = {"out": str(out), "source": source_label, "host": host, "total": total, "counts": counts,
               "url_outcomes": dict(stats), "seconds": round(time.time() - t0, 1)}
    if stats.get("our_host_other_route") and fail_on_other_routes:
        summary["refused"] = (f"{stats['our_host_other_route']} picture URLs on {host} are not /img/<source>/<key> "
                              "(e.g. /card/ or /r): the public door 404s those -- fix the server allow-list "
                              "design before publishing this list")
        return 3, summary
    if total < min_keys:
        summary["refused"] = f"only {total} keys (< --min-keys {min_keys})"
        return 3, summary
    prev = existing_count(out)
    summary["previous_total"] = prev
    if prev and total < prev * (1.0 - max_shrink) and not allow_shrink:
        summary["refused"] = (f"would shrink the list from {prev:,} to {total:,} keys (> {max_shrink:.0%}); "
                              "re-run with --allow-shrink if that is intended")
        return 3, summary
    if dry_run:
        summary["dry_run"] = True
        return 0, summary
    meta = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": source_label,
            "host": host, "rows_with_picture": stats.get("rows", 0),
            "skipped_other_host": stats.get("other_host", 0),
            "skipped_other_route": stats.get("our_host_other_route", 0),
            "skipped_bad": stats.get("bad_key", 0) + stats.get("unknown_source", 0) + stats.get("unparseable", 0)}
    for s, n in counts.items():
        meta[f"count_{s}"] = n
    summary["sha256"] = write_atomic(out, cis.format_allowlist(entries, meta))
    summary["built_at"] = meta["built_at"]
    return 0, summary


def check(path: Path) -> int:
    try:
        sets, meta = cis.load_allowlist_file(str(path))
    except (OSError, cis.AllowListError) as e:
        print(json.dumps({"path": str(path), "ok": False, "error": f"{type(e).__name__}: {e}"}, indent=1))
        return 3
    counts = {s: len(v) for s, v in sets.items() if v}
    print(json.dumps({"path": str(path), "ok": True, "total": sum(counts.values()), "counts": counts,
                      "meta": meta}, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(cis.ALLOWLIST_PATH))
    ap.add_argument("--host", default=IMAGE_HOST, help="the image server's public hostname (%(default)s)")
    ap.add_argument("--from-urls", nargs="+", type=Path, metavar="FILE",
                    help="offline: build from files of image URLs instead of the beta")
    ap.add_argument("--beta-password-file", type=Path, default=DEFAULT_PWFILE)
    ap.add_argument("--chunk", type=int, default=50_000, help="beta rows per keyset chunk")
    ap.add_argument("--pause", type=float, default=0.2, help="seconds between chunks")
    ap.add_argument("--statement-timeout", type=int, default=60, help="seconds per statement")
    ap.add_argument("--max-shrink", type=float, default=0.10)
    ap.add_argument("--allow-shrink", action="store_true")
    ap.add_argument("--min-keys", type=int, default=1000)
    ap.add_argument("--allow-other-routes", action="store_true",
                    help="publish even if the beta references non-/img/ URLs on our host")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="validate the current --out file and print its counts")
    a = ap.parse_args(argv)

    if a.check:
        return check(a.out)
    if a.chunk < 1000:
        print("--chunk must be >= 1000", file=sys.stderr)
        return 2

    a.out.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(a.out) + ".lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another build holds the lock; nothing done")
        return 0

    if a.from_urls:
        rows, label = url_file_rows(a.from_urls), "files:" + ",".join(p.name for p in a.from_urls)
    else:
        rows, label = beta_rows(a.beta_password_file, a.chunk, a.pause, a.statement_timeout), "beta_catalog"
    log(f"building public image allow-list from {label} -> {a.out}{' (dry run)' if a.dry_run else ''}")
    try:
        rc, summary = build(rows, a.out, host=a.host, source_label=label, max_shrink=a.max_shrink,
                            allow_shrink=a.allow_shrink, min_keys=a.min_keys, dry_run=a.dry_run,
                            fail_on_other_routes=not a.allow_other_routes)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 -- report and leave the current list in force
        if type(e).__module__.startswith("psycopg"):
            log(f"database error, current list left in force: {type(e).__name__}: {str(e)[:200]}")
            return 4
        raise
    print(json.dumps(summary, indent=1, sort_keys=True))
    if rc:
        log(f"REFUSED: {summary.get('refused')}; current list left in force")
    else:
        log(f"{'would write' if a.dry_run else 'wrote'} {summary['total']:,} keys {summary['counts']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
