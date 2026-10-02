#!/usr/bin/env python3
"""export_headline_beta.py -- load reviewed headline sales ($100K+) into the beta, restorably (2026-09-29).

Input: ~/mazi_headline/headline_sales_report.json (tools/headline_sales.py report): one canonical record per real sale,
with its MAZI ID status. A sale is eligible only when its ID resolved to exactly ONE catalog card (card_matches: set,
product line, parallel, year, print run, signatures). $1M+ sales additionally need their sale_id in --approved, because
Andy checks every $1M+ ID. Nothing else is guessed:

  held  month-precision date (the beta takes day precision) | no grade read from the title | already on the beta
        (sale_id, venue + source id, or occurrence_key) | the same card and grade already has a sale within 3 days and
        2% of the price (the sale reached the beta another way, e.g. SportsCardsPro) | price over 25x or under 1/25 of
        the card's median beta price in that grade

Rows follow the MAZIDEX preconditions (2026-09-29): sale_id 'mazi-hl:<venue>:<source id>', provider 'mazi_headline',
occurrence_key '<venue>:<source id>:<date>:<price>', verified=false, identity_reviewed/price_eligible/actual_price_known/
published=true, sold_status 'sold', USD, date_precision 'day', no evidence_image, canonical grade, and an evidence_note
naming the house, the price basis and the identity check. Insert-only; --revert unpublishes exactly the manifest's rows.

  python3 tools/export_headline_beta.py                                   # dry run: counts + plan JSON, nothing written
  python3 tools/export_headline_beta.py --apply --yes-i-understand-beta   # Andy's go; MAZIDEX pinged first
  python3 tools/export_headline_beta.py --revert <manifest.json> --yes-i-understand-beta
"""
from __future__ import annotations

import argparse, json, os, statistics, sys, time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import correct_veefriends_beta as C      # beta_connect, single_run_lock, refresh_summaries, SCOLS
import export_veefriends_beta as X       # beta_grade, other_writers

REPORT = Path(os.path.expanduser("~/mazi_headline/headline_sales_report.json"))
OUT = Path(os.path.expanduser("~/private/fixtures/headline_export"))
PROVIDER = "mazi_headline"
HOUSE = {"goldin": "Goldin", "fanatics": "Fanatics Collect", "heritage": "Heritage Auctions", "rea": "Robert Edward Auctions",
         "alt": "Alt", "ebay": "eBay", "hugginsandscott": "Huggins & Scott", "memorylane": "Memory Lane",
         "lelands": "Lelands"}
BASIS = {"goldin": "price realized incl. buyer's premium", "fanatics": "sale price as listed in Fanatics Collect's sales history",
         "heritage": "price realized as reported", "rea": "price realized as listed by REA", "alt": "sale price as reported",
         "ebay": "sold price", "hugginsandscott": "price realized as listed",
         # both sites: "Prices Shown Include Buyer's Premium"
         "memorylane": "price realized incl. buyer's premium", "lelands": "price realized incl. buyer's premium"}
MIN_PRICE, FENCE, CORROBORATE = 100_000, 25.0, 10.0


def family(card_id):
    """The card's parallel family: 'mazi:bk:2023-panini-prizm:victor-wembanyama:136~choice-nebula' -> '...:136'."""
    return (card_id or "").split("~", 1)[0]


def independent(row, evidence_venue, evidence_id):
    """Fanatics WEEKLY records do not vouch for Fanatics WEEKLY records: the bogus-looking Pokemon LV.X / ex sales
    ($900K Garchomp, $204K + $108K Palkia, $156K + $120K Lugia) all came in pairs from Fanatics weekly auctions."""
    weekly = lambda v, i: v == "fanatics" and str(i or "").startswith("WEEKLY")
    return not (weekly(row["venue"], row["source_transaction_id"]) and weekly(evidence_venue, evidence_id))


def corroborated(row, same_grade, card_prices, family):
    """(ok, why). A headline sale loads only if another sale backs its price within CORROBORATE x, judged in order:
      1. the same card in the same grade, when it has any sale (a base Ohtani BCRA-SO auto BGS 9.5 at $132K vs its
         BGS 9.5 sales topping out at $7,345 fails here even though a PSA 10 sold for $23.5K);
      2. else the same card in any grade;
      3. else the card's parallel family [(venue, source id, price)] -- independent records only.
    Rows that fail go to Andy's review list; a real gem-mint vintage record can fail too (lower grades sell for 2-5%)."""
    floor = row["price"] / CORROBORATE
    if same_grade:
        return (any(p >= floor for p in same_grade), "same-grade sales top out at $%s" % format(int(max(same_grade)), ","))
    if card_prices:
        return (any(p >= floor for p in card_prices), "other-grade sales top out at $%s" % format(int(max(card_prices)), ","))
    fam = [p for v, i, p in family if independent(row, v, i)]
    return (any(p >= floor for p in fam),
            ("parallel sales top out at $%s" % format(int(max(fam)), ",")) if fam else "no independent sale of the card or its parallels")


