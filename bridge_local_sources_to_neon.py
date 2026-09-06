#!/usr/bin/env python3
"""
bridge_local_sources_to_neon.py — land local-JSON comp sources into Neon (Tier-3)
==================================================================================
Bridges the new local sources (goldin v2, tcgplayer, myslabs, auctionreport) into
external_transactions. Idempotent: INSERT ... ON CONFLICT DO NOTHING (no target)
so it skips ANY of the 3 unique indexes (source_item / canonical_url / source+
title+date+price). Ensures each comp_sources registry row. Per-source mappers.

Design decisions (documented for audit):
- goldin v2 current_price = HAMMER → realized = hammer x (1 + buyer_premium/100).
- canonical_source_url left NULL (tcgplayer/AR urls repeat per product/post and
  would false-collide on the url unique index); item url goes in source_url.
- verified_price_eligible: TRUE for clean per-item realized sold data
  (goldin/tcgplayer/myslabs, matching fanatics/scp convention); FALSE for
  auctionreport (real realized price but prose-fuzzy card identity — reference).
- All reference-only; never Trusted/Mazified.

Usage:
  python3 bridge_local_sources_to_neon.py --source myslabs --dry-run
  python3 bridge_local_sources_to_neon.py --source myslabs --commit --yes
  python3 bridge_local_sources_to_neon.py --source all --commit --yes
"""
import argparse
import json
import os
import re
import sys

_MYSLABS_ID = re.compile(r"/slab/view/\d+")
# a card-like title carries a year, a grade, or a #number — used to drop
# AuctionReport prose-noise headlines (e.g. "Florida Contractor Bought Cards...")
_CARDISH = re.compile(r"\b(19|20)\d{2}\b|\b(PSA|BGS|SGC|CGC|BVG|BCCG)\b|#\w", re.I)

ROOT = os.path.expanduser("~/whatnot-sniper")
sys.path.insert(0, ROOT)
from mazi_db.scripts import import_ebay_scrub_store_to_neon as imp  # dsn_for_target
import psycopg


def _norm(t):
    return " ".join((t or "").lower().split())


# ── comp_sources registry entries ───────────────────────────────────────────
SOURCES = {
    "goldin": {  # already registered; refreshed for completeness
        "file": "goldin_comps_v2.json", "source_name": "Goldin",
        "source_type": "auction_house", "supports_sold": True,
        "supports_active": False, "obo_policy": "n/a", "vpe": True},
    "tcgplayer": {
        "file": "tcgplayer_sold_comps.json", "source_name": "TCGplayer",
        "source_type": "marketplace", "supports_sold": True,
        "supports_active": True, "obo_policy": "n/a", "vpe": True},
    "myslabs": {
        "file": "myslabs_comps.json", "source_name": "MySlabs",
        "source_type": "marketplace", "supports_sold": True,
        "supports_active": False, "obo_policy": "n/a", "vpe": True},
    "auctionreport": {
        "file": "auctionreport_comps.json", "source_name": "Auction Report",
        "source_type": "aggregator", "supports_sold": True,
        "supports_active": False, "obo_policy": "n/a", "vpe": False},
    "rea": {
        "file": "rea_comps.json", "source_name": "Robert Edward Auctions",
        "source_type": "auction_house", "supports_sold": True,
        "supports_active": False, "obo_policy": "n/a", "vpe": True},
}


# ── per-source row mappers: local comp dict -> external_transactions row ──────
def map_goldin(c):
    hammer = c.get("sold_price")
    bp = c.get("buyer_premium_pct")
    if hammer is None:
        return None
    try:
        hammer = float(hammer)
        bp = float(bp) if bp is not None else 0.0
    except (TypeError, ValueError):
        return None
    realized = round(hammer * (1 + bp / 100.0), 2)
    if realized <= 0 or not c.get("sold_date"):
        return None
    return {"source_item_id": c.get("lot_id") or c.get("comp_id"),
            "title": c.get("title", ""), "sold_price": realized,
            "sold_date": c["sold_date"], "source_url": c.get("url"),
            "grade": None, "set_name": None, "card_number": None,
            "scraped_at": c.get("scraped_at"),
            "raw_extra": {"_mazi_price_basis": {"basis": "hammer_plus_bp",
                          "hammer": hammer, "buyer_premium_pct": bp, "realized": realized}}}


