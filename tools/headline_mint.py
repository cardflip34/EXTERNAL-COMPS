#!/usr/bin/env python3
"""headline_mint.py -- propose MAZI IDs for headline sales whose card is not in the catalog (2026-10-01). Read-only.

The catalog lacks many headline cards outright (1955 Topps Clemente #164, 1961 Fleer Wilt #8, 1948 Leaf Jackie #79, the
2025 Topps Chrome Update NBA Debut Patch Autographs, 2003-04 Exquisite RPA LeBron #78 ...), so their sales cannot publish.
This proposes an ID by the locked spine rule

    mazi:<sport>:<year>-<set-slug>:<player-slug>:<number>[~<parallel-slug>]      (slug: NFKD, non-alnum -> '-')

from the sale title, with the evidence a reviewer needs: whether the catalog already uses that set slug for other
players (the naming is then consistent), the player's catalog cards that year, and a confidence. Nothing is minted here:
proposals go to Andy, and the catalog rows to MAZIDEX's load.

  python3 tools/headline_mint.py --min-price 1000000          # writes ~/mazi_headline/mint_proposals.json
"""
import argparse, json, os, re, sys, unicodedata
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import headline_sales as H  # noqa: E402

OUT = os.path.expanduser("~/mazi_headline")
SPORT_CATEGORY = {"bb": "Baseball Cards", "bk": "Basketball Cards", "fb": "Football Cards", "hk": "Hockey Cards", "sc": "Soccer Cards"}
SPORT_SETCAT = {"bb": "baseball", "bk": "basketball", "fb": "football", "hk": "hockey", "sc": "soccer"}
JUNK_KEYS = {"mvpaward", "waxpack", "loganpaul", "rookie", "logoman"}       # catalog player_keys that are not people
SET_STOP = {"rookie", "rc", "card", "cards", "signed", "the", "rookies", "sp", "ssp", "mint", "gem", "psa", "bgs", "sgc"}


def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def propose(s, sport_of):
    """-> proposal dict, or None when the title cannot carry an ID (no card number, no player)."""
    t = s["title"]
    keys = [k for k in s.get("player_keys") or [] if k not in JUNK_KEYS]
    if not s.get("code") or not s.get("year") or not keys:
        return None
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.'&\-/]*", t)
    folds = [H.fold(w) for w in words]
    # the player: the first 2-3 word window whose fold is one of the sale's player keys
    pstart = pend = None
    for n in (3, 2):
        for i in range(len(words) - n + 1):
            if "".join(folds[i:i + n]) in keys:
                pstart, pend = i, i + n
                break
        if pstart is not None:
            break
    if pstart is None:
        return None
    ystart = next((i for i, w in enumerate(words) if re.fullmatch(r"(18|19|20)\d{2}(-\d{2})?", w)), None)
    if ystart is None or ystart >= pstart:
        return None
    code_key = H.fold(s["code"])
    set_words = [w for w in words[ystart + 1:pstart]
                 if H.fold(w) not in SET_STOP and not H.fold(w).isdigit() and H.fold(w) != code_key and not w.startswith("#")]
    tw = H.tokens(H.NOT_PARALLEL_RE.sub(" ", t))
    par = [w for w in re.findall(r"[A-Za-z]+", t) if w.lower() in H.PARALLEL_TOKENS and w.lower() not in {x.lower() for x in set_words}]
    player = " ".join(words[pstart:pend])
    sport = sport_of.get(H.fold(player)) or sport_of.get(keys[0])
    number = s["code"]
    cid = f"mazi:{sport}:{s['year']}-{slug(' '.join(set_words))}:{slug(player)}:{slug(number)}"
    if par:
        cid += "~" + slug(" ".join(dict.fromkeys(p.lower() for p in par)))
    conf, notes = "high", []
    if len(keys) > 1:
        conf, notes = "low", notes + ["more than one player in the title (dual card)"]
    if not set_words:
        conf, notes = "low", notes + ["no set name read between year and player"]
    if not sport:
        conf, notes = "low", notes + ["sport unknown (player has no catalog cards)"]
    if {"auto", "signed"} & tw and "auto" not in H.tokens(" ".join(set_words + par)):
        notes.append("signed copy: confirm whether this is an autograph card or an after-market signature")
    return {"proposed_card_id": cid, "sport": sport, "category": SPORT_CATEGORY.get(sport), "season_year": s["year"],
            "set_name": f"{s['year']} {' '.join(set_words)}".strip(), "player": player, "number": number,
            "parallel": " ".join(dict.fromkeys(p.title() for p in par)) or None, "print_run": s.get("print_run"),
            "confidence": conf, "notes": notes, "sale": {k: s[k] for k in ("title", "price", "date", "venue")}}


