#!/usr/bin/env python3
"""Read-only: eBay's own 90-day SOLD result counts for a few card segments (one page load each, signed-in CDP browser)."""
import re, time, json
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

PROBES = [  # (label, category, query)
    ("Sports singles: major brands", "261328", "(topps,panini,bowman,donruss,upper deck,fleer,leaf,score)"),
    ("Sports singles: PSA 10", "261328", "psa 10"),
    ("Sports singles: rookie", "261328", "rookie"),
    ("Non-sport: topps/panini", "183050", "(topps,panini,upper deck)"),
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
