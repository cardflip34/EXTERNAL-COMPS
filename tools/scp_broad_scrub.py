#!/usr/bin/env python3
"""
scp_broad_scrub.py — broad SportsCardsPro historical sold-comp harvester.
=========================================================================
Feeds the External Comps moat with 2023-2025 sold rows from the FULL SCP catalog
(9.0M sports-card identities), not just the ~1.3K Trusted cards the deep-scrub
touches. This is the "millions of 2025 comps" engine.

Pipeline per catalog target (ranked by PriceCharting `sales-volume`):
  1. Construct the /game/<slug> URL DIRECTLY from console-name+product-name
     (slug = slugify(console)/slugify(product); verified deterministic against
     SCP's own slugs 2026-07-17) -> ZERO resolution fetches, no scoring failures.
  2. Fetch the card-page ledger (Playwright-stealth -> Jina fallback, reusing
     scp_scraper.fetch_scp_html). One fetch/card. If the constructed slug yields
     0 sales / looks blocked, fall back to /offers?product=<id> to read the REAL
     slug off the page, retry once (handles accents/&/edge slugs).
  3. parse_scp_ledger -> ScpSale rows (~30/grade-bucket, all years/grades).
  4. Keep sales in a CUSTOM historical window [FLOOR, CEILING) -- default
     2023-01-01 .. 2026-01-01 so SCP never double-counts the 2026 eBay-direct
     corpus (SCP + ebay source_codes do NOT cross-dedup). This window is OWNED
     here, decoupled from the deep-scrub's shared MAZI_SCP_EBAY_BOUNDARY_DAYS env.

Scrape lands LOCAL JSONL (idempotent: source_item_id + seen-target watermark).
Reference-only comps (source_code 'sportscardspro'); NEVER Whatnot identity/proof,
NEVER Trusted/Mazified. Bridge to Neon is a SEPARATE approved step.

Usage:
  python3 scp_broad_scrub.py --targets targets.jsonl --limit 300 --dry-run
  python3 scp_broad_scrub.py --targets targets.jsonl --limit 5000 --prefer playwright
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Optional

ROOT = Path(os.environ.get("MAZI_REPO_ROOT", os.path.expanduser("~/whatnot-sniper")))
sys.path.insert(0, str(ROOT))

from mazi_db.scripts.scp_scraper import (  # noqa: E402
    ExternalRow,
    ScpSale,
    _fold_grade_into_title,
    _looks_blocked,
    acquire_scp_budget,
    fetch_scp_html,
    parse_scp_ledger,
    release_scp_budget,
    scp_card_url,
    scp_source_item_id,
)

SCP_BASE_URL = "https://www.sportscardspro.com"
DEFAULT_FLOOR = date(2023, 1, 1)
DEFAULT_CEILING = date(2026, 1, 1)  # exclusive; keeps <= 2025-12-31 (dedup-safe vs eBay 2026)


# ── slug construction (verified deterministic 2026-07-17) ────────────────────
def slugify(s: str) -> str:
    """SCP slug rule: Unicode-fold (NFKD -> ASCII, so 'José Ramírez' -> 'jose-ramirez'),
    lowercase, every run of non-alphanumerics -> single hyphen, strip leading/trailing
    hyphens. Verified against SCP's own slugs:
      'Baseball Cards 1989 Upper Deck'          -> 'baseball-cards-1989-upper-deck'
      'Ken Griffey Jr. #1'                       -> 'ken-griffey-jr-1'
      'Ken Griffey Jr. [Star Rookie] #1'         -> 'ken-griffey-jr-star-rookie-1'
      'Victor Wembanyama #136'                   -> 'victor-wembanyama-136'
    """
    folded = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def catalog_slug(console_name: str, product_name: str) -> str:
    """'<console-name>/<product-name>' -> '<set-slug>/<player-slug>'."""
    return f"{slugify(console_name)}/{slugify(product_name)}"


def offers_url(product_id: str) -> str:
    return f"{SCP_BASE_URL}/offers?product={str(product_id).strip()}"


_GAME_SLUG_RE = re.compile(r"/game/([a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*)", re.IGNORECASE)


def slug_from_offers_html(html: str) -> str:
    """Fallback: read the canonical /game/<set>/<player> slug off an /offers?product=
    page (the 'See Historic Prices' link). '' when none present / blocked."""
    if not html or _looks_blocked(html):
        return ""
    m = _GAME_SLUG_RE.search(html)
    return m.group(1).lower() if m else ""


# ── row building (custom historical window; decoupled from deep-scrub env) ───
def in_window(d: Optional[date], floor: date, ceiling: date,
              exclude_from: Optional[date] = None,
              exclude_to: Optional[date] = None) -> bool:
    """Keep iff floor <= d < ceiling AND d is NOT inside the excluded band
    [exclude_from, exclude_to).

    The excluded band is the cross-source dedup boundary: it carves out the range
    eBay-direct already covers, letting ONE pass collect both deep history and the
    live post-block window. With exclude_from/exclude_to unset this is byte-identical
    to the original half-open [floor, ceiling) behavior."""
    if d is None or not (floor <= d < ceiling):
        return False
    if exclude_from is not None and exclude_to is not None and exclude_from <= d < exclude_to:
        return False
    return True


def broad_row_from_sale(
    sale: ScpSale, slug: str, scp_url: str, floor: date, ceiling: date,
    exclude_from: Optional[date] = None, exclude_to: Optional[date] = None,
) -> Optional[ExternalRow]:
    """One SCP ledger sale -> ExternalRow, filtered to [floor, ceiling). Reuses the
    SAME idempotent source_item_id + grade-folded title the deep-scrub emits, so a
    later re-scrape / the deep-scrub hit ON CONFLICT instead of duplicating. No
    Identity synthesis (broad scrub has no Trusted identity) -> raw grade-fold."""
    if not in_window(sale.sold_date, floor, ceiling, exclude_from, exclude_to):
        return None
    source_item_id = scp_source_item_id(slug, sale.ledger_anchor)
    title = _fold_grade_into_title(sale.title, sale.grade_label)
    raw = {
        "source": "sportscardspro",
        "slug": slug,
        "ledger_anchor": sale.ledger_anchor,
        "grade_label": sale.grade_label,
        "image_url": sale.image_url,
        "scp_original_title": sale.title,
        "soldDate": sale.sold_date.isoformat(),
        "best_offer": False,
        "broad_scrub": True,
    }
    return ExternalRow(
        title=title,
        sold_price=sale.sold_price,
        sold_date=sale.sold_date,
        source_item_id=source_item_id,
        source_url=scp_url,
        image_url=sale.image_url,
        best_offer=False,
        query_variant="scp_broad_ledger",
        query_text=slug,
        price_range="scp_history",
        raw=raw,
    )


# ── per-target scrape (construct -> fetch -> parse -> window) ─────────────────
def scrape_target(
    target: dict,
    floor: date,
    ceiling: date,
    fetch: Callable[[str], str],
    prefer: str = "playwright",
    exclude_from: Optional[date] = None,
    exclude_to: Optional[date] = None,
    band_from: Optional[date] = None,
    band_to: Optional[date] = None,
) -> dict:
    """Scrape ONE catalog target. Returns a summary dict incl. window-filtered
    ExternalRows under 'rows'. Fetch is injectable for tests.

    band_from/band_to (plan 1.7 4b): ALSO collect the sales that fall in
    [band_from, band_to) into summary['band_rows'], for a side file the bridge does
    not read. This is additive by construction -- 'rows' is built from the exact same
    call as before -- so a replay with and without the band flags yields identical
    main-JSONL kept counts. That equality is the gate; see --band-out."""
    console = target["console"]
    product = target["product"]
    pid = str(target.get("id", ""))
    slug = target.get("slug") or catalog_slug(console, product)
    summary = {
        "id": pid, "slug": slug, "console": console, "product": product,
        "blocked": False, "resolved": True, "sales_parsed": 0,
        "rows_in_window": 0, "fallback": False, "rows": [], "band_rows": [],
    }

    url = scp_card_url(slug)
    html = fetch(url)
    if _looks_blocked(html):
        summary["blocked"] = True
        return summary
    sales = parse_scp_ledger(html)

    # Fallback: constructed slug returned an empty/404 page -> resolve via /offers.
    if not sales and pid:
        offers_html = fetch(offers_url(pid))
        real_slug = slug_from_offers_html(offers_html)
        if real_slug and real_slug != slug:
            summary["fallback"] = True
            slug = real_slug
            summary["slug"] = slug
            url = scp_card_url(slug)
            html = fetch(url)
            if _looks_blocked(html):
                summary["blocked"] = True
                return summary
            sales = parse_scp_ledger(html)

    if not sales:
        summary["resolved"] = False
        return summary

    summary["sales_parsed"] = len(sales)
    rows = [r for s in sales
            if (r := broad_row_from_sale(s, slug, url, floor, ceiling,
                                         exclude_from, exclude_to)) is not None]
    summary["rows_in_window"] = len(rows)
    if band_from is not None and band_to is not None:
        # Same builder, own window, no exclusion: the band is a SEPARATE product of the
        # same fetch. Nothing here can change `rows` above.
        summary["band_rows"] = [r for r in
                                (broad_row_from_sale(sale, slug, url, band_from, band_to, None, None)
                                 for sale in sales) if r is not None]
    summary["rows"] = rows
    return summary


# ── persistent-browser fetcher (opt-in: --persistent-browser) ────────────────
class PersistentFetcher:
    """One chromium reused across fetches.

    Beats the shared per-call _fetch_via_playwright on two axes: it drops ~1-1.5s of
    browser-launch overhead per card, and it KEEPS cookies between pages so a
    Cloudflare clearance earned once is reused instead of re-challenged on every
    card. Recycles every ``recycle_after`` fetches to bound memory, and falls back to
    the shared fetch_scp_html on any error so a browser fault degrades gracefully."""

    def __init__(self, render_wait: float = 2.5, timeout_ms: int = 30000,
                 recycle_after: int = 250):
        self.render_wait = render_wait
        self.timeout_ms = timeout_ms
        self.recycle_after = recycle_after
        self._pw = self._browser = self._ctx = self._page = None
        self._n = 0
        self.launches = 0
        self.fallbacks = 0

    def _launch(self):
        from playwright.sync_api import sync_playwright
        try:
            from ebay_bulk_scraper import USER_AGENTS
            ua = USER_AGENTS[0]
        except Exception:
            ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])
        self._ctx = self._browser.new_context(
            user_agent=ua, viewport={"width": 1440, "height": 900})
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self._page = self._ctx.new_page()
        self._n = 0
        self.launches += 1

    def close(self):
        for obj, meth in ((self._page, "close"), (self._ctx, "close"),
                          (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._pw = self._browser = self._ctx = self._page = None

    def fetch(self, url: str) -> str:
        try:
            if self._page is None or self._n >= self.recycle_after:
                self.close()
                self._launch()
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._page.wait_for_timeout(int(self.render_wait * 1000))
            self._n += 1
            return self._page.content()
        except Exception as e:
            # Browser fault -> drop it and let the shared path (playwright->jina)
            # answer this one. Never let a browser problem look like a site block.
            print(f"[persistent-browser fallback] {type(e).__name__}: {str(e)[:120]}", flush=True)
            self.fallbacks += 1
            self.close()
            try:
                return fetch_scp_html(url, prefer="playwright")
            except Exception:
                return ""


# ── local JSONL persistence + resume watermark ───────────────────────────────
def row_to_jsonl(pid: str, r: ExternalRow) -> str:
    return json.dumps({
        "catalog_id": pid,
        "source_item_id": r.source_item_id,
        "title": r.title,
        "sold_price": str(r.sold_price),
        "sold_date": r.sold_date.isoformat(),
        "source_url": r.source_url,
        "image_url": r.image_url,
        "slug": r.query_text,
        "raw": r.raw,
    }, default=str)


def load_seen(state_file: Path) -> set:
    if state_file.exists():
        try:
            return set(json.loads(state_file.read_text()).get("done_ids", []))
        except Exception:
            pass
    return set()


def save_seen(state_file: Path, done: set) -> None:
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done_ids": sorted(done)}))
    tmp.replace(state_file)


def iter_targets(path: Path) -> Iterator[dict]:
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def run(args) -> int:
    floor = datetime.strptime(args.floor, "%Y-%m-%d").date() if args.floor else DEFAULT_FLOOR
    ceiling = datetime.strptime(args.ceiling, "%Y-%m-%d").date() if args.ceiling else DEFAULT_CEILING
    exclude_from = datetime.strptime(args.exclude_from, "%Y-%m-%d").date() if args.exclude_from else None
    exclude_to = datetime.strptime(args.exclude_to, "%Y-%m-%d").date() if args.exclude_to else None
    band_out = getattr(args, "band_out", None)
    band_from = datetime.strptime(args.band_from, "%Y-%m-%d").date() if getattr(args, "band_from", None) else exclude_from
    band_to = datetime.strptime(args.band_to, "%Y-%m-%d").date() if getattr(args, "band_to", None) else exclude_to
    if band_out and not (band_from and band_to):
        raise SystemExit("--band-out needs a band: pass --band-from/--band-to, or --exclude-from/--exclude-to")
    if not band_out:
        band_from = band_to = None          # no side file -> do not even collect
    targets_path = Path(args.targets)
    out_path = Path(args.out)
    state_path = Path(args.state)
    done = load_seen(state_path)

    pf = None
    if args.persistent_browser and args.prefer == "playwright":
        pf = PersistentFetcher(render_wait=args.render_wait)
        fetch = pf.fetch
    else:
        fetch = lambda u: fetch_scp_html(u, prefer=args.prefer)  # noqa: E731

    processed = kept = blocked = resolved = fallbacks = 0
    streak = 0                                    # consecutive blocked fetches (drives the backoff)
    year_counts: dict[str, int] = {}
    t0 = time.time()
    out_fh = None if args.dry_run else open(out_path, "a")
    band_fh = None
    if band_out and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(band_out)), exist_ok=True)
        band_fh = open(band_out, "a")
    band_kept = 0
    try:
        for target in iter_targets(targets_path):
            if processed >= args.limit:
                break
            pid = str(target.get("id", ""))
            if pid in done:
                continue

            acquire_scp_budget()
            try:
                summary = scrape_target(target, floor, ceiling, fetch, prefer=args.prefer,
                                        exclude_from=exclude_from, exclude_to=exclude_to,
                                        band_from=band_from, band_to=band_to)
            finally:
                release_scp_budget()

            processed += 1
            if summary["blocked"]:
                blocked += 1
                streak += 1
                # Escalate: a flat 30 s let a dead transport (Jina key at HTTP 402, 2026-09-25) burn
                # 60-85% of requests for days. Doubling per consecutive block, capped, reset on the
                # first good fetch, keeps a real SCP block from hardening the way eBay's did.
                wait = min(args.block_backoff * (2 ** (streak - 1)), args.block_backoff_max)
                print(f"[{processed}] BLOCKED {summary['slug']} — backoff {wait:.0f}s (streak {streak})", flush=True)
                time.sleep(wait)
                continue
            streak = 0
            if summary["resolved"]:
                resolved += 1
            if summary["fallback"]:
                fallbacks += 1
            rows = summary["rows"]
            kept += len(rows)
            for r in rows:
                year_counts[r.sold_date.isoformat()[:4]] = year_counts.get(r.sold_date.isoformat()[:4], 0) + 1
                if out_fh:
                    out_fh.write(row_to_jsonl(pid, r) + "\n")
            if out_fh:
                out_fh.flush()
            for r in summary["band_rows"]:
                band_kept += 1
                if band_fh:
                    band_fh.write(row_to_jsonl(pid, r) + "\n")
            if band_fh:
                band_fh.flush()
            done.add(pid)
            if not args.dry_run and processed % 25 == 0:
                save_seen(state_path, done)
            if args.verbose or processed <= 12 or processed % 50 == 0:
                print(f"[{processed}] {summary['slug']} parsed={summary['sales_parsed']} "
                      f"kept={len(rows)}{' (fallback)' if summary['fallback'] else ''}", flush=True)
            time.sleep(args.sleep)
    finally:
        if out_fh:
            out_fh.close()
        if band_fh:
            band_fh.close()
        if pf is not None:
            print(f"[persistent-browser] launches={pf.launches} fallbacks={pf.fallbacks}", flush=True)
            pf.close()
        if not args.dry_run:
            save_seen(state_path, done)

    dt = time.time() - t0
    print("\n=== SCP BROAD SCRUB SUMMARY ===", flush=True)
    print(f"processed={processed} resolved={resolved} fallbacks={fallbacks} blocked={blocked}", flush=True)
    band = f" EXCLUDING [{exclude_from} .. {exclude_to})" if exclude_from and exclude_to else ""
    print(f"rows_kept={kept} (window [{floor} .. {ceiling}){band})  by_year={dict(sorted(year_counts.items()))}", flush=True)
    if band_out:
        print(f"band_kept={band_kept} (band [{band_from} .. {band_to}) -> {band_out})", flush=True)
    print(f"elapsed={dt:.0f}s  rate={processed/dt:.2f} cards/s" if dt else "", flush=True)
    if not args.dry_run:
        print(f"out={out_path}  state={state_path}  total_done={len(done)}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", required=True, help="JSONL of {id, console, product[, slug, volume]}")
    ap.add_argument("--out", default="scp_broad_comps.jsonl")
    ap.add_argument("--state", default="scp_broad_state.json")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--floor", default=None, help="YYYY-MM-DD inclusive (default 2023-01-01)")
    ap.add_argument("--ceiling", default=None, help="YYYY-MM-DD exclusive (default 2026-01-01)")
    ap.add_argument("--exclude-from", default=None,
                    help="YYYY-MM-DD inclusive start of an EXCLUDED band (cross-source dedup boundary)")
    ap.add_argument("--exclude-to", default=None,
                    help="YYYY-MM-DD exclusive end of the excluded band")
    ap.add_argument("--band-out", default=None,
                    help="also write sales inside [--band-from, --band-to) to this side JSONL "
                         "(plan 1.7 4b). The main --out stream is byte-identical with or without it.")
    ap.add_argument("--band-from", default=None, help="YYYY-MM-DD inclusive; defaults to --exclude-from")
    ap.add_argument("--band-to", default=None, help="YYYY-MM-DD exclusive; defaults to --exclude-to")
    ap.add_argument("--prefer", choices=("playwright", "jina"), default="playwright")
    ap.add_argument("--sleep", type=float, default=1.2, help="politeness pause between cards (s)")
    ap.add_argument("--block-backoff", type=float, default=60.0, help="first pause after a blocked fetch (s); doubles per consecutive block")
    ap.add_argument("--block-backoff-max", type=float, default=900.0, help="cap for the escalating pause (s)")
    ap.add_argument("--dry-run", action="store_true", help="no JSONL write; still fetches + reports")
    ap.add_argument("--persistent-browser", action="store_true",
                    help="reuse ONE chromium across fetches (faster + keeps CF cookies)")
    ap.add_argument("--render-wait", type=float, default=2.5,
                    help="seconds to let the page render after domcontentloaded")
    ap.add_argument("--verbose", action="store_true")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
