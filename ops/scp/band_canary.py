#!/usr/bin/env python3
"""band_canary.py -- step-3 canary for --band-out on a real Lane A and Lane B batch (2026-09-26).

A live A/B cannot gate this (SCP ledgers change between fetches), so for each lane:
  1. take the next N cards that lane would scrape,
  2. run the REAL scp_broad_scrub.main() WITHOUT --band-out; every page is fetched once
     (under the shared SCP budget, anonymous pace) and cached,
  3. run it again WITH --band-out against the identical cached bytes.
Everything is written under band_canary_20260926/; the bridged scp_broad_comps.jsonl is
never opened (asserted). Gate: identical main output, band rows only inside the band,
no band row in the main output.
"""
import hashlib, io, json, os, sys, time
from contextlib import redirect_stdout
sys.path.insert(0, os.path.expanduser("~/whatnot-sniper"))
import tools.scp_broad_scrub as S                       # noqa: E402
import mazi_db.scripts.scp_scraper as SC                # noqa: E402

N = int(os.environ.get("CANARY_N", "20"))
D = os.path.expanduser("~/mazi_scp_broad")
C = os.path.join(D, "band_canary_20260926")
CACHE = os.path.join(C, "cache")
BRIDGED = os.path.realpath(os.path.join(D, "scp_broad_comps.jsonl"))
BAND = ("2026-01-01", "2026-07-01")
os.makedirs(CACHE, exist_ok=True)

_real_fetch = S.fetch_scp_html
def cached_fetch(url, prefer="jina"):
    key = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".html")
    if os.path.exists(key):
        return open(key, encoding="utf-8", errors="ignore").read()
    SC.acquire_scp_budget()                  # the real shared budget, once per real fetch
    try:
        html = _real_fetch(url, prefer=prefer) or ""
    finally:
        SC.release_scp_budget()
    with open(key, "w", encoding="utf-8") as f:
        f.write(html)
    time.sleep(3.5)                          # anonymous Jina pace
    return html
S.fetch_scp_html = cached_fetch
S.acquire_scp_budget = lambda: None          # budget is taken inside cached_fetch instead
S.release_scp_budget = lambda: None


def pick_lane_a():
    seen = set(json.load(open(f"{D}/scp_broad_state.json"))["done_ids"])
    out, skip = [], 2000                     # past the next history chunk, so no overlap
    for line in open(f"{D}/targets.jsonl"):
        d = json.loads(line)
        if str(d.get("id", "")) in seen:
            continue
        if skip:
            skip -= 1
            continue
        out.append(line)
        if len(out) >= N:
            return out


def scrub(argv):
    for i, a in enumerate(argv):
        if a in ("--out", "--band-out", "--state"):
            assert os.path.realpath(argv[i + 1]) != BRIDGED, "refusing to touch the bridged jsonl"
            assert os.path.realpath(argv[i + 1]).startswith(C), argv[i + 1]
    buf = io.StringIO()
    sys.argv = ["scp_broad_scrub.py"] + argv
    with redirect_stdout(buf):
        S.main()
    return buf.getvalue()


def lane(name, targets, window_args, band_args):
    tf = f"{C}/{name}_targets.jsonl"
    open(tf, "w").writelines(targets)
    common = ["--targets", tf, "--limit", str(N), "--prefer", "jina", "--sleep", "0",
              "--block-backoff", "5", "--block-backoff-max", "5"] + window_args
    a_out, b_out, band = f"{C}/{name}_main_noband.jsonl", f"{C}/{name}_main_band.jsonl", f"{C}/{name}_band_2026h1.jsonl"
    for p in (a_out, b_out, band, f"{C}/{name}_st_a.json", f"{C}/{name}_st_b.json"):
        if os.path.exists(p):
            os.remove(p)
    t0 = time.time()
    log_a = scrub(common + ["--out", a_out, "--state", f"{C}/{name}_st_a.json"])
    t1 = time.time()
    log_b = scrub(common + ["--out", b_out, "--state", f"{C}/{name}_st_b.json", "--band-out", band] + band_args)
    main_a, main_b = open(a_out, "rb").read(), open(b_out, "rb").read()
    band_rows = [json.loads(l) for l in open(band)] if os.path.exists(band) else []
    in_band = lambda r: BAND[0] <= r["sold_date"] < BAND[1]
    main_rows = [json.loads(l) for l in main_b.splitlines() if l.strip()]
    summary = [l for l in log_a.splitlines() if l.startswith("processed=")]
    g1 = main_a == main_b
    g2 = all(in_band(r) for r in band_rows)
    g3 = not any(in_band(r) for r in main_rows)
    g4 = json.load(open(f"{C}/{name}_st_a.json")) == json.load(open(f"{C}/{name}_st_b.json"))
    print(f"LANE {name}: {len(targets)} cards | fetch pass {t1 - t0:.0f}s, replay {time.time() - t1:.0f}s | {summary[-1] if summary else ''}")
    print(f"  rows_kept without flag {len(main_a.splitlines())}  with flag {len(main_rows)}  | band rows {len(band_rows)}")
    print(f"  GATE identical main output (bytes) ....... {'PASS' if g1 else 'FAIL'}")
    print(f"  GATE band rows all inside {BAND[0]}..{BAND[1]} {'PASS' if g2 else 'FAIL'}")
    print(f"  GATE no band row leaked into main ........ {'PASS' if g3 else 'FAIL'}")
    print(f"  GATE identical done-state ................ {'PASS' if g4 else 'FAIL'}")
    return g1 and g2 and g3 and g4


ok_a = lane("laneA", pick_lane_a(),
            ["--floor", "2015-01-01", "--ceiling", "2099-01-01", "--exclude-from", BAND[0], "--exclude-to", BAND[1]], [])
ok_b = lane("laneB", open(f"{D}/live_targets.jsonl").readlines()[:N],
            ["--floor", "2026-07-01", "--ceiling", "2099-01-01"], ["--band-from", BAND[0], "--band-to", BAND[1]])
print("CANARY", "PASS" if (ok_a and ok_b) else "FAIL")
