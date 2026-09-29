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

The price-sorted /archives listing stops at ~100 pages (~1,200 lots, all $50K+), so --pages alone only ever
reached the top ~970 lots (2026-09-29). --by-auction walks each auction's own archive (/archives/{year}/{season}),
which ends naturally (2025 Spring: 84 pages, ~1,000 lots down to $2,400), for every year in the archive's
year selector. Multi-card lots, sets, artwork and memorabilia are skipped (is_single_card) unless --all-lots.

Usage:
  python3 rea_scraper.py --dry-run
  python3 rea_scraper.py --pages 40
  python3 rea_scraper.py --by-auction                      # full archive, every auction, singles only
  python3 rea_scraper.py --by-auction --since-year 2025    # recent auctions (the daily supervisor leg)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(_HERE, "rea_comps.json")
STATE_FILE = os.path.join(_HERE, "rea_state.json")

BASE = "https://collectrea.com/archives"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_LOT_RE = re.compile(r'href="(/archives/(\d{4})/([^/"]+)/(\d+)/([^"]+))"')
_IMG_RE = re.compile(r'(?:src|data-src)="(https?://[^"]*(?:rea-image|digitaloceanspaces)[^"]*)"')
_PRICE_RE = re.compile(r"\$[\d,]{3,}")
# Every card reads "Lot N - $X". Some cards put that text outside the link's own HTML segment, so the segment search
# missed one lot per page (~8% of each auction, 2026-09-29; the prices it did find were all right).
_LOT_PRICE_RE = re.compile(r"Lot\s+(\d+)\s*-\s*\$([\d,]{3,})")
# approximate mid-season day (REA gives year+season granularity, not exact date)
SEASONS = ("winter", "spring", "summer", "fall", "marketplace", "encore")
# Not a single card: collections, sets, runs, sealed product, artwork, photos, documents, game-used memorabilia.
_MULTI_RE = re.compile(
    r"\b(collections?|lots? of|group of|complete set|near(?:ly)? complete|partial set|sets? of|sets|run of|hoard|archive|"
    r"album|scrapbook|uncut|sheets?|packs?|box(?:es)?|cases?|wax|display|original art(?:work)?|artwork|painting|"
    r"photographs?|letters?|documents?|contracts?|checks?|bats?|jerseys?|uniforms?|balls?|gloves?|helmets?|trophy|"
    r"trophies|rings?|tickets?|programs?|pennants?|posters?|trio|pair|duo)\b|\(\d{2,}\)|\b\d{2,}\s+cards\b", re.I)
# A card: a year (1850-2099) or a vintage catalog code (T206, E90, N172, M101, R319, ...).
_CARDISH_RE = re.compile(r"\b(18[5-9]\d|19\d\d|20\d\d)\b|\b[TENMRDW]\d{2,3}\b", re.I)
_SEASON_MD = {"winter": "01-15", "spring": "05-01", "summer": "08-01",
              "fall": "11-01", "marketplace": "07-01", "encore": "07-01",
              # Huggins & Scott names many auctions by month (2007/March, 2022/November)
              "january": "01-15", "february": "02-15", "march": "03-15", "april": "04-15", "may": "05-15",
              "june": "06-15", "july": "07-15", "august": "08-15", "september": "09-15", "october": "10-15",
              "november": "11-15", "december": "12-15"}
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
# Houses on the same auction-archive platform as REA (2026-09-29): /{prefix}/{year}/{auction}/{lot}/{slug} lot links
# and per-auction listings at /{prefix}/{year}/{auction}?page=N. Huggins & Scott: robots.txt allows everything.
HOUSES = {
    "rea": {"host": "https://collectrea.com", "prefix": "archives", "out": "rea_comps.json", "state": "rea_state.json",
            "source": "rea", "id": "REA", "seasons": SEASONS, "first_year": None},
    "hugginsandscott": {"host": "https://hugginsandscott.com", "prefix": "auction", "out": "hugginsandscott_comps.json",
                        "state": "hugginsandscott_state.json", "source": "hugginsandscott", "id": "HS",
                        "seasons": SEASONS[:4] + MONTHS, "first_year": 2005},
}
HOUSE = dict(HOUSES["rea"], name="rea")


def use_house(name):
    """Point the scraper (listing base, output and state files) at one house."""
    global HOUSE, BASE, OUT_FILE, STATE_FILE
    HOUSE = dict(HOUSES[name], name=name)
    BASE = "%s/%s" % (HOUSE["host"], HOUSE["prefix"])
    OUT_FILE = os.path.join(_HERE, HOUSE["out"])
    STATE_FILE = os.path.join(_HERE, HOUSE["state"])


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


def parse_rea_listing(html, host="https://collectrea.com", prefix="archives"):
    """Parse one archive listing page -> list of dicts
    {url, title, sold_price, year, season, lot, sold_date}. Price is the
    realized '$' amount in each lot's segment (links + prices are 1:1, in order).
    Rows without a parseable price are dropped."""
    out = []
    lot_re = _LOT_RE if prefix == "archives" else re.compile(
        r'href="(/%s/(\d{4})/([^/"]+)/(\d+)/([^"]+))"' % re.escape(prefix))
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))
    pairs = _LOT_PRICE_RE.findall(text)
    seen_n = [n for n, _ in pairs]
    # only lot numbers that occur once: a page mixing auctions (the global /archives listing) repeats "Lot 1"
    by_lot = {n: p for n, p in pairs if seen_n.count(n) == 1}
    matches = list(lot_re.finditer(html or ""))
    for i, m in enumerate(matches):
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else m.end() + 1200
        seg = html[m.start():seg_end]
        pm = _PRICE_RE.search(seg)
        raw_price = ("$" + by_lot[m.group(4)]) if m.group(4) in by_lot else (pm.group(0) if pm else None)
        if not raw_price:
            continue
        try:
            price = float(raw_price.replace("$", "").replace(",", ""))
        except ValueError:
            continue
        if price <= 0:
            continue
        year, season, lot, slug = m.group(2), m.group(3), m.group(4), m.group(5)
        im = _IMG_RE.search(seg)
        out.append({"url": host + m.group(1),
                    "title": deslug(slug), "sold_price": price, "year": year,
                    "season": season, "lot": lot,
                    "image_url": im.group(1) if im else "",
                    "sold_date": season_to_date(year, season)})
    return out