def map_tcgplayer(c):
    price = c.get("sold_price")
    if price is None or not c.get("sold_date"):
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    sid = "%s:%s" % (c.get("product_id"), c.get("order_ts") or c.get("sold_date"))
    return {"source_item_id": sid, "title": c.get("title", ""), "sold_price": price,
            "sold_date": c["sold_date"], "source_url": c.get("url"),
            "grade": None, "set_name": c.get("set_name"), "card_number": c.get("number"),
            "scraped_at": None,  # tcgplayer rows carry no scrape ts; INSERT COALESCEs to now() (2026-09-04 fix: NULL scraped_at made this source invisible to the added-last-24h freshness metric)
            "raw_extra": {"condition": c.get("condition"), "variant": c.get("variant"),
                          "rarity": c.get("rarity"), "sold_basis": "per_order_realized"}}


def map_myslabs(c):
    price = c.get("sold_price")
    url = c.get("url") or ""
    if price is None or not c.get("sold_date") or not _MYSLABS_ID.search(url):
        return None  # skip malformed/idless slab links (e.g. /slab/view//)
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return {"source_item_id": c["url"], "title": c.get("title", ""), "sold_price": price,
            "sold_date": c["sold_date"], "source_url": c.get("url"),
            "grade": None, "set_name": None, "card_number": None,
            "scraped_at": c.get("scraped_at"),
            "raw_extra": {"shipping": c.get("shipping"), "base": c.get("sold_price_base")}}


def map_auctionreport(c):
    price = c.get("sold_price")
    if price is None or not c.get("sold_date"):
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    title = c.get("title", "")
    if not _CARDISH.search(title):
        return None  # drop prose-noise headlines (keep only card-like lots)
    sid = "%s#%s#%s" % (c.get("post_url", ""), price, _norm(title)[:40])
    return {"source_item_id": sid, "title": title, "sold_price": price,
            "sold_date": c["sold_date"], "source_url": c.get("post_url"),
            "grade": None, "set_name": None, "card_number": None,
            "scraped_at": c.get("scraped_at"),
            "raw_extra": {"auction_house": c.get("auction_house"),
                          "capture_tier": "top_lots_reference"}}


def map_rea(c):
    price = c.get("sold_price")
    if price is None or not c.get("sold_date") or not c.get("url"):
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return {"source_item_id": c["url"], "title": c.get("title", ""), "sold_price": price,
            "sold_date": c["sold_date"], "source_url": c["url"],
            "grade": None, "set_name": None, "card_number": None, "scraped_at": None,
            "raw_extra": {"year": c.get("year"), "season": c.get("season"),
                          "lot": c.get("lot"), "sold_date_granularity": "season"}}


MAPPERS = {"goldin": map_goldin, "tcgplayer": map_tcgplayer,
           "myslabs": map_myslabs, "auctionreport": map_auctionreport, "rea": map_rea}

# verified_price_eligible is a GENERATED column (DB computes it: non-ebay sold
# rows resolve True) — do NOT insert it.
INSERT_SQL = """
INSERT INTO external_transactions
  (source_code, source_item_id, source_url, title, normalized_title,
   sold_price, sold_date, currency, best_offer, sold_status, duplicate_status,
   grade, set_name, card_number, scraped_at, raw)
VALUES
  (%(source_code)s, %(source_item_id)s, %(source_url)s, %(title)s, %(normalized_title)s,
   %(sold_price)s, %(sold_date)s, 'USD', false, 'sold', 'canonical',
   %(grade)s, %(set_name)s, %(card_number)s, COALESCE(%(scraped_at)s, now()), %(raw)s)
ON CONFLICT DO NOTHING
"""


