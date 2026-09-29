#!/usr/bin/env python3
"""ebay_veefriends_pilot.py -- signed-in, VeeFriends-ONLY eBay sold pilot (Route 1, operator-approved 2026-09-26).

Why: eBay shows sold listings only to signed-in accounts since ~2026-07-01, so our VeeFriends comps stop there
(Jul-Sep 2026: 10 sales). eBay's sold search only reaches back 90 days, so July rolls off from ~2026-09-29.

What it does:
  * opens the DEDICATED eBay account's Chrome profile that Andy signed into with ~/mazi_ebay_session/sign_in.sh
    (real Chrome, visible window, no stealth/evasion flags; the operator's own session is the only access);
  * newest-first sold search per query, one page per 8-15 s, stops when a page's oldest sale is before --floor,
    a hard --max-pages budget per run, and stops for good on a login wall, CAPTCHA or block (never retries);
  * writes a SIDE FILE (~/mazi_veefriends/ebay_pilot/) and runs the VeeFriends linker over it as a dry run.
    Nothing is written to Neon here.
The global source_health 'ebay' gate stays BLOCKED on purpose: external_engine/lanes.py would restart the bulk
eBay lanes if it opened. This pilot runs only with --operator-approved and logs its runs to ~/mazi_veefriends/ebay_pilot/pilot_events.jsonl.

  /usr/bin/python3 tools/ebay_veefriends_pilot.py --operator-approved --floor 2026-07-01 --max-pages 60
"""
from __future__ import annotations

import argparse, json, os, random, re, sys, time
from datetime import datetime, timezone
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "external_engine")); sys.path.insert(0, os.path.join(ROOT, "tools"))

PROFILE = os.path.expanduser("~/mazi_ebay_session/chrome_profile")
# ATTACH to Andy's own signed-in Chrome (sign_in.sh opens it with a 127.0.0.1-only debugging port). Launching Chrome
# from Playwright instead adds --use-mock-keychain on macOS: the real profile's encrypted cookies cannot be read, the
# browser looks logged out, and eBay's logged-out cookies overwrite the real ones (happened 2026-09-27 03:28Z).
CDP = "http://127.0.0.1:9333"
OUT_DIR = os.path.expanduser("~/mazi_veefriends/ebay_pilot")
STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
# (keyword, eBay category). 183050 = Non-Sport Trading Card Singles -- the search Andy confirmed on the signed-in
# page 2026-09-27; the category also keeps boxes/packs out.
QUERIES = [("veefriends", "183050")]
ITEM_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,14})")


def log_event(kind: str, detail: str) -> None:
    """Pilot events stay in the home dir: the desktop (Terminal) session has no access to the 6TB evidence volume,
    where source_health lives -- an open() there blocks on a macOS permission prompt. Copied into
    source_health 'ebay' events from an ssh session afterwards."""
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "pilot_events.jsonl"), "a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "type": kind, "detail": detail}) + "\n")


class Stop(RuntimeError):
    """Login wall / CAPTCHA / block: stop the whole run, never retry."""


def sold_url(query: str, page: int, category: str | None = None) -> str:
    base = f"https://www.ebay.com/sch/{category}/i.html?" if category else "https://www.ebay.com/sch/i.html?"
    return base + urlencode({"_nkw": query, "LH_Sold": 1, "LH_Complete": 1, "_sop": 13, "_ipg": 240, "_pgn": page})


def item_id(link: str) -> str | None:
    m = ITEM_RE.search(link or "")
    return m.group(1) if m else None


def page_rows(items, query, pno, parse_date, price):
    """Pure: parsed listing dicts -> staged rows (+ the oldest sold date seen on the page)."""
    rows, dates = [], []
    for it in items:
        iid, sd = item_id(it.get("link")), parse_date(it.get("soldDate") or "")
        if sd:
            dates.append(sd)
        if not iid or not sd or "veefriend" not in (it.get("title") or "").lower().replace(" ", ""):
            continue
        rows.append({"item_id": iid, "title": it.get("title"), "sold_price": price(it.get("priceText")), "sold_date": sd,
                     "best_offer": bool(it.get("bestOffer")), "shipping": it.get("shipping") or "",
                     "condition": it.get("condition") or "", "bids": it.get("bids") or "", "url": it.get("link"),
                     "image_url": it.get("imgUrl") or "", "query": query, "page": pno,
                     "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": "ebay_vf_pilot_v1"})
    return rows, (min(dates) if dates else None)


