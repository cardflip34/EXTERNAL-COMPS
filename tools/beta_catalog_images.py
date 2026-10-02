#!/usr/bin/env python3
"""Fill beta_catalog.image_small / image_large for SportsCardsPro-derived cards in the isolated
Mazi beta (Supabase jzxgtvxcuukxqkbwbuxg) from the SCP catalogue photo archive on the Mini.

WHY: the card page renders `card.image_large || card.image_small` straight from the catalog row
(mazidex-web-prototype lib/card-page.mjs). Pokemon rows carry pokemontcg.io URLs; every sports
card is NULL (docs/CURRENT-STATUS.md, 2026-09-18: "All 49 sports cards have no card image"), so
the UI falls back to a portrait or an initials placeholder. Filling the two columns lights up every
surface with zero front-end change. The catalog re-importer (scripts/backfill-catalog.py) upserts
every column EXCEPT the image ones, so what is written here survives future catalog imports.

HOW: join the archive's slug-keyed map to beta_catalog through the beta's OWN function
public.beta_scp_slug(card_id,set_id) -- the exact crosswalk the SCP history lane uses -- and set the
URL only where both image columns are NULL. image_kind stays 'reference' (the schema allows only
'reference'|'captured'; a catalogue photo IS the reference). Undo is by URL prefix.

The image URLs point at the Mini's public endpoint (Tailscale Funnel). That is fine for the beta
and reversible; the durable home is R2 (images.mazidex.com) -- rebuild the map with R2 URLs after an
upload, --undo, --apply, and the same rows repoint.

Modes (each needs --beta-password-file, mode 600, like the front end's own scripts):
  --verify            read-only: load map, compute matches, report counts + Kobe rows, ROLLBACK
  --apply [--limit N] write; --limit for a canary; requires --yes-i-understand-beta
  --undo              clear image columns where the URL carries our prefix; requires --yes...
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

BETA = "jzxgtvxcuukxqkbwbuxg"
HOST = "aws-0-us-west-1.pooler.supabase.com"   # session-mode pooler: TEMP tables + COPY need a session
MAP_DEFAULT = "/Volumes/MAZI_EVIDENCE_6TB/comp_images/_backfill/refmap/beta_catalog_images.csv"
OUR_PREFIX = "https://stavross-mac-mini.tail9fccf8.ts.net/"
BATCH = 20000     # rows per UPDATE commit
PAGE = 100000     # catalog cards per scan page (PK range); ~90 pages cover the 9M SCP rows


def connect(pw_file: Path, stmt_ms: int):
    import psycopg
    if not pw_file.is_file() or pw_file.stat().st_mode & 0o077:
        raise SystemExit(f"{pw_file}: must exist with private permissions (chmod 600)")
    # keepalives: a 25-minute silent join vanished server-side while this client waited forever
    # (2026-09-23) -- a NAT on the path dropped the idle TCP session. Keepalives hold the entry open
    # and surface a dead peer within a minute instead of never.
    conn = psycopg.connect(host=HOST, port=5432, user=f"postgres.{BETA}", dbname="postgres",
                           password=pw_file.read_text().strip(), sslmode="require", connect_timeout=10,
                           application_name="mazi-beta-catalog-images", autocommit=False,
                           keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
    # Set the timeout IN the session and read it back. Passing it as a startup option was silently
    # ignored by the pooler: a 30-minute value still had the slug join cancelled at ~2 minutes
    # (2026-09-23). Never trust a setting you have not read back from the server.
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(stmt_ms)}")
        cur.execute("SET lock_timeout = 5000")
        cur.execute("SHOW statement_timeout")
        print(f"statement_timeout in session: {cur.fetchone()[0]}", flush=True)
    conn.commit()
    return conn


def load_map(cur, path: str) -> int:
    cur.execute("CREATE TEMP TABLE img(slug text PRIMARY KEY, url text NOT NULL)")
    with open(path, newline="") as f, cur.copy("COPY img(slug,url) FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
        for chunk in iter(lambda: f.read(1 << 20), ""):
            cp.write(chunk)
    cur.execute("SELECT count(*) FROM img")
    return cur.fetchone()[0]


def build_matches(cur) -> dict:
    t0 = time.time()
    # Pre-filter by set before touching the slug function. The map names ~14K sets; the catalog
    # holds 9M SCP rows across far more. The set_id is the slug's console part carrying the sport
    # code beta_scp_slug() itself uses (baseball->bb, ...), i.e. the function's own rule inverted.
    # Unknown sports fall out as NULL and are dropped -- they are not sports cards anyway.
    cur.execute("""CREATE TEMP TABLE sets AS
        SELECT DISTINCT set_id FROM (
          SELECT 'scp:' || CASE split_part(split_part(slug,'/',1),'-cards-',1)
                   WHEN 'baseball' THEN 'bb' WHEN 'football' THEN 'fb' WHEN 'basketball' THEN 'bk'
                   WHEN 'soccer' THEN 'sc' WHEN 'hockey' THEN 'hk' WHEN 'wrestling' THEN 'wr'
                   WHEN 'racing' THEN 'rc' WHEN 'ufc' THEN 'ufc' WHEN 'tennis' THEN 'tn'
                   WHEN 'golf' THEN 'gf' WHEN 'boxing' THEN 'bx' END
                 || ':' || split_part(split_part(slug,'/',1),'-cards-',2) AS set_id
          FROM img WHERE split_part(slug,'/',1) LIKE '%-cards-%') x
        WHERE set_id IS NOT NULL""")
    cur.execute("ALTER TABLE sets ADD PRIMARY KEY (set_id)")
    cur.execute("SELECT count(*) FROM sets")
    print(f"sets named by the map: {cur.fetchone()[0]:,}", flush=True)
    # Walk the catalog by PRIMARY KEY in pages instead of one 9M-row statement. Each page is a short
    # query with a visible result, so progress is observable, a dropped connection costs one page,
    # and nothing sits silent long enough for a NAT to forget it. SCP ids all start with 'mazi:';
    # the walk starts there and the page over the tail of the key space returns nothing and stops.
    cur.execute("CREATE TEMP TABLE m(card_id text PRIMARY KEY, url text NOT NULL, has_image boolean NOT NULL)")
    last, scanned, pages = "mazi:", 0, 0
    while True:
        cur.execute("""WITH page AS (
                SELECT c.card_id, c.set_id, c.image_small, c.image_large
                FROM public.beta_catalog c
                WHERE c.card_id > %s AND c.card_id LIKE 'mazi:%%'
                ORDER BY c.card_id LIMIT %s),
            ins AS (
                INSERT INTO m
                SELECT p.card_id, i.url, (p.image_small IS NOT NULL OR p.image_large IS NOT NULL)
                FROM page p JOIN sets s ON s.set_id = p.set_id
                JOIN img i ON i.slug = public.beta_scp_slug(p.card_id, p.set_id)
                ON CONFLICT DO NOTHING RETURNING 1)
            SELECT count(*), max(card_id), (SELECT count(*) FROM ins) FROM page""", (last, PAGE))
        n, hi, got = cur.fetchone()
        if not n:
            break
        last, scanned, pages = hi, scanned + n, pages + 1
        cur.execute("SELECT count(*) FROM m")
        print(f"   page {pages}: scanned {scanned:,} catalog cards, matched {cur.fetchone()[0]:,} so far  "
              f"({time.time()-t0:.0f}s)  last_id={hi[:48]}", flush=True)
    cur.execute("SELECT count(*), count(*) FILTER (WHERE has_image), count(DISTINCT url) FROM m")
    matched, already, urls = cur.fetchone()
    return {"matched_cards": matched, "already_had_image": already, "distinct_urls": urls,
            "catalog_cards_scanned": scanned, "match_seconds": round(time.time() - t0, 1)}


def verify(a) -> int:
    with connect(a.beta_password_file, 1800000) as c, c.cursor() as cur:
        n = load_map(cur, a.map)
        print(f"map rows loaded : {n:,}", flush=True)
        r = build_matches(cur)
        cur.execute("SELECT card_id, url FROM m WHERE card_id LIKE 'mazi:bk:1996-topps-chrome:kobe-bryant:138%' ORDER BY card_id")
        r["kobe"] = cur.fetchall()
        cur.execute("SELECT card_id, url FROM m WHERE NOT has_image ORDER BY random() LIMIT 8")
        r["random_sample"] = cur.fetchall()
        r["would_write"] = r["matched_cards"] - r["already_had_image"]
        c.rollback()
    print(json.dumps(r, indent=1, default=str))
    if a.report:
        Path(a.report).write_text(json.dumps(r, indent=1, default=str) + "\n")
    return 0


def apply(a) -> int:
    with connect(a.beta_password_file, 1800000) as c, c.cursor() as cur:
        n = load_map(cur, a.map)
        r = build_matches(cur)
        cur.execute("CREATE TEMP TABLE todo AS SELECT card_id, url, row_number() OVER (ORDER BY card_id) rn FROM m WHERE NOT has_image"
                    + (f" LIMIT {int(a.limit)}" if a.limit else ""))
        cur.execute("SELECT count(*) FROM todo")
        total = cur.fetchone()[0]
        print(f"map {n:,} rows; matched {r['matched_cards']:,} cards ({r['match_seconds']}s); "
              f"{r['already_had_image']:,} already have an image; WOULD WRITE {total:,}"
              + (f" (canary --limit {a.limit})" if a.limit else ""), flush=True)
        if not a.yes_i_understand_beta:
            c.rollback()
            print("dry: pass --yes-i-understand-beta to write. Nothing written.")
            return 0
        c.commit()                      # keep the temp tables; commit per batch from here on
        done = 0
        for lo in range(1, total + 1, BATCH):
            cur.execute("""UPDATE public.beta_catalog c SET image_small = t.url, image_large = t.url, image_kind = 'reference'
                           FROM todo t WHERE c.card_id = t.card_id AND t.rn BETWEEN %s AND %s
                           AND c.image_small IS NULL AND c.image_large IS NULL""", (lo, lo + BATCH - 1))
            done += cur.rowcount
            c.commit()
            print(f"   written {done:,}/{total:,}", flush=True)
        cur.execute("INSERT INTO public.beta_import_runs(source, cards, sales, note) VALUES (%s,%s,0,%s)",
                    ("scp-catalog-images", done,
                     f"reference images from the Mini SCP archive; url prefix {OUR_PREFIX}; undo: tools/beta_catalog_images.py --undo"))
        c.commit()
    print(f"APPLY COMPLETE: {done:,} cards now carry a reference image")
    return 0


def undo(a) -> int:
    with connect(a.beta_password_file, 1800000) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.beta_catalog WHERE image_small LIKE %s", (OUR_PREFIX + "%",))
        total = cur.fetchone()[0]
        print(f"rows carrying our prefix: {total:,}")
        if not a.yes_i_understand_beta:
            print("dry: pass --yes-i-understand-beta to clear them. Nothing written.")
            return 0
        done = 0
        while True:
            cur.execute("""UPDATE public.beta_catalog SET image_small = NULL, image_large = NULL
                           WHERE card_id IN (SELECT card_id FROM public.beta_catalog WHERE image_small LIKE %s ORDER BY card_id LIMIT %s)""",
                        (OUR_PREFIX + "%", BATCH))
            if not cur.rowcount:
                break
            done += cur.rowcount
            c.commit()
            print(f"   cleared {done:,}/{total:,}", flush=True)
        c.commit()
    print(f"UNDO COMPLETE: {done:,} cards cleared")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--beta-password-file", type=Path, required=True)
    ap.add_argument("--map", default=MAP_DEFAULT)
    ap.add_argument("--report")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verify", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--undo", action="store_true")
    ap.add_argument("--limit", type=int, help="apply: only the first N cards (canary)")
    ap.add_argument("--yes-i-understand-beta", action="store_true")
    a = ap.parse_args()
    return verify(a) if a.verify else apply(a) if a.apply else undo(a)


if __name__ == "__main__":
    sys.exit(main())