def bridge_source(cur, code, commit):
    cfg = SOURCES[code]
    path = os.path.join(ROOT, cfg["file"])
    comps = json.load(open(path))
    mapper = MAPPERS[code]
    rows, skipped_map = [], 0
    for c in comps:
        m = mapper(c)
        if not m or not m.get("title") or not m.get("source_item_id"):
            skipped_map += 1
            continue
        raw = dict(c)
        raw.update(m.pop("raw_extra", {}) or {})
        rows.append({
            "source_code": code, "source_item_id": str(m["source_item_id"]),
            "source_url": m.get("source_url"), "title": m["title"],
            "normalized_title": _norm(m["title"]), "sold_price": m["sold_price"],
            "sold_date": m["sold_date"], "vpe": cfg["vpe"], "grade": m.get("grade"),
            "set_name": m.get("set_name"), "card_number": m.get("card_number"),
            "scraped_at": m.get("scraped_at"), "raw": json.dumps(raw, default=str)})
    print("  [%s] %d local comps -> %d mappable (%d unmappable), vpe=%s" % (
        code, len(comps), len(rows), skipped_map, cfg["vpe"]))
    if rows[:2]:
        for r in rows[:2]:
            print("    e.g. %s | $%s | %s | sid=%s" % (
                r["sold_date"], r["sold_price"], r["title"][:44], r["source_item_id"][:40]))
    if not commit:
        return 0
    # ensure comp_sources row
    cur.execute("""INSERT INTO comp_sources
        (source_code, source_name, source_type, supports_sold, supports_active, obo_policy, active)
        VALUES (%s,%s,%s,%s,%s,%s,true) ON CONFLICT (source_code) DO NOTHING""",
        (code, cfg["source_name"], cfg["source_type"], cfg["supports_sold"],
         cfg["supports_active"], cfg["obo_policy"]))
    # Pre-load existing source_item_ids for THIS source (indexed, ~0.1s) so we skip
    # already-landed rows instead of paying a network round-trip to discover the
    # ON CONFLICT. This is what let goldin's 40k landed rows eat the whole timeout.
    cur.execute(
        "select source_item_id from external_transactions where source_code=%s", (code,))
    have = {r[0] for r in cur.fetchall()}
    fresh = [r for r in rows if r["source_item_id"] not in have]
    print("  [%s] %d local rows, %d already in Neon -> %d to insert"
          % (code, len(rows), len(rows) - len(fresh), len(fresh)), flush=True)

    # executemany batches get pipelined by psycopg3: ~100 round-trips instead of 90k.
    inserted = 0
    BATCH = 500
    for i in range(0, len(fresh), BATCH):
        chunk = fresh[i:i + BATCH]
        cur.executemany(INSERT_SQL, chunk)
        # rowcount after executemany = rows ACTUALLY inserted; ON CONFLICT drops
        # (from the other two unique indexes) must not be counted as landed.
        rc = cur.rowcount
        inserted += rc if rc is not None and rc >= 0 else len(chunk)
    return inserted


def refresh_images(cur, code):
    """Backfill raw.image_url for existing rows from the (re-scraped) local file,
    for sources whose scraper newly captures an image. Merges image_url into raw
    (|| jsonb) without disturbing other fields; only touches rows lacking one."""
    cfg = SOURCES[code]
    comps = json.load(open(os.path.join(ROOT, cfg["file"])))
    mapper = MAPPERS[code]
    updated = 0
    for c in comps:
        img = (c.get("image_url") or "").strip()
        if not img.startswith(("http://", "https://")):
            continue
        m = mapper(c)
        if not m or not m.get("source_item_id"):
            continue
        cur.execute(
            "update external_transactions "
            "set raw = raw || jsonb_build_object('image_url', %s::text), updated_at = now() "
            "where source_code=%s and source_item_id=%s and coalesce(raw->>'image_url','')=''",
            (img, code, str(m["source_item_id"])))
        updated += cur.rowcount
    print("  [%s] image-backfilled %d rows" % (code, updated))
    return updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    choices=list(SOURCES) + ["all"])
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--refresh-images", action="store_true",
                    help="backfill raw.image_url from re-scraped local file (no inserts)")
    a = ap.parse_args()
    codes = list(SOURCES) if a.source == "all" else [a.source]
    if a.commit and not a.yes:
        sys.exit("refusing to commit without --yes")

    dsn = imp.dsn_for_target("prod")
    conn = psycopg.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    print("=== BRIDGE %s (commit=%s) ===" % (codes, a.commit))
    before = {}
    for code in codes:
        cur.execute("select count(*) from external_transactions where source_code=%s", (code,))
        before[code] = cur.fetchone()[0]
    total_ins = 0
    for code in codes:
        if a.refresh_images:
            if a.commit:
                total_ins += refresh_images(cur, code)
            continue
        ins = bridge_source(cur, code, a.commit)
        total_ins += ins
        if a.commit:
            conn.commit()   # per-source commit: a kill can no longer roll back everything
            print("  [%s] inserted %d (was %d) [committed]" % (code, ins, before[code]))
    if a.commit:
        conn.commit()
    conn.close()

    if a.commit:
        # verify from a FRESH connection (psycopg savepoint/scoping safety)
        v = psycopg.connect(dsn); vc = v.cursor()
        print("\n=== VERIFY (fresh connection) ===")
        for code in codes:
            vc.execute("select count(*),min(sold_date),max(sold_date) from external_transactions where source_code=%s", (code,))
            n, mn, mx = vc.fetchone()
            print("  %-14s %d rows (delta +%d)  span %s -> %s" % (code, n, n - before[code], mn, mx))
        vc.execute("select count(*) from external_transactions")
        print("  NEON TOTAL:", vc.fetchone()[0])
        v.close()
    else:
        print("\n[dry-run] no writes. total mappable would attempt:", total_ins)


if __name__ == "__main__":
    main()
