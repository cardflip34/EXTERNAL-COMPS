#!/usr/bin/env python3
"""eBay gentle probe — ONE request, real scraper path, never retries (PRD V2 §28/§29).

Answers: "would ebay_player_scraper.py be served right now from this host?"
 - Same transport as production (Playwright Chromium, same launch args + UA pool) so the answer is
   representative. Adds NO evasion of any kind.
 - Exactly one navigation. Classifies the body with ebay_bulk_scraper.block_signature (the production
   'Error Page' / CAPTCHA / access-denied detector) and counts result items.
 - Writes the verdict into source_health/ebay.json and appends a probe log line. Never flips state to
   HEALTHY on its own: a GREEN probe records `last_probe: GREEN` and sets state COOLDOWN->DEGRADED for
   operator-coordinated resumption (the gate note says: on GREEN, coordinate).
 - Refuses to run before `probe_not_before` unless --force (72h zero-contact quiet window).

Usage:
  /usr/bin/python3 external_engine/ebay_gentle_probe.py            # honours probe_not_before
  /usr/bin/python3 external_engine/ebay_gentle_probe.py --force    # operator override
  /usr/bin/python3 external_engine/ebay_gentle_probe.py --dry-run  # print URL only, no network
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import source_health as sh  # noqa: E402

PROBE_QUERY = "shohei ohtani card"  # roster rank 1; broad, always has results when served


def probe_url(query: str = PROBE_QUERY) -> str:
    q = "+".join(query.lower().split())
    return (f"https://www.ebay.com/sch/i.html?_from=R40&_nkw={q}&_sacat=0&LH_Sold=1&LH_Complete=1"
            f"&_sop=13&_ipg=60")


def classify(page_text: str, item_count: int) -> str:
    from ebay_bulk_scraper import block_signature  # production detector
    sig = block_signature(page_text)
    if sig == "captcha":
        return "RED_CAPTCHA"
    if sig == "blocked":
        return "RED_BLOCKED"
    if item_count > 0:
        return "GREEN"
    if sig == "no_results":
        return "AMBER_NO_RESULTS"
    return "AMBER_UNKNOWN"


def run(force: bool, dry_run: bool, store: str | None, timeout_ms: int) -> int:
    doc = sh.load("ebay", store)
    nb = doc.get("probe_not_before")
    now = datetime.now(timezone.utc)
    if nb and not force and not dry_run:
        try:
            nb_dt = datetime.fromisoformat(nb.replace("Z", "+00:00"))
        except ValueError:
            nb_dt = None
        if nb_dt and now < nb_dt:
            print(f"REFUSED: probe_not_before={nb} (now {now.isoformat(timespec='seconds')}); use --force to override")
            return 4
    url = probe_url()
    if dry_run:
        print("DRY RUN url:", url)
        return 0

    # lazy imports so --dry-run needs no browser
    from playwright.sync_api import sync_playwright
    from ebay_player_scraper import USER_AGENTS  # production UA pool

    verdict, item_count, status, snippet, err = "AMBER_UNKNOWN", 0, None, "", None
    t0 = time.time()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=random.choice(USER_AGENTS), viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            resp = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            status = resp.status if resp else None
            page.wait_for_timeout(1500)
            text = page.evaluate("() => document.body.innerText.substring(0, 800)") or ""
            snippet = " ".join(text.split())[:160]
            try:
                item_count = page.evaluate(
                    "() => document.querySelectorAll('li.s-item, li.s-card, .srp-results .s-item').length")
            except Exception:
                item_count = 0
            verdict = classify(text, int(item_count or 0))
            ctx.close(); browser.close()
    except Exception as e:  # navigation error, timeout, etc.
        err = f"{type(e).__name__}: {str(e)[:200]}"
        verdict = "AMBER_NAV_ERROR"
    elapsed = round(time.time() - t0, 1)

    result = {"at": now.isoformat(timespec="seconds"), "url": url, "http_status": status,
              "verdict": verdict, "item_count": item_count, "elapsed_s": elapsed,
              "body_snippet": snippet, "error": err, "host": os.uname().nodename}
    print(json.dumps(result, indent=2))

    # persist: event + last_probe; state transitions are conservative
    sh.add_event("ebay", "probe", detail=verdict, store=store, http_status=status, item_count=item_count)
    doc = sh.load("ebay", store)
    doc["last_probe"] = result
    sh.save(doc, store)
    if verdict.startswith("RED"):
        sh.set_state("ebay", "BLOCKED", note=f"probe {verdict} http={status}", store=store,
                     probe_not_before=_plus_hours(now, 72))
        print(f"STATE: BLOCKED (next probe not before {_plus_hours(now, 72)})")
    elif verdict == "GREEN":
        sh.set_state("ebay", "DEGRADED", note="probe GREEN — coordinate resumption; start polite/low volume", store=store)
        print("STATE: DEGRADED (GREEN probe; resumption must be coordinated + polite)")
    else:
        sh.add_event("ebay", "probe_inconclusive", detail=verdict, store=store)
        print("STATE: unchanged (inconclusive probe)")
    # append-only probe log next to the health file
    try:
        with open(os.path.join(sh.health_dir(store), "ebay_probe_log.jsonl"), "a") as f:
            f.write(json.dumps(result) + "\n")
    except OSError:
        pass
    return 0 if verdict == "GREEN" else 2


def _plus_hours(dt: datetime, h: int) -> str:
    from datetime import timedelta
    return (dt + timedelta(hours=h)).isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--store", default=None)
    ap.add_argument("--timeout-ms", type=int, default=30000)
    a = ap.parse_args()
    sys.exit(run(a.force, a.dry_run, a.store, a.timeout_ms))


if __name__ == "__main__":
    main()
