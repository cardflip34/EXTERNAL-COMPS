#!/usr/bin/env python3
"""Bring the beta's VeeFriends rows in line with the checklist + Neon veefriends_guaranteed_sales -- restorably.

The catalog is built from the CHECKLIST (tools/veefriends_variants.py): every version of every built product is a
beta_catalog row, sold or not (MAZIDEX audit 2026-09-29: only ~33% of versions existed because a variant was created
only by a confirmed-price sale). Sales still come only from the guaranteed view. Nothing is deleted:

  add        a checklist version missing from the beta: INSERT beta_catalog (reference-only while it has no sale)
  restore    a checklist version this tool withdrew earlier because it had no sale: visibility 'visible', and the
             redirect it was given is removed (a real '~refractor' must never redirect to '~wave-refractor')
  number_key 'oss-2' -> 'oss2': the site's search key is letters and digits only, as on the sports rows
  re-point   a sale whose card changed: UPDATE beta_sales.card_id (guarded on the old card)
  retract    a sale no longer guaranteed: published = false
  republish  a sale guaranteed again after an earlier retract: published = true (on its now-correct card)
  insert     a sale newly guaranteed (insert-only)
  withdraw   a variant NOT on the checklist that has no published sale (a mislabel/rename): visibility 'withdrawn';
             when all its sales moved to one variant it also gets a beta_redirects row old -> new

Every touched row's prior state goes to a backup JSON first; --revert <backup> restores it. Rows inserted by an apply
(new catalog versions, new sales) are withdrawn/unpublished by --revert only with --revert-inserts.

  python3 tools/correct_veefriends_beta.py                                   # dry run: counts, no writes
  python3 tools/correct_veefriends_beta.py --apply --yes-i-understand-beta   # needs Andy's go
  python3 tools/correct_veefriends_beta.py --revert <backup.json> --yes-i-understand-beta [--revert-inserts]
"""
from __future__ import annotations

import argparse, collections, json, os, sys, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_veefriends_beta as X
import veefriends_link as V
import veefriends_variants as VV

OUT = Path(os.path.expanduser("~/private/fixtures/veefriends_correction"))


