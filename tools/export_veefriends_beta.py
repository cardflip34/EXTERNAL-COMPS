#!/usr/bin/env python3
"""Export VeeFriends guaranteed sales as beta fixtures and, on request, insert them into the beta.

WHAT GOES IN (read-only from Neon public.veefriends_guaranteed_sales, EXTERNAL COMPS lane 2026-09-27):
  one sale = one external marketplace sale with an exact sold price (no Best Offer asking prices, no Goldin
  premium-basis rows), linked by tools/veefriends_link.py to exactly one checklist card, inside the price fence.

beta_catalog   one row per card VARIANT (mazi_variant_id = MAZI card id + '~<parallel>' unless Base), in the
               same shape as the SCP rows already there: name 'Character [Parallel]', category 'veefriends',
               set_id 'mazi:vf:<year>-<set line>', source_card_id = card_id, source_version 0.
               catalog_generation stays 0 (<= the active release), visibility 'visible'.
beta_sets      one row per VeeFriends set (source 'mazi').
beta_sales     sale_id 'mazi-vf:<Neon external_transactions.id>'; venue ebay|fanatics; provider names how we got
               it; occurrence_key '<venue>:<item id>:<date>:<price>' so the same marketplace sale can never be
               imported twice through another path; verified=false (never a MAZIFIED claim); identity_reviewed
               and price_eligible true = the strict checklist link (the BETA-RUNBOOK meaning); observation_id NULL
               (the seed-importer path the Whatnot load used, which the receipt trigger accepts).
               Grades outside the beta vocabulary (RAW | PSA/BGS/SGC/CGC n) are not exported.

INSERT ONLY: ON CONFLICT DO NOTHING everywhere. An existing beta row is never updated or deleted by this tool.
Fixtures are written <= 900 cards each (the importer contract caps at 1,000) and validated with the front
end's own lib/import-contract.mjs when node + the contract copy are present.

  python3 tools/export_veefriends_beta.py --out-dir ~/private/fixtures/veefriends            # build + validate only
  python3 tools/export_veefriends_beta.py --out-dir DIR --apply --yes-i-understand-beta [--limit-cards N]
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, subprocess, sys, time, unicodedata
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import veefriends_link as V
import veefriends_variants as VV

BETA = "jzxgtvxcuukxqkbwbuxg"
BHOST = "aws-0-us-west-1.pooler.supabase.com"
CHUNK_CARDS = 900
GRADE_OK = re.compile(r"^(RAW|(PSA|BGS|SGC|CGC) (10|[1-9](\.5)?))$")
VENUE = {"ebay": ("ebay", "mazi-external-comps:ebay-sold-search"), "fanatics": ("fanatics", "mazi-external-comps:fanatics-collect")}
VENUE_LABEL = {"ebay": "eBay", "fanatics": "Fanatics"}
CONTRACT_DIR = Path(os.path.expanduser("~/mazi_veefriends/beta_contract"))
SOURCE = "mazi-external-comps-veefriends"



def single_run_lock(name):
    """One copy at a time: a second run (e.g. the command pasted twice) exits instead of racing the first."""
    import fcntl
    path = os.path.expanduser(f"~/private/fixtures/.{name}.lock")
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another {name} run is already in progress -- nothing done")
    return fh

def fold(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def name_key(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def beta_grade(g):
    g = (g or "").strip()
    g = "RAW" if g.lower() == "raw" else g.upper()
    return g if GRADE_OK.match(g) else None


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def number_key(n):
    """The site's search key: lowercase letters and digits only ("OSS-2" -> "oss2"), as the sports rows store it."""
    return re.sub(r"[^a-z0-9]", "", (n or "").lower()) or None


def fetch(conn=None, guaranteed="public.veefriends_guaranteed_sales", cards_rel="public.veefriends_cards",
          links_rel="public.veefriends_sale_links"):
    """Guaranteed sales + their card/link fields. conn/relations are parameters so a dry run can read simulated tables."""
    import psycopg
    own = conn is None
    c = conn or psycopg.connect(os.environ["MAZI_DB_URL"], connect_timeout=20)
    try:
        c.execute("SET statement_timeout='300s'")
        rows = c.execute(f"""
            SELECT g.external_transaction_id, g.vf_card_id, g.mazi_card_id, g.mazi_variant_id, g.set_key, g.character,
                   g.parallel, g.grade, g.sold_price, g.sold_date, g.source_code, g.item_url,
                   e.source_item_id, k.set_name, k.section, k.section_name, k.code, l.print_run
            FROM {guaranteed} g
            JOIN public.external_transactions e ON e.id = g.external_transaction_id
            JOIN {cards_rel} k ON k.card_id = g.vf_card_id
            JOIN {links_rel} l ON l.external_transaction_id = g.external_transaction_id
            ORDER BY g.mazi_variant_id, g.sold_date, g.external_transaction_id""").fetchall()
        if own:
            c.rollback()
    finally:
        if own:
            c.close()
    return rows


