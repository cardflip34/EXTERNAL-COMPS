#!/usr/bin/env python3
"""
auctionreport_scraper.py — multi-house top-lot realized-price poller
=====================================================================
AuctionReport.com publishes an editorial "Auction Results" post per closed
auction (Heritage, Goldin, REA, Memory Lane, CertifiedLink, ...), each listing
that sale's TOP LOTS with full card descriptions + exact realized prices in a
<li> list (plus a headline lot in prose). No anti-bot wall (RSS + article pages
fetch fine over plain HTTP), newest-first, hourly feed.

This poller is a RECENCY-first reference feed: walk the RSS newest-first, stop
at the stored pubDate watermark, fetch each new post, extract (card, price)
pairs. It is TOP-LOTS-ONLY and high-end skewed — reference/context comps, never
identity/proof, never Trusted/Mazified. source_code = 'auctionreport'.

Output: auctionreport_comps.json (local). Neon bridge is a separate approved step
(comp_sources registry insert = Tier-3).

Usage:
  python3 auctionreport_scraper.py --dry-run            # list new posts, fetch none
  python3 auctionreport_scraper.py --max-posts 8        # bounded first run
  python3 auctionreport_scraper.py                      # all new since watermark
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, date, timezone
from xml.etree import ElementTree as ET

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(_SCRIPT_DIR, "auctionreport_comps.json")
STATE_FILE = os.path.join(_SCRIPT_DIR, "auctionreport_state.json")

FEED_URL = "https://www.auctionreport.com/category/auction-results/feed/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Known auction houses -> canonical label (matched against RSS <category> tags
# and post titles). Order matters: first hit wins.
HOUSE_PATTERNS = [
    ("heritage", "Heritage Auctions"),
    ("goldin", "Goldin"),
    ("pwcc", "PWCC"),
    ("fanatics collect", "Fanatics Collect"),
    ("robert edward", "REA"),
    ("rea", "REA"),
    ("memory lane", "Memory Lane"),
    ("certifiedlink", "CertifiedLink"),
    ("comiclink", "ComicLink"),
    ("mile high", "Mile High Card Co"),
    ("clean sweep", "Clean Sweep"),
    ("sterling", "Sterling Sports"),
    ("iconic", "Iconic"),
    ("lelands", "Lelands"),
    ("classic auctions", "Classic Auctions"),
    ("rr auction", "RR Auction"),
]

_MULT = {"million": 1_000_000, "m": 1_000_000, "billion": 1_000_000_000,
         "b": 1_000_000_000, "thousand": 1_000, "k": 1_000}
# a $-amount, optional word multiplier. Group1 = number, group2 = multiplier word
_PRICE_RE = re.compile(
    r"\$\s*([\d][\d,]*(?:\.\d+)?)\s*(million|billion|thousand|[mkb])?\b",
    re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


# ── pure core (unit-tested in test_auctionreport.py) ─────────────────────────
def parse_ar_price(text):
    """Parse the FIRST $-amount in text -> float dollars, or None. Handles
    '$2,562,229', '$3 Million', '$2.56 Million', '$1.342 million', '$400,000'."""
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        val = float(num)
    except ValueError:
        return None
    mult = (m.group(2) or "").lower()
    if mult:
        val *= _MULT.get(mult, 1)
    return val


def _last_price_match(text):
    """Return the LAST _PRICE_RE match object in text (lot lines put the price
    at the end), or None."""
    last = None
    for m in _PRICE_RE.finditer(text or ""):
        last = m
    return last


def split_lot_line(text, min_desc=12):
    """A lot line is '<card description> ... $<price>'. Split on the LAST price
    token: description = text before it (trailing separators/grade-dash kept),
    price from that token. Returns (description, price_str, price_val) or None
    when there's no trailing price or the description is too short (nav/cruft)."""
    if not text:
        return None
    t = _WS_RE.sub(" ", text).strip()
    m = _last_price_match(t)
    if not m:
        return None
    desc = t[:m.start()].strip()
    # strip a dangling separator between grade and price
    desc = re.sub(r"[\-–—:;,\s]+$", "", desc).strip()
    if len(desc) < min_desc:
        return None
    price_str = t[m.start():m.end()].strip()
    price_val = parse_ar_price(price_str)
    if price_val is None or price_val <= 0:
        return None
    return desc, price_str, price_val