def primary_source(s):
    """The venue's own record of the sale (Fanatics API or a Neon row from the house), never a press report."""
    for r in s["sources"]:
        if r.get("src") in ("fanatics_api", "neon") and r.get("venue") == s["venue"] and r.get("source_id"):
            return r
    return None


def build(canon, approved):
    """-> (rows to consider, Counter of held reasons). Pure: no DB."""
    rows, held = [], Counter()
    for s in canon:
        st = s["mazi"]["status"]
        if s["price"] < MIN_PRICE or s["venue"] not in HOUSE:
            continue
        if st == "resolved_needs_review":
            ok = None
        elif st == "resolved":
            ok = s["mazi"]["candidates"][0]
        else:
            continue
        src = primary_source(s)
        if not src:
            held["no venue record (press only)"] += 1; continue
        if "/buy-now/" in (src.get("url") or ""):
            held["Fanatics buy-now listing (an asking price, not a sale)"] += 1; continue
        sale_id = f"mazi-hl:{s['venue']}:{src['source_id']}"
        if ok is None:
            if sale_id not in approved:
                held["$1M+ not yet approved by Andy"] += 1; continue
            ok = s["mazi"]["candidates"][0]
        if s.get("prec") != "day":
            held["month-precision date"] += 1; continue
        tl = s["title"].lower()
        if "black label" in tl or ("gold label" in tl and "topps gold label" not in tl):
            held["Black/Gold Label slab (its own grade bucket is not decided yet)"] += 1; continue
        if s.get("payment") == "Unpaid":
            held["Fanatics: unpaid when captured (re-check later)"] += 1; continue
        grade = X.beta_grade(s.get("grade") or "")
        if not grade:
            held["no grade read from the title"] += 1; continue
        price = round(float(s["price"]), 2)
        rows.append({
            "sale_id": sale_id, "card_id": ok["card_id"], "venue": s["venue"], "provider": PROVIDER,
            "source_transaction_id": str(src["source_id"]), "source_url": src.get("url"), "price": price, "currency": "USD",
            "sold_at": f"{s['date']}T00:00:00.000Z", "date_precision": "day", "grade": grade, "verified": False,
            "price_eligible": True, "published": True, "sold_status": "sold", "best_offer": False, "actual_price_known": True,
            "identity_reviewed": True,
            "evidence_note": (f"{HOUSE[s['venue']]} sale (Mazi external comps, headline sales); {BASIS[s['venue']]}. "
                              f"Identity matched on set, card number, parallel, year and print run; reviewed 2026-09-29."),
            "occurrence_key": f"{s['venue']}:{src['source_id']}:{s['date']}:{price}",
            "_title": s["title"]})
    return rows, held