def make_card(variant, mazi_id, vf_id, set_key, character, parallel, set_name, section, section_name, code, print_run):
    year, line = V.MAZI_SET_LINE[set_key]
    par = None if parallel == "Base" else parallel
    shown_set = set_name if section in ("base", "spectacular-stickers") else f"{set_name} \u00b7 {section_name}"
    return {"card_id": variant, "name": character + (f" [{par}]" if par else ""), "player_key": fold(character),
            "set_name": shown_set, "number": code, "season_year": year, "category": "veefriends", "parallel": par,
            "image_kind": "reference",
            "aliases": (["Very Lucky Black Cat", "VVVV Lucky Black Cat"] if character.startswith("Very, Very") else []),
            "featured": False, "_set_id": f"mazi:vf:{year}-{V.mazi_slug(line)}", "_print_run": print_run,
            "_mazi_card_id": mazi_id, "_vf_card_id": vf_id, "_checklist": False}


def build(rows, matrix_rows=None):
    """cards = every checklist version when matrix_rows is given (reference-only when unsold), plus any sold variant."""
    cards, sets, sales, skipped = {}, {}, [], {"grade_not_in_beta_vocabulary": 0, "venue": 0, "no_date": 0}
    runs = {}
    for m in matrix_rows or []:
        c = make_card(m["variant_id"], m["mazi_card_id"], m["vf_card_id"], m["set_key"], m["character"], m["parallel"],
                      m["set_name"], m["section"], m["section_name"], m["code"], m["print_run"])
        c["_checklist"] = True
        cards[m["variant_id"]] = c
        year, _ = V.MAZI_SET_LINE[m["set_key"]]
        sets.setdefault(c["_set_id"], {"set_id": c["_set_id"], "name": m["set_name"], "category": "veefriends",
                                       "season_year": year, "source": "mazi"})
    for (tx, vf_id, mazi_id, variant, set_key, character, parallel, grade, price, sold, src, url,
         item_id, set_name, section, section_name, code, print_run) in rows:
        g = beta_grade(grade)
        if not g:
            skipped["grade_not_in_beta_vocabulary"] += 1; continue
        if src not in VENUE:
            skipped["venue"] += 1; continue
        if not sold:
            skipped["no_date"] += 1; continue
        year, line = V.MAZI_SET_LINE[set_key]
        set_id = f"mazi:vf:{year}-{V.mazi_slug(line)}"
        sets.setdefault(set_id, {"set_id": set_id, "name": set_name, "category": "veefriends", "season_year": year,
                                 "source": "mazi"})
        if variant not in cards:
            pr = V.VS.parallel_print_run(section, parallel) if set_key == V.VS.SET_KEY else None
            cards[variant] = make_card(variant, mazi_id, vf_id, set_key, character, parallel, set_name, section,
                                       section_name, code, pr)
        if print_run and not cards[variant]["_print_run"]:
            runs.setdefault(variant, set()).add(int(print_run))
        venue, provider = VENUE[src]
        d = sold.isoformat()[:10]
        rule = ("set, section and character on the official checklist (this set prints no card numbers)"
                if set_key == V.VS.SET_KEY else "card number and character on the published checklist")
        sales.append({"sale_id": f"mazi-vf:{tx}", "card_id": variant, "venue": venue, "provider": provider,
                      "source_transaction_id": str(item_id) if item_id else None,
                      **({"source_url": url} if url and url.startswith("https://") else {}),
                      "price": float(price), "currency": "USD", "sold_at": f"{d}T00:00:00.000Z", "date_precision": "day",
                      "grade": g, "verified": False, "price_eligible": True, "published": True, "sold_status": "sold",
                      "best_offer": False, "actual_price_known": True, "identity_reviewed": True,
                      "evidence_note": f"Externally reported {VENUE_LABEL[venue]} sale (Mazi external comps). Identity matched from the "
                                       f"listing title to {rule}; exact sold price.",
                      "_occurrence_key": f"{venue}:{item_id}:{d}:{price}" if item_id else None})
    for v, rs in runs.items():            # an observed denominator becomes the card's print run only when unanimous
        if len(rs) == 1 and not cards[v]["_checklist"]:
            cards[v]["_print_run"] = next(iter(rs))
    for s in sets.values():
        s["source_hash"] = sha(s)
    return cards, sets, sales, skipped


