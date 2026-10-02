#!/usr/bin/env python3
"""build_image_allowlist.py -- the PUBLIC allow-list for external_engine/comp_image_server.py (2026-10-01).

READ-ONLY on the beta. It never writes the database: every transaction is BEGIN READ ONLY (psycopg's read_only)
and also says SET TRANSACTION READ ONLY.

The image server's public door (Tailscale Serve/Funnel) may only serve pictures the site actually shows. The
site shows exactly what beta_catalog.image_small / image_large point at, so this reads those two columns and
writes every /img/<source>/<key> on our image host to one file:

    # mazi-image-allowlist v1
    # built_at=2026-10-01T23:40:00Z
    # ...counts (count_<source>=N)...
    scp_catalog/3108997300ae3e33f70d
    ebay/206162825097
    # end count=398231

The server re-reads the file when it changes (within ~30 s), ignores a file that does not parse (keeping its
last good list, also across restarts via public_allowlist.last_good.txt), so a failed or partial build can never
blank the site. The write here is atomic anyway (tmp file, fsync, re-parse with the server's parser, rename).

HOW IT READS THE BETA (2026-10-02). beta_catalog is ~9.1M rows / ~755K pages (~6.2 GB heap) on a Small instance
(shared_buffers 512MB) shared with the live site and staging, and no index covers the picture columns, so every
build reads the whole heap. The default --scan seq does that ONCE, as a plain sequential scan behind a NO SCROLL
server-side cursor in one READ ONLY transaction (EXPLAIN DECLARE on the beta: "Seq Scan on beta_catalog", no
parallel workers -- cursors never get them). A sequential scan of a table bigger than shared_buffers/4 reads
through Postgres's small bulk-read ring buffer, so it does NOT push the site's hot pages out of shared_buffers;
the old keyset walk (index range scans, ~190 chunks) did, and also read the 1.15 GB primary-key index. The cost
of the cursor is one snapshot held for the length of the build: FETCHes of --fetch rows with --pause between
them, each FETCH under SET LOCAL statement_timeout, the whole run under --max-seconds (then it rolls back and
leaves the current list in force). --scan keyset keeps the old chunked walk for when a long snapshot is the
bigger concern. All settings are SET LOCAL / SET TRANSACTION (transaction-scoped): nothing session-level is ever
SET through the Supabase pooler, and prepared statements are off (prepare_threshold=None).
The full-build time on the live beta has NOT been measured (only the plan): the first --dry-run on the Mini is
the measurement, and should be run off-peak. A partial index on beta_catalog(card_id) WHERE image_small IS NOT
NULL OR image_large IS NOT NULL would cut a build to ~400K rows; that is a Tier-3 schema change, not done here.
Schedule: twice a day (ops/launchd/com.mazi.image-allowlist.plist), plus an on-demand
`launchctl kickstart gui/$(id -u)/com.mazi.image-allowlist` after any lane writes new picture URLs.

SHRINK GUARD. The site loses pictures if this list loses keys, so the build refuses (exit 3) to replace the
previous list when the TOTAL, or ANY SOURCE that had at least --shrink-floor (100) keys, would shrink by more than
--max-shrink (10%), unless --allow-shrink. Per source because the 5,036 VeeFriends eBay keys are ~1.3% of the
total: they could all vanish (a host constant or classification bug) without moving the total. The previous
list is the current file, parsed with the server's parser; if that is missing or bad, the server's
public_allowlist.last_good.txt copy.

STAGED KEYS. staged.txt (see the server) never expires on its own, so every build reports the staged keys that
are already on the new list (safe to drop) and those the beta does not reference (about to be applied -- or
withdrawn, in which case remove them: a staged key stays public). --prune-staged drops the already-listed ones
from staged.txt after a successful write (atomic, and skipped if the file changed while the build ran). The
schedule runs with --prune-staged, so a key that was staged, applied and listed is no longer pinned public by
staged.txt and a later withdrawal from the beta takes it down at the next build.

Lot photos (lotphoto_goldin, lotphoto_fanatics) are public as whole folders on the server side; the ones the
beta references are listed here too, only so the counts are complete.

  python3 tools/build_image_allowlist.py                       # beta -> ~/mazi_local_evidence/image_allowlist/public_allowlist.txt
  python3 tools/build_image_allowlist.py --dry-run             # read + count + guards, write nothing
  python3 tools/build_image_allowlist.py --from-urls FILE ...  # offline: build from text/CSV files of image URLs
  python3 tools/build_image_allowlist.py --check               # validate the current list file and print its counts

Exit codes: 0 written (or dry run), 2 usage/config, 3 refused by a guard (shrink / too few keys / unsupported
routes), 4 database error or --max-seconds exceeded (the current list is left in force).
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

# --scan seq (default): one sequential scan behind a server-side cursor
CURSOR_NAME = "mazi_image_allowlist"
SEQ_SQL = ("SELECT image_small, image_large FROM public.beta_catalog "
           "WHERE image_small IS NOT NULL OR image_large IS NOT NULL")
# --scan keyset: chunked primary-key walk
BOUND_SQL = ("SELECT card_id FROM public.beta_catalog WHERE card_id > %s "
             "ORDER BY card_id LIMIT 1 OFFSET %s")
RANGE_SQL = ("SELECT image_small, image_large FROM public.beta_catalog "
             "WHERE card_id > %s AND card_id <= %s AND (image_small IS NOT NULL OR image_large IS NOT NULL)")
TAIL_SQL = ("SELECT image_small, image_large FROM public.beta_catalog "
            "WHERE card_id > %s AND (image_small IS NOT NULL OR image_large IS NOT NULL)")
URL_RE = re.compile(r"https?://[^\s,\"'<>]+")


class BuildTimeout(RuntimeError):
    pass


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
def tx_settings(conn, statement_timeout_s: int, idle_timeout_s: int | None = None) -> None:
    """First statements of EVERY transaction. Transaction-scoped only: a session-level SET through a pooler can
    outlive this connection's use of the backend (the Neon-pooler lesson), SET LOCAL / SET TRANSACTION cannot."""
    conn.execute("SET TRANSACTION READ ONLY")
    conn.execute(f"SET LOCAL statement_timeout = '{int(statement_timeout_s)}s'")
    conn.execute("SET LOCAL lock_timeout = '10s'")
    if idle_timeout_s:
        # if this process stalls or dies mid-cursor, the server ends the transaction (and its snapshot) itself
        conn.execute(f"SET LOCAL idle_in_transaction_session_timeout = '{int(idle_timeout_s)}s'")


