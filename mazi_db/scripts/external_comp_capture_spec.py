"""Shared capture spec helpers for external comp imports.

Additive only: these helpers never delete or promote rows. They persist listing
photos to the Mini evidence volume and add optional metadata columns used by the
external comps matcher/display path.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SPEC_VERSION = "external_comp_capture_v2_20260612"
DEFAULT_IMAGE_ROOT = Path(os.environ.get("MAZI_COMP_IMAGES_ROOT", "/Volumes/MAZI_EVIDENCE_6TB/comp_images"))
USER_AGENT = os.environ.get(
    "MAZI_COMP_IMAGE_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
)
DETAIL_KEYS = {
    "player": ("player", "player/athlete", "athlete", "subject"),
    "year": ("year", "season"),
    "set_name": ("set", "card set", "product"),
    "parallel": ("parallel", "parallel/variety", "variety"),
    "card_number": ("card number", "card no", "card #", "number"),
    "grade": ("grade", "professional grader grade", "card grade"),
    "grading_company": ("grader", "professional grader", "grading company"),
    "cert_number": ("certification number", "cert number", "certification"),
}


CAPTURE_COLUMNS = (
    ("sale_format", "text"), ("bid_count", "integer"), ("seller_id", "text"), ("shipping", "text"), ("player", "text"),
    ("year", "text"), ("set_name", "text"), ("parallel", "text"), ("card_number", "text"), ("grade", "text"),
    ("grading_company", "text"), ("cert_number", "text"), ("item_specifics_raw", "jsonb"), ("local_image_paths", "jsonb"),
    ("image_sha256_paths", "jsonb"), ("image_count", "integer"), ("scraper_version", "text"),
)


def ensure_external_transactions_capture_columns(cur: Any) -> None:
    """Install optional capture-spec columns on external_transactions -- only the ones actually missing.

    ALTER TABLE takes an ACCESS EXCLUSIVE lock even when every column already exists (ADD COLUMN IF NOT EXISTS still
    locks). external_transactions is written all day (SCP bridge, eBay importers), so the ALTER could not get its lock
    inside the callers' 10 s lock_timeout and the whole Fanatics import failed (2026-09-29 05:27 LockNotAvailable: the
    $1K+ backfill, incl. the $8.04M Flagg, never landed). Reading the catalog takes no table lock.
    """
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'external_transactions'")
    have = {r["column_name"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}
    missing = [(c, t) for c, t in CAPTURE_COLUMNS if c not in have]
    if missing:
        cur.execute("ALTER TABLE public.external_transactions "
                    + ", ".join(f"ADD COLUMN IF NOT EXISTS {c} {t}" for c, t in missing))


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def normalize_item_id(item_id: Any, url: str | None = None) -> str:
    text = str(item_id or "").strip()
    if text:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    if url:
        match = re.search(r"/itm/(?:[^/?#]+/)?(\d+)|[?&]item=(\d+)", url)
        if match:
            return match.group(1) or match.group(2)
    return hashlib.sha256(str(url or time.time()).encode("utf-8")).hexdigest()[:24]


def collect_image_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("image_urls", "images", "photos", "photo_urls", "gallery_urls"):
        value = payload.get(key)
        if isinstance(value, list):
            urls.extend(str(v) for v in value if v)
        elif isinstance(value, str) and value.strip():
            urls.append(value)
    for key in ("image_url", "imgUrl", "img_url", "thumbnail", "thumbnail_url"):
        value = payload.get(key)
        if value:
            urls.append(str(value))
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        cleaned = url.strip()
        if not cleaned or cleaned.startswith("data:"):
            continue
        cleaned = re.sub(r"/s-l\\d+\\.", "/s-l1600.", cleaned)
        if cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def item_specifics_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("item_specifics", "specifics", "itemSpecifics", "details"):
        value = payload.get(key)
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items() if v not in (None, "")}
    return {}


def promoted_identity_fields(payload: dict[str, Any], specifics: dict[str, Any]) -> dict[str, Any]:
    lowered = {str(k).strip().lower(): v for k, v in specifics.items()}
    out: dict[str, Any] = {}
    for column, aliases in DETAIL_KEYS.items():
        value = None
        for alias in aliases:
            if alias in lowered:
                value = lowered[alias]
                break
        out[column] = _first_text(payload.get(column), payload.get(column.replace("_name", "")), value)
    if not out.get("player"):
        out["player"] = _first_text(payload.get("player_query"), payload.get("query_label"), payload.get("source_query"))
    return out


def parse_bid_count(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"(\d[\d,]*)\s+bid", text, re.I)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def sale_format_from_payload(payload: dict[str, Any]) -> str | None:
    if payload.get("best_offer") or payload.get("bestOffer"):
        return "OBO"
    explicit = _first_text(payload.get("sale_format"), payload.get("format"), payload.get("listing_type"))
    if explicit:
        return explicit
    if parse_bid_count(payload.get("bids")):
        return "auction"
    return "BIN"


def capture_fields(payload: dict[str, Any], source_code: str, item_id: Any, url: str | None = None, download: bool = True) -> dict[str, Any]:
    specifics = item_specifics_from_payload(payload)
    identity = promoted_identity_fields(payload, specifics)
    image_urls = collect_image_urls(payload)
    local_paths: list[str] = []
    sha_paths: list[str] = []
    if download and image_urls:
        local_paths, sha_paths = persist_images(source_code, normalize_item_id(item_id, url), image_urls)
    bid_count = parse_bid_count(payload.get("bids") or payload.get("bid_count"))
    return {
        **identity,
        "sale_format": sale_format_from_payload(payload),
        "bid_count": bid_count,
        "seller_id": _first_text(payload.get("seller_id"), payload.get("seller"), payload.get("sellerName")),
        "shipping": _first_text(payload.get("shipping"), payload.get("shipping_cost"), payload.get("shippingText")),
        "item_specifics_raw": specifics,
        "local_image_paths": local_paths,
        "image_sha256_paths": sha_paths,
        "image_count": len(local_paths) if local_paths else len(image_urls),
        "scraper_version": SPEC_VERSION,
        "image_urls": image_urls,
    }


def spec_raw_patch(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "_mazi_capture_spec": {
            "version": SPEC_VERSION,
            "image_urls": fields.get("image_urls") or [],
            "local_image_paths": fields.get("local_image_paths") or [],
            "image_sha256_paths": fields.get("image_sha256_paths") or [],
            "item_specifics_raw": fields.get("item_specifics_raw") or {},
            "sale_format": fields.get("sale_format"),
            "bid_count": fields.get("bid_count"),
            "seller_id": fields.get("seller_id"),
            "shipping": fields.get("shipping"),
        }
    }


def persist_images(source_code: str, item_id: str, urls: list[str], root: Path | None = None, throttle_seconds: float = 0.15) -> tuple[list[str], list[str]]:
    root = root or DEFAULT_IMAGE_ROOT
    item_dir = root / source_code / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []
    sha_paths: list[str] = []
    for idx, url in enumerate(urls, start=1):
        try:
            suffix = Path(urlparse(url).path).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                suffix = mimetypes.guess_extension("image/jpeg") or ".jpg"
            target = item_dir / f"{idx:02d}{suffix}"
            sha_target = target.with_suffix(target.suffix + ".sha256")
            if target.exists() and sha_target.exists():
                local_paths.append(str(target))
                sha_paths.append(str(sha_target))
                continue
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=20) as resp:
                data = resp.read()
            if len(data) < 128:
                continue
            digest = hashlib.sha256(data).hexdigest()
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            sha_target.write_text(f"{digest}  {target.name}\\n", encoding="utf-8")
            local_paths.append(str(target))
            sha_paths.append(str(sha_target))
            time.sleep(throttle_seconds)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            continue
    return local_paths, sha_paths


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
