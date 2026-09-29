#!/usr/bin/env python3
"""
myslabs_scraper_v2.py — MySlabs SOLD graded-card comps
=======================================================
MySlabs' public sold archive (myslabs.com/browse/archive/) lists SOLD slabs
newest-first with title + realized price + sold date + shipping. WAF 403s plain
HTTP but renders fine via headless Playwright from Mini (verified 2026-07-16).
Each slab card's container text is: "<title incl grade>\\n$<price>\\n<Mon DD, YYYY>
[\\n+ $<ship> Shipping]". This scraper renders + scrolls the archive, extracts
per-slab rows, and lands only NEW ones (seen-URL set + newest-date watermark).

Reference-only comps (graded singles). source_code 'myslabs'. Output:
myslabs_comps.json (local). Bridge = separate approved step.

Usage:
  python3 myslabs_scraper_v2.py --dry-run
  python3 myslabs_scraper_v2.py --scrolls 12
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(_HERE, "myslabs_comps.json")
STATE_FILE = os.path.join(_HERE, "myslabs_state.json")

URL = "https://myslabs.com/browse/archive/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)")
_DATE_RE = re.compile(r"\b([A-Z][a-z]{2,8})\s+(\d{1,2}),\s+(\d{4})\b")
_SHIP_RE = re.compile(r"\+\s*\$([\d,]+(?:\.\d{2})?)\s*Shipping", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# ── pure core (unit-tested in test_myslabs.py) ───────────────────────────────
def parse_money(s):
    if not s:
        return None
    m = _PRICE_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_sold_date(text):
    """'Jul 15, 2026' -> '2026-07-15', or '' if no month-name date present.
    Full month names ('July') also accepted."""
    if not text:
        return ""
    m = _DATE_RE.search(text)
    if not m:
        return ""
    mon = m.group(1)[:3].title()
    if mon not in _MONTHS:
        return ""
    try:
        return "%04d-%02d-%02d" % (int(m.group(3)), _MONTHS[mon], int(m.group(2)))
    except ValueError:
        return ""


def parse_slab_text(text, url=""):
    """Container innerText for one sold slab -> comp dict, or None. Format:
    '<title>\\n$<price>\\n<Mon DD, YYYY>[\\n+ $<ship> Shipping]'. title = text
    before the first price token; price = first $, EXCLUDING a '+ $x Shipping';
    realized = price + shipping."""
    if not text:
        return None
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None
    # title = leading lines up to the first bare price line
    title_parts, rest_idx = [], None
    for i, ln in enumerate(lines):
        if _PRICE_RE.search(ln) and not _SHIP_RE.search(ln):
            rest_idx = i
            break
        title_parts.append(ln)
    if rest_idx is None:
        return None
    title = " ".join(title_parts).strip()
    if len(title) < 6:
        return None
    price = parse_money(lines[rest_idx])
    if price is None or price <= 0:
        return None
    joined = " ".join(lines)
    sold_date = parse_sold_date(joined)
    if not sold_date:
        return None
    sm = _SHIP_RE.search(joined)
    ship = parse_money("$" + sm.group(1)) if sm else 0.0
    return {"title": title, "sold_price": round(price + (ship or 0.0), 2),
            "sold_price_base": price, "shipping": ship or 0.0,
            "sold_date": sold_date, "url": url}


# ── browser extraction ───────────────────────────────────────────────────────
_EXTRACT_JS = """() => {
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll("a[href*='/slab/']")) {
    let href = a.href || ''; const key = href.split('?')[0];
    if (!key || seen.has(key)) continue;
    let c = a;
    for (let i = 0; i < 6 && c.parentElement; i++) {
      c = c.parentElement;
      if ((c.innerText || '').includes('$')) break;
    }
    const t = c.innerText || '';
    if (t.includes('$')) {
      let img = '';
      const ie = c.querySelector('img');
      if (ie) img = ie.currentSrc || ie.src || ie.getAttribute('data-src') || '';
      seen.add(key); out.push({url: key, text: t, img: img});
    }
  }
  return out;
}"""


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"seen_urls": [], "next_id": 1, "last_date": ""}


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
    from playwright.sync_api import sync_playwright
    state = load_state()
    comps = load_comps()
    seen = set(state.get("seen_urls") or [])
    next_id = int(state.get("next_id") or 1)

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(7000)
            for _ in range(args.scrolls):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(1200)
            items = page.evaluate(_EXTRACT_JS)
        finally:
            try: b.close()
            except Exception: pass

    print("[extract] %d slab containers" % len(items))
    parsed = [(it["url"], it.get("img", ""), parse_slab_text(it["text"], it["url"])) for it in items]
    ok = [(u, img, r) for u, img, r in parsed if r]
    print("  parsed OK: %d / %d" % (len(ok), len(items)))
    if args.dry_run:
        for u, img, r in ok[:10]:
            print("  %s $%.2f %s | img=%s" % (r["sold_date"], r["sold_price"], r["title"][:40], "Y" if img else "n"))
        return 0

    new = 0
    max_date = state.get("last_date", "")
    for u, img, r in ok:
        if u in seen:
            continue
        seen.add(u)
        r["image_url"] = img
        r["comp_id"] = "MS-%d" % next_id
        r["source"] = "myslabs"
        r["scraped_at"] = datetime.now().isoformat()
        comps.append(r); next_id += 1; new += 1
        if r["sold_date"] > max_date:
            max_date = r["sold_date"]
    state["seen_urls"] = sorted(seen)[-20000:]
    state["next_id"] = next_id
    state["last_date"] = max_date
    save_comps(comps); save_state(state)
    print("\n[done] +%d new MySlabs sold comps (total %d, newest %s)" % (new, len(comps), max_date))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scrolls", type=int, default=10, help="scroll passes (infinite archive)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