def strip_html(fragment):
    """<li> inner HTML -> clean text (tags removed, entities unescaped, ws
    collapsed)."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def extract_lot_lines(article_html):
    """Pull (description, price_str, price_val) from every <li> in the article
    that ends in a price. Deduped by (description, price_str), order preserved."""
    out, seen = [], set()
    for m in _LI_RE.finditer(article_html or ""):
        line = strip_html(m.group(1))
        parsed = split_lot_line(line)
        if not parsed:
            continue
        key = (parsed[0], parsed[1])
        if key in seen:
            continue
        seen.add(key)
        out.append(parsed)
    return out


def house_from_text(*texts):
    """First known auction house named across the given texts (categories,
    title), or '' if none matched. WORD-BOUNDARY match so short tokens like
    'rea' don't false-hit inside 'Breannan'/'area'/'great'."""
    blob = " ".join(t for t in texts if t).lower()
    for needle, label in HOUSE_PATTERNS:
        if re.search(r"\b" + re.escape(needle) + r"\b", blob):
            return label
    return ""


def parse_pubdate(s):
    """RFC-822 RSS pubDate -> aware datetime (UTC), or None."""
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def parse_rss(xml_text):
    """RSS XML -> list of dicts {title, link, pubdate(str), pubdt(datetime|None),
    categories[list]}, in feed order (newest first)."""
    items = []
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return items
    for it in channel.findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        cats = [c.text.strip() for c in it.findall("category") if c.text]
        items.append({"title": title, "link": link, "pubdate": pub,
                      "pubdt": parse_pubdate(pub), "categories": cats})
    return items


# ── io / driver ──────────────────────────────────────────────────────────────
def _fetch(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"last_pubdate": None, "seen_links": [], "next_id": 1}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def load_comps():
    if os.path.exists(OUT_FILE):
        try:
            return json.load(open(OUT_FILE))
        except Exception:
            pass
    return []


def save_comps(comps):
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(comps, f, indent=1)
    os.replace(tmp, OUT_FILE)


def run(args):
    state = load_state()
    comps = load_comps()
    seen_links = set(state.get("seen_links") or [])
    next_id = int(state.get("next_id") or 1)
    wm = parse_pubdate(state.get("last_pubdate")) if state.get("last_pubdate") else None

    print("[rss] %s" % FEED_URL)
    try:
        items = parse_rss(_fetch(FEED_URL))
    except Exception as e:
        print("  FATAL: RSS fetch/parse failed: %s" % e)
        return 1
    print("  %d items in feed" % len(items))

    new_items = []
    for it in items:
        if it["link"] in seen_links:
            continue
        if wm and it["pubdt"] and it["pubdt"] <= wm:
            continue
        new_items.append(it)
    print("  %d NEW posts past watermark (%s)" % (
        len(new_items), state.get("last_pubdate") or "none"))

    if args.max_posts:
        new_items = new_items[:args.max_posts]

    if args.dry_run:
        for it in new_items:
            print("  would fetch: %s | %s | %s" % (
                it["pubdate"], house_from_text(" ".join(it["categories"]), it["title"]) or "?",
                it["title"][:70]))
        print("[dry-run] no articles fetched.")
        return 0

    max_pubdt = wm
    total_new = 0
    for it in new_items:
        house = house_from_text(" ".join(it["categories"]), it["title"])
        try:
            art = _fetch(it["link"])
        except Exception as e:
            print("  [skip] fetch failed %s: %s" % (it["link"], str(e)[:80]))
            continue
        lots = extract_lot_lines(art)
        sold_date = it["pubdt"].date().isoformat() if it["pubdt"] else ""
        kept = 0
        for desc, price_str, price_val in lots:
            comps.append({
                "comp_id": "AR-%d" % next_id,
                "title": desc,
                "sold_price": price_val,
                "sold_price_str": price_str,
                "sold_date": sold_date,
                "auction_house": house,
                "source": "auctionreport",
                "post_url": it["link"],
                "post_title": it["title"],
                "capture_tier": "top_lots_reference",
                "scraped_at": datetime.now().isoformat(),
            })
            next_id += 1
            kept += 1
            total_new += 1
        seen_links.add(it["link"])
        if it["pubdt"] and (max_pubdt is None or it["pubdt"] > max_pubdt):
            max_pubdt = it["pubdt"]
        print("  [post] %-16s %2d lots | %s" % (house or "?", kept, it["title"][:60]))
        save_comps(comps)
        time.sleep(1.0 + (0.5 if kept else 0))

    state["seen_links"] = sorted(seen_links)[-500:]
    state["next_id"] = next_id
    if max_pubdt is not None:
        state["last_pubdate"] = max_pubdt.strftime("%a, %d %b %Y %H:%M:%S %z")
    save_state(state)
    print("\n[done] +%d reference comps across %d posts -> %s (total %d)" % (
        total_new, len(new_items), os.path.basename(OUT_FILE), len(comps)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="list new posts, fetch no articles")
    ap.add_argument("--max-posts", type=int, default=0,
                    help="cap posts this run (0 = all new)")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
