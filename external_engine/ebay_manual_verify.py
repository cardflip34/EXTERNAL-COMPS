#!/usr/bin/env python3
"""Open ONE visible eBay window on the Mini for the OPERATOR to clear eBay's human-verification check.

This is the human flow eBay is asking for: a person verifies themselves in a real browser. No stealth,
no solver, no automation of the challenge itself — the script only opens the page, then WATCHES and reports
what state the page is in. The operator does the clicking.

Purpose is diagnostic: after a human verifies, does this network get served normally (→ the block is a
clearable rate-limit) or does it stay walled (→ the IP is flagged for automation and needs a new transport)?

Profile is persistent (browser_state/ebay_manual), so a cleared verification survives restarts.
Writes status lines to logs/ebay_manual_verify.log and a final verdict JSON.

  nohup /usr/bin/python3 -u external_engine/ebay_manual_verify.py > logs/ebay_manual_verify.log 2>&1 &
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://www.ebay.com/sch/i.html?_nkw=cards&_sacat=0&_from=R40&rt=nc&LH_Sold=1"
PROFILE = os.path.join(ROOT, "browser_state", "ebay_manual")
VERDICT = os.path.join(ROOT, "logs", "ebay_manual_verify_verdict.json")
WATCH_MINUTES = 20
POLL_S = 5


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def page_state(pg) -> tuple[str, int, str]:
    text = pg.evaluate("() => document.body.innerText.substring(0,400)") or ""
    t = " ".join(text.split())
    tl = t.lower()
    items = pg.evaluate(
        "() => document.querySelectorAll('li.s-item, li.s-card, .srp-results .s-item').length") or 0
    if "something went wrong on our end" in tl or "error page" in tl:
        return "BLOCKED_403", items, t[:120]
    if "verify yourself" in tl or "please verify" in tl or ("verify" in tl and "robot" in tl):
        return "VERIFY_WALL", items, t[:120]
    if items > 0:
        return "SERVED", items, t[:120]
    return "UNKNOWN", items, t[:120]


def main() -> int:
    os.makedirs(PROFILE, exist_ok=True)
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    from playwright.sync_api import sync_playwright

    print(f"[{now()}] opening ONE visible eBay window for operator verification", flush=True)
    print(f"[{now()}] profile={PROFILE}", flush=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=False, viewport={"width": 1400, "height": 900},
            args=["--window-position=80,60", "--window-size=1400,940"])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pg.goto(URL, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[{now()}] initial nav error: {type(e).__name__}", flush=True)
        time.sleep(3)
        state, items, head = page_state(pg)
        print(f"[{now()}] STATE={state} items={items} :: {head}", flush=True)
        print(f"[{now()}] >>> OPERATOR: complete the verification in the window on screen. "
              f"Watching {WATCH_MINUTES} min; I will report the moment the page changes.", flush=True)

        deadline = time.time() + WATCH_MINUTES * 60
        last = state
        final = state
        while time.time() < deadline:
            time.sleep(POLL_S)
            try:
                state, items, head = page_state(pg)
            except Exception:
                continue  # page navigating
            if state != last:
                print(f"[{now()}] STATE CHANGE: {last} -> {state} items={items} :: {head}", flush=True)
                last = state
            final = state
            if state == "SERVED" and items > 0:
                print(f"[{now()}] VERIFIED_AND_SERVED items={items} — eBay is serving this browser now.", flush=True)
                break
        json.dump({"at": now(), "final_state": final, "items": items, "url": URL, "profile": PROFILE},
                  open(VERDICT, "w"), indent=1)
        print(f"[{now()}] final={final} (verdict -> {VERDICT}); leaving window open for you.", flush=True)
        # keep the window open so the operator keeps the verified session
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
