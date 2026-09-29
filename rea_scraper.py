#!/usr/bin/env python3
"""
rea_scraper.py — Robert Edward Auctions (REA) realized-price comps
==================================================================
REA's public archive (collectrea.com/archives) is server-rendered plain HTML
(robots.txt permissive, low anti-bot) listing SOLD lots with realized prices.
Each lot: /archives/{year}/{season}/{lot}/{slug} + "Lot N - $<realized>". The
listing is price-descending (top lots first); ?page=N paginates. This scraper
pages the archive, parses each lot (url/title/price/auction-season), dedups by
lot URL, and lands new ones. sold_date is season-granular (REA lists auctions by
year+season, not exact day) — mapped to an approximate mid-season date.

High-end vintage comps; reference-only. source_code 'rea'. Output rea_comps.json.

Usage:
  python3 rea_scraper.py --dry-run
  python3 rea_scraper.py --pages 40
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(_HERE, "rea_comps.json")
STATE_FILE = os.path.join(_HERE, "rea_state.json")

BASE = "https://collectrea.com/archives"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_LOT_RE = re.compile(r'href="(/archives/(\d{4})/([^/"]+)/(\d+)/([^"]+))"')
_IMG_RE = re.compile(r'(?:src|data-src)="(https?://[^"]*(?:rea-image|digitaloceanspaces)[^"]*)"')
_PRICE_RE = re.compile(r"\$[\d,]{3,}")
# approximate mid-season day (REA gives year+season granularity, not exact date)
_SEASON_MD = {"winter": "01-15", "spring": "05-01", "summer": "08-01",
              "fall": "11-01", "marketplace": "07-01", "encore": "07-01"}


# ── pure core (unit-tested in test_rea.py) ───────────────────────────────────
def deslug(slug):
    """URL slug -> readable title. 'extremely-rare-1914-...' -> 'Extremely Rare
    1914 ...'. Keeps it deterministic (the rendered title is mixed with category
    + lot markup; the slug is the clean canonical title)."""
    words = (slug or "").split("-")
    return " ".join(w.upper() if w and w.isdigit() is False and len(w) <= 3
                    and w.isalpha() and w.isupper() else w.capitalize()
                    for w in words if w).strip()


def season_to_date(year, season):
    """(year, season) -> approximate 'YYYY-MM-DD' mid-season sale date, or '' if
    the year is unparseable. Season is case-insensitive; unknown -> mid-year."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return ""
    md = _SEASON_MD.get((season or "").strip().lower(), "07-01")
    return "%04d-%s" % (y, md)


def parse_rea_listing(html):
    """Parse one archive listing page -> list of dicts
    {url, title, sold_price, year, season, lot, sold_date}. Price is the
    realized '$' amount in each lot's segment (links + prices are 1:1, in order).
    Rows without a parseable price are dropped."""
    out = []
    matches = list(_LOT_RE.finditer(html or ""))
    for i, m in enumerate(matches):
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else m.end() + 1200
        seg = html[m.start():seg_end]
        pm = _PRICE_RE.search(seg)
        if not pm:
            continue
        try:
            price = float(pm.group(0).replace("$", "").replace(",", ""))
        except ValueError:
            continue
        if price <= 0:
            continue
        year, season, lot, slug = m.group(2), m.group(3), m.group(4), m.group(5)
        im = _IMG_RE.search(seg)
        out.append({"url": "https://collectrea.com" + m.group(1),
                    "title": deslug(slug), "sold_price": price, "year": year,
                    "season": season, "lot": lot,
                    "image_url": im.group(1) if im else "",
                    "sold_date": season_to_date(year, season)})
    return out


# ── io / driver ──────────────────────────────────────────────────────────────
def _fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"seen_urls": [], "next_id": 1}


def save_state(s):
    tmp = STATE_FILE + ".tmp"; json.dump(s, open(tmp, "w")); os.replace(tmp, STATE_FILE)


def load_comps():
    if os.path.exists(OUT_FILE):
        try:
            return json.load(open(OUT_FILE))
        except Exception:
            pass
    return []


def save_comps(c):
    tmp = OUT_FILE + ".tmp"; json.dump(c, open(tmp, "w"), indent=1); os.replace(tmp, OUT_FILE)


def run(args):
    state = load_state()
    comps = load_comps()
    seen = set(state.get("seen_urls") or [])
    next_id = int(state.get("next_id") or 1)
    new = 0
    empty_streak = 0
    for pg in range(1, args.pages + 1):
        url = BASE if pg == 1 else "%s?page=%d" % (BASE, pg)
        try:
            lots = parse_rea_listing(_fetch(url))
        except Exception as e:
            print("  [page %d] fetch/parse error: %s" % (pg, str(e)[:70])); break
        if not lots:
            empty_streak += 1
            print("  [page %d] 0 lots" % pg)
            if empty_streak >= 2:
                print("  two empty pages — end of archive"); break
            continue
        empty_streak = 0
        page_new = 0
        for lot in lots:
            if lot["url"] in seen:
                continue
            seen.add(lot["url"])
            if args.dry_run:
                page_new += 1; continue
            lot["comp_id"] = "REA-%d" % next_id
            lot["source"] = "rea"
            comps.append(lot); next_id += 1; new += 1; page_new += 1
        print("  [page %d] %d lots, +%d new" % (pg, len(lots), page_new))
        if not args.dry_run and pg % 10 == 0:
            state["seen_urls"] = sorted(seen)[-40000:]; state["next_id"] = next_id
            save_comps(comps); save_state(state)
        time.sleep(args.sleep)
    if args.dry_run:
        print("[dry-run] would add %d new lots" % new if new else "[dry-run] (counts above)")
        return 0
    state["seen_urls"] = sorted(seen)[-40000:]; state["next_id"] = next_id
    save_comps(comps); save_state(state)
    print("\n[done] +%d new REA comps (total %d)" % (new, len(comps)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=40, help="archive listing pages (12 lots/page)")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
