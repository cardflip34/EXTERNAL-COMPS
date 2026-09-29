#!/usr/bin/env python3
"""Import Fanatics full-catalog V3 JSONL chunks into Neon external_transactions.

Mini-side bridge only. This is additive/idempotent and intentionally lands rows
as external reference comps (`source_code='fanatics'`) for the 8504 Fanatics tab.
It never promotes rows into trust, Mazified, or valuation workflows.

Resumable: a per-chunk byte-offset checkpoint (PROGRESS_PATH_FMT) records how far each
append-only chunk has been committed, so a run reads only lines added since the last
commit. --full-rescan ignores it; --seed-progress-from-report bootstraps it.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mazi_db.scripts.external_comp_capture_spec import (  # noqa: E402
    capture_fields,
    ensure_external_transactions_capture_columns,
    spec_raw_patch,
)
from mazi_db.scripts.import_ebay_scrub_store_to_neon import (  # noqa: E402
    connect_db,
    normalize_title,
)
from mazi_db.scripts.import_local_fanatics_comps_to_neon import ensure_source  # noqa: E402

import re as _re_mod
import os
_re_lock = _re_mod.compile(r"pid=(\d+)")

SOURCE_CODE = "fanatics"
STORE = Path("/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/fanatics_full_catalog_v3")
CHUNK_DIR = STORE / "chunks"
REPORT_DIR = Path("/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store/bridge_reports")
LOCK_PATH = STORE / ".import_fanatics_v3_chunks_to_neon.lock"
# Per-chunk byte-offset checkpoint, one file per target. "offset" = end of the last line whose
# batch COMMITTED, so the next run seeks there instead of re-reading the catalog. Without it every
# nightly run re-read all ~1.5M rows (65-170 min) to insert a few thousand found at the very end, and
# a run killed by the 4h leg timeout delivered nothing (2026-09-11..13: three runs, zero rows).
PROGRESS_PATH_FMT = str(STORE / ".import_fanatics_v3_chunks_to_neon.progress.{target}.json")
# Neon drops sessions / turns briefly read-only while autoscaling. Those are SESSION failures, not
# data errors: reconnect and retry the same batch. ReadOnlySqlTransaction is an InternalError in
# psycopg 3, not an OperationalError, so it must be listed explicitly.
RETRYABLE_DB_ERRORS = (psycopg.errors.ReadOnlySqlTransaction, psycopg.OperationalError)
RETRY_DELAYS_S = (5, 15, 30, 60, 120, 240)
TITLE_FLAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "lot": re.compile(r"\b(lot|lots)\b", re.I),
    "box": re.compile(r"\b(box|boxes)\b", re.I),
    "sealed": re.compile(r"\bsealed\b", re.I),
    "break": re.compile(r"\b(break|breaks|breaker)\b", re.I),
    "pack": re.compile(r"\b(pack|packs)\b", re.I),
    "reprint": re.compile(r"\b(reprint|reprints)\b", re.I),
    "custom": re.compile(r"\bcustom\b", re.I),
    "digital": re.compile(r"\bdigital\b", re.I),
    "non_card": re.compile(r"\b(non[- ]?card|poster|photo|coin|pin|sticker only)\b", re.I),
}


def json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def acquire_lock() -> None:
    """Exclusive bridge lock, with dead-holder reclaim.

    A lock whose recorded pid is gone is not a lock -- it is litter from a killed run.
    Without this, one SIGKILL wedges every subsequent nightly leg forever: measured
    2026-07-31, seven days of Fanatics chunks never reached Neon because pid 84513
    (dead since 07-30) still owned the file and each leg exited rc=1 in ~3s.
    """
    for attempt in (1, 2):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt == 2:
                raise SystemExit(f"ERROR: bridge lock exists: {LOCK_PATH}")
            stale = False
            try:
                txt = open(str(LOCK_PATH)).read()
                m = _re_lock.search(txt)
                if not m:
                    stale = True                 # no pid recorded -> unusable
                else:
                    try:
                        os.kill(int(m.group(1)), 0)
                    except ProcessLookupError:
                        stale = True             # holder is dead -> reclaim
                    except PermissionError:
                        stale = False            # another uid -> respect it
            except OSError:
                stale = True
            if not stale:
                raise SystemExit(f"ERROR: bridge lock exists: {LOCK_PATH}")
            print(f"[stale-lock] reclaiming {LOCK_PATH} (holder dead)", flush=True)
            try:
                os.unlink(str(LOCK_PATH))
            except FileNotFoundError:
                pass
            continue                             # retry the exclusive create
        with os.fdopen(fd, "w") as f:
            f.write(f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat()}\n")
        return


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def coerce_price(value: Any) -> Decimal | None:
    if value in (None, "", "-", "—") or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = re.sub(r"(?i)\bUSD\b|US\s*\$", "", str(value)).replace("$", "").replace(",", "").strip()
    if text in ("", "-", "—"):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def coerce_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    tz_stripped = re.sub(r"\s+(PST|PDT|EST|EDT|CST|CDT|MST|MDT)$", "", text, flags=re.I)
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(tz_stripped.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def coerce_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = re.sub(r"\s+(PST|PDT|EST|EDT|CST|CDT|MST|MDT)$", "", str(value).strip(), flags=re.I)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(str(url))
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")


def stable_source_item_id(row: dict[str, Any], canonical_source_url: str | None) -> str | None:
    for key in ("source_item_id", "api_id", "listing_uuid", "ref_id", "comp_id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return canonical_source_url


def title_flags(title: str | None) -> list[str]:
    return [name for name, pattern in TITLE_FLAG_PATTERNS.items() if pattern.search(title or "")]


def best_image(row: dict[str, Any]) -> str | None:
    value = row.get("image_url")
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    urls = row.get("image_urls")
    if isinstance(urls, list):
        for candidate in urls:
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
    return None


def build_row(raw_row: dict[str, Any], chunk_name: str, line_no: int) -> tuple[dict[str, Any] | None, list[str]]:
    title = str(raw_row.get("title") or "").strip()
    price = coerce_price(raw_row.get("sold_price") if raw_row.get("sold_price") is not None else raw_row.get("price"))
    sold_date = coerce_date(raw_row.get("sold_date"))
    canonical_source_url = canonical_url(raw_row.get("url"))
    source_item_id = stable_source_item_id(raw_row, canonical_source_url)
    rejects: list[str] = []
    if not title:
        rejects.append("missing_title")
    if price is None:
        rejects.append("missing_price")
    elif price <= 0:
        rejects.append("nonpositive_price")
    if sold_date is None:
        rejects.append("missing_sold_date")
    if not source_item_id:
        rejects.append("missing_source_item_id")
    rejects.extend(f"title_flag:{flag}" for flag in title_flags(title))
    if rejects:
        return None, rejects

    image_url = best_image(raw_row)
    raw = dict(raw_row)
    if image_url:
        raw.setdefault("image_url", image_url)
    raw.setdefault("source_item_id", source_item_id)
    raw.setdefault("source_url", raw_row.get("url"))
    raw.setdefault("sale_format", raw_row.get("sale_format") or "auction")
    raw.setdefault(
        "item_specifics",
        {
            "api_id": raw_row.get("api_id"),
            "ref_id": raw_row.get("ref_id"),
            "listing_uuid": raw_row.get("listing_uuid"),
            "auction_type": raw_row.get("auction_type"),
            "buyers_premium": raw_row.get("buyers_premium"),
            "category": raw_row.get("category"),
            "condition": raw_row.get("condition"),
            "shard": raw_row.get("shard"),
        },
    )
    raw["_mazi_bridge"] = {
        "source_file": chunk_name,
        "source_line": line_no,
        "source_kind": "fanatics_full_catalog_v3",
        "reference_only": True,
        "not_trusted": True,
        "not_mazified": True,
    }
    capture = capture_fields(raw, SOURCE_CODE, source_item_id, raw_row.get("url"), download=False)
    raw.update(spec_raw_patch(capture))
    image_urls = raw_row.get("image_urls") if isinstance(raw_row.get("image_urls"), list) else []
    return {
        "source_code": SOURCE_CODE,
        "source_item_id": str(source_item_id),
        "source_url": raw_row.get("url"),
        "canonical_source_url": canonical_source_url,
        "title": title,
        "normalized_title": normalize_title(title),
        "sold_price": price,
        "sold_date": sold_date,
        "best_offer": False,
        "raw": raw,
        "scraped_at": coerce_datetime(raw_row.get("captured_at") or raw_row.get("scraped_at")),
        "sale_format": raw_row.get("sale_format") or capture.get("sale_format") or "auction",
        "bid_count": capture.get("bid_count"),
        "seller_id": capture.get("seller_id"),
        "shipping": capture.get("shipping"),
        "player": capture.get("player"),
        "year": str(raw_row.get("year")) if raw_row.get("year") not in (None, "") else capture.get("year"),
        "set_name": capture.get("set_name"),
        "parallel": capture.get("parallel"),
        "card_number": capture.get("card_number"),
        "grade": (
            f"{raw_row.get('grader')} {raw_row.get('grade')}".strip()
            if raw_row.get("grader") or raw_row.get("grade")
            else capture.get("grade")
        ),
        "grading_company": raw_row.get("grader") or capture.get("grading_company"),
        "cert_number": capture.get("cert_number"),
        "item_specifics_raw": {
            "api_id": raw_row.get("api_id"),
            "ref_id": raw_row.get("ref_id"),
            "listing_uuid": raw_row.get("listing_uuid"),
            "auction_type": raw_row.get("auction_type"),
            "buyers_premium": raw_row.get("buyers_premium"),
            "category": raw_row.get("category"),
            "condition": raw_row.get("condition"),
            "image_url": image_url,
            "image_urls": image_urls,
            "target_type_guess": raw_row.get("target_type_guess"),
            "shard": raw_row.get("shard"),
        },
        "local_image_paths": [],
        "image_sha256_paths": [],
        "image_count": len(image_urls) or (1 if image_url else 0),
        "scraper_version": raw_row.get("scraper_version") or "fanatics_full_catalog_v3",
    }, []


def insert_batch(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO external_transactions (
          source_code, source_item_id, source_url, canonical_source_url,
          title, normalized_title, sold_price, sold_date, best_offer, raw, scraped_at,
          sale_format, bid_count, seller_id, shipping, player, year, set_name,
          parallel, card_number, grade, grading_company, cert_number,
          item_specifics_raw, local_image_paths, image_sha256_paths, image_count,
          scraper_version
        ) VALUES (
          %(source_code)s, %(source_item_id)s, %(source_url)s, %(canonical_source_url)s,
          %(title)s, %(normalized_title)s, %(sold_price)s, %(sold_date)s, %(best_offer)s,
          %(raw)s, %(scraped_at)s, %(sale_format)s, %(bid_count)s, %(seller_id)s,
          %(shipping)s, %(player)s, %(year)s, %(set_name)s, %(parallel)s,
          %(card_number)s, %(grade)s, %(grading_company)s, %(cert_number)s,
          %(item_specifics_raw)s, %(local_image_paths)s, %(image_sha256_paths)s,
          %(image_count)s, %(scraper_version)s
        )
        ON CONFLICT DO NOTHING
    """
    with conn.cursor() as cur:
        params_rows: list[dict[str, Any]] = []
        for row in rows:
            params = dict(row)
            params["raw"] = Jsonb(params["raw"])
            params["item_specifics_raw"] = Jsonb(params["item_specifics_raw"])
            params["local_image_paths"] = Jsonb(params["local_image_paths"])
            params["image_sha256_paths"] = Jsonb(params["image_sha256_paths"])
            params_rows.append(params)
        cur.executemany(sql, params_rows)
        return max(cur.rowcount or 0, 0)


