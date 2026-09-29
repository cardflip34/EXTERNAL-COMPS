#!/usr/bin/env python3
"""amp_console_backfill.py -- targets for SCP sets whose slug holds a literal '&' (2026-09-26).

The SCP identity library, and so targets.jsonl, has ZERO such sets (e.g. every Allen & Ginter before 2025;
SCP renamed the 2025 console "Allen and Ginter"). One console page per set, sorted by sales volume, lists
the top 150 cards with their REAL SCP ids -- enough to backfill the cards that matter. Fetches go through
the shared SCP budget. Resumable. Output: target lines {id, console, product, slug, volume_rank, added}
plus the console uid (for the library drip, full lists later).
"""
import html, json, os, re, sys, time
sys.path.insert(0, os.path.expanduser("~/whatnot-sniper"))
from mazi_db.scripts.scp_scraper import _looks_blocked, acquire_scp_budget, fetch_scp_html, release_scp_budget  # noqa: E402

src, out = sys.argv[1], sys.argv[2]
done = {json.loads(l)["console_slug"] for l in open(out + ".consoles")} if os.path.exists(out + ".consoles") else set()
todo = [s for s in json.load(open(src)) if s not in done]
ok = bad = cards = 0
for cs in todo:
    acquire_scp_budget()
    try:
        h = fetch_scp_html(f"https://www.sportscardspro.com/console/{cs}?sort=volume", prefer="jina") or ""
    finally:
        release_scp_budget()
    uid = re.search(r'VGPC\.console_uid\s*=\s*"(G\d+)"', h)
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", h, re.S)
    name = html.unescape(re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", h1.group(1)))).strip() if h1 else ""
    sport = cs.split("-cards-", 1)[0].replace("-", " ").title() + " Cards" if "-cards-" in cs else None
    name = re.sub(r"^Prices for ", "", name)
    if sport and name.endswith(" " + sport):
        name = name[: -len(sport) - 1]
    rows = re.findall(r'(<tr[^>]*id="product-(\d+)".*?</tr>)', h, re.S)
    if not h or _looks_blocked(h) or not rows or not sport or not name:
        with open(out + ".unresolved", "a") as f:
            f.write(json.dumps({"console_slug": cs, "why": "blocked" if not h or _looks_blocked(h) else "no_rows"}) + "\n")
        bad += 1; print(f"MISS {cs}", flush=True); time.sleep(3.5); continue
    n = 0
    with open(out, "a") as f:
        for rank, (block, pid) in enumerate(rows, 1):
            hrefs = re.findall(r'href="[^"]*?/game/([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            slug = next((html.unescape(u) for u, _ in hrefs), None)
            prod = next((html.unescape(re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", t))).strip() for _, t in hrefs
                         if re.sub("<[^>]+>", "", t).strip()), None)
            if slug and prod and slug.split("/", 1)[0] == cs:
                f.write(json.dumps({"id": pid, "console": f"{sport} {name}", "product": prod, "slug": slug,
                                    "volume_rank": rank, "added": "amp_backfill_20260926"}) + "\n"); n += 1
    with open(out + ".consoles", "a") as f:
        f.write(json.dumps({"console_slug": cs, "uid": uid.group(1) if uid else None, "console": f"{sport} {name}", "cards": n}) + "\n")
    ok += 1; cards += n
    print(f"OK   {cs} uid={uid.group(1) if uid else None} cards={n}", flush=True)
    time.sleep(3.5)
print(f"consoles ok {ok}, missed {bad}, cards {cards}", flush=True)