def public(d):
    return {k: v for k, v in d.items() if not k.startswith("_") and v is not None}


def write_fixtures(cards, sales, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    by_card = {}
    for s in sales:
        by_card.setdefault(s["card_id"], []).append(s)
    ids = sorted(cards)
    files = []
    for n, i in enumerate(range(0, len(ids), CHUNK_CARDS), 1):
        chunk = ids[i:i + CHUNK_CARDS]
        fx = {"source": SOURCE, "exported_at": time.strftime("%FT%TZ", time.gmtime()),
              "cards": [public(cards[c]) for c in chunk], "sales": [public(s) for c in chunk for s in by_card.get(c, [])],
              "provenance": {"catalog": "permanent MAZI ids (mazi:vf:...) minted by tools/veefriends_link.py from published checklists",
                             "sales": "external marketplace sold records, exact prices only (Neon veefriends_guaranteed_sales)",
                             "verification": "No MAZIFIED proof claim; verified=false on every row."}}
        p = os.path.join(out_dir, f"veefriends_fixture_{n:02d}.json")
        with open(p, "w") as f:
            json.dump(fx, f, indent=1)
        files.append((p, len(fx["cards"]), len(fx["sales"])))
    return files


def validate_with_contract(files):
    """Run the front end's own validateFixture (lib/import-contract.mjs) on every file. Returns True/False/None."""
    contract = CONTRACT_DIR / "lib" / "import-contract.mjs"
    node = next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
    if not contract.exists() or not node:
        print("   contract validation SKIPPED (node or contract copy missing)"); return None
    js = ("import {readFileSync} from 'node:fs'; import {validateFixture} from '" + str(contract) + "';"
          "for (const f of process.argv.slice(1)) { const r = validateFixture(JSON.parse(readFileSync(f,'utf8')));"
          " console.log('   contract OK', f.split('/').pop(), r.cards.length, 'cards', r.sales.length, 'sales'); }")
    r = subprocess.run([node, "--input-type=module", "-e", js, *[p for p, _, _ in files]], capture_output=True, text=True)
    print(r.stdout.rstrip() or "", r.stderr.strip()[-600:] if r.returncode else "")
    return r.returncode == 0


def other_writers(cur):
    cur.execute("""SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()
                   AND backend_type = 'client backend' AND state <> 'idle'
                   AND query ~* '\\m(insert|update|delete|copy)\\M' AND query ~* 'beta_(catalog|sales|sets)'""")
    return cur.fetchone()[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes-i-understand-beta", action="store_true")
    ap.add_argument("--limit-cards", type=int, help="canary: insert only the first N cards (and their sales)")
    ap.add_argument("--beta-password-file", type=Path, default=Path(os.path.expanduser("~/private/beta-password")))
    a = ap.parse_args()
    _lock = single_run_lock("export_veefriends_beta")

    rows = fetch()
    cards, sets, sales, skipped = build(rows, VV.matrix())
    print(f"guaranteed rows {len(rows):,} -> sales {len(sales):,} on {len(cards):,} card variants in {len(sets)} sets; skipped {skipped}")
    files = write_fixtures(cards, sales, os.path.expanduser(a.out_dir))
    for p, c, s in files:
        print(f"   {os.path.basename(p)}: {c} cards, {s} sales")
    ok = validate_with_contract(files)
    if ok is False:
        print("CONTRACT VALIDATION FAILED -- nothing written"); return 2
    if not a.apply:
        print("dry: fixtures only. Nothing written to the beta."); return 0
    if not a.yes_i_understand_beta:
        print("dry: pass --yes-i-understand-beta to insert. Nothing written."); return 0

    import psycopg
    pw = a.beta_password_file
    if not pw.is_file() or pw.stat().st_mode & 0o077:
        raise SystemExit(f"{pw}: must exist with mode 600")
    ids = sorted(cards)
    if a.limit_cards:          # canary: half stickers (no-number ids), half numbered cards, deterministic
        st = [i for i in ids if f":{V.VS.SET_KEY.split('-', 1)[0]}-super-stickers" in i]
        ids = st[: a.limit_cards // 2] + [i for i in ids if i not in set(st)][: a.limit_cards - min(len(st), a.limit_cards // 2)]
    keep = set(ids)
    ins_sales = [s for s in sales if s["card_id"] in keep]
    ins_sets = sorted({cards[i]["_set_id"] for i in ids})
    if a.limit_cards:
        print(f"   CANARY: {len(ids)} cards, {len(ins_sales)} sales")
    bc = psycopg.connect(host=BHOST, port=5432, user=f"postgres.{BETA}", dbname="postgres", password=pw.read_text().strip(),
                         sslmode="require", connect_timeout=10, keepalives=1, keepalives_idle=30, keepalives_interval=10)
    cur = bc.cursor()
    cur.execute("SET statement_timeout = 300000"); cur.execute("SET lock_timeout = '10s'")
    busy = other_writers(cur)
    if busy:
        raise SystemExit(f"another session is writing to the beta catalog/sales right now ({busy}); one beta writer at a time -- nothing written")
    cur.execute("SELECT active_catalog_generation FROM public.beta_release_state WHERE id = 1")
    print(f"   beta release generation {cur.fetchone()[0]} (new rows use generation 0, visible at once)")

    cur.executemany("""INSERT INTO public.beta_sets (set_id, name, category, season_year, source, source_hash, source_version)
                       VALUES (%s,%s,%s,%s,%s,%s,0) ON CONFLICT DO NOTHING""",
                    [(s, sets[s]["name"], "veefriends", sets[s]["season_year"], "mazi", sets[s]["source_hash"]) for s in ins_sets])
    bc.commit()
    ccols = ["card_id", "name", "player_key", "set_name", "number", "season_year", "category", "parallel", "image_kind",
             "aliases", "featured", "set_id", "source_card_id", "name_key", "number_key", "print_run", "source_hash", "source_version"]
    crow = lambda c: (c["card_id"], c["name"], c["player_key"], c["set_name"], c["number"], c["season_year"], "veefriends",
                      c["parallel"], "reference", c["aliases"], False, c["_set_id"], c["card_id"], name_key(c["name"]),
                      number_key(c["number"]), c["_print_run"], sha(public(c)), 0)
    new_cards = 0
    for i in range(0, len(ids), 500):
        cur.executemany(f"INSERT INTO public.beta_catalog ({','.join(ccols)}) VALUES ({','.join(['%s'] * len(ccols))}) "
                        "ON CONFLICT DO NOTHING RETURNING card_id", [crow(cards[c]) for c in ids[i:i + 500]], returning=True)
        while True:
            if cur.fetchone() is not None:
                new_cards += 1
            if not cur.nextset():
                break
        bc.commit()
    print(f"   catalog: {new_cards:,} of {len(ids):,} variants inserted (the rest already existed and were left alone)")
    scols = ["sale_id", "card_id", "venue", "provider", "source_transaction_id", "source_url", "price", "currency", "sold_at",
             "date_precision", "grade", "verified", "price_eligible", "published", "sold_status", "best_offer",
             "actual_price_known", "identity_reviewed", "evidence_note", "occurrence_key"]
    srow = lambda s: tuple(s.get(k) if k != "occurrence_key" else s["_occurrence_key"] for k in scols)
    done = 0
    for i in range(0, len(ins_sales), 500):
        cur.executemany(f"INSERT INTO public.beta_sales ({','.join(scols)}) VALUES ({','.join(['%s'] * len(scols))}) "
                        "ON CONFLICT DO NOTHING RETURNING sale_id", [srow(s) for s in ins_sales[i:i + 500]], returning=True)
        while True:
            if cur.fetchone() is not None:
                done += 1
            if not cur.nextset():
                break
        bc.commit()
        print(f"   sales {min(i + 500, len(ins_sales)):,}/{len(ins_sales):,} processed, inserted {done:,}", flush=True)
    refreshed = 0
    while True:
        cur.execute("SELECT public.beta_refresh_summaries(500)"); n = cur.fetchone()[0]; bc.commit(); refreshed += n
        if n == 0:
            break
    print(f"   summaries refreshed: {refreshed:,}")
    cur.execute("INSERT INTO public.beta_import_runs (source, cards, sales, note) VALUES (%s,%s,%s,%s)",
                (SOURCE, new_cards, done, "direct insert from tools/export_veefriends_beta.py (insert-only); "
                 f"{'canary ' + str(len(ids)) + ' cards; ' if a.limit_cards else ''}external sold records, verified=false"))
    bc.commit()
    print(f"APPLY COMPLETE: {new_cards:,} cards, {done:,} sales inserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