def match_catalog_set(b, p):
    """Use the catalog's own set (beta_sets, e.g. scp:bb:1956-topps 'Baseball Cards 1956 Topps') when its name words are all
    in the sale title, so a minted ID sits beside the set's other cards. Else keep the parsed set and list the nearest sets."""
    cat = SPORT_SETCAT.get(p["sport"])
    if not cat:
        p["notes"].append("no catalog set check (sport unknown)"); return
    tw = H.tokens(p["sale"]["title"])
    scored = []
    for set_id, name in b.execute("SELECT set_id, name FROM public.beta_sets WHERE category = %s AND season_year = %s",
                                  (cat, p["season_year"])):
        words = H.tokens(name) - H.SET_STOP - {cat}
        if not words:
            continue
        missing = words - tw                                  # brand words count here: '1997 Fleer' must not fit a Ultra title
        scored.append((len(missing), -len(words & tw), set_id, name))
    scored.sort()
    full = [x for x in scored if x[0] == 0]
    if full:
        best = full[0] if len(full) == 1 or full[0][1] < full[1][1] else None
        if best:
            slug_part = best[2].split(":", 2)[2]                       # '1956-topps'
            old = p["proposed_card_id"].split(":")
            p["proposed_card_id"] = ":".join(old[:2] + [slug_part] + old[3:])
            p["set_name"] = best[3]
            p["catalog_set"] = best[2]
            return
        p["notes"].append("several catalog sets fit: " + ", ".join(x[3] for x in full[:3]))
    else:
        p["notes"].append("no catalog set for %s %s matches the title; nearest: %s" % (
            cat, p["season_year"], ", ".join(x[3] for x in scored[:3]) or "none"))
    if p["confidence"] == "high":
        p["confidence"] = "medium"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-price", type=float, default=1_000_000)
    a = ap.parse_args()
    canon = json.load(open(os.path.join(OUT, "headline_sales_report.json")))
    todo = [s for s in canon if s["price"] >= a.min_price and s["mazi"]["status"] == "mint_candidate"]
    b = H.beta()
    try:
        keys = sorted({k for s in todo for k in s.get("player_keys") or []})
        sport_of = {}
        for pk, cid in b.execute("SELECT player_key, card_id FROM public.beta_catalog WHERE player_key = ANY(%s) AND card_id LIKE 'mazi:%%' LIMIT 200000", (keys,)):
            sport_of.setdefault(pk, Counter())[cid.split(":")[1]] += 1
        sport_of = {k: c.most_common(1)[0][0] for k, c in sport_of.items()}
        props = [p for p in (propose(s, sport_of) for s in todo) if p]
        for p in props:
            match_catalog_set(b, p)
            p["exists"] = bool(b.execute("SELECT 1 FROM public.beta_catalog WHERE card_id = %s", (p["proposed_card_id"],)).fetchone())
            p["player_cards_that_year"] = [r[0] for r in b.execute(
                "SELECT DISTINCT set_name FROM public.beta_catalog WHERE player_key = %s AND season_year = %s LIMIT 6",
                (H.fold(p["player"]), p["season_year"]))]
    finally:
        b.rollback(); b.close()
    json.dump(props, open(os.path.join(OUT, "mint_proposals.json"), "w"), indent=1, default=str)
    print(f"{len(todo)} mint candidates >= ${a.min_price:,.0f}; {len(props)} proposals "
          f"({Counter(p['confidence'] for p in props).most_common()}) -> {OUT}/mint_proposals.json")
    for p in sorted(props, key=lambda p: -p["sale"]["price"]):
        print(f"  ${p['sale']['price']:>12,.0f} {p['confidence']:<6} {p['proposed_card_id']}  [{p.get('catalog_set') or 'NEW SET'}]"
              f"{'  EXISTS' if p['exists'] else ''}{'  | ' + '; '.join(p['notes']) if p['notes'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