def connect_beta(pwfile: Path):
    import psycopg
    if not pwfile.is_file() or pwfile.stat().st_mode & 0o077:
        raise SystemExit(f"{pwfile}: must exist with mode 600")
    conn = psycopg.connect(host=BHOST, port=5432, user=f"postgres.{BETA}", dbname="postgres",
                           password=pwfile.read_text().strip(), sslmode="require", connect_timeout=15,
                           application_name="mazi-image-allowlist", autocommit=True,
                           prepare_threshold=None)      # no named prepared statements through the pooler
    conn.read_only = True                     # every transaction below is BEGIN READ ONLY
    return conn


def beta_rows(pwfile: Path, *, scan: str, chunk: int, fetch: int, pause: float, statement_timeout_s: int,
              max_seconds: float):
    """Yield (image_small, image_large) for every beta_catalog row with a picture."""
    conn = connect_beta(pwfile)
    try:
        if scan == "seq":
            yield from seq_rows(conn, fetch, pause, statement_timeout_s, max_seconds)
        else:
            yield from keyset_chunks(conn, chunk, pause, statement_timeout_s, max_seconds)
    finally:
        conn.close()


def seq_rows(conn, fetch: int, pause: float, statement_timeout_s: int, max_seconds: float):
    """One READ ONLY transaction, one sequential scan behind a NO SCROLL cursor, read --fetch rows at a time.
    Leaving the generator early (an exception downstream, a timeout) rolls the transaction back."""
    t0, n_rows, n_fetch = time.time(), 0, 0
    with conn.transaction():
        tx_settings(conn, statement_timeout_s, idle_timeout_s=int(max(60, pause * 4 + 60)))
        conn.execute(f"DECLARE {CURSOR_NAME} NO SCROLL CURSOR FOR {SEQ_SQL}")
        while True:
            rows = conn.execute(f"FETCH FORWARD {int(fetch)} FROM {CURSOR_NAME}").fetchall()
            n_fetch += 1
            n_rows += len(rows)
            yield from rows
            if n_fetch % 10 == 0:
                log(f"  fetch {n_fetch}: {n_rows:,} rows with a picture, {time.time() - t0:.0f}s")
            if len(rows) < fetch:
                break
            if time.time() - t0 > max_seconds:
                raise BuildTimeout(f"beta read passed --max-seconds {max_seconds:.0f} after {n_rows:,} rows")
            if pause:
                time.sleep(pause)
        conn.execute(f"CLOSE {CURSOR_NAME}")
    log(f"  beta scan done: {n_rows:,} rows with a picture, {n_fetch} fetches, {time.time() - t0:.0f}s")