def create_import_run(conn: psycopg.Connection, target: str, chunks: list[Path], commit: bool) -> int | None:
    if not commit:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.comp_import_runs (
              lane, source_code, input_uri, target_db, dry_run, status, params, started_at
            ) VALUES (
              'nightly_delta', 'fanatics', %s, %s, FALSE, 'started', %s, NOW()
            )
            RETURNING id
            """,
            (
                f"fanatics_v3_chunks:{len(chunks)}",
                target,
                Jsonb({"bridge": "import_fanatics_v3_chunks_to_neon", "chunks": [str(p) for p in chunks]}),
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row[0] if not isinstance(row, dict) else row["id"])


def finish_import_run(conn: psycopg.Connection, run_id: int | None, report: dict[str, Any]) -> None:
    if not run_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.comp_import_runs
            SET status='completed', rows_seen=%s, rows_eligible=%s, rows_rejected=%s,
                rows_duplicates=%s, rows_inserted=%s, reject_counts=%s,
                duplicate_counts=%s, source_counts=%s, params=COALESCE(params, '{}'::jsonb) || %s,
                samples=%s, finished_at=NOW()
            WHERE id=%s
            """,
            (
                report["rows_seen"],
                report["rows_eligible"],
                report["rows_rejected"],
                report["rows_duplicate"],
                report["rows_inserted"],
                Jsonb(report["reject_reasons"]),
                Jsonb(report["duplicate_reasons"]),
                Jsonb(report["chunk_rows"]),
                Jsonb({"report_path": report.get("report_path")}),
                Jsonb(report["samples"]),
                run_id,
            ),
        )
        conn.commit()


