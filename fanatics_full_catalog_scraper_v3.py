#!/usr/bin/env python3
"""Fanatics Collect full sales-history crawler.

This drives the same public sales-history API used by the site's "See more"
pagination. It is intentionally single-worker, checkpointed, and slow enough to
coexist with the Mini eBay lanes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import time
import urllib.parse
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROOT = Path(__file__).resolve().parent
EVIDENCE_ROOT = Path(os.environ.get("MAZI_EVIDENCE_ROOT", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper"))
OUT_DIR = Path(os.environ.get("MAZI_FANATICS_FULL_STORE", EVIDENCE_ROOT / "fanatics_full_catalog_v3"))
CHUNK_DIR = OUT_DIR / "chunks"
LOG_DIR = OUT_DIR / "logs"
STATE_FILE = OUT_DIR / "fanatics_full_catalog_v3_state.json"
UNRESOLVED_FILE = OUT_DIR / "fanatics_full_catalog_v3_unresolved_shards.jsonl"
INDEX_FILE = OUT_DIR / "fanatics_full_catalog_v3_index.sqlite"
LEGACY_FILE = ROOT / "fanatics_comps.json"

API_BASE = "https://sales-history-api.services.fanaticscollect.com/api/v1/pub/sales"
SITE_URL = "https://sales-history.fanaticscollect.com/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15"
)

PAGE_SIZE = 20
SOURCE_PAGE_CAP = 50
MAX_SOURCE_ROWS_PER_SHARD = 950
SCRAPER_VERSION = "fanatics_full_catalog_v3_20260622"

COOKIE_JAR = CookieJar()
OPENER = build_opener(HTTPCookieProcessor(COOKIE_JAR))
SESSION_WARMED = False

CATEGORIES = [
    "Baseball",
    "Basketball",
    "Football",
    "Hockey",
    "Golf",
    "Boxing & MMA",
    "Soccer",
    "Tennis",
    "Racing",
    "Misc Sports",
    "Non-Sport",
    "Yu-Gi-Oh!",
    "Magic The Gathering",
    "Pokémon",
    "Collectible Card Games",
    "Memorabilia",
    "Photos",
    "Comics",
    "Tickets",
    "Coins",
    "Video Games",
]

AUCTION_TYPES = ["PREMIER", "FIXED", "WEEKLY,FLASH,MONTHLY"]
GRADING_SERVICES = ["PSA", "SGC", "BGS", "BVG", "BCCG", "CGC", "CSG", "ungraded"]

# These are only used when category/auction/year/grader still exceeds the
# source's accessible page cap. They are not considered exhaustive; unresolved
# high shards are logged for the next split pass.
TITLE_BUCKETS = [
    "topps",
    "bowman",
    "panini",
    "donruss",
    "leaf",
    "upper deck",
    "fleer",
    "skybox",
    "prizm",
    "select",
    "optic",
    "mosaic",
    "chrome",
    "finest",
    "heritage",
    "stadium club",
    "rookie",
    "auto",
    "autograph",
    "refractor",
    "patch",
    "psa",
    "bgs",
    "sgc",
    "cgc",
    "pokemon",
    "charizard",
    "pikachu",
    "jordan",
    "ohtani",
    "brady",
    "mahomes",
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def save_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def warm_session(force: bool = False) -> None:
    global SESSION_WARMED
    if SESSION_WARMED and not force:
        return
    req = Request(SITE_URL, headers={"User-Agent": USER_AGENT})
    with OPENER.open(req, timeout=30) as resp:
        resp.read(4096)
    SESSION_WARMED = True


def request_json(params: dict[str, Any], retries: int = 8) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in (None, "", [])},
        doseq=True,
        safe=",",
    )
    url = f"{API_BASE}?{query}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            warm_session()
            req = Request(url, headers={"User-Agent": USER_AGENT, "Referer": SITE_URL})
            with OPENER.open(req, timeout=30) as resp:
                return json.load(resp)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            is_403 = isinstance(exc, HTTPError) and exc.code == 403
            if is_403:
                try:
                    warm_session(force=True)
                except Exception as warm_exc:  # noqa: BLE001
                    print(f"[WARN] warm_session_failed err={warm_exc}", flush=True)
            wait = min(420, (2 ** attempt) + random.uniform(2.0, 6.0))
            if is_403:
                wait = max(wait, min(420, 60 * (attempt + 1)))
            print(f"[WARN] request_failed attempt={attempt + 1} wait={wait:.1f}s url={url} err={exc}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"request failed after retries: {last_error}")


def records_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    embedded = data.get("_embedded") or {}
    rows = embedded.get("SalesRecords")
    if isinstance(rows, list):
        return rows
    for value in embedded.values():
        if isinstance(value, list):
            return value
    return []


def total_from(data: dict[str, Any]) -> int:
    page = data.get("page") or {}
    try:
        return int(page.get("totalElements") or 0)
    except (TypeError, ValueError):
        return 0


def stable_hash(value: Any, length: int = 24) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def normalize_key(value: Any) -> str:
    return str(value or "").strip()


def row_id(record: dict[str, Any]) -> str:
    for key in ("id", "listingUuid", "refId"):
        value = normalize_key(record.get(key))
        if value:
            return value
    return stable_hash(record)


def listing_url(record: dict[str, Any]) -> str:
    uuid = normalize_key(record.get("listingUuid"))
    auction_type = normalize_key(record.get("auctionType")).upper()
    if uuid:
        if auction_type == "PREMIER":
            return f"https://www.fanaticscollect.com/premier/{uuid}"
        if auction_type in {"WEEKLY", "MONTHLY", "FLASH"}:
            return f"https://www.fanaticscollect.com/weekly/{uuid}"
        if auction_type == "FIXED":
            return f"https://www.fanaticscollect.com/buy-now/{uuid}"
    self_href = (((record.get("_links") or {}).get("self") or {}).get("href") or "").strip()
    return self_href.replace("http://", "https://")


def image_urls(record: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in (
        "largeImage1",
        "largeImage2",
        "mediumImage1",
        "mediumImage2",
        "smallImage1",
        "smallImage2",
        "thumbnailImage1",
        "thumbnailImage2",
    ):
        value = normalize_key(record.get(key))
        if value and value not in urls:
            urls.append(value)
    return urls


def sale_format(record: dict[str, Any]) -> str:
    auction_type = normalize_key(record.get("auctionType")).upper()
    return "BIN" if auction_type == "FIXED" else "auction"


def target_type_guess(record: dict[str, Any]) -> str:
    title = (record.get("title") or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", title))
    if tokens.intersection({"lot", "lots", "bundle", "collection"}):
        return "multi_card_or_lot"
    if tokens.intersection({"box", "boxes", "pack", "packs", "sealed", "case"}):
        return "sealed_or_pack"
    if tokens.intersection({"ticket", "photo", "poster", "comic", "coin", "game"}):
        return "non_card_collectible"
    return "card_or_unknown"


def normalize_record(record: dict[str, Any], shard: dict[str, Any]) -> dict[str, Any] | None:
    title = normalize_key(record.get("title"))
    try:
        price = float(record.get("purchasePrice") or 0)
    except (TypeError, ValueError):
        price = 0.0
    sold_date = normalize_key(record.get("soldDate"))
    if not title or price <= 0 or not sold_date:
        return None
    api_url = (((record.get("_links") or {}).get("self") or {}).get("href") or "").replace("http://", "https://")
    urls = image_urls(record)
    source_item_id = row_id(record)
    return {
        "comp_id": f"FNC-{stable_hash(source_item_id, 20)}",
        "source": "fanatics",
        "source_item_id": source_item_id,
        "api_id": normalize_key(record.get("id")),
        "ref_id": normalize_key(record.get("refId")),
        "listing_uuid": normalize_key(record.get("listingUuid")),
        "url": listing_url(record),
        "api_url": api_url,
        "title": title,
        "subtitle": normalize_key(record.get("subtitle")),
        "sold_price": price,
        "sold_date": sold_date,
        "sale_format": sale_format(record),
        "auction_type": normalize_key(record.get("auctionType")),
        "buyers_premium": normalize_key(record.get("auctionType")).upper() != "FIXED",
        "category": normalize_key(record.get("category")),
        "year": normalize_key(record.get("year")),
        "grade": normalize_key(record.get("grade")),
        "grader": normalize_key(record.get("gradingService")),
        "condition": normalize_key(record.get("eyeAppealGrade")),
        "image_url": urls[0] if urls else "",
        "image_urls": urls,
        "target_type_guess": target_type_guess(record),
        "item_specifics_raw": {
            "category": record.get("category"),
            "year": record.get("year"),
            "gradingService": record.get("gradingService"),
            "grade": record.get("grade"),
            "eyeAppealGrade": record.get("eyeAppealGrade"),
            "auctionType": record.get("auctionType"),
        },
        "raw_payload": record,
        "capture_source": "fanatics_full_catalog",
        "scraper_version": SCRAPER_VERSION,
        "captured_at": now_iso(),
        "shard": shard,
    }


def dedupe_keys(row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for key in ("source_item_id", "api_id", "listing_uuid", "ref_id", "url", "api_url"):
        value = normalize_key(row.get(key))
        if value:
            keys.append(f"{key}:{value}")
    title_key = "|".join(normalize_key(row.get(k)) for k in ("title", "sold_price", "sold_date"))
    if title_key.strip("|"):
        keys.append(f"title_price_date:{title_key}")
    return keys


def init_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(INDEX_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS keys (key TEXT PRIMARY KEY, row_ref TEXT, created_at TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()
    return conn


def key_exists(conn: sqlite3.Connection, keys: list[str]) -> bool:
    for key in keys:
        if conn.execute("SELECT 1 FROM keys WHERE key = ? LIMIT 1", (key,)).fetchone():
            return True
    return False


def add_keys(conn: sqlite3.Connection, keys: list[str], row_ref: str) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO keys(key, row_ref, created_at) VALUES (?, ?, ?)",
        [(k, row_ref, now_iso()) for k in keys],
    )


def seed_index(conn: sqlite3.Connection, limit: int | None = None) -> int:
    if conn.execute("SELECT value FROM meta WHERE key='seeded_v3'").fetchone():
        return 0
    seeded = 0
    if LEGACY_FILE.exists():
        rows = load_json(LEGACY_FILE, [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            keys = dedupe_keys(row)
            if keys:
                add_keys(conn, keys, f"legacy:{row.get('comp_id') or row.get('source_item_id') or seeded}")
                seeded += 1
            if limit and seeded >= limit:
                break
    for path in sorted(CHUNK_DIR.glob("*.jsonl")):
        with path.open() as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                keys = dedupe_keys(row)
                if keys:
                    add_keys(conn, keys, f"{path.name}:{row.get('comp_id')}")
                    seeded += 1
                if limit and seeded >= limit:
                    break
        if limit and seeded >= limit:
            break
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('seeded_v3', ?)", (now_iso(),))
    conn.commit()
    return seeded


def shard_key(params: dict[str, Any]) -> str:
    return urllib.parse.urlencode(sorted(params.items()), doseq=True, safe=",")


def load_state() -> dict[str, Any]:
    return load_json(STATE_FILE, {
        "created_at": now_iso(),
        "accepted_shards": [],
        "completed_shards": {},
        "totals": {"seen": 0, "written": 0, "dupe": 0, "rejected": 0, "unresolved": 0},
        "next_chunk": 1,
    })


def chunk_path(state: dict[str, Any]) -> Path:
    return CHUNK_DIR / f"fanatics_full_catalog_v3_{int(state.get('next_chunk') or 1):06d}.jsonl"


def maybe_rotate_chunk(state: dict[str, Any], rows_per_chunk: int) -> Path:
    path = chunk_path(state)
    if not path.exists():
        return path
    line_count = 0
    with path.open() as f:
        for line_count, _ in enumerate(f, 1):
            pass
    if line_count >= rows_per_chunk:
        state["next_chunk"] = int(state.get("next_chunk") or 1) + 1
        save_json_atomic(STATE_FILE, state)
        return chunk_path(state)
    return path


def split_year_windows(params: dict[str, Any], year_min: int, year_max: int) -> list[dict[str, Any]]:
    shards = []
    for start in range(year_max, year_min - 1, -10):
        begin = max(year_min, start - 9)
        shards.append({**params, "yearMin": begin, "yearMax": start})
    return shards


def split_shard(params: dict[str, Any], total: int, year_min: int, year_max: int) -> tuple[list[dict[str, Any]], bool]:
    y0 = params.get("yearMin")
    y1 = params.get("yearMax")
    if y0 is None and y1 is None:
        return split_year_windows(params, year_min, year_max), True
    if y0 is not None and y1 is not None and int(y0) < int(y1):
        mid = (int(y0) + int(y1)) // 2
        return [
            {**params, "yearMin": mid + 1, "yearMax": int(y1)},
            {**params, "yearMin": int(y0), "yearMax": mid},
        ], True
    if not params.get("gradingService"):
        return [{**params, "gradingService": grader} for grader in GRADING_SERVICES], True
    if not params.get("title"):
        return [{**params, "title": term} for term in TITLE_BUCKETS], True
    return [], False


def discover_shards(
    base_shards: list[dict[str, Any]],
    year_min: int,
    year_max: int,
    sleep_seconds: float,
    max_discovery_probes: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queue = list(base_shards)
    accepted: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    probes = 0
    while queue:
        params = queue.pop(0)
        key = shard_key(params)
        if key in seen:
            continue
        seen.add(key)
        probes += 1
        data = request_json({**params, "page": 0, "size": 1})
        total = total_from(data)
        print(f"[DISCOVER] total={total} params={params}", flush=True)
        if total == 0:
            time.sleep(sleep_seconds)
            continue
        if total <= MAX_SOURCE_ROWS_PER_SHARD:
            accepted.append({**params, "_source_total": total})
        else:
            children, did_split = split_shard(params, total, year_min, year_max)
            if did_split:
                queue.extend(children)
            else:
                unresolved.append({**params, "_source_total": total, "_reason": "over_cap_after_all_splits"})
                append_jsonl(UNRESOLVED_FILE, unresolved[-1])
                print(f"[UNRESOLVED] total={total} params={params}", flush=True)
        if max_discovery_probes and probes >= max_discovery_probes:
            print(f"[DISCOVER_LIMIT] probes={probes} accepted={len(accepted)} queued={len(queue)}", flush=True)
            break
        time.sleep(sleep_seconds)
    return accepted, unresolved


def streaming_discover_and_scrape(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    base_shards_: list[dict[str, Any]],
    year_min: int,
    year_max: int,
    sleep_seconds: float,
    max_discovery_probes: int | None,
    max_shards: int | None,
    dry_run: bool,
    max_pages_per_shard: int | None,
    rows_per_chunk: int,
) -> tuple[int, int]:
    """Discover bounded shards and scrape each accepted shard immediately."""
    queue = state.get("discovery_queue")
    if not isinstance(queue, list):
        queue = list(base_shards_)
    accepted = state.setdefault("accepted_shards", [])
    completed = state.setdefault("completed_shards", {})
    totals = state.setdefault("totals", {"seen": 0, "written": 0, "dupe": 0, "rejected": 0, "unresolved": 0})

    seen_keys = set(state.get("discovery_seen_keys") or [])
    accepted_keys = {
        shard_key({k: v for k, v in shard.items() if not str(k).startswith("_")})
        for shard in accepted
        if isinstance(shard, dict)
    }
    probes = 0
    accepted_this_run = 0
    unresolved_this_run = 0
    unresolved_base = int(state.get("unresolved_count") or 0)
    state["streaming_mode"] = True
    state["streaming_started_at"] = state.get("streaming_started_at") or now_iso()
    state["completed"] = False
    state.pop("completed_at", None)

    def run_stream_shard(shard: dict[str, Any], label: str) -> None:
        run_key = shard_key({k: v for k, v in shard.items() if not str(k).startswith("_")})
        if completed.get(run_key) or dry_run:
            return
        print(
            f"[{label}] source_total={shard.get('_source_total')} {shard}",
            flush=True,
        )
        stats = scrape_shard(conn, state, shard, sleep_seconds, max_pages_per_shard, rows_per_chunk)
        for field in ("seen", "written", "dupe", "rejected"):
            totals[field] = int(totals.get(field) or 0) + stats[field]
        completed[run_key] = {"completed_at": now_iso(), "stats": stats, "shard": shard}
        state["completed_shards"] = completed
        state["totals"] = totals
        save_json_atomic(STATE_FILE, state)
        print(f"[SAVE] totals={totals} completed={len(completed)}/{len(accepted)}", flush=True)

    for shard in accepted:
        if isinstance(shard, dict):
            run_stream_shard(shard, "RESUME_SHARD_STREAM")

    while queue:
        params = queue.pop(0)
        key = shard_key(params)
        if key in seen_keys:
            state["discovery_queue"] = queue
            save_json_atomic(STATE_FILE, state)
            continue

        probes += 1
        data = request_json({**params, "page": 0, "size": 1})
        total = total_from(data)
        seen_keys.add(key)
        print(f"[DISCOVER] total={total} params={params}", flush=True)

        if total == 0:
            pass
        elif total <= MAX_SOURCE_ROWS_PER_SHARD:
            shard = {**params, "_source_total": total}
            if key not in accepted_keys:
                accepted.append(shard)
                accepted_keys.add(key)
            accepted_this_run += 1
            state["accepted_shards"] = accepted
            state["discovery_seen_keys"] = sorted(seen_keys)
            state["discovery_queue"] = queue
            state["last_accepted_shard_at"] = now_iso()
            save_json_atomic(STATE_FILE, state)
            run_stream_shard(shard, "RUN_SHARD_STREAM")
        else:
            children, did_split = split_shard(params, total, year_min, year_max)
            if did_split:
                queue.extend(children)
            else:
                unresolved = {**params, "_source_total": total, "_reason": "over_cap_after_all_splits"}
                append_jsonl(UNRESOLVED_FILE, unresolved)
                unresolved_this_run += 1
                totals["unresolved"] = int(totals.get("unresolved") or 0) + 1
                print(f"[UNRESOLVED] total={total} params={params}", flush=True)

        state["discovery_queue"] = queue
        state["discovery_seen_keys"] = sorted(seen_keys)
        state["unresolved_count"] = unresolved_base + unresolved_this_run
        state["last_discovery_probe_at"] = now_iso()
        save_json_atomic(STATE_FILE, state)

        if max_discovery_probes and probes >= max_discovery_probes:
            print(
                f"[DISCOVER_LIMIT] probes={probes} accepted_this_run={accepted_this_run} queued={len(queue)}",
                flush=True,
            )
            break
        if max_shards and accepted_this_run >= max_shards:
            print(f"[SHARD_LIMIT] accepted_this_run={accepted_this_run} queued={len(queue)}", flush=True)
            break
        time.sleep(sleep_seconds)

    return accepted_this_run, unresolved_this_run


def scrape_shard(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    shard: dict[str, Any],
    sleep_seconds: float,
    max_pages_per_shard: int | None,
    rows_per_chunk: int,
) -> dict[str, int]:
    params = {k: v for k, v in shard.items() if not str(k).startswith("_")}
    stats = {"seen": 0, "written": 0, "dupe": 0, "rejected": 0, "pages": 0}
    max_pages = min(SOURCE_PAGE_CAP, max_pages_per_shard or SOURCE_PAGE_CAP)
    for page in range(max_pages):
        data = request_json({**params, "page": page, "size": PAGE_SIZE})
        records = records_from(data)
        if not records:
            break
        page_written = page_dupes = page_rejected = 0
        for record in records:
            stats["seen"] += 1
            row = normalize_record(record, params)
            if not row:
                stats["rejected"] += 1
                page_rejected += 1
                continue
            keys = dedupe_keys(row)
            if key_exists(conn, keys):
                stats["dupe"] += 1
                page_dupes += 1
                continue
            path = maybe_rotate_chunk(state, rows_per_chunk)
            append_jsonl(path, row)
            add_keys(conn, keys, f"{path.name}:{row['comp_id']}")
            stats["written"] += 1
            page_written += 1
        stats["pages"] += 1
        conn.commit()
        print(
            f"[PAGE] page={page} got={len(records)} written={page_written} "
            f"dupe={page_dupes} rejected={page_rejected} params={params}",
            flush=True,
        )
        time.sleep(sleep_seconds + random.uniform(0.0, sleep_seconds))
    return stats


def base_shards(categories: list[str], auction_types: list[str], sort: str) -> list[dict[str, Any]]:
    return [
        {"sort": sort, "category": category, "auctionTypes": auction_type}
        for category in categories
        for auction_type in auction_types
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep", type=float, default=5.0)
    parser.add_argument("--sort", default="soldDate,desc")
    parser.add_argument("--year-min", type=int, default=1900)
    parser.add_argument("--year-max", type=int, default=datetime.now().year + 1)
    parser.add_argument("--category", action="append", choices=CATEGORIES)
    parser.add_argument("--auction-types", action="append", choices=AUCTION_TYPES)
    parser.add_argument("--max-shards", type=int)
    parser.add_argument("--max-pages-per-shard", type=int)
    parser.add_argument("--max-discovery-probes", type=int)
    parser.add_argument("--rows-per-chunk", type=int, default=10000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed-limit", type=int)
    parser.add_argument(
        "--legacy-plan-first",
        action="store_true",
        help="preserve the original behavior of discovering all shards before scraping",
    )
    args = parser.parse_args()

    ensure_dirs()
    conn = init_db()
    seeded = seed_index(conn, args.seed_limit)
    state = load_state()
    print(f"[START] out_dir={OUT_DIR} seeded={seeded} dry_run={args.dry_run}", flush=True)

    categories = args.category or CATEGORIES
    auction_types = args.auction_types or AUCTION_TYPES

    if args.legacy_plan_first:
        accepted, unresolved = discover_shards(
            base_shards(categories, auction_types, args.sort),
            args.year_min,
            args.year_max,
            args.sleep,
            args.max_discovery_probes,
        )
        if args.max_shards:
            accepted = accepted[: args.max_shards]
        state["accepted_shards"] = accepted
        state["unresolved_count"] = len(unresolved)
        save_json_atomic(STATE_FILE, state)
        print(f"[SHARDS] accepted={len(accepted)} unresolved={len(unresolved)}", flush=True)
        if args.dry_run:
            return 0

        completed = state.setdefault("completed_shards", {})
        totals = state.setdefault("totals", {"seen": 0, "written": 0, "dupe": 0, "rejected": 0, "unresolved": 0})
        for idx, shard in enumerate(accepted, 1):
            key = shard_key({k: v for k, v in shard.items() if not str(k).startswith("_")})
            if completed.get(key):
                continue
            print(f"[RUN_SHARD] {idx}/{len(accepted)} source_total={shard.get('_source_total')} {shard}", flush=True)
            stats = scrape_shard(conn, state, shard, args.sleep, args.max_pages_per_shard, args.rows_per_chunk)
            for field in ("seen", "written", "dupe", "rejected"):
                totals[field] = int(totals.get(field) or 0) + stats[field]
            completed[key] = {"completed_at": now_iso(), "stats": stats, "shard": shard}
            save_json_atomic(STATE_FILE, state)
            print(f"[SAVE] totals={totals} completed={len(completed)}/{len(accepted)}", flush=True)
        totals["unresolved"] = int(totals.get("unresolved") or 0) + len(unresolved)
    else:
        accepted_count, unresolved_count = streaming_discover_and_scrape(
            conn,
            state,
            base_shards(categories, auction_types, args.sort),
            args.year_min,
            args.year_max,
            args.sleep,
            args.max_discovery_probes,
            args.max_shards,
            args.dry_run,
            args.max_pages_per_shard,
            args.rows_per_chunk,
        )
        print(
            f"[STREAMING_DONE] accepted_this_run={accepted_count} unresolved_this_run={unresolved_count} "
            f"queued={len(state.get('discovery_queue') or [])}",
            flush=True,
        )
        totals = state.setdefault("totals", {"seen": 0, "written": 0, "dupe": 0, "rejected": 0, "unresolved": 0})

    state["completed"] = True
    state["completed_at"] = now_iso()
    save_json_atomic(STATE_FILE, state)
    unresolved_total = int(state.get("unresolved_count") or 0)
    print(f"[DONE] totals={totals} chunks={len(list(CHUNK_DIR.glob('*.jsonl')))} unresolved={unresolved_total}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