def check_against_beta(cur, rows, canon_family=None, approved=frozenset(), review=None):
    """-> (rows to insert, Counter of held reasons). Reads the beta only. canon_family: {family: [(sale_id, price)]}
    from the headline report (resolved sales), used with the beta's own sales to corroborate each price."""
    held = Counter()
    ids = [r["sale_id"] for r in rows]
    keys = [r["occurrence_key"] for r in rows]
    cur.execute("""SELECT sale_id, venue, source_transaction_id, occurrence_key FROM public.beta_sales
                   WHERE sale_id = ANY(%s) OR occurrence_key = ANY(%s)
                      OR (venue, source_transaction_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))""",
                (ids, keys, [r["venue"] for r in rows], [r["source_transaction_id"] for r in rows]))
    seen_ids, seen_keys, seen_src = set(), set(), set()
    for sid, v, stx, ok in cur.fetchall():
        seen_ids.add(sid); seen_keys.add(ok); seen_src.add((v, stx))
    cur.execute("""SELECT card_id, grade, price::float, sold_at::date, provider FROM public.beta_sales
                   WHERE card_id = ANY(%s) AND published AND price_eligible""", (sorted({r["card_id"] for r in rows}),))
    by_cg, by_card, by_cg_own = defaultdict(list), defaultdict(list), defaultdict(list)
    for cid, g, p, d, prov in cur.fetchall():
        by_cg[(cid, g)].append((p, d))
        if prov != PROVIDER:                       # our own headline rows never vouch for each other
            by_card[cid].append(p); by_cg_own[(cid, g)].append(p)
    out = []
    for r in rows:
        if r["sale_id"] in seen_ids or r["occurrence_key"] in seen_keys or (r["venue"], r["source_transaction_id"]) in seen_src:
            held["already on the beta"] += 1; continue
        d = date.fromisoformat(r["sold_at"][:10])
        sales = by_cg.get((r["card_id"], r["grade"]), [])
        if any(abs((d - sd).days) <= 3 and abs(p - r["price"]) <= 0.02 * r["price"] for p, sd in sales):
            held["same card+grade sale within 3 days and 2% (already there another way)"] += 1; continue
        if len(sales) >= 3:
            med = statistics.median(p for p, _ in sales)
            if med > 0 and not (med / FENCE <= r["price"] <= med * FENCE):
                held[f"price fence (>{FENCE:.0f}x off the card's median)"] += 1; r["_median"] = med
                if review is not None:
                    review.append({k: r[k] for k in ("sale_id", "card_id", "venue", "price", "sold_at", "grade", "_title", "source_url")}
                                  | {"why": "%.0fx off the card's median $%s in this grade" % (FENCE, format(int(med), ",")),
                                     "best_other": med})
                continue
        fam = [(v, i, p) for sid, v, i, p in (canon_family or {}).get(family(r["card_id"]), []) if sid != r["sale_id"]]
        ok, why = corroborated(r, by_cg_own.get((r["card_id"], r["grade"]), []), by_card.get(r["card_id"], []), fam)
        # a sale Andy approved by name is corroborated by that check
        if r["sale_id"] not in approved and not ok:
            if review is not None:
                review.append({k: r[k] for k in ("sale_id", "card_id", "venue", "price", "sold_at", "grade", "_title", "source_url")}
                              | {"why": why + " (needs one within %.0fx)" % CORROBORATE, "best_other": None})
            held[f"uncorroborated: no other sale of this card or its parallels within {CORROBORATE:.0f}x (to Andy's review)"] += 1
            r["_uncorroborated"] = True; continue
        out.append(r)
    return out, held


