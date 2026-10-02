#!/usr/bin/env python3
"""headline_sales.py -- MAZI as the source of truth for HEADLINE card sales ($100K+, every $1M+). 2026-09-29, EXTERNAL COMPS.

Andy: "make sure mazi is the source of truth for all external comps, including the HUGE sales ... many are not on ebay or
whatnot ... make sure it follows our MAZI ID system". This builds ONE canonical record per real sale, from every source:

  fanatics_api  Fanatics Collect's public sales-history API at priceMin (Premier + Weekly + fixed) -- the refresh used to miss
                Premier closes (Flagg $8.04M), so this reads the API directly
  neon          external_transactions rows at/above the floor from goldin / fanatics / rea / heritage / ebay (never Best Offer:
                its price is the list price, context only)
  seed          press/tracker-reported sales (headline_seed_*.json) -- the ONLY source for private sales and Alt

Reports of the same sale merge: same venue id, or same price (+/-1%) and player and a date within the window (3 days,
or the same month when only the month is public). Estimates, "could reach", asking prices never enter.

Identity: year, card code (#DPA-CF), serial (1/1), grade and player (checked against the catalog's player keys) are read
from the title; MAZI ID candidates come from the beta catalog (read-only). Nothing is minted or published here -- see
the plan (docs/HEADLINE_SALES_PLAN_20260929.md): table + mint + export are separate, approved steps.

  python3 tools/headline_sales.py report --min-price 100000 --since 2025-01-01    # read-only; writes JSON + markdown
  python3 tools/test_headline_sales.py                                            # parsing + same-sale tests, no DB
"""
from __future__ import annotations

import argparse, json, os, re, sys, time, unicodedata
from html import unescape
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.expanduser("~/whatnot-sniper"))      # the Mini's project (Fanatics crawler) when run from staging
OUT = os.path.expanduser("~/mazi_headline")
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "headline_seed_2026.json")
IMAGES = "/Volumes/MAZI_EVIDENCE_6TB/comp_images"             # lot photos: comp_images/lotphoto_<house>/<source id>/01.jpg
# hugginsandscott / memorylane / lelands: the archive scrubs of 2026-09-29..10-01 (46 sales of $100K+ since 2025 were
# in Neon but never read here)
NEON_SOURCES = ["goldin", "fanatics", "rea", "heritage", "ebay", "hugginsandscott", "memorylane", "lelands"]
AUCTION_HOUSES = ("goldin", "heritage", "rea", "hugginsandscott", "memorylane", "lelands")

YEAR_RE = re.compile(r"\b(19[0-9]{2}|20[0-4][0-9])(?:[-/](\d{2}))?\b")
CODE_RE = re.compile(r"#\s*([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)")
SERIAL_RE = re.compile(r"(?<![#\w])(\d{1,3})\s*/\s*(\d{1,4})\b")   # "1/1", "05/10" -- never a card number: "#78 /99"
RUN_RE = re.compile(r"/\s*(\d{1,4})\b")                                  # "/23", "#78/99" (no serial): print run only
# a T206-era back is "<brand> <series>/<factory>" ("Sweet Caporal 350/30", "Piedmont 150/25"): not a serial or a print run
# (1,616 Memory Lane and 96 Goldin titles, 2026-10-01)
BACK_RE = re.compile(r"\b(?:sweet\s+caporal|piedmont|sovereign|polar\s+bear|old\s+mill|hindu|tolstoi|american\s+beauty|"
                     r"carolina\s+brights|cycle|broad\s*leaf|uzit|lenox|drum)\s+\d{2,3}\s*/\s*\d{2,3}\b", re.I)
# up to three descriptor words between grader and number: "SGC NM+ 7.5", "PSA EX-MT 6", "PSA Good 2", "BGS GEM MINT 9.5"
# (the first version stopped at vintage descriptors and held 44 real $100K+ sales as "no grade", 2026-09-29)
GRADE_RE = re.compile(r"\b(PSA|BGS|SGC|CGC)\s*(?:[A-Za-z][A-Za-z+\-]*\.?\s+){0,3}"
                      r"(10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5\.5|5|4\.5|4|3\.5|3|2\.5|2|1\.5|1)(?![\d.]*\d)", re.I)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z.'\-]*")
