#!/usr/bin/env python3
"""Read-only: eBay's own 90-day SOLD result counts for a few card segments (one page load each, signed-in CDP browser)."""
import re, time, json
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

PROBES = [  # (label, category, query)
    ("VeeFriends (calibration)", "183050", "veefriends"),
    ("Sports singles, all", "261328", ""),
    ("Pokemon singles, all", "183454", "pokemon"),
    ("Pokemon graded PSA 10", "183454", "pokemon psa 10"),
    ("Non-sport singles, all", "183050", ""),
    ("One Piece singles", "183454", "one piece"),
    ("Lorcana singles", "183454", "lorcana"),
]
out = []
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9333")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    try:
        for label, cat, q in PROBES:
            params = {"LH_Sold": 1, "LH_Complete": 1, "_sop": 13, "_ipg": 60}
            if q:
                params["_nkw"] = q
            url = f"https://www.ebay.com/sch/{cat}/i.html?" + urlencode(params)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(2.5)
            head = ""
            for sel in (".srp-controls__count-heading", "h1.srp-controls__count-heading", ".result-count__count-heading"):
                el = page.query_selector(sel)
                if el:
                    head = el.inner_text().strip(); break
            signed_in = "signin.ebay.com" not in page.url
            m = re.search(r"([\d,]+)\+?\s+results?", head)
            n = int(m.group(1).replace(",", "")) if m else None
            out.append({"segment": label, "sold_90d": n, "per_day": round(n / 90) if n else None, "header": head[:80], "signed_in": signed_in})
            time.sleep(4)
    finally:
        page.close()
for r in out:
    print(json.dumps(r))