def single_run_lock(name):
    """One copy at a time: a second run (e.g. the command pasted twice) exits instead of racing the first."""
    import fcntl
    fh = open(os.path.expanduser(f"~/private/fixtures/.{name}.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another {name} run is already in progress -- nothing done")
    return fh


def beta_connect(pwfile):
    import psycopg
    if not pwfile.is_file() or pwfile.stat().st_mode & 0o077:
        raise SystemExit(f"{pwfile}: must exist with mode 600")
    c = psycopg.connect(host=X.BHOST, port=5432, user=f"postgres.{X.BETA}", dbname="postgres", password=pwfile.read_text().strip(),
                        sslmode="require", connect_timeout=10, keepalives=1, keepalives_idle=30, keepalives_interval=10)
    c.execute("SET statement_timeout = 300000"); c.execute("SET lock_timeout = '10s'")
    return c


def plan(bc, rows=None):
    """rows: guaranteed-sale rows (X.fetch shape). Default = the live Neon view; a dry run may pass simulated rows."""
    rows = X.fetch() if rows is None else rows
    mat = VV.matrix()
    cards, sets, sales, skipped = X.build(rows, mat)
    checklist = {m["variant_id"] for m in mat}
    want = {s["sale_id"]: s for s in sales}
    ids = [r[0] for r in bc.execute("SELECT card_id FROM public.beta_catalog WHERE category = 'veefriends'")]
    idset = set(ids)
    meta = {r[0]: {"visibility": r[1], "number": r[2], "number_key": r[3]} for r in bc.execute(
        "SELECT card_id, visibility, number, number_key FROM public.beta_catalog WHERE card_id = ANY(%s)", (ids,))}
    have = {r[0]: {"card_id": r[1], "published": r[2], "price": float(r[3])} for r in bc.execute(
        "SELECT sale_id, card_id, published, price FROM public.beta_sales WHERE card_id = ANY(%s)", (ids,))}
    redirects = {r[0]: (r[1], r[2]) for r in bc.execute(
        "SELECT old_card_id, new_card_id, reason FROM public.beta_redirects WHERE old_card_id = ANY(%s)", (ids,))}
    moved = {k: (v["card_id"], want[k]["card_id"]) for k, v in have.items() if k in want and want[k]["card_id"] != v["card_id"]}
    retract = {k: v["card_id"] for k, v in have.items() if k not in want and v["published"]}
    republish = {k: want[k]["card_id"] for k, v in have.items() if k in want and not v["published"]}
    insert = [want[k] for k in want if k not in have]
    live = {s["card_id"] for s in want.values()}
    add_cards = sorted(set(cards) - idset)
    restore = sorted(c for c in ids if c in checklist and meta[c]["visibility"] != "visible")
    bad_redirects = {o: redirects[o] for o in redirects if o in checklist}
    nk_fix = {c: (m["number_key"], X.number_key(m["number"])) for c, m in meta.items()
              if m["number"] and m["number_key"] != X.number_key(m["number"])}
    withdraw = [c for c in ids if c not in live and c not in checklist and meta[c]["visibility"] == "visible"]
    targets = collections.defaultdict(set)
    for old, new in moved.values():
        targets[old].add(new)
    retracted_from = set(retract.values())
    redirect = {c: next(iter(targets[c])) for c in withdraw if len(targets[c]) == 1 and c not in retracted_from}
    ref_only = [c for c in cards if c not in live]
    return {"cards": cards, "sets": sets, "have": have, "moved": moved, "retract": retract, "republish": republish, "insert": insert,
            "add_cards": add_cards, "restore": restore, "bad_redirects": bad_redirects, "nk_fix": nk_fix,
            "withdraw": withdraw, "redirect": redirect, "checklist": checklist, "live": live, "ref_only": ref_only,
            "skipped": skipped, "beta_ids": idset}


def summary(p):
    usd = lambda keys: sum(p["have"][k]["price"] for k in keys)
    kinds = collections.Counter("same card, parallel corrected" if o.split("~", 1)[0] == n.split("~", 1)[0] else "different card"
                                for o, n in p["moved"].values())
    return [f"beta VeeFriends sales {len(p['have']):,} | catalog variants {len(p['beta_ids']):,}",
            f"  checklist versions {len(p['checklist']):,} (with a guaranteed sale {len(p['checklist'] & p['live']):,}, "
            f"reference-only {len(p['checklist'] - p['live']):,})",
            f"  add catalog rows {len(p['add_cards']):,}   restore withdrawn checklist rows {len(p['restore']):,}   "
            f"remove wrong redirects {len(p['bad_redirects']):,}   number_key fixes {len(p['nk_fix']):,}",
            f"  re-point {len(p['moved']):,} (${usd(p['moved']):,.0f}) {dict(kinds)}   retract {len(p['retract']):,} "
            f"(${usd(p['retract']):,.0f})   republish {len(p['republish']):,} (${usd(p['republish']):,.0f})   "
            f"insert {len(p['insert']):,} (${sum(s['price'] for s in p['insert']):,.0f})",
            f"  withdraw {len(p['withdraw']):,} non-checklist variants (redirect {len(p['redirect']):,})"]


def refresh_summaries(bc, cur):
    n = 0
    while True:
        cur.execute("SELECT public.beta_refresh_summaries(500)"); k = cur.fetchone()[0]; bc.commit(); n += k
        if k == 0:
            return n


CCOLS = ["card_id", "name", "player_key", "set_name", "number", "season_year", "category", "parallel", "image_kind", "aliases",
         "featured", "set_id", "source_card_id", "name_key", "number_key", "print_run", "source_hash", "source_version"]
SCOLS = ["sale_id", "card_id", "venue", "provider", "source_transaction_id", "source_url", "price", "currency", "sold_at",
         "date_precision", "grade", "verified", "price_eligible", "published", "sold_status", "best_offer", "actual_price_known",
         "identity_reviewed", "evidence_note", "occurrence_key"]


def crow(c):
    return (c["card_id"], c["name"], c["player_key"], c["set_name"], c["number"], c["season_year"], "veefriends", c["parallel"],
            "reference", c["aliases"], False, c["_set_id"], c["card_id"], X.name_key(c["name"]), X.number_key(c["number"]),
            c["_print_run"], X.sha(X.public(c)), 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", type=Path)
    ap.add_argument("--revert-inserts", action="store_true")
    ap.add_argument("--yes-i-understand-beta", action="store_true")
    ap.add_argument("--beta-password-file", type=Path, default=Path(os.path.expanduser("~/private/beta-password")))
    a = ap.parse_args()
    _lock = single_run_lock("correct_veefriends_beta")
    bc = beta_connect(a.beta_password_file)
    cur = bc.cursor()

    if a.revert:
        if not a.yes_i_understand_beta:
            raise SystemExit("pass --yes-i-understand-beta to revert")
        b = json.load(open(a.revert))
        if X.other_writers(cur):
            raise SystemExit("another session is writing to the beta right now -- nothing reverted")
        cur.executemany("UPDATE public.beta_sales SET card_id = %s, published = %s WHERE sale_id = %s",
                        [(v["card_id"], v["published"], k) for k, v in b["sales"].items()])
        cur.executemany("DELETE FROM public.beta_redirects WHERE old_card_id = %s AND new_card_id = %s", list(b["redirects"].items()))
        cur.executemany("INSERT INTO public.beta_redirects (old_card_id, new_card_id, reason) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        [(o, n, r) for o, (n, r) in b.get("removed_redirects", {}).items()])
        cur.executemany("UPDATE public.beta_catalog SET visibility = %s WHERE card_id = %s", [(v, k) for k, v in b["visibility"].items()])
        cur.executemany("UPDATE public.beta_catalog SET number_key = %s WHERE card_id = %s", [(v, k) for k, v in b.get("number_key", {}).items()])
        if a.revert_inserts:
            cur.executemany("UPDATE public.beta_sales SET published = false WHERE sale_id = %s", [(k,) for k in b["inserted"]])
            cur.executemany("UPDATE public.beta_catalog SET visibility = 'withdrawn' WHERE card_id = %s", [(k,) for k in b.get("added_cards", [])])
        bc.commit()
        n = refresh_summaries(bc, cur)
        print(f"REVERTED from {a.revert.name}: {len(b['sales'])} sales, {len(b['visibility'])} visibility, "
              f"{len(b['redirects'])} redirects removed, {len(b.get('removed_redirects', {}))} redirects put back, "
              f"{len(b.get('number_key', {}))} number_keys; summaries {n}")
        return 0

    p = plan(bc)
    print("\n".join(summary(p)))
    if not a.apply:
        print("dry run: nothing written."); return 0
    if not a.yes_i_understand_beta:
        print("pass --yes-i-understand-beta to apply. Nothing written."); return 0
    if X.other_writers(cur):
        raise SystemExit("another session is writing to the beta right now -- nothing written")

    OUT.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    touched = set(p["moved"]) | set(p["retract"]) | set(p["republish"])
    backup = {"taken_at": ts,
              "sales": {k: {"card_id": p["have"][k]["card_id"], "published": p["have"][k]["published"]} for k in touched},
              "visibility": {**{c: "visible" for c in p["withdraw"]}, **{c: "withdrawn" for c in p["restore"]}},
              "redirects": p["redirect"], "removed_redirects": p["bad_redirects"],
              "number_key": {c: old for c, (old, _) in p["nk_fix"].items()},
              "inserted": [s["sale_id"] for s in p["insert"]], "added_cards": p["add_cards"]}
    bpath = OUT / f"backup_{ts}.json"
    json.dump(backup, open(bpath, "w"), indent=1)
    os.chmod(bpath, 0o600)
    print(f"  backup written: {bpath}")

    cur.executemany("INSERT INTO public.beta_sets (set_id, name, category, season_year, source, source_hash, source_version) "
                    "VALUES (%s,%s,%s,%s,%s,%s,0) ON CONFLICT DO NOTHING",
                    [(s["set_id"], s["name"], "veefriends", s["season_year"], "mazi", s["source_hash"]) for s in p["sets"].values()])
    for i in range(0, len(p["add_cards"]), 500):
        cur.executemany(f"INSERT INTO public.beta_catalog ({','.join(CCOLS)}) VALUES ({','.join(['%s'] * len(CCOLS))}) ON CONFLICT DO NOTHING",
                        [crow(p["cards"][c]) for c in p["add_cards"][i:i + 500]])
        bc.commit()
    print(f"  1/7 catalog rows added: {len(p['add_cards']):,}")
    cur.executemany("UPDATE public.beta_catalog SET visibility = 'visible' WHERE card_id = %s", [(c,) for c in p["restore"]])
    cur.executemany("DELETE FROM public.beta_redirects WHERE old_card_id = %s", [(o,) for o in p["bad_redirects"]])
    bc.commit(); print(f"  2/7 restored: {len(p['restore']):,}; wrong redirects removed: {len(p['bad_redirects']):,}")
    cur.executemany("UPDATE public.beta_catalog SET number_key = %s WHERE card_id = %s", [(new, c) for c, (_, new) in p["nk_fix"].items()])
    bc.commit(); print(f"  3/7 number_key fixed: {len(p['nk_fix']):,}")
    cur.executemany("UPDATE public.beta_sales SET card_id = %s WHERE sale_id = %s AND card_id = %s",
                    [(new, k, old) for k, (old, new) in p["moved"].items()])
    bc.commit(); print(f"  4/7 re-pointed: {len(p['moved']):,}")
    cur.executemany("UPDATE public.beta_sales SET published = false WHERE sale_id = %s AND card_id = %s", list(p["retract"].items()))
    cur.executemany("UPDATE public.beta_sales SET published = true WHERE sale_id = %s AND card_id = %s", list(p["republish"].items()))
    bc.commit(); print(f"  5/7 retracted (published=false): {len(p['retract']):,}; republished: {len(p['republish']):,}")
    cur.executemany(f"INSERT INTO public.beta_sales ({','.join(SCOLS)}) VALUES ({','.join(['%s'] * len(SCOLS))}) ON CONFLICT DO NOTHING",
                    [tuple(s.get(k) if k != "occurrence_key" else s["_occurrence_key"] for k in SCOLS) for s in p["insert"]])
    bc.commit(); print(f"  6/7 sales inserted: {len(p['insert']):,}")
    cur.executemany("UPDATE public.beta_catalog SET visibility = 'withdrawn' WHERE card_id = %s", [(c,) for c in p["withdraw"]])
    cur.executemany("INSERT INTO public.beta_redirects (old_card_id, new_card_id, reason) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    [(o, n, "VeeFriends variant not on the checklist; its sales moved here (2026-09-29)") for o, n in p["redirect"].items()])
    bc.commit(); print(f"  7/7 withdrawn: {len(p['withdraw']):,} (redirects {len(p['redirect']):,})")
    n = refresh_summaries(bc, cur)
    cur.execute("INSERT INTO public.beta_import_runs (source, cards, sales, note) VALUES (%s,%s,%s,%s)",
                (X.SOURCE + "-checklist", len(p["add_cards"]), len(p["insert"]),
                 f"checklist catalog: +{len(p['add_cards'])} rows (reference-only while unsold), restored {len(p['restore'])}, "
                 f"number_key {len(p['nk_fix'])}, re-pointed {len(p['moved'])}, retracted {len(p['retract'])}, "
                 f"republished {len(p['republish'])}, "
                 f"withdrew {len(p['withdraw'])}; backup {bpath.name}"))
    bc.commit()
    print(f"APPLY COMPLETE (summaries {n}). Undo: python3 tools/correct_veefriends_beta.py --revert {bpath} --yes-i-understand-beta")
    return 0


if __name__ == "__main__":
    sys.exit(main())
