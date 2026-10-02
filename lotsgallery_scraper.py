#!/usr/bin/env python3
"""lotsgallery_scraper.py -- sold single-card lots from auction houses on the createauction.com "Lots/Gallery" platform
(Memory Lane, Lelands), 2026-10-01.

Cloudflare challenges a headless browser after a few requests, so this drives a REAL Chrome window through CDP: a
dedicated profile on 127.0.0.1:9335 (no sign-in; public pages), launched like the eBay sweep's:
  open -na "Google Chrome" --args --user-data-dir=$HOME/mazi_auction_session/chrome_profile \
       --remote-debugging-port=9335 --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check
The archive is an ASP.NET postback: choosing an auction in <select id="Auction"> sets it for the session; GET pages
?size=250&page=N then stay on that auction. Each lot shows its number, title, bids, "Status: Sold" and "SOLD FOR $X"
(Memory Lane states prices include the buyer's premium). Only Sold lots; single cards only (rea_scraper.is_single_card).

  python3 lotsgallery_scraper.py --house memorylane --dry-run --limit-auctions 2
  python3 lotsgallery_scraper.py --house memorylane          # every auction in the archive; resumable (done auctions skipped)
"""
import argparse, json, os, re, sys, time
from datetime import datetime
from html import unescape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rea_scraper  # noqa: E402  (is_single_card)

_HERE = os.path.dirname(os.path.abspath(__file__))
HOUSES = {"memorylane": {"host": "https://bid.memorylaneinc.com", "id": "ML"},
          "lelands": {"host": "https://auction.lelands.com", "id": "LL"}}
CDP = "http://127.0.0.1:9335"
_ITEM_SPLIT = re.compile(r'<div class="item">')
_ITEM_RE = re.compile(r'bidplace\.aspx\?itemid=(\d+)"[^>]*>([^<]+)</a>', re.S)
_LOT_RE = re.compile(r'<h5 class="boxed">\s*([^<]+?)\s*</h5>')
_BIDS_RE = re.compile(r"Bids:\s*<strong>\s*(\d+)")
_STATUS_RE = re.compile(r"Status:\s*<strong>\s*([^<]+?)\s*</strong>")
_SOLD_RE = re.compile(r"SOLD FOR \$([\d,]+(?:\.\d\d)?)")
_END_RE = re.compile(r"End:\s*(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_gallery(html):
    """One gallery page -> [{itemid, lot, title, bids, status, sold_price}]; lots without a 'SOLD FOR' price are kept with
    sold_price None so the caller can count unsold."""
    out = []
    for chunk in _ITEM_SPLIT.split(html or "")[1:]:
        m = _ITEM_RE.search(chunk)
        if not m:
            continue
        lot, bids, st, sold = _LOT_RE.search(chunk), _BIDS_RE.search(chunk), _STATUS_RE.search(chunk), _SOLD_RE.search(chunk)
        title = unescape(re.sub(r"\s+", " ", m.group(2)).strip())     # "Stars &amp; Rookies" -> "Stars & Rookies"
        out.append({"itemid": m.group(1), "lot": lot.group(1) if lot else None, "title": title,
                    "bids": int(bids.group(1)) if bids else None, "status": st.group(1) if st else None,
                    "sold_price": float(sold.group(1).replace(",", "")) if sold else None})
    return out


def auction_end(html):
    """'End: 11/18/2004 1:00 PM ET' in the page header -> '2004-11-18'."""
    m = _END_RE.search(re.sub(r"<[^>]+>", " ", html or ""))
    return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2))) if m else None


def page_html(page, tries=4, pause=2.0):
    """page.content(), retried while the page is still navigating: a late postback/redirect can outlast the fixed wait,
    and Playwright then raises "Unable to retrieve content because the page is navigating" (it ended the 2026-10-01
    Lelands leg 39 auctions in). Any other error, or the last try, is raised as before."""
    for i in range(tries):
        try:
            return page.content()
        except Exception as e:
            if "navigating" not in str(e) or i == tries - 1:
                raise
            try:
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
            time.sleep(pause)


