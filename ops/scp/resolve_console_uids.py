#!/usr/bin/env python3
"""resolve_console_uids.py -- SCP console slug -> console uid (G\\d+), one page fetch each (2026-09-26).

The SCP identity library (M4 ~/mazidex_catalog_raw/scp_catalog) has ZERO consoles whose slug holds a
literal '&' (e.g. baseball-cards-2021-topps-allen-&-ginter): its sitemap capture dropped them. The
drip downloads a console's full card list by uid, so resolve the uids here, under the shared SCP
budget, and hand them to the drip. Resumable; unresolved slugs go to <out>.unresolved.
"""
import json, os, re, sys, time
sys.path.insert(0, os.path.expanduser("~/whatnot-sniper"))
from mazi_db.scripts.scp_scraper import _looks_blocked, acquire_scp_budget, fetch_scp_html, release_scp_budget  # noqa: E402

# The Jina-rendered CONSOLE page has no uid; every CARD page does: VGPC.console_uid = "G76754".
UID_RE = re.compile(r'VGPC\.console_uid\s*=\s*"(G\d+)"')
src, out = sys.argv[1], sys.argv[2]
done = {json.loads(l)["slug"] for l in open(out)} if os.path.exists(out) else set()
jobs = [j for j in json.load(open(src)) if j["console"] not in done]   # [{console, card_slug}]
ok = bad = 0
with open(out, "a") as fo, open(out + ".unresolved", "a") as fm:
    for j in jobs:
        s = j["console"]
        acquire_scp_budget()
        try:
            page = fetch_scp_html(f"https://www.sportscardspro.com/game/{j['card_slug']}", prefer="jina") or ""
        finally:
            release_scp_budget()
        uids = sorted(set(UID_RE.findall(page)))
        if page and not _looks_blocked(page) and len(uids) == 1:
            fo.write(json.dumps({"slug": s, "uid": uids[0]}) + "\n"); fo.flush(); ok += 1
            print(f"OK   {s} -> {uids[0]}", flush=True)
        else:
            why = "blocked" if not page or _looks_blocked(page) else f"uids={uids[:3]}"
            fm.write(json.dumps({"slug": s, "why": why}) + "\n"); fm.flush(); bad += 1
            print(f"MISS {s} ({why})", flush=True)
        time.sleep(3.5)
print(f"resolved {ok}, unresolved {bad}", flush=True)
