#!/usr/bin/env python3
"""Memory-safe streaming audit of the external comps JSON stores (PRD V2, Phase 1).

Streams each store with ijson (never json.load — the Mini has ~1.5 GB free RAM while the
capture fleet runs). Produces ONLY aggregate statistics (no buyer/seller/handle data) to:

    <store>/audit/store_audit_<stamp>.json
    <store>/audit/store_audit_<stamp>.md

Measures, per store and combined:
  rows, eBay item-id presence, unique item ids, intra-file duplicates, cross-file overlap,
  price completeness (parseable / zero / missing), best_offer share, image_url presence,
  grader/grade presence, source tally, sold_date monthly histogram (sizes the freshness hole),
  scraped_at monthly histogram (when the store was actually fed), category v0
  (copy of generate_feed.detect_category — KEYWORD HEURISTIC, not real classification),
  sport v0 (keyword heuristic, labelled as such).

Usage:
  /usr/bin/python3 external_engine/audit_stores.py [--store DIR] [--limit N] [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

try:
    import ijson
except ImportError:  # pragma: no cover
    print("ijson required (system /usr/bin/python3 has it)", file=sys.stderr)
    raise

DEFAULT_STORE = "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store"
STORE_FILES = ["ebay_comps.json", "player_comps.json", "raw_player_comps.json"]
FILE_BIT = {"ebay_comps.json": 1, "player_comps.json": 2, "raw_player_comps.json": 4}

ITEM_RE = re.compile(r"/itm/(\d+)")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
SOLD_DATE_RE = re.compile(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})")


# --- category v0: VERBATIM COPY of generate_feed.detect_category (keyword heuristic) ------
def detect_category_v0(title="", search_query="", sport=""):
    t = (title or "").lower()
    q = (search_query or "").lower()
    s = (sport or "").lower()
    combined = f"{t} {q} {s}"
    if any(kw in combined for kw in ["pokemon", "pikachu", "charizard", "mewtwo",
            "pokémon", "eevee", "vmax", "vstar", "paldea", "scarlet & violet",
            "obsidian flames", "crown zenith", "brilliant stars"]):
        return "pokemon"
    if any(kw in combined for kw in ["yu-gi-oh", "yugioh", "yu gi oh",
            "dark magician", "blue-eyes white dragon", "exodia"]):
        return "yugioh"
    if any(kw in combined for kw in ["veefriends", "vee friends", "gary vee",
            "garyvee", "zerocool", "vfriends", "vee friends series"]):
        return "veefriends"
    if any(kw in combined for kw in ["rolex", "patek philippe", "audemars piguet",
            "omega seamaster", "omega speedmaster", "cartier", "panerai",
            "grand seiko", "tudor", "iwc", "tag heuer", "breitling",
            "jaeger-lecoultre", "jaeger lecoultre", "hublot", "chopard",
            "richard mille", "vacheron constantin", "a. lange", "a lange",
            " watch ", "wristwatch", "chronograph"]):
        return "watches"
    if any(kw in combined for kw in ["coin", "coins", "morgan dollar",
            "peace dollar", "silver eagle", "half dollar", "numismatic",
            "pcgs ", "ngc ", "double eagle", "saint-gaudens", "saint gaudens",
            "walking liberty", "mercury dime", "buffalo nickel",
            "krugerrand", "maple leaf", "american eagle", "quarter eagle",
            "half eagle", "liberty head"]):
        return "coins"
    return "sports"


# --- sport v0: keyword heuristic (LOW confidence; audit sizing only) ----------------------
SPORT_KW = [
    ("basketball", ["basketball", " nba", "hoops", "wembanyama", "lebron", "jordan", "kobe",
                    "curry", "luka", "giannis", "jokic", "tatum", "prizm basketball", "court kings"]),
    ("football", ["football", " nfl", "mahomes", "brady", "burrow", "stroud", "jayden daniels",
                  "caleb williams", "gridiron", "contenders football", "prizm football"]),
    ("baseball", ["baseball", " mlb", "topps chrome", "bowman", "ohtani", "judge", "skenes",
                  "elly de la cruz", "topps now", "heritage", "stadium club", "topps update"]),
    ("hockey", ["hockey", " nhl", "upper deck", "young guns", "bedard", "mcdavid", "crosby",
                "ovechkin", "gretzky", "o-pee-chee", "opc "]),
    ("soccer", ["soccer", "futbol", "football club", "premier league", "uefa", "messi", "ronaldo",
                "mbappe", "mbappé", "haaland", "bellingham", "yamal", "topps chrome uefa", "panini select fifa", "fifa"]),
    ("wnba", ["wnba", "caitlin clark", "angel reese", "paige bueckers"]),
    ("ufc_mma", [" ufc", " mma", "conor mcgregor", "jon jones", "khabib"]),
    ("wrestling", [" wwe", " aew", "wrestling", "hulk hogan", "the rock dwayne"]),
    ("racing", ["f1 ", "formula 1", "formula one", "nascar", "verstappen", "hamilton lewis", "topps chrome f1", "turbo attax"]),
    ("golf", [" golf", " pga", "tiger woods", "scheffler"]),
    ("tennis", ["tennis", "wimbledon", "alcaraz", "sinner ", "serena", "federer", "nadal", "djokovic"]),
    ("boxing", ["boxing", "tyson", "ali muhammad", "canelo"]),
]


def detect_sport_v0(title: str, player_query: str = "") -> str:
    t = f" {(title or '').lower()} {(player_query or '').lower()} "
    for sport, kws in SPORT_KW:
        if any(k in t for k in kws):
            return sport
    return "unknown"


def parse_price(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    # handle "70.0 to 80.0" ranges → take first number
    m = re.match(r"-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def sold_month(s) -> str | None:
    if not s:
        return None
    s = str(s)
    m = SOLD_DATE_RE.search(s)
    if m:
        mon = MONTHS.get(m.group(1).lower()[:3])
        if mon:
            return f"{m.group(3)}-{mon:02d}"
    m = re.match(r"(\d{4})-(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def iso_month(s) -> str | None:
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})", str(s))
    return f"{m.group(1)}-{m.group(2)}" if m else None


def audit(store: str, limit: int | None, out_dir: str) -> dict:
    t0 = time.time()
    id_mask: dict[int, int] = {}          # item_id -> bitmask of files containing it
    per_file: dict[str, dict] = {}
    combined = Counter()
    sold_hist_all = Counter()
    scraped_hist_all = Counter()
    for fn in STORE_FILES:
        path = os.path.join(store, fn)
        bit = FILE_BIT[fn]
        st = {
            "rows": 0, "no_item_id": 0, "unique_item_ids": 0, "intra_file_dupes": 0,
            "price_ok": 0, "price_zero": 0, "price_missing": 0, "price_sum": 0.0,
            "best_offer_true": 0, "best_offer_missing": 0,
            "image_url_present": 0, "url_present": 0,
            "grader_present": 0, "grade_present": 0, "player_query_present": 0,
            "source": Counter(), "grader": Counter(), "category_v0": Counter(), "sport_v0": Counter(),
            "sold_month": Counter(), "scraped_month": Counter(), "comp_id_prefix": Counter(),
            "keys": Counter(), "price_buckets": Counter(), "schema_variant": Counter(),
            "shipping_present": 0,
            "min_sold_month": None, "max_sold_month": None, "max_scraped_at": None,
        }
        seen_here: set[int] = set()
        size = os.path.getsize(path)
        print(f"[audit] {fn} ({size/1e9:.2f} GB) ...", flush=True)
        with open(path, "rb") as f:
            for i, rec in enumerate(ijson.items(f, "item")):
                if limit and i >= limit:
                    break
                st["rows"] += 1
                if st["rows"] % 250000 == 0:
                    print(f"[audit]   {fn}: {st['rows']:,} rows, {time.time()-t0:.0f}s", flush=True)
                st["keys"].update(rec.keys())
                url = rec.get("url") or ""
                if url:
                    st["url_present"] += 1
                m = ITEM_RE.search(url) if url else None
                if not m:
                    st["no_item_id"] += 1
                else:
                    iid = int(m.group(1))
                    if iid in seen_here:
                        st["intra_file_dupes"] += 1
                    else:
                        seen_here.add(iid)
                    id_mask[iid] = id_mask.get(iid, 0) | bit
                # schema variants: v1 uses sold_price + bool best_offer + "Mar 1, 2026";
                # v2 (priority/v2 scraper rows merged into ebay_comps) uses price/price_text + "True"/"False" + ISO dates
                if "sold_price" in rec:
                    st["schema_variant"]["v1_sold_price"] += 1
                elif "price" in rec:
                    st["schema_variant"]["v2_price"] += 1
                else:
                    st["schema_variant"]["unknown"] += 1
                p = parse_price(rec.get("sold_price") if rec.get("sold_price") not in (None, "") else rec.get("price"))
                if p is None:
                    st["price_missing"] += 1
                elif p <= 0:
                    st["price_zero"] += 1
                else:
                    st["price_ok"] += 1
                    st["price_sum"] += p
                    b = ("<1" if p < 1 else "1-10" if p < 10 else "10-50" if p < 50 else "50-100" if p < 100
                         else "100-500" if p < 500 else "500-1k" if p < 1000 else "1k-5k" if p < 5000
                         else "5k-50k" if p < 50000 else ">=50k")
                    st["price_buckets"][b] += 1
                bo = rec.get("best_offer")
                if bo is None:
                    st["best_offer_missing"] += 1
                elif bo is True or str(bo).lower() == "true":
                    st["best_offer_true"] += 1
                if rec.get("shipping"):
                    st["shipping_present"] += 1
                if rec.get("image_url"):
                    st["image_url_present"] += 1
                if rec.get("grader"):
                    st["grader_present"] += 1
                    st["grader"][str(rec.get("grader"))[:12]] += 1
                if rec.get("grade"):
                    st["grade_present"] += 1
                pq = rec.get("player_query") or rec.get("search_query") or ""
                if pq:
                    st["player_query_present"] += 1
                st["source"][str(rec.get("source") or "")] += 1
                cid = str(rec.get("comp_id") or "")
                st["comp_id_prefix"][cid.split("-")[0] if "-" in cid else cid[:4]] += 1
                title = rec.get("title") or ""
                cat = detect_category_v0(title, pq)
                st["category_v0"][cat] += 1
                if cat == "sports":
                    st["sport_v0"][detect_sport_v0(title, pq)] += 1
                sm = sold_month(rec.get("sold_date"))
                if sm:
                    st["sold_month"][sm] += 1
                scm = iso_month(rec.get("scraped_at"))
                if scm:
                    st["scraped_month"][scm] += 1
                sa = rec.get("scraped_at")
                if sa and (st["max_scraped_at"] is None or str(sa) > st["max_scraped_at"]):
                    st["max_scraped_at"] = str(sa)
        st["unique_item_ids"] = len(seen_here)
        if st["sold_month"]:
            ks = sorted(st["sold_month"])
            st["min_sold_month"], st["max_sold_month"] = ks[0], ks[-1]
        sold_hist_all.update(st["sold_month"])
        scraped_hist_all.update(st["scraped_month"])
        del seen_here
        per_file[fn] = st
        print(f"[audit] {fn} done: rows={st['rows']:,} unique_ids={st['unique_item_ids']:,} "
              f"intra_dupes={st['intra_file_dupes']:,} ({time.time()-t0:.0f}s)", flush=True)

    # cross-file overlap from masks
    overlap = Counter()
    for mask in id_mask.values():
        overlap[mask] += 1
    names = {1: "ebay_comps", 2: "player_comps", 4: "raw_player_comps"}
    overlap_named = {}
    for mask, n in overlap.items():
        label = "+".join(names[b] for b in (1, 2, 4) if mask & b)
        overlap_named[label] = n
    total_rows = sum(per_file[f]["rows"] for f in STORE_FILES)
    unique_total = len(id_mask)
    dup_rows_total = total_rows - unique_total - sum(per_file[f]["no_item_id"] for f in STORE_FILES)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "store": store,
        "limit": limit,
        "elapsed_seconds": round(time.time() - t0, 1),
        "files": {fn: {k: (dict(v) if isinstance(v, Counter) else v) for k, v in st.items()}
                  for fn, st in per_file.items()},
        "combined": {
            "total_rows": total_rows,
            "unique_item_ids": unique_total,
            "duplicate_rows_total": dup_rows_total,
            "duplicate_pct": round(100.0 * dup_rows_total / total_rows, 2) if total_rows else None,
            "rows_without_item_id": sum(per_file[f]["no_item_id"] for f in STORE_FILES),
            "file_overlap_by_item_id": overlap_named,
            "sold_month_hist": dict(sorted(sold_hist_all.items())),
            "scraped_month_hist": dict(sorted(scraped_hist_all.items())),
        },
        "notes": [
            "category_v0 = verbatim copy of generate_feed.detect_category (keyword heuristic; NOT real classification).",
            "sport_v0 = keyword heuristic over title+player_query; LOW confidence; for sizing only.",
            "duplicate = same eBay item id appearing more than once across the three stores (intra + cross file).",
            "No buyer/seller/bidder/handle data is read or emitted by this audit.",
        ],
    }
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jpath = os.path.join(out_dir, f"store_audit_{stamp}.json")
    with open(jpath, "w") as f:
        json.dump(result, f, indent=1, default=str)
    mpath = os.path.join(out_dir, f"store_audit_{stamp}.md")
    with open(mpath, "w") as f:
        f.write(render_md(result))
    # stable "latest" pointers
    for src, dst in ((jpath, "store_audit_latest.json"), (mpath, "store_audit_latest.md")):
        dst = os.path.join(out_dir, dst)
        try:
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(os.path.basename(src), dst)
        except OSError:
            pass
    print(f"[audit] wrote {jpath}\n[audit] wrote {mpath}", flush=True)
    return result


def render_md(r: dict) -> str:
    c = r["combined"]
    L = []
    L.append(f"# External comps store audit — {r['generated_at'][:19]}Z\n")
    L.append(f"Store: `{r['store']}`  |  elapsed {r['elapsed_seconds']}s  |  limit={r['limit']}\n")
    L.append("## Combined\n")
    L.append("| metric | value |\n|---|---|")
    for k in ("total_rows", "unique_item_ids", "duplicate_rows_total", "duplicate_pct", "rows_without_item_id"):
        L.append(f"| {k} | {c[k]:,} |" if isinstance(c[k], int) else f"| {k} | {c[k]} |")
    L.append("\n### Item-id overlap between files\n")
    L.append("| files | unique item ids |\n|---|---|")
    for k, v in sorted(c["file_overlap_by_item_id"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {k} | {v:,} |")
    L.append("\n## Per file\n")
    for fn, st in r["files"].items():
        rows = st["rows"] or 1
        L.append(f"### {fn}\n")
        L.append("| metric | value | % |\n|---|---|---|")
        for k in ("rows", "unique_item_ids", "intra_file_dupes", "no_item_id", "price_ok", "price_zero",
                  "price_missing", "best_offer_true", "best_offer_missing", "image_url_present",
                  "shipping_present", "grader_present", "grade_present", "player_query_present"):
            v = st[k]
            L.append(f"| {k} | {v:,} | {100.0*v/rows:.2f} |")
        L.append(f"| sold_month range | {st['min_sold_month']} → {st['max_sold_month']} | |")
        L.append(f"| max scraped_at | {st['max_scraped_at']} | |")
        L.append("\n**schema variant:** " + ", ".join(f"{k}={v:,}" for k, v in st["schema_variant"].items()))
        L.append("\n**source:** " + ", ".join(f"{k}={v:,}" for k, v in sorted(st["source"].items(), key=lambda kv: -kv[1])[:8]))
        L.append("\n**comp_id prefix:** " + ", ".join(f"{k}={v:,}" for k, v in sorted(st["comp_id_prefix"].items(), key=lambda kv: -kv[1])[:6]))
        L.append("\n**category_v0 (keyword heuristic):** " + ", ".join(f"{k}={v:,}" for k, v in sorted(st["category_v0"].items(), key=lambda kv: -kv[1])))
        L.append("\n**sport_v0 (keyword heuristic, low confidence):** " + ", ".join(f"{k}={v:,}" for k, v in sorted(st["sport_v0"].items(), key=lambda kv: -kv[1])))
        L.append("\n**grader:** " + ", ".join(f"{k}={v:,}" for k, v in sorted(st["grader"].items(), key=lambda kv: -kv[1])[:8]))
        L.append("\n**price buckets:** " + ", ".join(f"{k}={v:,}" for k, v in st["price_buckets"].items()))
        L.append("")
    L.append("## Sold-date month histogram (combined, all rows incl. dupes)\n")
    L.append("| month | rows |\n|---|---|")
    for k, v in c["sold_month_hist"].items():
        if k >= "2024-01":
            L.append(f"| {k} | {v:,} |")
    L.append("\n## Scraped-at month histogram (combined) — when the store was actually fed\n")
    L.append("| month | rows |\n|---|---|")
    for k, v in c["scraped_month_hist"].items():
        L.append(f"| {k} | {v:,} |")
    L.append("\n## Notes\n")
    for n in r["notes"]:
        L.append(f"- {n}")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--limit", type=int, default=None, help="rows per file (smoke test)")
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args()
    out_dir = a.out_dir or os.path.join(a.store, "audit")
    audit(a.store, a.limit, out_dir)


if __name__ == "__main__":
    main()