def auctions(html):
    m = re.search(r'<select[^>]*id="Auction"[^>]*>(.*?)</select>', html or "", re.S)
    return [(v, re.sub(r"&amp;", "&", t).strip()) for v, t in re.findall(r'value="(\d+)"[^>]*>([^<]*)<', m.group(1))] if m else []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--house", choices=sorted(HOUSES), required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit-auctions", type=int)
    ap.add_argument("--sleep", type=float, default=5.0)
    ap.add_argument("--max-pages", type=int, default=40)
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright
    h = HOUSES[a.house]
    out_file = os.path.join(_HERE, f"{a.house}_comps.json")
    state_file = os.path.join(_HERE, f"{a.house}_state.json")
    comps = json.load(open(out_file)) if os.path.exists(out_file) else []
    state = json.load(open(state_file)) if os.path.exists(state_file) else {"done_auctions": {}, "next_id": 1}
    seen = {c["itemid"] for c in comps}
    new = skipped = unsold = 0
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(CDP)
        page = b.contexts[0].new_page()
        page.set_default_timeout(90000)
        try:
            page.goto(h["host"] + "/Lots/Gallery?size=250", wait_until="domcontentloaded"); page.wait_for_timeout(4000)
            todo = [x for x in auctions(page_html(page)) if x[0] not in state["done_auctions"]]
            if a.limit_auctions:
                todo = todo[:a.limit_auctions]
            print(f"[{a.house}] {len(todo)} auctions to read", flush=True)
            for aid, name in todo:
                # back to page 1 first: the postback keeps the current query string, so switching from an (empty)
                # last page would open the next auction on that page number
                page.goto(h["host"] + "/Lots/Gallery?size=250", wait_until="domcontentloaded"); page.wait_for_timeout(3000)
                page.select_option("#Auction", aid)
                page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(int(a.sleep * 1000) + 3000)
                html = page_html(page)
                if "Attention Required" in html:
                    print("  Cloudflare challenge -- stopping (state saved)", flush=True); break
                end = auction_end(html)
                got = 0
                for pg in range(1, a.max_pages + 1):
                    if pg > 1:
                        page.goto(h["host"] + f"/Lots/Gallery?size=250&page={pg}", wait_until="domcontentloaded")
                        page.wait_for_timeout(int(a.sleep * 1000)); html = page_html(page)
                    lots = parse_gallery(html)
                    if not lots or not any(l["sold_price"] for l in lots):
                        break
                    got += len(lots)
                    for l in lots:
                        if l["itemid"] in seen:
                            continue
                        seen.add(l["itemid"])
                        if not l["sold_price"] or (l["status"] or "").lower() != "sold":
                            unsold += 1; continue
                        if not rea_scraper.is_single_card(l["title"]):
                            skipped += 1; continue
                        new += 1
                        if not a.dry_run:
                            comps.append({**l, "comp_id": f"{h['id']}-{state['next_id']}", "source": a.house, "auction_id": aid,
                                          "auction_title": name, "sold_date": end, "sold_date_granularity": "auction_end",
                                          "url": f"{h['host']}/bids/bidplace.aspx?itemid={l['itemid']}",
                                          "scraped_at": datetime.now().isoformat(timespec="seconds")})
                            state["next_id"] += 1
                print(f"  {name[:48]:<48} end {end} | lots {got} | running +{new} singles, {skipped} non-singles, {unsold} unsold", flush=True)
                if not a.dry_run and got:
                    state["done_auctions"][aid] = {"name": name, "end": end, "lots": got}
                    json.dump(comps, open(out_file + ".tmp", "w")); os.replace(out_file + ".tmp", out_file)
                    json.dump(state, open(state_file + ".tmp", "w")); os.replace(state_file + ".tmp", state_file)
        finally:
            page.close()
    print(f"[done] {'[dry-run] ' if a.dry_run else ''}+{new} {a.house} single-card comps (total {len(comps)})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