def is_single_card(title):
    """True for a single card lot; False for sets, collections, sealed product, artwork and memorabilia."""
    t = title or ""
    return bool(_CARDISH_RE.search(t)) and not _MULTI_RE.search(t)


def lots_of_auction(lots, year, season):
    """Keep only lots that belong to /archives/{year}/{season} (a bad route must never pass off another listing)."""
    return [l for l in lots if l["year"] == str(year) and l["season"].lower() == season.lower()]


def parse_years(html):
    """4-digit years from the archive's soldYear <select>, newest first."""
    i = (html or "").find('name="soldYear"')
    sel = html[i:html.find("</select>", i)] if i >= 0 else ""
    return sorted({int(y) for y in re.findall(r'value="(\d{4})"', sel)}, reverse=True)


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
    seen = set(state.get("seen_urls") or []) | {c.get("url") for c in comps}
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
            state["seen_urls"] = sorted(u for u in seen if u); state["next_id"] = next_id
            save_comps(comps); save_state(state)
        time.sleep(args.sleep)
    if args.dry_run:
        print("[dry-run] would add %d new lots" % new if new else "[dry-run] (counts above)")
        return 0
    state["seen_urls"] = sorted(u for u in seen if u); state["next_id"] = next_id
    save_comps(comps); save_state(state)
    print("\n[done] +%d new REA comps (total %d)" % (new, len(comps)))
    return 0


def run_by_auction(args):
    state = load_state()
    comps = load_comps()
    seen = set(state.get("seen_urls") or []) | {c.get("url") for c in comps}
    next_id = int(state.get("next_id") or 1)
    first = HOUSE.get("first_year")
    all_years = list(range(date.today().year, first - 1, -1)) if first else parse_years(_fetch(BASE))
    years = [y for y in all_years if y >= args.since_year]
    print("[by-auction] years %s" % years, flush=True)
    new = skipped = 0
    for year in years:
        for season in HOUSE["seasons"]:
            listed = 0
            for pg in range(1, args.max_pages + 1):
                # 100 lots a page where the house honours pageSize (REA does; Huggins & Scott ignores it: 10). Each REA
                # listing stops at 1,000 results, so an auction past 1,000 lots is read down to its 1,000th-priced lot.
                url = "%s/%d/%s?pageSize=100%s" % (BASE, year, season, "" if pg == 1 else "&page=%d" % pg)
                try:
                    lots = lots_of_auction(parse_rea_listing(_fetch(url), HOUSE["host"], HOUSE["prefix"]), year, season)
                except urllib.error.HTTPError as e:
                    # page 1: 404 (REA) or 500 (Huggins & Scott) = no auction by that name that year -- auction names
                    # (seasons, and months at H&S) are probed blind
                    if not (pg == 1 and e.code in (404, 500)):
                        print("  [%d %s p%d] HTTP %s" % (year, season, pg, e.code), flush=True)
                    break
                except Exception as e:
                    print("  [%d %s p%d] fetch/parse error: %s" % (year, season, pg, str(e)[:70]), flush=True)
                    break
                if not lots:
                    break
                listed += len(lots)
                for lot in lots:
                    if lot["url"] in seen:
                        continue
                    seen.add(lot["url"])
                    if not args.all_lots and not is_single_card(lot["title"]):
                        skipped += 1
                        continue
                    new += 1
                    if not args.dry_run:
                        lot["comp_id"] = "%s-%d" % (HOUSE["id"], next_id)
                        lot["source"] = HOUSE["source"]
                        comps.append(lot)
                        next_id += 1
                if max(l["sold_price"] for l in lots) < args.min_price:
                    break
                time.sleep(args.sleep)
            if listed:
                print("  [%d %s] %d lots listed | running: +%d singles, %d non-singles skipped" % (
                    year, season, listed, new, skipped), flush=True)
                if not args.dry_run:
                    state["seen_urls"] = sorted(u for u in seen if u); state["next_id"] = next_id
                    save_comps(comps); save_state(state)
    print("\n[done] %s+%d new %s single-card comps, %d non-single lots skipped (total %d)" % (
        "[dry-run] " if args.dry_run else "", new, HOUSE["name"], skipped, len(comps)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=40, help="archive listing pages (12 lots/page)")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--by-auction", action="store_true", help="walk every auction's own archive (full depth)")
    ap.add_argument("--since-year", type=int, default=0, help="--by-auction: only auctions from this year on")
    ap.add_argument("--max-pages", type=int, default=600,
                    help="--by-auction: page cap per auction (REA 100 lots/page ends by ~11; Huggins & Scott 10/page)")
    ap.add_argument("--min-price", type=float, default=0, help="--by-auction: stop an auction below this price")
    ap.add_argument("--all-lots", action="store_true", help="--by-auction: keep non-single lots too")
    ap.add_argument("--house", choices=sorted(HOUSES), default="rea")
    args = ap.parse_args()
    use_house(args.house)
    if args.house != "rea" and not args.by_auction:
        sys.exit("--house %s needs --by-auction" % args.house)
    sys.exit(run_by_auction(args) if args.by_auction else run(args))


if __name__ == "__main__":
    main()
