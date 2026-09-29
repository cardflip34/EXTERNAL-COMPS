#!/usr/bin/env python3
"""render_pages_cdp.py -- render JavaScript-only pages (official VeeFriends checklists) in the Mini's real Chrome.

Attaches to the Chrome that ~/mazi_ebay_session/sign_in.sh opened (127.0.0.1:9333), opens ONE new tab per page,
waits for the page to render, saves visible text + HTML to ~/mazi_veefriends/checklists/rendered/, closes the tab.
  /usr/bin/python3 tools/render_pages_cdp.py <url> [<url> ...]
"""
import os, re, sys, time
from playwright.sync_api import sync_playwright
OUT = os.path.expanduser("~/mazi_veefriends/checklists/rendered"); os.makedirs(OUT, exist_ok=True)
with sync_playwright() as pw:
    ctx = pw.chromium.connect_over_cdp("http://127.0.0.1:9333").contexts[0]
    for url in sys.argv[1:]:
        page = ctx.new_page()
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            for _ in range(8):                       # lazy-loaded sections
                page.mouse.wheel(0, 4000); page.wait_for_timeout(700)
            text = page.evaluate("() => document.body.innerText")
            name = re.sub(r"[^a-z0-9]+", "-", url.lower().split("//", 1)[1])[:90].strip("-")
            open(f"{OUT}/{name}.txt", "w").write(text); open(f"{OUT}/{name}.html", "w").write(page.content())
            print(f"OK  {len(text):7,} chars  {name}")
        except Exception as e:
            print(f"ERR {url}: {e}")
        finally:
            page.close()
        time.sleep(3)
