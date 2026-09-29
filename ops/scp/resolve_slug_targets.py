#!/usr/bin/env python3
"""resolve_slug_targets.py -- give slug-only SCP cards a REAL catalog id (2026-09-26).

scp_broad_scrub copies a target's `id` into every scraped row as `catalog_id`, and the
front end joins on it, so a card we only know by slug must never get a made-up id.
Fetch each card page ONCE under the shared SCP politeness budget (run with the driver's
MAZI_SCP_POLITENESS_LOCK) and read the id SCP publishes: `VGPC.product = { id: N }`.

  --slugs   JSONL of {"slug": ..., "n": ...}
  --out     target lines {id, console, product, slug, volume}; re-running resumes
Unresolved slugs (blocked / no id on the page) go to <out>.unresolved and are NOT guessed.
"""
import argparse, html, json, os, re, sys, time
from urllib.parse import unquote
sys.path.insert(0, os.path.expanduser("~/whatnot-sniper"))
from mazi_db.scripts.scp_scraper import (  # noqa: E402
    _looks_blocked, acquire_scp_budget, fetch_scp_html, release_scp_budget, scp_card_url,
)

ID_RE = re.compile(r"VGPC\.product\s*=\s*\{\s*id:\s*(\d+)")
NAME_RE = re.compile(r'<h1 id="product_name"[^>]*>\s*([^<]+?)\s*<')


def console_names(targets_path):
    """console slug -> display name, taken from cards we already target."""
    m = {}
    for line in open(targets_path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        s, c = d.get("slug") or "", d.get("console")
        if "/" in s and c:
            m.setdefault(s.split("/", 1)[0], c)
    return m


def console_from_page(page, cslug):
    """Console name for a set targets.jsonl has never seen (e.g. every "Allen & Ginter" set).
    The page links its console as '2022 Topps Allen & Ginter' (no sport); targets.jsonl names
    it 'Baseball Cards 2022 Topps Allen & Ginter', so prefix the sport from the slug."""
    norm = lambda h: unquote(html.unescape(h))
    sport = cslug.split("-cards-", 1)[0] + "-cards" if "-cards-" in cslug else None
    for href, text in re.findall(r'href="/console/([^"]+)"[^>]*>\s*([^<]+?)\s*<', page):
        if norm(href) == norm(cslug) and sport:
            return f"{sport.replace('-', ' ').title()} {html.unescape(text).strip()}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", required=True)
    ap.add_argument("--targets", required=True, help="targets.jsonl, for console names")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sleep", type=float, default=3.5)
    a = ap.parse_args()
    consoles = console_names(a.targets)
    done = set()
    if os.path.exists(a.out):
        done = {json.loads(l)["slug"] for l in open(a.out) if l.strip()}
    todo = [json.loads(l) for l in open(a.slugs) if l.strip()]
    ok = bad = 0
    with open(a.out, "a") as out, open(a.out + ".unresolved", "a") as miss:
        for t in todo:
            slug = t["slug"]
            if slug in done:
                continue
            acquire_scp_budget()
            try:
                page = fetch_scp_html(scp_card_url(slug), prefer="jina") or ""
            finally:
                release_scp_budget()
            cslug = slug.split("/", 1)[0]
            m, name = ID_RE.search(page), NAME_RE.search(page)
            console = consoles.get(cslug)
            if not console:
                console = console_from_page(page, cslug)
            if page and not _looks_blocked(page) and m and name and console:
                out.write(json.dumps({"id": m.group(1), "console": console,
                                      "product": html.unescape(name.group(1)).strip(), "slug": slug,
                                      "volume": t.get("n"), "added": "hot_missing_20260926"}) + "\n")
                out.flush()
                ok += 1
                print(f"[{ok + bad}/{len(todo)}] OK  {slug} -> {m.group(1)}", flush=True)
            else:
                why = ("blocked" if not page or _looks_blocked(page) else "no_id" if not m
                       else "no_name" if not name else "no_console")
                miss.write(json.dumps({"slug": slug, "why": why}) + "\n")
                miss.flush()
                bad += 1
                print(f"[{ok + bad}/{len(todo)}] MISS {slug} ({why})", flush=True)
            time.sleep(a.sleep)
    print(f"resolved {ok}, unresolved {bad}", flush=True)


if __name__ == "__main__":
    main()
