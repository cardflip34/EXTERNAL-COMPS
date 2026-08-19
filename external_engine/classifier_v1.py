#!/usr/bin/env python3
"""Classifier v1 (PRD V2 §35) — category + sport with confidence, as a SIDE TABLE over the canonical store.

Layers (all SQL in DuckDB, runs over 3.3M rows in ~1-3 min under a memory cap):
  1. category: explicit franchise / product signals (pokemon, yugioh, magic, other TCG, veefriends, coins,
     watches, comics, funko, video games) else 'sports' (then sport) else 'unknown'.
  2. sport — explicit: league tokens (NBA/NFL/MLB/NHL/WNBA/UFC/MLS/PGA/F1/NASCAR/WWE…), unambiguous team
     nicknames, sport-specific product lines (Hoops, Young Guns, Bowman, Contenders Football, UEFA…)  → HIGH
  3. sport — subject map: for every primary_subject_query, majority vote of explicit signals over ITS titles
     (e.g. "Shohei Ohtani" → baseball 0.97) → HIGH/MEDIUM by share + support. Persisted as JSON (roster seed).
  4. sport — brand lean (Bowman→baseball, Prizm/Select with no sport word → unknown)               → LOW
Confidence: HIGH | MEDIUM | LOW | NONE.  Never rewrites canonical; writes
  external_store/classification/classification_v1.parquet   (observation_key, category, category_conf, sport, sport_conf, method)
  external_store/classification/subject_sport_map.json      (subject → sport, share, n)
  external_store/classification/summary_v1.json / .md
Usage: .venv_extcomps/bin/python external_engine/classifier_v1.py [--store DIR] [--memory-limit 1000MB] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone

import duckdb

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
CLASSIFIER_VERSION = "1.0.1"

# --- category signals (lowercased title + subject) ---------------------------------------------------
CATEGORY_RULES = [  # (category, regex) — first match wins
    ("pokemon", r"\b(pokemon|pok[eé]mon|pikachu|charizard|mewtwo|eevee|umbreon|gengar|lugia|rayquaza|blastoise|venusaur|"
                r"vmax|vstar|gx|ex card|paldea|scarlet & violet|obsidian flames|crown zenith|brilliant stars|evolving skies|"
                r"base set|jungle|fossil|neo genesis|team rocket|shadowless|1st edition holo|illustration rare|151 |prismatic evolutions|"
                r"surging sparks|twilight masquerade|temporal forces|paradox rift|lost origin|silver tempest|fusion strike|chilling reign|"
                r"celebrations|hidden fates|shining fates|champion'?s path|sword & shield|sun & moon|xy |black & white|heartgold|"
                r"pokémon center|tcg)\b"),
    ("yugioh", r"\b(yu-?gi-?oh|yugioh|dark magician|blue-eyes|exodia|kaiba|yugi)\b"),
    ("magic", r"\b(mtg|magic the gathering|magic: the gathering|black lotus|mox |planeswalker|commander deck)\b"),
    ("tcg_other", r"\b(one piece card|one piece tcg|dragon ball super card|lorcana|flesh and blood|weiss schwarz|digimon card|"
                  r"metazoo|cardfight|union arena|star wars unlimited)\b"),
    ("veefriends", r"\b(veefriends|vee friends|vfriends|vee-friends)\b"),   # zerocool/gary vee alone over-match (Topps zerocool Star Wars etc.)
    ("coins", r"\b(morgan dollar|peace dollar|silver eagle|half dollar|numismatic|pcgs ms|ngc ms|double eagle|saint-?gaudens|"
              r"walking liberty|mercury dime|buffalo nickel|krugerrand|maple leaf coin|american eagle coin|quarter eagle|half eagle|"
              r"liberty head|bullion|troy oz|\d+ coin\b|coins?\b.*\b(lot|roll|proof|uncirculated)|proof set|mint set|ms-?6\d|ms-?70)\b"),
    ("watches", r"\b(rolex|patek philippe|audemars piguet|omega seamaster|omega speedmaster|cartier|panerai|grand seiko|tudor|"
                r"iwc|tag heuer|breitling|jaeger-?lecoultre|hublot|chopard|richard mille|vacheron|a\. lange|wristwatch|chronograph)\b"),
    ("comics", r"\b(cgc \d\.\d|cbcs|comic|marvel comics|dc comics|first appearance|1st appearance|x-men #|spider-man #|batman #)\b"),
    ("funko", r"\b(funko|pop! vinyl|funko pop)\b"),
    ("video_games", r"\b(wata|vga graded|nintendo|playstation|xbox|sega genesis|game boy|nes |snes |n64)\b"),
]

# --- sport explicit signals ----------------------------------------------------------------------------
LEAGUE = {
    "basketball": r"\b(nba|basketball|hoops|court kings|prizm basketball|select basketball|donruss basketball|optic basketball|"
                  r"mosaic basketball|fleer basketball|skybox|topps basketball|slam dunk|all-nba|nba finals|mcdonald'?s all american)\b",
    "football": r"\b(nfl|football|gridiron|contenders football|prizm football|select football|donruss football|optic football|"
                r"mosaic football|score football|playoff contenders|nfl draft|super bowl|heisman|college football|ncaa football)\b",
    "baseball": r"\b(mlb|baseball|bowman|topps chrome baseball|topps series|topps update|stadium club|heritage baseball|topps now|"
                r"topps finest|topps archives|allen & ginter|gypsy queen|donruss baseball|prizm baseball|home run|world series|"
                r"all-star game|cy young|rookie card rc baseball|minor league)\b",
    "hockey": r"\b(nhl|hockey|young guns|upper deck hockey|o-pee-chee|opc |sp authentic hockey|the cup hockey|ice hockey|"
              r"stanley cup|ahl|chl|ohl|whl|khl)\b",
    "soccer": r"\b(soccer|futbol|fútbol|football club|premier league|uefa|champions league|bundesliga|la liga|serie a|ligue 1|"
              r"mls|world cup|euro 20\d\d|copa america|topps chrome uefa|panini select fifa|fifa|merlin|obsidian soccer|"
              r"prizm premier league|donruss soccer|select la liga|real madrid|barcelona|manchester united|man utd|liverpool fc|"
              r"arsenal|chelsea fc|bayern|psg|juventus|inter miami)\b",
    "wnba": r"\b(wnba|caitlin clark|angel reese|paige bueckers|a'?ja wilson|sabrina ionescu|breanna stewart)\b",
    "ufc_mma": r"\b(ufc|mma|bellator|conor mcgregor|jon jones|khabib|israel adesanya|alex pereira|ilia topuria)\b",
    "wrestling": r"\b(wwe|aew|wrestling|wrestler|hulk hogan|stone cold|undertaker|john cena|roman reigns|cody rhodes|rey mysterio|ric flair)\b",
    "racing": r"\b(f1|formula 1|formula one|nascar|indycar|verstappen|hamilton f1|lando norris|leclerc|topps chrome f1|turbo attax|"
              r"dale earnhardt|kyle larson|chase elliott)\b",
    "golf": r"\b(golf|pga|masters|tiger woods|scottie scheffler|rory mcilroy|sgc 10 golf|upper deck golf)\b",
    "tennis": r"\b(tennis|wimbledon|us open tennis|roland garros|alcaraz|jannik sinner|serena williams|federer|nadal|djokovic|coco gauff)\b",
    "boxing": r"\b(boxing|muhammad ali|mike tyson|canelo|floyd mayweather|tyson fury|jake paul)\b",
}
TEAMS = {  # unambiguous nicknames only (ambiguous ones like Kings/Jets/Cardinals/Panthers/Giants/Rangers/Stars are excluded)
    "basketball": ["lakers", "celtics", "warriors", "bulls", "knicks", "nets", "heat", "spurs", "mavericks", "nuggets", "bucks",
                   "76ers", "sixers", "suns", "rockets", "thunder", "grizzlies", "pelicans", "timberwolves", "trail blazers",
                   "blazers", "jazz", "clippers", "hawks", "hornets", "cavaliers", "cavs", "pistons", "pacers", "raptors", "wizards"],
    "football": ["chiefs", "eagles", "cowboys", "49ers", "niners", "packers", "steelers", "patriots", "bills", "dolphins", "ravens",
                 "bengals", "browns", "texans", "colts", "jaguars", "titans", "broncos", "raiders", "chargers", "seahawks", "rams",
                 "falcons", "saints", "buccaneers", "bucs", "bears", "lions", "vikings", "commanders", "redskins"],
    "baseball": ["yankees", "dodgers", "red sox", "cubs", "mets", "braves", "astros", "phillies", "padres", "orioles", "blue jays",
                 "mariners", "guardians", "indians", "twins", "tigers", "royals", "white sox", "brewers", "reds", "pirates", "rockies",
                 "diamondbacks", "marlins", "nationals", "athletics", "angels"],
    "hockey": ["maple leafs", "canadiens", "bruins", "blackhawks", "red wings", "penguins", "capitals", "oilers", "flames", "canucks",
               "avalanche", "golden knights", "lightning", "hurricanes", "devils", "islanders", "flyers", "sabres", "senators",
               "predators", "wild", "ducks", "sharks", "kraken", "blue jackets", "coyotes", "mammoth"],
}
BRAND_LEAN = {  # LOW-confidence fallback when nothing explicit matched
    "baseball": r"\b(bowman|topps chrome|topps update|topps series|heritage|stadium club|topps now|topps finest)\b",
    "hockey": r"\b(upper deck|sp authentic|artifacts|synergy|black diamond|ice )\b",
    "basketball": r"\b(court kings|hoops|crown royale|revolution|noir basketball)\b",
    "football": r"\b(playbook|contenders|absolute football|certified football|limited football|gold standard)\b",
}


def _q(rx: str) -> str:
    """escape single quotes for embedding a regex in a SQL string literal"""
    return rx.replace("'", "''")


def _or(words: list[str]) -> str:
    return r"\b(" + "|".join(re.escape(w) for w in words) + r")\b"


def build_sql_case(col: str) -> tuple[str, str]:
    """Return (category_case, sport_explicit_case) SQL expressions over a lowercased text column."""
    cat = "CASE " + " ".join(f"WHEN regexp_matches({col}, '{_q(rx)}') THEN '{c}'" for c, rx in CATEGORY_RULES) + " ELSE 'sports' END"
    sp_parts = []
    for sport, rx in LEAGUE.items():
        sp_parts.append(f"WHEN regexp_matches({col}, '{_q(rx)}') THEN '{sport}'")
    for sport, teams in TEAMS.items():
        sp_parts.append(f"WHEN regexp_matches({col}, '{_q(_or(teams))}') THEN '{sport}'")
    sport_explicit = "CASE " + " ".join(sp_parts) + " ELSE NULL END"
    return cat, sport_explicit


def build_lean_case(col: str) -> str:
    return "CASE " + " ".join(f"WHEN regexp_matches({col}, '{_q(rx)}') THEN '{s}'" for s, rx in BRAND_LEAN.items()) + " ELSE NULL END"


def run(store: str, memory_limit: str, limit: int | None) -> dict:
    t0 = time.time()
    out = os.path.join(store, "external_store", "classification"); os.makedirs(out, exist_ok=True)
    canon = os.path.join(store, "external_store", "parquet", "source=ebay")
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory_limit}'; SET threads=1; SET temp_directory='{os.path.join(store, 'external_store', 'duckdb_tmp')}'")
    lim = f" LIMIT {limit}" if limit else ""
    cat_case, sp_case = build_sql_case("t")
    lean_case = build_lean_case("t")
    # 1) base: lowercased text per row
    con.execute(f"""
        CREATE TEMP TABLE base AS
        SELECT observation_key, primary_subject_query AS subject,
               lower(COALESCE(title,'') || ' ' || COALESCE(primary_subject_query,'')) AS t
        FROM read_parquet('{canon}/**/*.parquet', hive_partitioning=true, union_by_name=true){lim}""")
    n = con.execute("SELECT count(*) FROM base").fetchone()[0]
    print(f"[clf] rows: {n:,} ({time.time()-t0:.0f}s)", flush=True)
    # 2) explicit category + sport
    con.execute(f"""
        CREATE TEMP TABLE explicit AS
        SELECT observation_key, subject, t, {cat_case} AS category, {sp_case} AS sport_explicit, {lean_case} AS sport_lean
        FROM base""")
    print(f"[clf] explicit done ({time.time()-t0:.0f}s)", flush=True)
    # 3) subject → sport map (votes from explicit signals among sports rows)
    con.execute("""
        CREATE TEMP TABLE subject_votes AS
        SELECT subject, sport_explicit AS sport, count(*) AS votes
        FROM explicit WHERE subject IS NOT NULL AND category='sports' AND sport_explicit IS NOT NULL
        GROUP BY subject, sport_explicit""")
    con.execute("""
        CREATE TEMP TABLE subject_map AS
        SELECT subject, sport, votes, total, votes::DOUBLE/total AS share
        FROM (SELECT *, sum(votes) OVER (PARTITION BY subject) AS total,
                     row_number() OVER (PARTITION BY subject ORDER BY votes DESC) AS rn FROM subject_votes)
        WHERE rn = 1""")
    smap = con.execute("SELECT subject, sport, votes, total, round(share,3) FROM subject_map ORDER BY total DESC").fetchall()
    with open(os.path.join(out, "subject_sport_map.json"), "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "classifier_version": CLASSIFIER_VERSION,
                   "subjects": [{"subject": s, "sport": sp, "votes": v, "total": tt, "share": sh} for s, sp, v, tt, sh in smap]}, f, indent=1)
    print(f"[clf] subject map: {len(smap):,} subjects ({time.time()-t0:.0f}s)", flush=True)
    # 4) final assignment with confidence
    con.execute("""
        CREATE TEMP TABLE final AS
        SELECT e.observation_key,
               e.category,
               CASE WHEN e.category <> 'sports' THEN 'HIGH' ELSE
                    CASE WHEN e.sport_explicit IS NOT NULL OR (m.share >= 0.8 AND m.total >= 20) THEN 'HIGH'
                         WHEN (m.share >= 0.5 AND m.total >= 5) OR e.sport_lean IS NOT NULL THEN 'MEDIUM'
                         WHEN m.sport IS NOT NULL THEN 'LOW' ELSE 'NONE' END END AS category_conf,
               CASE WHEN e.category <> 'sports' THEN NULL
                    WHEN e.sport_explicit IS NOT NULL THEN e.sport_explicit
                    WHEN m.sport IS NOT NULL AND (m.share >= 0.5 AND m.total >= 5) THEN m.sport
                    WHEN e.sport_lean IS NOT NULL THEN e.sport_lean
                    WHEN m.sport IS NOT NULL THEN m.sport
                    ELSE 'unknown' END AS sport,
               CASE WHEN e.category <> 'sports' THEN NULL
                    WHEN e.sport_explicit IS NOT NULL THEN 'HIGH'
                    WHEN m.sport IS NOT NULL AND m.share >= 0.8 AND m.total >= 20 THEN 'HIGH'
                    WHEN m.sport IS NOT NULL AND m.share >= 0.5 AND m.total >= 5 THEN 'MEDIUM'
                    WHEN e.sport_lean IS NOT NULL THEN 'LOW'
                    WHEN m.sport IS NOT NULL THEN 'LOW' ELSE 'NONE' END AS sport_conf,
               CASE WHEN e.category <> 'sports' THEN 'category_rule'
                    WHEN e.sport_explicit IS NOT NULL THEN 'explicit_signal'
                    WHEN m.sport IS NOT NULL AND m.share >= 0.5 AND m.total >= 5 THEN 'subject_map'
                    WHEN e.sport_lean IS NOT NULL THEN 'brand_lean'
                    WHEN m.sport IS NOT NULL THEN 'subject_map_weak' ELSE 'none' END AS method,
               '""" + CLASSIFIER_VERSION + """' AS classifier_version
        FROM explicit e LEFT JOIN subject_map m ON m.subject = e.subject""")
    path = os.path.join(out, "classification_v1.parquet")
    con.execute(f"COPY final TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    # 5) summary
    cat = con.execute("SELECT category, count(*) FROM final GROUP BY 1 ORDER BY 2 DESC").fetchall()
    sport = con.execute("SELECT sport, count(*) FROM final WHERE category='sports' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    conf = con.execute("SELECT sport_conf, count(*) FROM final WHERE category='sports' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    method = con.execute("SELECT method, count(*) FROM final GROUP BY 1 ORDER BY 2 DESC").fetchall()
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "classifier_version": CLASSIFIER_VERSION,
               "rows": n, "category": dict(cat), "sport": dict(sport), "sport_confidence": dict(conf), "method": dict(method),
               "subjects_mapped": len(smap), "seconds": round(time.time() - t0, 1), "output": path}
    with open(os.path.join(out, "summary_v1.json"), "w") as f:
        json.dump(summary, f, indent=1)
    md = [f"# Classification v1 summary ({summary['generated_at'][:19]}Z)", "", f"rows {n:,} · subjects mapped {len(smap):,} · {summary['seconds']}s", "",
          "## Category", "| category | rows |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in cat] + \
         ["", "## Sport (sports rows)", "| sport | rows |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in sport] + \
         ["", "## Sport confidence", "| conf | rows |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in conf] + \
         ["", "## Method", "| method | rows |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in method]
    with open(os.path.join(out, "summary_v1.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print(json.dumps(summary, indent=1))
    con.close()
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=STORE); ap.add_argument("--memory-limit", default="1000MB"); ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    run(a.store, a.memory_limit, a.limit)
