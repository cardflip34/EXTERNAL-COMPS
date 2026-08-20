#!/usr/bin/env python3
"""Roster seeds mined from the canonical store + classification_v1 (PRD V2 §23/§24 discovery audits).

Writes (for operator curation — these are SEEDS, not final rosters):
  external_store/rosters/pokemon_roster_seed.json    characters (from player_query/subject), sets, graders, price bands
  external_store/rosters/veefriends_roster_seed.json series/product tokens, character bigrams, graders
  external_store/rosters/sports_subject_sport_seed.json  subject → sport (from classifier subject map) with row counts
Usage: .venv_extcomps/bin/python external_engine/roster_seeds.py [--store DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

import duckdb

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")

POKEMON_SETS = ["base set", "jungle", "fossil", "team rocket", "gym heroes", "gym challenge", "neo genesis", "neo discovery",
    "neo revelation", "neo destiny", "legendary collection", "expedition", "aquapolis", "skyridge", "ex ruby", "ex sapphire",
    "crystal guardians", "ex dragon frontiers", "diamond & pearl", "platinum", "heartgold", "call of legends", "black & white",
    "plasma", "xy", "flashfire", "phantom forces", "primal clash", "roaring skies", "ancient origins", "breakthrough", "breakpoint",
    "generations", "fates collide", "steam siege", "evolutions", "sun & moon", "guardians rising", "burning shadows", "shining legends",
    "crimson invasion", "ultra prism", "forbidden light", "celestial storm", "dragon majesty", "lost thunder", "team up", "detective pikachu",
    "unbroken bonds", "unified minds", "hidden fates", "cosmic eclipse", "sword & shield", "rebel clash", "darkness ablaze",
    "champion's path", "vivid voltage", "shining fates", "battle styles", "chilling reign", "evolving skies", "celebrations",
    "fusion strike", "brilliant stars", "astral radiance", "pokemon go", "lost origin", "silver tempest", "crown zenith",
    "scarlet & violet", "paldea evolved", "obsidian flames", "151", "paradox rift", "paldean fates", "temporal forces",
    "twilight masquerade", "shrouded fable", "stellar crown", "surging sparks", "prismatic evolutions", "journey together",
    "destined rivals", "black bolt", "white flare", "mega evolution", "japanese", "promo", "shadowless", "1st edition"]
VEE_TOKENS = ["series 1", "series 2", "series 3", "compete and collect", "compete & collect", "zerocool", "core", "rare", "very rare",
    "epic", "legendary", "1/1", "gold", "silver", "holo", "autograph", "auto", "emoji", "psa 10", "psa 9", "bgs", "cgc", "sgc",
    "gary vee", "garyvee", "box", "pack", "sealed", "topps", "uno", "funko"]
STOP = set("the and of for with card cards a an in to rc rookie psa bgs sgc cgc 10 9 8 gem mint holo rare very epic legendary series 1 2 3 4 5 6 7 8 "
           "veefriends vee friends vf gary vee garyvee zerocool compete collect topps uno - | & # / ( ) graded auto autograph".split())


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--store", default=STORE); a = ap.parse_args()
    canon = os.path.join(a.store, "external_store", "parquet")  # ALL sources (multi-source canonical)
    clf = os.path.join(a.store, "external_store", "classification", "classification_v1.parquet")
    out = os.path.join(a.store, "external_store", "rosters"); os.makedirs(out, exist_ok=True)
    con = duckdb.connect(); con.execute("SET memory_limit='800MB'; SET threads=1")
    con.execute(f"""CREATE TEMP TABLE j AS
        SELECT c.observation_key, c.title, c.primary_subject_query AS subject, c.grade_company, c.grade, c.sold_price_usd,
               c.valuation_gate, k.category, k.sport
        FROM read_parquet('{canon}/**/*.parquet', hive_partitioning=true, union_by_name=true) c
        JOIN read_parquet('{clf}') k USING (observation_key)""")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- Pokémon ---
    chars = con.execute("""SELECT subject, count(*) n, median(sold_price_usd) med FROM j WHERE category='pokemon' AND subject IS NOT NULL
                           GROUP BY subject ORDER BY n DESC LIMIT 400""").fetchall()
    sets = Counter(); rows = con.execute("SELECT lower(title) FROM j WHERE category='pokemon'").fetchall()
    for (t,) in rows:
        for s in POKEMON_SETS:
            if s in t:
                sets[s] += 1
    graders = con.execute("SELECT COALESCE(grade_company,'raw'), count(*) FROM j WHERE category='pokemon' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    bands = con.execute("""SELECT CASE WHEN sold_price_usd<10 THEN '<10' WHEN sold_price_usd<50 THEN '10-50' WHEN sold_price_usd<200 THEN '50-200'
                           WHEN sold_price_usd<1000 THEN '200-1k' WHEN sold_price_usd<10000 THEN '1k-10k' ELSE '>=10k' END b, count(*)
                           FROM j WHERE category='pokemon' AND valuation_gate='ok' GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    total_p = con.execute("SELECT count(*) FROM j WHERE category='pokemon'").fetchone()[0]
    json.dump({"generated_at": now, "rows_pokemon": total_p, "note": "SEED for curation; characters = player_query subjects seen in the matrix scrubs; sets by title substring",
               "characters": [{"name": s, "rows": n, "median_price": m} for s, n, m in chars],
               "sets": [{"set": s, "rows": n} for s, n in sets.most_common()],
               "graders": dict(graders), "price_bands_verified": dict(bands)},
              open(os.path.join(out, "pokemon_roster_seed.json"), "w"), indent=1, default=str)

    # --- VeeFriends ---
    vrows = con.execute("SELECT title, grade_company, sold_price_usd FROM j WHERE category='veefriends'").fetchall()
    tok = Counter(); big = Counter()
    for t, g, p in vrows:
        lt = (t or "").lower()
        for v in VEE_TOKENS:
            if v in lt:
                tok[v] += 1
        words = [w for w in re.findall(r"[a-z][a-z'&-]+", lt) if w not in STOP]
        for i in range(len(words) - 1):
            big[f"{words[i]} {words[i+1]}"] += 1
    vg = Counter((g or "raw") for _, g, _ in vrows)
    json.dump({"generated_at": now, "rows_veefriends": len(vrows), "note": "SEED for curation; character/product bigrams need human review",
               "tokens": dict(tok.most_common()), "bigrams_top": [{"bigram": b, "rows": n} for b, n in big.most_common(300)],
               "graders": dict(vg)}, open(os.path.join(out, "veefriends_roster_seed.json"), "w"), indent=1)

    # --- sports subject→sport seed (from classifier subject map) ---
    smap = json.load(open(os.path.join(a.store, "external_store", "classification", "subject_sport_map.json")))
    json.dump({"generated_at": now, "note": "subject (player_query) → majority sport with support; seeds sports rosters by league",
               "subjects": smap["subjects"]}, open(os.path.join(out, "sports_subject_sport_seed.json"), "w"), indent=1)
    print(json.dumps({"pokemon_rows": total_p, "pokemon_characters": len(chars), "pokemon_sets": len(sets),
                      "veefriends_rows": len(vrows), "sports_subjects": len(smap["subjects"]), "out": out}, indent=1))
    con.close()


if __name__ == "__main__":
    main()