def keyset_chunks(conn, chunk: int, pause: float, statement_timeout_s: int = 60, max_seconds: float = 3600):
    after, n_chunks, t0 = "", 0, time.time()
    while True:
        with conn.transaction():
            tx_settings(conn, statement_timeout_s)
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
        if time.time() - t0 > max_seconds:
            raise BuildTimeout(f"beta read passed --max-seconds {max_seconds:.0f} after {n_chunks} chunks")
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


# ------------------------------------------------------------------------------------------- previous list
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


def previous_list(path: Path) -> dict | None:
    """What the shrink guard compares against: the current list, parsed with the server's parser, else the
    server's last-good copy of it. None if neither parses (first build)."""
    for p, label in ((Path(path), "list"), (Path(cis.last_good_path_for(str(path))), "last_good")):
        try:
            sets, meta = cis.load_allowlist_file(str(p))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:      # AllowListError and UnicodeDecodeError are ValueErrors
            log(f"previous list {p} unusable for the shrink guard ({type(e).__name__}: {e})")
            continue
        counts = {s: len(v) for s, v in sets.items() if v}
        return {"path": str(p), "from": label, "counts": counts, "total": sum(counts.values()),
                "built_at": meta.get("built_at")}
    return None


def shrink_problems(prev: dict, counts: dict, max_shrink: float, shrink_floor: int) -> list:
    """Every way the new counts shrink past --max-shrink: the total, and each source that had >= shrink_floor keys."""
    out = []
    total = sum(counts.values())
    if prev["total"] and total < prev["total"] * (1.0 - max_shrink):
        out.append(f"total {prev['total']:,} -> {total:,}")
    for s, n_prev in sorted(prev["counts"].items()):
        n_new = counts.get(s, 0)
        if n_prev >= shrink_floor and n_new < n_prev * (1.0 - max_shrink):
            out.append(f"{s} {n_prev:,} -> {n_new:,}")
    return out


# ------------------------------------------------------------------------------------------- staged file
def staged_report(staged_path: str | None, entries: dict, sample: int = 25) -> dict | None:
    """Which staged keys are already on the new list (safe to drop) and which the source does not reference."""
    if not staged_path:
        return None
    rep: dict = {"path": str(staged_path)}
    try:
        text = Path(staged_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        rep["present"] = False
        return rep
    except (OSError, ValueError) as e:
        rep.update(present=True, error=f"{type(e).__name__}: {e}")
        return rep
    try:
        staged = cis.parse_staged(text)
    except cis.AllowListError as e:
        rep.update(present=True, error=f"does not parse ({e}); the server keeps its last good staged set")
        return rep
    listed = sorted(f"{s}/{k}" for s, ks in staged.items() for k in ks if k in entries.get(s, ()))
    unlisted = sorted(f"{s}/{k}" for s, ks in staged.items() for k in ks if k not in entries.get(s, ()))
    rep.update(present=True, total=len(listed) + len(unlisted), already_listed=len(listed),
               not_referenced=len(unlisted), not_referenced_keys=unlisted[:sample],
               already_listed_keys=listed[:sample])
    return rep


def _sig(st) -> tuple:
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def prune_staged(staged_path: str, entries: dict) -> int:
    """Drop staged lines whose key is on the new list; keep comments, blank lines and everything else as written.
    Atomic, keeps the file mode, and does nothing if the file changed since it was read or does not parse.
    -> lines dropped."""
    p = Path(staged_path)
    try:
        with open(p, "rb") as f:
            st = os.fstat(f.fileno())
            text = f.read().decode("utf-8")
    except FileNotFoundError:
        return 0
    cis.parse_staged(text)                    # never rewrite a file the server would reject
    keep, dropped = [], 0
    for line in text.splitlines(keepends=True):
        ln = line.strip()
        if ln and not ln.startswith("#"):
            (s, ks), = cis.parse_staged(ln).items()
            if next(iter(ks)) in entries.get(s, ()):
                dropped += 1
                continue
        keep.append(line)
    if not dropped:
        return 0
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write("".join(keep).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, st.st_mode & 0o777)
        if _sig(os.stat(p)) != _sig(st):
            log(f"staged file {p} changed while building; not pruning it this time")
            return 0
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()
    return dropped


# ------------------------------------------------------------------------------------------- output
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
          min_keys: int, dry_run: bool, fail_on_other_routes: bool, shrink_floor: int = 100,
          staged_path: str | None = None, prune: bool = False) -> tuple[int, dict]:
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
    prev = previous_list(out)
    if prev:
        summary.update(previous_total=prev["total"], previous_counts=prev["counts"], previous_from=prev["from"])
        problems = shrink_problems(prev, counts, max_shrink, shrink_floor)
        if problems:
            summary["shrink"] = problems
            if not allow_shrink:
                summary["refused"] = (f"would shrink by more than {max_shrink:.0%}: {'; '.join(problems)} (previous "
                                      f"list: {prev['path']}); re-run with --allow-shrink if that is intended")
                return 3, summary
    summary["staged"] = staged_report(staged_path, entries)
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
    # only after the new list is on disk: the server reads the list before the staged file in the same poll, so a
    # pruned key is never missing from both
    if prune and staged_path and (summary["staged"] or {}).get("already_listed"):
        try:
            summary["staged"]["pruned"] = prune_staged(staged_path, entries)
        except (OSError, ValueError) as e:
            summary["staged"]["prune_error"] = f"{type(e).__name__}: {e}"
    return 0, summary


