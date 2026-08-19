#!/usr/bin/env python3
"""eBay polite-mode adapter (PRD V2 §26-§29) — operator decision 2026-08-19: NO stealth.

 - one Playwright session per run, ONE fixed realistic desktop UA (no rotation), NO automation-hiding flags
 - randomised polite delay between page loads (default 8-15 s), bounded pages per query, daily request budget
 - every request gated by source_health.contact_allowed('ebay') + resource governor (pause on RED)
 - block signature ('Error Page' / CAPTCHA / access denied) → set source_health BLOCKED (probe +72 h), raise SourceBlocked
 - reuses the production DOM parsing (ebay_bulk_scraper.extract_listings etc.) and URL builders
 - produces STAGED rows (scraper v1 shape + source/query_label) for the canonical writer; never touches stores

Contract (subset of ExternalSourceAdapter): health_check(), search(query, window_day, max_pages), stage(rows, ...),
rate_budget(), backoff(). discover()/checkpoint()/resume() live in the lane runners + ledger.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)

import source_health as sh  # noqa: E402
import resource_governor as rg  # noqa: E402

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
FIXED_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36")
RECENTLY_ENDED_SORT = "13"
DEFAULT_SORT = "16"
ADAPTER_VERSION = "0.1.0"


class SourceBlocked(RuntimeError):
    pass


class SourceUnavailable(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EbayPoliteAdapter:
    def __init__(self, store: str | None = None, min_delay_s: float = 8.0, max_delay_s: float = 15.0, max_pages: int = 3,
                 timeout_ms: int = 30000, daily_budget: int = 1500, headless: bool = True, log=print):
        self.store = store or STORE
        self.min_delay, self.max_delay, self.max_pages = min_delay_s, max_delay_s, max_pages
        self.timeout_ms, self.daily_budget, self.headless, self.log = timeout_ms, daily_budget, headless, log
        self._pw = self._browser = self._ctx = self._page = None
        self.requests_this_run = 0
        self.staging_dir = os.path.join(self.store, "external_store", "staging", "ebay")
        os.makedirs(self.staging_dir, exist_ok=True)

    # --- gates -------------------------------------------------------------------------------
    def health_check(self) -> tuple[bool, str]:
        ok, why = sh.contact_allowed("ebay", self.store)
        if not ok:
            return False, why
        level, reasons = rg.classify(rg.sample())
        if level == "RED":
            return False, "resource RED: " + "; ".join(reasons)
        used = self.rate_budget()["used_today"]
        if used >= self.daily_budget:
            return False, f"daily request budget reached ({used}/{self.daily_budget})"
        return True, f"ok (level {level}, budget {used}/{self.daily_budget})"

    def rate_budget(self) -> dict:
        doc = sh.load("ebay", self.store)
        today = _now().date().isoformat()
        rb = doc.get("rate_budget") or {}
        if rb.get("day") != today:
            rb = {"day": today, "used_today": 0}
        return rb

    def _count_request(self) -> None:
        doc = sh.load("ebay", self.store)
        rb = self.rate_budget(); rb["used_today"] = rb.get("used_today", 0) + 1
        doc["rate_budget"] = rb; doc["last_contact_at"] = _now().isoformat(timespec="seconds")
        sh.save(doc, self.store)
        self.requests_this_run += 1

    def backoff(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    # --- session -----------------------------------------------------------------------------
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)   # no automation-hiding args
        self._ctx = self._browser.new_context(user_agent=FIXED_UA, viewport={"width": 1440, "height": 900})
        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        for closer in (lambda: self._ctx.close(), lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        return False

    # --- search ------------------------------------------------------------------------------
    def search(self, query: str, window_day: str | None = None, max_pages: int | None = None, query_label: str = "") -> dict:
        """Bounded polite search. Returns {'rows': [...staged rows...], 'pages': n, 'seen': n, 'blocked': False, 'early_stop': bool}."""
        from ebay_bulk_scraper import extract_listings, click_next_page, block_signature, should_exclude, get_total_results
        from ebay_player_scraper import build_matrix_url, parse_listing_sold_date, is_graded_title, GRADER_KEYWORDS
        ok, why = self.health_check()
        if not ok:
            raise SourceUnavailable(why)
        max_pages = max_pages or self.max_pages
        url = build_matrix_url(query, sort_order=RECENTLY_ENDED_SORT if window_day else DEFAULT_SORT)
        rows, seen, pages, early_stop = [], 0, 0, False
        page = self._page
        for pno in range(1, max_pages + 1):
            if pno == 1:
                page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            else:
                if not click_next_page(page):
                    break
            self._count_request()
            page.wait_for_timeout(1200)
            text = page.evaluate("() => document.body.innerText.substring(0, 800)") or ""
            sig = block_signature(text)
            if sig in ("blocked", "captcha"):
                sh.set_state("ebay", "BLOCKED", note=f"polite adapter saw {sig} on '{query[:40]}' p{pno}", store=self.store,
                             probe_not_before=(_now() + timedelta(hours=72)).isoformat(timespec="seconds"))
                sh.add_event("ebay", "source_block_event", detail=f"{sig} query={query[:60]} page={pno}", store=self.store)
                raise SourceBlocked(f"{sig} on page {pno}")
            items = extract_listings(page) or []
            pages = pno
            dates = []
            for it in items:
                seen += 1
                title = it.get("title") or ""
                if should_exclude(title):
                    continue
                sd = parse_listing_sold_date(it.get("soldDate") or "")
                if sd:
                    dates.append(sd)
                if window_day and sd != window_day:
                    continue
                link = it.get("link") or ""
                if "/itm/" not in link:
                    continue
                grader_m = GRADER_KEYWORDS.search(title)
                rows.append({
                    "title": title, "sold_price": str(_price(it.get("priceText"))), "sold_date": it.get("soldDate") or "",
                    "condition": it.get("condition") or "", "bids": it.get("bids") or "", "shipping": it.get("shipping") or "",
                    "url": link, "image_url": it.get("imgUrl") or "", "best_offer": bool(it.get("bestOffer")),
                    "player_query": query, "query_label": query_label,
                    "grader": grader_m.group(1).upper() if grader_m else "",
                    "grade": _grade(title) if grader_m else "",
                    "source": "ebay_polite_v2", "scraped_at": _now().isoformat(timespec="seconds"),
                })
            if window_day and dates and all(d < window_day for d in dates):
                early_stop = True
                break
            if pno < max_pages:
                self.backoff()
        return {"rows": rows, "pages": pages, "seen": seen, "blocked": False, "early_stop": early_stop, "query": query,
                "requests": self.requests_this_run}

    # --- staging -----------------------------------------------------------------------------
    def stage(self, rows: list[dict], lane: str, query_id: str, window_day: str | None = None) -> str:
        tag = f"{lane}_{(window_day or 'nowin')}_{uuid.uuid4().hex[:8]}"
        path = os.path.join(self.staging_dir, f"{tag}.json")
        with open(path, "w") as f:
            json.dump(rows, f)
        meta = {"lane": lane, "query_id": query_id, "window_day": window_day, "rows": len(rows),
                "at": _now().isoformat(timespec="seconds"), "adapter_version": ADAPTER_VERSION}
        with open(path + ".meta.json", "w") as f:
            json.dump(meta, f)
        return path


def _price(text) -> float | None:
    from ebay_bulk_scraper import parse_price
    try:
        return parse_price(text or "")
    except Exception:
        return None


def _grade(title: str) -> str:
    import re
    m = re.search(r"\b(PSA|BGS|SGC|CGC|BCCG)\s*(10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b", title, re.IGNORECASE)
    return f"{m.group(1).upper()} {m.group(2)}" if m else ""