def audit(cur, bc, manifest_path, approved, stamp):
    """Re-check rows already loaded (a manifest) against today's price checks. Read-only; writes a revert manifest."""
    m = json.load(open(manifest_path))
    ids = set(m["sale_ids"])
    rows = [r for r in json.load(open(m["plan"]))["insert"] if r["sale_id"] in ids]
    cur.execute("""SELECT card_id, grade, price::float, provider FROM public.beta_sales
                   WHERE card_id = ANY(%s) AND published AND price_eligible""", (sorted({r["card_id"] for r in rows}),))
    by_cg, by_card = defaultdict(list), defaultdict(list)
    for cid, g, p, prov in cur.fetchall():
        if prov != PROVIDER:
            by_cg[(cid, g)].append(p); by_card[cid].append(p)        # by_cg: same card + grade, non-headline
    bc.rollback()
    canon = json.load(open(REPORT))
    fam_of = defaultdict(list)
    for s in canon:
        if s["mazi"]["status"] in ("resolved", "resolved_needs_review") and s["mazi"]["candidates"]:
            src = primary_source(s)
            if src:
                fam_of[family(s["mazi"]["candidates"][0]["card_id"])].append(
                    (f"mazi-hl:{s['venue']}:{src['source_id']}", s["venue"], str(src["source_id"]), float(s["price"])))
    bad = []
    for r in rows:
        if r["sale_id"] in approved:
            continue
        same = by_cg.get((r["card_id"], r["grade"]), [])
        fam = [(v, i, p) for sid, v, i, p in fam_of.get(family(r["card_id"]), []) if sid != r["sale_id"]]
        ok, why = corroborated(r, same, by_card.get(r["card_id"], []), fam)
        if not ok:
            bad.append((r, why))
    for r, why in sorted(bad, key=lambda x: -x[0]["price"]):
        print(f"  FAIL ${r['price']:,.0f} {r['venue']} {r['grade']} {r['card_id']} | {r['_title'][:60]} | {why}")
    out = OUT / f"revert_audit_{stamp}.json"
    json.dump({"sale_ids": [r["sale_id"] for r, _ in bad], "why": {r["sale_id"]: w for r, w in bad},
               "audited": str(manifest_path)}, open(out, "w"), indent=1)
    print(f"audited {len(rows)} loaded rows: {len(bad)} fail today's checks -> revert manifest {out} (NOT applied)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", type=Path)
    ap.add_argument("--approved", type=Path, help="file with one approved $1M+ sale_id per line")
    ap.add_argument("--limit", type=int, help="canary: insert at most this many")
    ap.add_argument("--audit", type=Path, help="re-check the rows of an applied manifest; writes a revert manifest of failures")
    ap.add_argument("--yes-i-understand-beta", action="store_true")
    ap.add_argument("--beta-password-file", type=Path, default=Path(os.path.expanduser("~/private/beta-password")))
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if (a.apply or a.revert) and not a.yes_i_understand_beta:
        sys.exit("writes need --yes-i-understand-beta (Andy's go; ping MAZIDEX first)")
    lock = C.single_run_lock("export_headline_beta") if (a.apply or a.revert) else None  # noqa: F841
    bc = C.beta_connect(a.beta_password_file)
    cur = bc.cursor()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if a.revert:
        ids = json.load(open(a.revert))["sale_ids"]
        if X.other_writers(cur):
            sys.exit("another beta writer is active -- nothing done")
        cur.execute("UPDATE public.beta_sales SET published = false WHERE sale_id = ANY(%s) AND provider = %s AND published",
                    (ids, PROVIDER))
        n = cur.rowcount; bc.commit()
        print(f"REVERT: unpublished {n} of {len(ids)} (summaries {C.refresh_summaries(bc, cur)})")
        return 0
    approved = {l.strip() for l in open(a.approved)} if a.approved else set()
    if a.audit:
        return audit(cur, bc, a.audit, approved, stamp)
    canon = json.load(open(REPORT))
    rows, held = build(canon, approved)
    canon_family = defaultdict(list)                # every resolved headline sale, by parallel family, for corroboration
    for s in canon:
        if s["mazi"]["status"] in ("resolved", "resolved_needs_review") and s["mazi"]["candidates"]:
            src = primary_source(s)
            if src and "/buy-now/" not in (src.get("url") or ""):
                canon_family[family(s["mazi"]["candidates"][0]["card_id"])].append(
                    (f"mazi-hl:{s['venue']}:{src['source_id']}", s["venue"], str(src["source_id"]), float(s["price"])))
    review = []
    ins, held2 = check_against_beta(cur, rows, canon_family, approved, review)
    bc.rollback()
    held.update(held2)
    if a.limit:
        ins = sorted(ins, key=lambda r: -r["price"])[:a.limit]
    plan = OUT / f"plan_{stamp}.json"
    json.dump({"report": str(REPORT), "insert": ins, "held": held, "review": review}, open(plan, "w"), indent=1, default=str)
    print(f"headline sales considered {len(rows):,} | to insert {len(ins):,} (${sum(r['price'] for r in ins):,.0f})")
    for k, v in held.most_common():
        print(f"  held {v:,}: {k}")
    print("  by venue:", dict(Counter(r["venue"] for r in ins)), "| by grade:", dict(Counter(r["grade"] for r in ins).most_common(6)))
    for r in sorted(ins, key=lambda r: -r["price"])[:5]:
        print(f"  e.g. ${r['price']:,.0f} {r['sold_at'][:10]} {r['venue']} {r['grade']} -> {r['card_id']} | {r['_title'][:60]}")
    print(f"  plan: {plan}")
    if not a.apply:
        print("dry run: nothing written.")
        return 0
    if X.other_writers(cur):
        sys.exit("another beta writer is active -- nothing done")
    manifest = OUT / f"manifest_{stamp}.json"
    json.dump({"sale_ids": [r["sale_id"] for r in ins], "plan": str(plan)}, open(manifest, "w"), indent=1)
    cols = C.SCOLS
    done = 0
    for i in range(0, len(ins), 200):
        part = ins[i:i + 200]
        cur.executemany(f"INSERT INTO public.beta_sales ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) "
                        "ON CONFLICT DO NOTHING RETURNING sale_id", [tuple(r[k] for k in cols) for r in part], returning=True)
        got = 0
        while True:
            got += len(cur.fetchall())
            if not cur.nextset():
                break
        bc.commit(); done += got
    print(f"APPLY COMPLETE: inserted {done} (summaries {C.refresh_summaries(bc, cur)}). "
          f"Undo: python3 tools/export_headline_beta.py --revert {manifest} --yes-i-understand-beta")
    return 0


if __name__ == "__main__":
    sys.exit(main())