def select_chunks(args: argparse.Namespace) -> tuple[list[Path], list[dict[str, Any]]]:
    paths = [Path(p) for p in sorted(glob.glob(str(Path(args.chunk_dir) / "fanatics_full_catalog_v3_*.jsonl")))]
    skipped: list[dict[str, Any]] = []
    now = time.time()
    selected: list[Path] = []
    explicit = set(args.chunks or [])
    for path in paths:
        if explicit and path.name not in explicit and str(path) not in explicit:
            continue
        age_minutes = (now - path.stat().st_mtime) / 60
        if not explicit and age_minutes < args.skip_active_minutes:
            skipped.append({"path": str(path), "reason": "recent_active_chunk", "age_minutes": round(age_minutes, 1)})
            continue
        selected.append(path)
    return selected, skipped


def load_progress(path: Path) -> dict[str, dict[str, int]]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8"))["chunks"])
    except FileNotFoundError:
        return {}
    except (ValueError, KeyError, TypeError) as exc:
        print(f"[progress] unreadable {path} ({exc}); reading every chunk from byte 0", file=sys.stderr, flush=True)
        return {}


def save_progress(path: Path, target: str, progress: dict[str, dict[str, int]]) -> None:
    doc = {"target": target, "updated_at": datetime.now(timezone.utc).isoformat(), "chunks": progress}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def seed_progress_from_report(report_path: Path, progress_path: Path, chunk_dir: Path, target: str) -> int:
    """Bootstrap the checkpoint from a finished --commit report instead of a multi-hour full re-read.

    Sound because a report is written only after every batch of its run committed, and a chunk
    last modified before that run started is byte-for-byte what the run read."""
    if progress_path.exists():
        raise SystemExit(f"ERROR: {progress_path} already exists; delete it to re-seed")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("commit") or report.get("target") != target or not report.get("finished_at") or report.get("limit"):
        raise SystemExit(f"ERROR: {report_path} is not a finished, unlimited --commit report for target={target}")
    started = datetime.fromisoformat(report["started_at"]).timestamp()
    chunk_rows = report.get("chunk_rows") or {}
    progress: dict[str, dict[str, int]] = {}
    left_to_read: list[str] = []
    for selected in report.get("chunks_selected") or []:
        path = chunk_dir / Path(selected).name
        st = path.stat()
        if st.st_mtime >= started:
            left_to_read.append(path.name)  # grew during/after that run: read it normally
            continue
        progress[path.name] = {"offset": st.st_size, "lines": int(chunk_rows.get(path.name) or 0)}
    save_progress(progress_path, target, progress)
    print(json.dumps({"seeded_from": str(report_path), "progress_path": str(progress_path),
                      "chunks_seeded": len(progress), "chunks_left_to_read": left_to_read}, indent=2))
    return 0