def run(args):
    import resource_governor as rg
    from playwright.sync_api import sync_playwright
    from ebay_bulk_scraper import extract_listings, block_signature
    from ebay_player_scraper import parse_listing_sold_date
    from ebay_polite_adapter import _price
    if not args.operator_approved:
        sys.exit("refusing: pass --operator-approved (Andy approved Route 1 on 2026-09-26)")
    import urllib.request
    try:
        urllib.request.urlopen(CDP + "/json/version", timeout=5).read()
    except Exception:
        sys.exit(f"no signed-in Chrome on {CDP}: run ~/mazi_ebay_session/sign_in.sh, sign in, and leave the window open")
    level, reasons = rg.classify(rg.sample())
    if level == "RED":
        sys.exit("resource RED: " + "; ".join(reasons))
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"sold_{datetime.now(timezone.utc):%Y%m%dT%H%MZ}.jsonl")
    seen, pages, stop_why = set(), 0, None
    log_event("pilot_run_start", f"VeeFriends signed-in pilot floor={args.floor} max_pages={args.max_pages}")
    with sync_playwright() as pw, open(out, "w") as f:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = ctx.new_page()              # our own tab; Andy's window and tabs are left alone
        try:
            for q, cat in QUERIES:
                for pno in range(args.start_page, 1000):
                    if pages >= args.max_pages:
                        stop_why = f"page budget {args.max_pages} reached"; break
                    if pages:
                        time.sleep(random.uniform(8, 15))
                    page.goto(sold_url(q, pno, cat), timeout=45000, wait_until="domcontentloaded")
                    pages += 1
                    page.wait_for_timeout(1500)
                    if "signin.ebay.com" in page.url:
                        raise Stop("LOGIN_REQUIRED: redirected to signin -- the profile is not signed in (re-run sign_in.sh)")
                    sig = block_signature(page.evaluate("() => document.body.innerText.substring(0, 800)") or "")
                    if sig in ("blocked", "captcha"):
                        raise Stop(f"{sig.upper()} on '{q}' page {pno}")
                    rows, oldest = page_rows(extract_listings(page) or [], q, pno, parse_listing_sold_date, _price)
                    new = [r for r in rows if r["item_id"] not in seen and r["sold_date"] >= args.floor]
                    for r in new:
                        seen.add(r["item_id"]); f.write(json.dumps(r) + "\n")
                    f.flush()
                    print(f"[{q}] page {pno}: {len(rows)} rows, {len(new)} new, oldest {oldest}", flush=True)
                    # eBay mixes a few old sales into recent pages, so ONE old date must not end the walk: stop only when
                    # the page adds nothing at/after the floor, or when >=90% of its dated sales are before it.
                    dated = [r["sold_date"] for r in rows]
                    old_share = sum(1 for x in dated if x < args.floor) / max(len(dated), 1)
                    if not rows or not new or old_share >= 0.9:
                        break
                if stop_why:
                    break
        except Stop as e:
            stop_why = str(e)
            log_event("pilot_stop", stop_why)
        finally:
            try:
                page.close()                   # close ONLY our tab; the signed-in Chrome stays open
            except Exception:
                pass
    log_event("pilot_run_end", f"pages={pages} rows={len(seen)} stop={stop_why}")
    print(f"pages {pages} | VeeFriends sold rows {len(seen)} -> {out} | stop: {stop_why or 'floor reached on every query'}")
    if seen:
        import veefriends_link as V
        L = V.Linker(json.load(open(V.CATALOG)))
        from collections import Counter
        why = Counter(L.link(json.loads(l)["title"])[1] for l in open(out))
        print("linker dry run:", dict(why.most_common()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--operator-approved", action="store_true")
    ap.add_argument("--floor", default="2026-07-01")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--start-page", type=int, default=1, help="resume a walk that hit its page budget")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
