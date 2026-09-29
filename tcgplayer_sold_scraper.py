#!/usr/bin/env python3
"""
tcgplayer_sold_scraper.py — TCGplayer per-order SOLD comps (TCG singles)
=========================================================================
TCGplayer product pages expose real per-order sales via an internal API
(mpapi latestsales), unauthenticated, newest-first. This poller builds a bounded
watchlist of card-single productIds from the free tcgcsv.com daily catalog dumps
(Pokemon = category 3), then pulls each product's latest sales, keeping only rows
past a per-product orderDate watermark. Real realized prices with dates.

Bounds/ethics: TCGplayer is eBay-owned — keep volume LOW (bounded groups, gentle
sleep, stop at watermark). Reference-only comps (never identity/proof). NOTE: the
repo's OLD tcgplayer_scraper.py mapped LISTED price into sold_price — this writes
a SEPARATE file (tcgplayer_sold_comps.json) with genuine SOLD orders; never mix.

Usage:
  python3 tcgplayer_sold_scraper.py --dry-run --groups 3
  python3 tcgplayer_sold_scraper.py --groups 5 --max-products 150
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(_HERE, "tcgplayer_sold_comps.json")
STATE_FILE = os.path.join(_HERE, "tcgplayer_sold_state.json")

CAT_POKEMON = 3
TCGCSV = "https://tcgcsv.com/tcgplayer"
MPAPI = "https://mpapi.tcgplayer.com/v2/product/{pid}/latestsales"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# sealed / non-single product markers (exclude — we want card singles)
SEALED_RE = re.compile(
    r"\b(booster|blister|box|pack|bundle|case|tin|collection|deck|"
    r"elite trainer|etb|build ?& ?battle|display|carton|sleeve|portfolio|"
    r"binder|figure|pin|playmat|premium collection|mini tin|3 pack|"
    r"poster|promo pack)\b", re.IGNORECASE)


# ── pure core (unit-tested in test_tcgplayer_sold.py) ────────────────────────
def is_single_card(product):
    """True if a tcgcsv product is a card SINGLE (not sealed). Excludes sealed
    keywords in the name; requires a card Number in extendedData (cards carry a
    collector number, sealed products don't)."""
    name = (product or {}).get("name", "") or ""
    if SEALED_RE.search(name):
        return False
    ext = {e.get("name"): e.get("value")
           for e in (product.get("extendedData") or []) if isinstance(e, dict)}
    return bool(ext.get("Number"))


def sale_to_comp(sale, product, next_id):
    """Map one latestsales order + its product to a comp row. Returns None for
    rows missing a usable date or price. sold_price includes shipping (realized
    all-in), with the base kept alongside for audit."""
    od = (sale or {}).get("orderDate")
    pp = sale.get("purchasePrice")
    if not od or pp is None:
        return None
    try:
        price = float(pp)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    ship = 0.0
    try:
        ship = float(sale.get("shippingPrice") or 0)
    except (TypeError, ValueError):
        ship = 0.0
    sold_date = od[:10]  # YYYY-MM-DD from ISO8601
    ext = {e.get("name"): e.get("value")
           for e in (product.get("extendedData") or []) if isinstance(e, dict)}
    return {
        "comp_id": "TCG-%d" % next_id,
        "title": product.get("name", ""),
        "sold_price": round(price + ship, 2),
        "sold_price_base": price,
        "shipping": ship,
        "sold_date": sold_date,
        "order_ts": od,
        "condition": sale.get("condition", ""),
        "variant": sale.get("variant", ""),
        "quantity": sale.get("quantity", 1),
        "product_id": product.get("productId"),
        "set_name": product.get("group_name", ""),
        "number": ext.get("Number", ""),
        "rarity": ext.get("Rarity", ""),
        "url": product.get("url", ""),
        "source": "tcgplayer",
        "sold_basis": "per_order_realized",
        "category": "Pokemon",
    }


def newest_order_ts(sales):
    """Max orderDate string across a latestsales page (ISO8601 sorts lexically)."""
    ts = [s.get("orderDate") for s in (sales or []) if s.get("orderDate")]
    return max(ts) if ts else None


def new_sales_past(sales, watermark):
    """Keep only sales strictly newer than the stored per-product watermark
    (an orderDate string). watermark None ⇒ all. ISO8601 lexical compare."""
    if not watermark:
        return list(sales or [])
    return [s for s in (sales or []) if (s.get("orderDate") or "") > watermark]


# ── http ─────────────────────────────────────────────────────────────────────
def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url, body, timeout=30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json",
        "Origin": "https://www.tcgplayer.com", "Referer": "https://www.tcgplayer.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def latest_sales(pid, limit=25, offset=0):
    body = {"conditions": [], "languages": [], "variants": [],
            "listingType": "All", "limit": limit, "offset": offset}
    d = _post(MPAPI.format(pid=pid), body)
    return d.get("data") or [], d.get("nextPage")


# ── state / io ───────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"watermarks": {}, "next_id": 1}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE_FILE)


def load_comps():
    if os.path.exists(OUT_FILE):
        try:
            return json.load(open(OUT_FILE))
        except Exception:
            pass
    return []


def save_comps(c):
    tmp = OUT_FILE + ".tmp"
    json.dump(c, open(tmp, "w"), indent=1)
    os.replace(tmp, OUT_FILE)


def build_watchlist(n_groups, max_products):
    """Newest n_groups Pokemon sets → their card singles (bounded)."""
    groups = _get(f"{TCGCSV}/{CAT_POKEMON}/groups").get("results", [])
    groups = sorted(groups, key=lambda g: g.get("groupId", 0), reverse=True)[:n_groups]
    watch = []
    for g in groups:
        gid, gname = g.get("groupId"), g.get("name")
        prods = _get(f"{TCGCSV}/{CAT_POKEMON}/{gid}/products").get("results", [])
        for p in prods:
            if is_single_card(p):
                p["group_name"] = gname
                watch.append(p)
        if len(watch) >= max_products:
            break
    return watch[:max_products]


def run(args):
    state = load_state()
    comps = load_comps()
    wm = state.setdefault("watermarks", {})
    next_id = int(state.get("next_id") or 1)

    print("[catalog] newest %d Pokemon groups (cap %d singles)..." % (
        args.groups, args.max_products))
    watch = build_watchlist(args.groups, args.max_products)
    print("  watchlist: %d card singles" % len(watch))
    if args.dry_run:
        for p in watch[:10]:
            print("  would poll pid=%s %s (%s)" % (
                p.get("productId"), p.get("name"), p.get("group_name")))
        print("[dry-run] no sales fetched."); return 0

    total_new = 0
    for i, p in enumerate(watch):
        pid = str(p.get("productId"))
        try:
            sales, _ = latest_sales(pid)
        except Exception as e:
            print("  [skip pid=%s] %s" % (pid, str(e)[:70])); continue
        fresh = new_sales_past(sales, wm.get(pid))
        for s in fresh:
            row = sale_to_comp(s, p, next_id)
            if row:
                comps.append(row); next_id += 1; total_new += 1
        newest = newest_order_ts(sales)
        if newest:
            wm[pid] = newest
        if fresh:
            print("  +%d  %s" % (len(fresh), p.get("name", "")[:56]))
        if (i + 1) % 25 == 0:
            state["next_id"] = next_id; save_state(state); save_comps(comps)
        time.sleep(args.sleep)

    state["next_id"] = next_id
    save_state(state); save_comps(comps)
    print("\n[done] +%d new TCGplayer sold comps across %d products (total %d)" % (
        total_new, len(watch), len(comps)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=5, help="newest N Pokemon sets")
    ap.add_argument("--max-products", type=int, default=200)
    ap.add_argument("--sleep", type=float, default=0.6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