def check(path: Path) -> int:
    try:
        sets, meta = cis.load_allowlist_file(str(path))
    except (OSError, ValueError) as e:
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
    ap.add_argument("--scan", choices=("seq", "keyset"), default="seq",
                    help="seq: one sequential scan behind a cursor (default); keyset: chunked primary-key walk")
    ap.add_argument("--fetch", type=int, default=20_000, help="--scan seq: rows per FETCH")
    ap.add_argument("--chunk", type=int, default=50_000, help="--scan keyset: beta rows per chunk")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between fetches/chunks")
    ap.add_argument("--statement-timeout", type=int, default=120, help="seconds per statement (SET LOCAL)")
    ap.add_argument("--max-seconds", type=float, default=1200, help="give up (list left in force) after this long")
    ap.add_argument("--max-shrink", type=float, default=0.10)
    ap.add_argument("--shrink-floor", type=int, default=100,
                    help="sources with fewer keys than this in the previous list are not shrink-checked on their own")
    ap.add_argument("--allow-shrink", action="store_true")
    ap.add_argument("--min-keys", type=int, default=1000)
    ap.add_argument("--allow-other-routes", action="store_true",
                    help="publish even if the beta references non-/img/ URLs on our host")
    ap.add_argument("--staged", default=cis.STAGED_PATH, help="the server's staged file to report on ('' = skip)")
    ap.add_argument("--prune-staged", action="store_true",
                    help="after a successful write, drop staged keys that are now on the list")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="validate the current --out file and print its counts")
    a = ap.parse_args(argv)

    if a.check:
        return check(a.out)
    if a.chunk < 1000 or a.fetch < 1000:
        print("--chunk and --fetch must be >= 1000", file=sys.stderr)
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
        rows = beta_rows(a.beta_password_file, scan=a.scan, chunk=a.chunk, fetch=a.fetch, pause=a.pause,
                         statement_timeout_s=a.statement_timeout, max_seconds=a.max_seconds)
        label = "beta_catalog"
    log(f"building public image allow-list from {label}{'' if a.from_urls else f' (scan={a.scan})'} -> {a.out}"
        f"{' (dry run)' if a.dry_run else ''}")
    try:
        rc, summary = build(rows, a.out, host=a.host, source_label=label, max_shrink=a.max_shrink,
                            allow_shrink=a.allow_shrink, min_keys=a.min_keys, dry_run=a.dry_run,
                            fail_on_other_routes=not a.allow_other_routes, shrink_floor=a.shrink_floor,
                            staged_path=a.staged or None, prune=a.prune_staged)
    except SystemExit:
        raise
    except BuildTimeout as e:
        log(f"{e}; current list left in force")
        return 4
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
    st = summary.get("staged") or {}
    if st.get("error"):
        log(f"staged file {st['path']}: {st['error']}")
    elif st.get("total"):
        log(f"staged file: {st['total']} keys; {st['already_listed']} already listed"
            f"{' (pruned %d)' % st['pruned'] if 'pruned' in st else ' (safe to drop; --prune-staged)'}; "
            f"{st['not_referenced']} not referenced by the beta -- about to be applied, or withdrawn (then remove "
            "them from staged.txt: a staged key stays public)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