PARALLEL_WORDS = {"superfractor", "gold", "red", "orange", "black", "blue", "green", "purple", "pink", "logoman", "logo", "shield",
                  "platinum", "masterpiece", "precious", "gems", "refractor", "padparadscha", "rookie", "patch", "auto", "autograph",
                  "dual", "debut", "mvp", "dynasty", "exquisite", "treasures"}


def fold(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_title(t):
    y = YEAR_RE.search(t or "")
    code = CODE_RE.search(t or "")
    nb = BACK_RE.sub(" ", t or "")
    ser = SERIAL_RE.search(nb)
    run = None if ser else RUN_RE.search(nb)
    g = GRADE_RE.search(t or "")
    return {"year": int(y.group(1)) if y else None, "code": code.group(1) if code else None,
            "code_key": fold(code.group(1)) if code else None,
            "serial": f"{ser.group(1)}/{ser.group(2)}" if ser else None, "print_run": int(ser.group(2)) if ser else (int(run.group(1)) if run else None),
            "grade": f"{g.group(1).upper()} {g.group(2)}" if g else None}


def player_windows(t):
    words = [w.strip(".'-") for w in WORD_RE.findall(t or "") if w[0].isupper() or w.isupper()]
    out = set()
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            out.add(fold(" ".join(words[i:i + n])))
    return out


# ------------------------------------------------------------------------------------------------ collect
FANATICS_DEPTH = 999        # the sales-history API returns at most 999 rows per query, whatever the page size (probed 2026-09-29)


def collect_fanatics(min_price, since, sleep=3.0):
    """Every Fanatics sale >= min_price sold on/after `since`, newest first. Read in price bands (priceMin inclusive,
    priceMax exclusive) so no query passes the API's 999-row depth; a band that still hits it is reported, never cut
    silently."""
    import fanatics_full_catalog_scraper_v3 as F
    edges = sorted({int(min_price)} | {e for e in (250_000, 1_000_000) if e > min_price})
    rows = []
    for lo, hi in zip(edges, edges[1:] + [None]):
        q = {"priceMin": lo, **({"priceMax": hi} if hi else {}), "sort": "soldDate,desc", "size": 50}
        got, page, reached_since = 0, 0, False
        while True:
            recs = F.records_from(F.request_json({**q, "page": page}))
            if not recs:
                break
            for r in recs:
                sd = (r.get("soldDate") or "")[:10]
                if sd and sd < since:
                    reached_since = True; continue
                if r.get("isComplete") is False:
                    continue                       # voided
                pay = r.get("paymentStatus")
                upd = (r.get("updatedAt") or "")[:10]
                # Unpaid alone is a capture-time snapshot (MAZIDEX 2026-10-01); unpaid 7+ days with no update = non-payment
                if pay == "Unpaid" and sd and upd == sd and (date.today() - date.fromisoformat(sd)).days >= 7:
                    continue
                rows.append({"src": "fanatics_api", "venue": "fanatics", "sale_type": (r.get("auctionType") or "").lower() or "unknown",
                             "source_id": r.get("id"), "title": r.get("title"), "price": float(r.get("purchasePrice") or 0),
                             "date": sd, "prec": "day", "url": f"https://sales-history.fanaticscollect.com/?id={r.get('id')}",
                             "grade_src": " ".join(str(x) for x in (r.get("gradingService"), r.get("grade")) if x not in (None, "")) or None,
                             "category": r.get("category"), "image": r.get("mediumImage1"), "payment": pay})
            got += len(recs); page += 1
            if reached_since:
                break
            time.sleep(sleep)
        if not reached_since and got >= FANATICS_DEPTH:
            print(f"[warn] fanatics band ${lo:,}-{hi or 'inf'}: hit the API's {FANATICS_DEPTH}-row depth before {since} -- "
                  f"add a band edge", flush=True)
    return rows


def collect_neon(min_price, since):
    import psycopg
    with psycopg.connect(os.environ["MAZI_DB_URL"], connect_timeout=20) as c:
        c.execute("SET statement_timeout='300s'")
        out = []
        for i, s, sid, t, p, d, bo, url, lot_id, pin, img in c.execute(
                """SELECT id, source_code, source_item_id, title, sold_price, sold_date, best_offer,
                          -- Goldin rows keep the lot URL in raw.url only (75 of 92 loaded Goldin rows had no source link)
                          coalesce(canonical_source_url, source_url, raw->>'url'),
                          raw->>'lot_id', raw->>'primary_image_name', raw->>'image_url'
                   FROM public.external_transactions WHERE sold_price >= %s AND sold_date >= %s AND source_code = ANY(%s)
                     AND NOT coalesce(best_offer, false)
                     -- Fanatics buy-now LISTINGS (asking prices, e.g. a "$1,000,000" Eevee PSA 10) were imported as sales
                     -- in June; 5 reached the beta in the 2026-09-30 canary. A listing is not a sale.
                     AND coalesce(canonical_source_url, source_url, raw->>'url', '') NOT LIKE '%%/buy-now/%%'""",
                (min_price, since, NEON_SOURCES)):
            image = ("https://d2tt46f3mh26nl.cloudfront.net/public/Lots/%s/%s@2x" % (lot_id, pin)) if (s == "goldin" and lot_id and pin) else img
            out.append({"src": "neon", "venue": s, "sale_type": "auction" if s in AUCTION_HOUSES else "unknown",
                        # Memory Lane / Lelands titles were stored HTML-escaped ("Stars &amp; Rookies", 2,041 rows)
                        "source_id": sid, "neon_id": i, "title": unescape(t or ""), "price": float(p), "date": d.isoformat(), "prec": "day", "url": url,
                        "image": image})
        c.rollback()
    return out


def collect_seed(path=SEED):
    d = json.load(open(path))
    return [{"src": "seed", "venue": s["venue"], "sale_type": s["sale_type"], "source_id": None, "title": s["title"],
             "price": float(s["price"]), "date": s["date"], "prec": s["prec"], "url": d["sources"].get(s["source"]),
             "broker": s.get("broker")} for s in d["sales"]]


# ------------------------------------------------------------------------------------------------ canonicalize
def same_sale(a, b, players):
    if abs(a["price"] - b["price"]) > 0.01 * max(a["price"], b["price"]):
        return False
    da, db = date.fromisoformat(a["date"]), date.fromisoformat(b["date"])
    if "month" in (a["prec"], b["prec"]):
        if (da.year, da.month) != (db.year, db.month):
            return False
    elif abs((da - db).days) > 3:
        return False
    pa, pb = players.get(id(a), set()), players.get(id(b), set())
    if pa and pb:
        return bool(pa & pb)
    ta = {w.lower() for w in WORD_RE.findall(a["title"] or "") if len(w) > 2}
    tb = {w.lower() for w in WORD_RE.findall(b["title"] or "") if len(w) > 2}
    return len(ta & tb) / max(len(ta | tb), 1) >= 0.3          # no player read on one side: the titles must agree


def canonicalize(rows, players):
    groups, by_key = [], {}
    for r in sorted(rows, key=lambda r: ({"fanatics_api": 0, "neon": 1, "seed": 2}[r["src"]], -r["price"])):
        key = (r["venue"], r["source_id"]) if r["source_id"] else None
        g = by_key.get(key) if key else None
        if g is None:
            g = next((g for g in groups if same_sale(g["rows"][0], r, players)), None)
        if g is None:
            g = {"rows": []}; groups.append(g)
        g["rows"].append(r)
        if key:
            by_key[key] = g
    out = []
    for g in groups:
        best = min(g["rows"], key=lambda r: ({"fanatics_api": 0, "neon": 1, "seed": 2}[r["src"]], r["prec"] != "day"))
        venue = next((r["venue"] for r in g["rows"] if r["venue"] not in ("unknown", "private")), best["venue"])
        lot = next((r for r in g["rows"] if r["src"] in ("fanatics_api", "neon") and r["venue"] in ("goldin", "fanatics")
                    and r.get("source_id")), None)
        local = None
        if lot and os.path.isfile(os.path.join(IMAGES, "lotphoto_" + lot["venue"], str(lot["source_id"]), "01.jpg")):
            local = "/img/lotphoto_%s/%s" % (lot["venue"], lot["source_id"])
        out.append({"title": best["title"], "price": best["price"], "date": best["date"], "prec": best["prec"], "venue": venue,
                    "payment": best.get("payment"),
                    # the lot's own front photo (display art only, CLAUDE.md 2026-10-01): origin URL + our served copy
                    "lot_image_url": (lot or {}).get("image"), "lot_image_local": local,
                    "sale_type": best["sale_type"], "sources": [{k: r.get(k) for k in ("src", "venue", "source_id", "neon_id", "url", "price", "date")}
                                                                 for r in g["rows"]],
                    **parse_title(best["title"]), "player_keys": sorted(players.get(id(best), set()))})
    return out


# ------------------------------------------------------------------------------------------------ identity
def beta():
    import psycopg
    pw = open(os.path.expanduser("~/private/beta-password")).read().strip()
    c = psycopg.connect(host="aws-0-us-west-1.pooler.supabase.com", port=5432, user="postgres.jzxgtvxcuukxqkbwbuxg", dbname="postgres",
                        password=pw, sslmode="require", connect_timeout=15)
    c.execute("SET statement_timeout='60s'"); c.execute("SET default_transaction_read_only=on")
    return c


def known_players(b, rows):
    cands = {r_id: player_windows(r["title"]) for r_id, r in ((id(r), r) for r in rows)}
    allk = sorted(set().union(*cands.values())) if cands else []
    real = set()
    for i in range(0, len(allk), 5000):
        real |= {x[0] for x in b.execute("SELECT DISTINCT player_key FROM public.beta_catalog WHERE player_key = ANY(%s)", (allk[i:i + 5000],))}
    return {k: {p for p in v if p in real} for k, v in cands.items()}


# --- the match test (2026-09-29): a first review found ~1 in 4 "matched" rows wrong -- a Donruss Kaboom card matched to
# Select (same player, year and #7), SuperFractor / Green 1/1 sales matched to base cards, a 2018 Bowman Chrome
# Superfractor matched to 2019 Topps Archives. Player + year + card number is not an identity; set and parallel must agree.
SET_STOP = {"cards", "card", "trading", "basketball", "baseball", "football", "hockey", "soccer", "the", "and", "of", "a", "set"}
BRANDS = {"panini", "topps", "upper", "deck", "ud", "pokemon", "japanese", "english", "skybox", "fleer"}      # titles often leave these out
PARALLEL_TOKENS = {"superfractor", "refractor", "xfractor", "gold", "red", "orange", "black", "blue", "green", "purple", "pink",
                   "silver", "platinum", "nebula", "shimmer", "wave", "mojo", "atomic", "sapphire", "padparadscha", "emerald",
                   "ruby", "cracked", "ice", "hyper", "power", "disco", "camo", "tiger", "zebra", "snakeskin", "laser", "lazer",
                   "velocity", "rainbow", "sepia", "negative", "aqua", "teal", "bronze", "copper", "yellow", "lime", "magenta",
                   "fuchsia", "sparkle", "geometric", "precious", "gems"}
# (2026-10-01 MAZIDEX audit: Gold SPARKLE /24 was filed on Gold /10, a White GEOMETRIC on Refractor Gold, a Championship
#  Precious Metal Gems /50 on the base card)                       # not "white": T206 "White Border" is the set
# product lines: a title naming one the card's set/parallel doesn't (1975 Topps MINI vs 1975 Topps; Topps CHROME vs Topps;
# Topps Chrome UPDATE vs Topps Chrome) is a different card with the same number
PRODUCT_TOKENS = {"chrome", "finest", "bowman", "mini", "tiffany", "update", "traded", "optic", "select", "mosaic", "prizm",
                  "stadium", "archives", "gallery", "draft", "national", "treasures", "contenders", "absolute", "donruss",
                  "ultra", "metal", "exquisite", "flawless", "immaculate", "spectra", "obsidian", "origins",
                  "chronicles", "hoops", "kaboom", "downtown", "sticker", "dynasty", "tribute", "inception", "sterling",
                  "variation", "championship"}                                     # image variations are separate cards
# a set, lot or sealed product is never a single card ("1986 Fleer Basketball Complete Set w/ Michael Jordan ROOKIE #57"
# matched the Jordan card, 2026-09-30). "Exquisite Collection" is a product, so "collection" counts only as "... of".
NOT_SINGLE_RE = re.compile(r"\b(complete set|near(?:ly)? complete|team set|partial set|master set|set w/|set with|sets? of|"
                           r"lot of|lot \(|collection of|card collection|\d+ different|unopened|sealed|wax box|hobby box|"
                           r"blaster box|wax pack|box of|case of|hobby case)\b|\(\d+\)(?!\s*#)", re.I)
NOT_PARALLEL_RE = re.compile(r"red sox|white sox|blue jays|golden state|(?:pristine )?black label|gold label|silver label|"
                             r"mba (?:gold|silver|bronze)(?: diamond)?(?: certified)?|gold diamond certified", re.I)
SYNONYM = {"autographs": "auto", "autograph": "auto", "autos": "auto", "autographed": "auto", "rookies": "rookie", "rc": "rookie",
           "refractors": "refractor", "prizms": "prizm", "superfractors": "superfractor", "patches": "patch"}


def tokens(text):
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return {SYNONYM.get(w, w) for w in re.findall(r"[a-z0-9]+", t) if not w.isdigit()}


def card_matches(title, print_run, set_name, parallel, cand_print_run, year=None, cand_year=None):
    """(ok, reason): may this catalog card be the card in this sale title? Player and card number are checked by the
    caller; this checks year, set, parallel and print run."""
    if NOT_SINGLE_RE.search(title or ""):
        return False, "not a single card (set, lot or sealed product)"
    if year and cand_year and int(year) != int(cand_year):
        return False, "year %s vs catalog %s" % (year, cand_year)     # 2024 vs 2025 Topps Chrome #1 are different cards
    tw = tokens(NOT_PARALLEL_RE.sub(" ", title))
    sw = tokens(set_name) - SET_STOP
    missing = sorted(sw - BRANDS - tw)
    if missing:
        return False, "set words not in the title: " + " ".join(missing)
    pw = tokens(parallel) - SET_STOP
    other = sorted((PRODUCT_TOKENS & tw) - sw - pw)
    if other:
        return False, "title names another product line: " + " ".join(other)
    if ({"auto", "signed"} & tw) and "auto" not in (sw | pw):
        return False, "signed copy of a card with no autograph version (after-market signature)"
    if pw - tw:
        return False, "catalog parallel not in the title: " + " ".join(sorted(pw - tw))
    extra = sorted((PARALLEL_TOKENS & tw) - sw - pw)
    if extra:
        return False, "title names a parallel the card lacks: " + " ".join(extra)
    if print_run and cand_print_run and int(print_run) != int(cand_print_run):
        return False, "print run /%s vs /%s" % (print_run, cand_print_run)
    return True, ""


def resolve(b, s):
    """MAZI ID candidates for one canonical sale. resolved = exactly ONE catalog card with the same player, year (+/-1)
    and card number whose set and parallel also agree with the title (card_matches)."""
    if not s["player_keys"] or not s["year"]:
        return {"status": "needs_review", "why": "player or year not read from the title", "candidates": []}
    if not s["code_key"]:
        return {"status": "needs_review", "why": "no card number in the title", "candidates": []}
    # the card number is filtered in SQL: an unordered LIMIT 3000 over the player's whole catalog (LeBron, Wembanyama,
    # Jayden Daniels have more in three seasons) dropped the right card at random -- 5 resolved sales flipped, 2026-10-01
    rows = b.execute("""SELECT card_id, set_name, number, parallel, print_run, season_year FROM public.beta_catalog
                        WHERE player_key = ANY(%s) AND season_year BETWEEN %s AND %s AND visibility = 'visible'
                          AND lower(regexp_replace(number, '[^A-Za-z0-9]', '', 'g')) = %s""",
                     (s["player_keys"], s["year"] - 1, s["year"] + 1, s["code_key"])).fetchall()
    same_number = [r for r in rows if fold(r[2]) == s["code_key"]]
    if not same_number:
        return {"status": "mint_candidate", "why": "card not in the catalog (player/year/card number)", "candidates": []}
    ok, near = [], []
    for cid, sname, num, par, pr, yr in same_number:
        good, why = card_matches(s["title"], s.get("print_run"), sname, par, pr, s["year"], yr)
        (ok if good else near).append({"card_id": cid, "set_name": sname, "number": num, "parallel": par, "why_not": why})
    if len(ok) == 1:
        return {"status": "resolved" if s["price"] < 1_000_000 else "resolved_needs_review",
                "why": "one card matches set, number and parallel", "candidates": ok}
    if len(ok) > 1:
        return {"status": "needs_review", "why": "%d cards match set, number and parallel" % len(ok), "candidates": ok[:3]}
    return {"status": "needs_review", "why": "same player/year/number, but " + near[0]["why_not"], "candidates": near[:3]}


# ------------------------------------------------------------------------------------------------ report
def report(a):
    os.makedirs(OUT, exist_ok=True)
    cache = os.path.join(OUT, f"fanatics_api_{int(a.min_price)}_{a.since}_{date.today()}.json")   # paced API pull: reuse today's
    if os.path.exists(cache):
        fan = json.load(open(cache))
    else:
        fan = collect_fanatics(a.min_price, a.since)
        json.dump(fan, open(cache, "w"))
    rows = fan + collect_neon(a.min_price, a.since) + collect_seed()
    b = beta()
    try:
        players = known_players(b, rows)
        canon = canonicalize(rows, players)
        for s in canon:
            s["mazi"] = resolve(b, s)
    finally:
        b.close()
    canon.sort(key=lambda s: -s["price"])
    json.dump(canon, open(os.path.join(OUT, "headline_sales_report.json"), "w"), indent=1, default=str)
    seed_groups = [s for s in canon if any(r["src"] == "seed" for r in s["sources"])]
    feed_found = [s for s in seed_groups if any(r["src"] != "seed" for r in s["sources"])]
    from collections import Counter
    st = Counter(s["mazi"]["status"] for s in canon)
    lines = [f"# Headline sales report -- {datetime.now():%Y-%m-%d %H:%M} (floor ${a.min_price:,.0f}, since {a.since})", "",
             f"- canonical sales: **{len(canon):,}** from {len(rows):,} source rows "
             f"(fanatics_api {sum(r['src']=='fanatics_api' for r in rows):,}, neon {sum(r['src']=='neon' for r in rows):,}, seed {sum(r['src']=='seed' for r in rows)})",
             f"- MAZI ID status: " + ", ".join(f"{k} {v:,}" for k, v in st.most_common()),
             f"- 2026 $1M+ checklist (seed): {len(seed_groups)} sales; also in a feed we scrape: **{len(feed_found)}**; "
             f"only in press (private/Alt/unknown venue): {len(seed_groups) - len(feed_found)}", "",
             "| price | date | venue | sale | sources | MAZI ID status | best catalog candidate |", "|---|---|---|---|---|---|---|"]
    for s in [s for s in canon if s["price"] >= 1_000_000]:
        cand = s["mazi"]["candidates"][0]["card_id"] if s["mazi"]["candidates"] else "-"
        lines.append(f"| ${s['price']:,.0f} | {s['date']} | {s['venue']} | {s['title'][:70]} | {'+'.join(sorted({r['src'] for r in s['sources']}))} "
                     f"| {s['mazi']['status']} | {cand} |")
    open(os.path.join(OUT, "headline_sales_report.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:6]))
    print(f"wrote {OUT}/headline_sales_report.md + .json")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--min-price", type=float, default=100000)
    r.add_argument("--since", default="2025-01-01")
    a = ap.parse_args()
    return report(a)


if __name__ == "__main__":
    sys.exit(main())
