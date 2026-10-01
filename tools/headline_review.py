#!/usr/bin/env python3
"""headline_review.py -- turn the headline report into a page Andy can check before anything is written (2026-09-29).

Reads ~/mazi_headline/headline_sales_report.json (tools/headline_sales.py report) and writes
~/mazi_headline/review/headline_review.html:

  A. every $1M+ sale, any MAZI ID status -- Andy checks each ID before it can publish
  B. $100K-$1M sales at venues the beta already takes whose ID resolved to ONE catalog card -- the first export batch
  C. counts of everything else (new ID needed / needs review), by venue

Each proposed card links to its MAZIDEX page so the match can be compared by eye. Read-only; no DB, no network.

  python3 tools/headline_review.py
"""
import argparse, html, json, os, sys
from collections import Counter
from urllib.parse import quote

OUT = os.path.expanduser("~/mazi_headline")
BETA_VENUES = {"goldin", "fanatics", "heritage", "alt", "rea", "private", "ebay"}      # accepted by the beta after 033
STATUS_LABEL = {"resolved": "matched", "resolved_needs_review": "matched - check", "mint_candidate": "new ID needed",
                "needs_review": "needs review"}


def esc(x):
    return html.escape(str(x if x is not None else ""))


def card_link(cid):
    return f'<a href="https://mazidex.com/#card/{quote(cid, safe="")}">{esc(cid)}</a>'


def sources_html(s):
    out = []
    for r in s["sources"]:
        label = f'{r.get("src")}:{r.get("venue")}'
        out.append(f'<a href="{esc(r["url"])}">{esc(label)}</a>' if r.get("url") else esc(label))
    return " · ".join(out)


def row(s, n):
    m = s["mazi"]
    cands = m.get("candidates") or []
    cand = "<br>".join(f'{card_link(c["card_id"])} <span class="dim">{esc(c.get("set_name"))} #{esc(c.get("number"))}'
                       f'{(" · " + esc(c.get("parallel"))) if c.get("parallel") else ""} · score {esc(c.get("score"))}</span>'
                       for c in cands[:3]) or '<span class="dim">-</span>'
    read = " · ".join(x for x in (str(s.get("year") or ""), ("#" + s["code"]) if s.get("code") else "", s.get("serial") or "",
                                  s.get("grade") or "") if x)
    return (f'<tr><td class="n">{n}</td><td class="p">${s["price"]:,.0f}</td><td>{esc(s["date"])}{" (month)" if s.get("prec") == "month" else ""}</td>'
            f'<td>{esc(s["venue"])}</td><td>{esc(s["title"])}<div class="dim">read: {esc(read) or "-"} · {sources_html(s)}</div></td>'
            f'<td><span class="st st-{esc(m["status"])}">{esc(STATUS_LABEL.get(m["status"], m["status"]))}</span>'
            f'<div class="dim">{esc(m.get("why"))}</div></td><td>{cand}</td><td class="ok"></td></tr>')


def table(rows):
    head = ('<table><thead><tr><th>#</th><th>price</th><th>date</th><th>venue</th><th>sale</th><th>MAZI ID</th>'
            '<th>proposed catalog card</th><th>OK?</th></tr></thead><tbody>')
    return head + "".join(row(s, i + 1) for i, s in enumerate(rows)) + "</tbody></table>"


def held_section(plan_path):
    """D. rows the loader held for Andy: price not backed by any other sale of the card or its parallels, or far off
    the card's median."""
    p = json.load(open(plan_path))
    rows = sorted(p.get("review") or [], key=lambda r: -r["price"])
    body = "".join(
        f'<tr><td class="n">{i + 1}</td><td class="p">${r["price"]:,.0f}</td><td>{esc(r["sold_at"][:10])}</td><td>{esc(r["venue"])}</td>'
        f'<td>{esc(r["_title"])}<div class="dim">{esc(r["grade"])} · <a href="{esc(r.get("source_url") or "")}">source</a></div></td>'
        f'<td>{esc(r["why"])}<div class="dim">best other: {("$" + format(int(r["best_other"]), ",")) if r.get("best_other") else "none"}</div></td>'
        f'<td>{card_link(r["card_id"])}</td><td class="ok"></td></tr>' for i, r in enumerate(rows))
    return (f'<h2>D. Held by the loader for your check ({len(rows)}): price not backed by other sales</h2>'
            '<p class="dim">Each of these would load only with your OK. A real record sale can look like this; so can a bad '
            'record (e.g. the $900,000 Garchomp LV.X in the canary).</p><div class="wrap"><table><thead><tr><th>#</th><th>price</th>'
            '<th>date</th><th>venue</th><th>sale</th><th>why held</th><th>card</th><th>OK?</th></tr></thead><tbody>'
            + body + "</tbody></table></div>")


