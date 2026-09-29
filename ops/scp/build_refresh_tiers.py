#!/usr/bin/env python3
"""build_refresh_tiers.py -- (re)build the SCP refresh-rotation target files (2026-09-26).

Tier = how busy a card's busiest (slug, grading company) bucket was over the last 120 days
in Neon (source_code='sportscardspro'): A >=120 sales -> daily, B >=30 -> weekly,
C >=10 -> monthly, D >=1 -> quarterly. Dormant = has SCP sales, none in 120 days -> 180 days.
SCP keeps only the latest ~30 sales per grade, so the busiest bucket sets the cadence.

Writes, next to this script:
  targets_tier_{A_daily,B_weekly,C_monthly,D_quarterly,dormant}.jsonl
  live_targets_by_need.jsonl            (A + B, busiest first)
Lines are copied from targets.jsonl. A card missing there is counted, never guessed --
resolve_slug_targets.py adds those with the real SCP id.

Read-only against Neon. Usage (on the Mini):
  cd ~/whatnot-sniper && set -a && . ./.env.external_comps_bridge && set +a
  /usr/bin/python3 ~/mazi_scp_broad/build_refresh_tiers.py [--dry-run]
"""
import argparse, json, os, re, time
import psycopg

D = os.path.dirname(os.path.abspath(__file__))
TIERS = [("A_daily", 120, 1), ("B_weekly", 30, 7), ("C_monthly", 10, 30), ("D_quarterly", 1, 90)]
DORMANT_DAYS = 180
SLUG_RE = re.compile(r"/game/(.+?)/?$")


def by_slug(path):
    m = {}
    for line in open(path):
        if '"slug"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        s = d.get("slug")
        if s and s not in m:
            m[s] = line if line.endswith("\n") else line + "\n"
    return m


def write(path, lines):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(lines)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="query and report, write nothing")
    a = ap.parse_args()
    t0 = time.time()
    targets = by_slug(f"{D}/targets.jsonl")
    with psycopg.connect(os.environ["MAZI_DB_URL"], connect_timeout=20) as nc:
        cur = nc.cursor()
        cur.execute("SET statement_timeout='600s'")
        cur.execute("""WITH b AS (SELECT raw->>'slug' s, coalesce(substring(title from '(PSA|BGS|SGC|CGC)'), 'raw') g,
                                         count(*) n
                                  FROM public.external_transactions
                                  WHERE source_code='sportscardspro' AND sold_date >= current_date - 120
                                  GROUP BY 1, 2)
                       SELECT s, max(n) FROM b WHERE s IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
        active = cur.fetchall()
        t1 = time.time()
        # Every SCP card with any sale. source_url is the card page URL (…/game/<slug>) and is
        # stored inline; raw->>'slug' needs the TOASTed raw of ~10M rows and timed out at 600s.
        cur.execute("""SELECT DISTINCT source_url FROM public.external_transactions
                       WHERE source_code='sportscardspro'""")
        every = {m.group(1) for (u,) in cur.fetchall() if (m := SLUG_RE.search(u or ""))}
    print(f"targets.jsonl {len(targets):,} slugs | active 120d {len(active):,} ({t1 - t0:.0f}s) | "
          f"all SCP slugs {len(every):,} ({time.time() - t1:.0f}s)")
    out, missing, total = {}, {}, 0.0
    for name, floor, _ in TIERS:
        out[name], missing[name] = [], 0
    for s, n in active:
        name = next(t for t, floor, _ in TIERS if n >= floor)
        if s in targets:
            out[name].append(targets[s])
        else:
            missing[name] += 1
    active_set = {s for s, _ in active}
    dormant = [s for s in every if s not in active_set]
    vol = lambda line: json.loads(line).get("volume") or 0
    out["dormant"] = sorted((targets[s] for s in dormant if s in targets), key=vol, reverse=True)
    missing["dormant"] = sum(1 for s in dormant if s not in targets)
    print(f"{'tier':12s} {'cards':>8s} {'every':>6s} {'cards/day':>10s} {'not in targets.jsonl':>21s}")
    for name, days in [(t, d) for t, _, d in TIERS] + [("dormant", DORMANT_DAYS)]:
        per = len(out[name]) / days
        total += per
        print(f"{name:12s} {len(out[name]):8,} {days:5d}d {per:10,.0f} {missing[name]:21,}")
        if not a.dry_run:
            write(f"{D}/targets_tier_{name}.jsonl", out[name])
    print(f"{'ROTATION':12s} {'':8s} {'':6s} {total:10,.0f} cards/day")
    if not a.dry_run:
        write(f"{D}/live_targets_by_need.jsonl", out["A_daily"] + out["B_weekly"])
        print(f"live_targets_by_need.jsonl: {len(out['A_daily']) + len(out['B_weekly']):,} cards (A+B)")
    else:
        print("dry run: nothing written")


if __name__ == "__main__":
    main()