class NeonSession:
    """A Neon connection that outlives autoscaling blips: on a session-level failure the connection is
    dropped and the SAME unit of work retried on a fresh one. Inserts are ON CONFLICT DO NOTHING, so
    replaying a batch whose commit may or may not have landed is a no-op."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.conn: psycopg.Connection | None = None
        self.retries = 0

    def run(self, what: str, fn: Callable[[psycopg.Connection], Any]) -> Any:
        delays = iter(RETRY_DELAYS_S)
        while True:
            try:
                if self.conn is None or self.conn.closed:
                    self.conn = connect_db(self.target)
                return fn(self.conn)
            except RETRYABLE_DB_ERRORS as exc:
                delay = next(delays, None)
                if delay is None:
                    raise
                self.retries += 1
                print(f"[neon-retry] {what}: {type(exc).__name__}: {str(exc).strip()[:200]} -- reconnect in {delay}s",
                      file=sys.stderr, flush=True)
                self.close()
                time.sleep(delay)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        # ALTER TABLE takes an ACCESS EXCLUSIVE lock even when every column already exists, and while it
        # waits on a busy table every other query queues behind it. Fail fast instead; NeonSession retries.
        cur.execute("SET LOCAL lock_timeout = '10s'")
        ensure_external_transactions_capture_columns(cur)
        ensure_source(cur)
    conn.commit()


def commit_batch(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> int:
    inserted = insert_batch(conn, rows)
    conn.commit()
    return inserted


def process_chunks(session: NeonSession, chunks: list[Path], args: argparse.Namespace, run_id: int | None,
                   progress_path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "target": args.target,
        "commit": args.commit,
        "limit": args.limit,
        "rows_seen": 0,
        "rows_eligible": 0,
        "rows_rejected": 0,
        "rows_duplicate": 0,
        "rows_inserted": 0,
        "rows_with_image_url": 0,
        "reject_reasons": Counter(),
        "duplicate_reasons": Counter(),
        "chunk_rows": {},
        "samples": {"inserted_or_candidate": [], "duplicate": [], "rejected": []},
        "resume": {
            "progress_path": str(progress_path),
            "full_rescan": args.full_rescan,
            "chunks_unchanged": 0,
            "chunks_read": 0,
            "chunks_reset": [],
            "bytes_read": 0,
            "partial_tail_lines_left": 0,
        },
    }
    progress = load_progress(progress_path)
    pending: dict[str, dict[str, int]] = {}  # checkpoints for lines consumed since the last commit
    batch: list[dict[str, Any]] = []
    file_seen: set[str] = set()
    t0 = time.time()

    def flush() -> None:
        if not args.commit:
            pending.clear()  # a dry run must never advance the checkpoint
            return
        if batch:
            inserted = session.run(f"insert batch of {len(batch)}", lambda conn: commit_batch(conn, batch))
            duplicate = len(batch) - inserted
            report["rows_inserted"] += inserted
            report["rows_duplicate"] += duplicate
            if duplicate:
                report["duplicate_reasons"].update({"duplicate_existing_db_or_unique_index": duplicate})
            batch.clear()
        if pending:
            # Only AFTER the commit: dying between the two just replays those rows next run (no-op inserts).
            progress.update(pending)
            pending.clear()
            save_progress(progress_path, args.target, progress)

    for path in chunks:
        if args.limit and report["rows_seen"] >= args.limit:
            break
        size = path.stat().st_size
        done = {} if args.full_rescan else progress.get(path.name, {})
        offset, line_no = int(done.get("offset", 0)), int(done.get("lines", 0))
        if offset > size:
            # Chunks are append-only; a SHORTER file was rewritten, so its checkpoint is meaningless.
            report["resume"]["chunks_reset"].append(path.name)
            offset, line_no = 0, 0
        if offset == size:
            report["resume"]["chunks_unchanged"] += 1
            continue
        report["resume"]["chunks_read"] += 1
        chunk_seen = 0
        with path.open("rb") as f:
            f.seek(offset)
            for line in f:
                if args.limit and report["rows_seen"] >= args.limit:
                    break
                if not line.endswith(b"\n"):
                    # A writer is mid-append. Consuming this would reject it as invalid_json forever;
                    # leave it (and the checkpoint) for the next run, which will see the whole line.
                    report["resume"]["partial_tail_lines_left"] += 1
                    break
                offset += len(line)
                line_no += 1
                report["resume"]["bytes_read"] += len(line)
                pending[path.name] = {"offset": offset, "lines": line_no}
                if not line.strip():
                    continue
                report["rows_seen"] += 1
                chunk_seen += 1
                try:
                    raw_row = json.loads(line)
                except ValueError as exc:
                    report["rows_rejected"] += 1
                    report["reject_reasons"].update(["invalid_json"])
                    if len(report["samples"]["rejected"]) < 10:
                        report["samples"]["rejected"].append({"chunk": path.name, "line": line_no, "reason": str(exc)})
                    continue
                row, rejects = build_row(raw_row, path.name, line_no)
                if rejects:
                    report["rows_rejected"] += 1
                    report["reject_reasons"].update(rejects)
                    if len(report["samples"]["rejected"]) < 10:
                        report["samples"]["rejected"].append({"chunk": path.name, "line": line_no, "title": raw_row.get("title"), "reasons": rejects})
                    continue
                key = row["source_item_id"]
                if key in file_seen:
                    report["rows_duplicate"] += 1
                    report["duplicate_reasons"].update(["duplicate_in_fanatics_v3_chunks"])
                    continue
                file_seen.add(key)
                report["rows_eligible"] += 1
                if row["item_specifics_raw"].get("image_url"):
                    report["rows_with_image_url"] += 1
                if len(report["samples"]["inserted_or_candidate"]) < 10:
                    report["samples"]["inserted_or_candidate"].append(
                        {
                            "chunk": path.name,
                            "source_item_id": row["source_item_id"],
                            "title": row["title"],
                            "sold_price": str(row["sold_price"]),
                            "sold_date": row["sold_date"].isoformat(),
                            "image_url": bool(row["item_specifics_raw"].get("image_url")),
                        }
                    )
                if args.commit:
                    batch.append(row)
                    if len(batch) >= args.batch_size:
                        flush()
        report["chunk_rows"][path.name] = chunk_seen
    flush()
    report["neon_retries"] = session.retries
    report["reject_reasons"] = dict(report["reject_reasons"].most_common())
    report["duplicate_reasons"] = dict(report["duplicate_reasons"].most_common())
    report["elapsed_seconds"] = round(time.time() - t0, 2)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["dev", "prod"], default="prod")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--yes-i-understand-prod", action="store_true")
    ap.add_argument("--chunk-dir", default=str(CHUNK_DIR))
    ap.add_argument("--chunks", nargs="*", default=None, help="Optional explicit chunk filenames/paths")
    ap.add_argument("--skip-active-minutes", type=int, default=0,
                    help="Skip chunks modified this recently. Default 0: a partial trailing line is never consumed, "
                         "so the live chunk is safe to read, and skipping it delays its rows a whole bridge cycle.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--full-rescan", action="store_true",
                    help="Ignore the checkpoint and re-read the selected chunks from byte 0 (e.g. after changing build_row)")
    ap.add_argument("--seed-progress-from-report", metavar="REPORT_JSON", default=None,
                    help="Write the checkpoint from a finished --commit bridge report, then exit (no DB access)")
    args = ap.parse_args()
    if args.commit and args.target == "prod" and not args.yes_i_understand_prod:
        raise SystemExit("ERROR: prod commit requires --yes-i-understand-prod")
    progress_path = Path(PROGRESS_PATH_FMT.format(target=args.target))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    chunks, skipped = select_chunks(args)
    acquire_lock()
    session = NeonSession(args.target)
    try:
        if args.seed_progress_from_report:
            return seed_progress_from_report(Path(args.seed_progress_from_report), progress_path,
                                             Path(args.chunk_dir), args.target)
        session.run("ensure schema", ensure_schema)
        run_id = session.run("create import run", lambda conn: create_import_run(conn, args.target, chunks, args.commit))
        report = process_chunks(session, chunks, args, run_id, progress_path)
        report["chunks_selected"] = [str(p) for p in chunks]
        report["chunks_skipped"] = skipped
        report["import_run_id"] = run_id
        out = REPORT_DIR / f"fanatics_v3_bridge_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{'commit' if args.commit else 'dryrun'}.json"
        report["report_path"] = str(out)
        out.write_text(json.dumps(report, indent=2, default=json_default) + "\n", encoding="utf-8")
        session.run("finish import run", lambda conn: finish_import_run(conn, run_id, report))
        print(json.dumps(report, indent=2, default=json_default))
        print(f"REPORT={out}")
        return 0
    finally:
        session.close()
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