def mint_section(path):
    """E. proposed new MAZI IDs (tools/headline_mint.py) for sales whose card is not in the catalog."""
    props = sorted(json.load(open(path)), key=lambda p: -p["sale"]["price"])
    body = "".join(
        f'<tr><td class="n">{i + 1}</td><td class="p">${p["sale"]["price"]:,.0f}</td><td>{esc(p["sale"]["venue"])}</td>'
        f'<td>{esc(p["sale"]["title"])}</td><td><code>{esc(p["proposed_card_id"])}</code>'
        f'<div class="dim">{esc(p.get("set_name"))} · #{esc(p["number"])}{(" · " + esc(p["parallel"])) if p.get("parallel") else ""}</div></td>'
        f'<td>{esc(p.get("catalog_set") or "NEW SET")}</td><td class="st st-{"resolved" if p["confidence"] == "high" else "needs_review"}">'
        f'{esc(p["confidence"])}</td><td class="dim">{esc("; ".join(p["notes"]))}</td><td class="ok"></td></tr>'
        for i, p in enumerate(props))
    return (f'<h2>E. Proposed new MAZI IDs ({len(props)}): cards the catalog does not have</h2>'
            '<p class="dim">Built from the sale title by the spine rule mazi:&lt;sport&gt;:&lt;year&gt;-&lt;set&gt;:&lt;player&gt;:&lt;number&gt;[~parallel], '
            'placed in the catalog\'s own set when one fits. Nothing is minted until you OK an ID; MAZIDEX then loads the catalog row.</p>'
            '<div class="wrap"><table><thead><tr><th>#</th><th>price</th><th>venue</th><th>sale</th><th>proposed ID</th><th>catalog set</th>'
            '<th>confidence</th><th>notes</th><th>OK?</th></tr></thead><tbody>' + body + "</tbody></table></div>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", help="a loader plan JSON (tools/export_headline_beta.py) -> adds section D")
    ap.add_argument("--mint", help="mint proposals JSON (tools/headline_mint.py) -> adds section E")
    args = ap.parse_args()
    canon = json.load(open(os.path.join(OUT, "headline_sales_report.json")))
    a = [s for s in canon if s["price"] >= 1_000_000]
    b = [s for s in canon if 100_000 <= s["price"] < 1_000_000 and s["venue"] in BETA_VENUES and s["mazi"]["status"] == "resolved"]
    rest = Counter((s["venue"], STATUS_LABEL.get(s["mazi"]["status"], s["mazi"]["status"])) for s in canon
                   if s["price"] < 1_000_000 and s not in b)
    st = Counter(STATUS_LABEL.get(s["mazi"]["status"]) for s in canon)
    css = """:root{--bg:#fff;--fg:#1b1b1b;--dim:#6b6b6b;--line:#e3e3e3;--ok:#1a7f37;--warn:#9a6700;--bad:#b42318;--head:#f6f6f6}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#eaeaea;--dim:#9a9a9a;--line:#2c2c2c;--ok:#4ac26b;--warn:#d4a72c;--bad:#f47067;--head:#1d1d1d}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1500px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}p{margin:6px 0}.dim{color:var(--dim);font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left}
th{background:var(--head);position:sticky;top:0}td.p{white-space:nowrap;font-weight:600}td.n{color:var(--dim)}td.ok{min-width:40px}
a{color:inherit}.st{font-weight:600}.st-resolved{color:var(--ok)}.st-resolved_needs_review{color:var(--warn)}
.st-mint_candidate,.st-needs_review{color:var(--bad)}.wrap{overflow-x:auto}"""
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Headline Sales Review</title><style>{css}</style></head><body>
<h1>Headline sales: MAZI ID review before export</h1>
<p class="dim">{len(canon):,} canonical sales of $100K+ since 2025-01-01 · MAZI ID: {", ".join(f"{k} {v:,}" for k, v in st.most_common())}.
Fanatics sales are as of the 2026-09-28 22:01 API pull (today's pull was rate-limited); Goldin/REA/Heritage/eBay from Neon now. Nothing has been written to the site. Check the proposed card for each row: set, card number, parallel and print run must all match the sale.</p>
<h2>A. Every $1M+ sale ({len(a)}): each ID needs your check before it publishes</h2><div class="wrap">{table(a)}</div>
<h2>B. First export batch: $100K–$1M, matched to one catalog card ({len(b)})</h2>
<p class="dim">Venues the site already accepts. A wrong parallel (e.g. the base autograph instead of the /25 refractor) is the main risk: spot-check the "proposed catalog card" column.</p>
<div class="wrap">{table(b)}</div>
{held_section(args.plan) if args.plan else ""}
{mint_section(args.mint) if args.mint else ""}
<h2>C. Not ready yet (under $1M)</h2><div class="wrap"><table><thead><tr><th>venue</th><th>MAZI ID</th><th>sales</th></tr></thead><tbody>
{"".join(f"<tr><td>{esc(v)}</td><td>{esc(k)}</td><td>{n:,}</td></tr>" for (v, k), n in sorted(rest.items(), key=lambda x: -x[1]))}
</tbody></table></div></body></html>"""
    os.makedirs(os.path.join(OUT, "review"), exist_ok=True)
    path = os.path.join(OUT, "review", "headline_review.html")
    open(path, "w").write(doc)
    print(f"A {len(a)} ($1M+) | B {len(b)} (first batch) | C {sum(rest.values())} not ready -> {path}")


if __name__ == "__main__":
    sys.exit(main())
