#!/usr/bin/env python3
"""veefriends_variants.py -- EVERY checklist version (card x parallel) of the built VeeFriends products, sold or not.

A MAZI variant ID used to exist only after a confirmed-price sale, so the catalog held ~33% of the checklist versions
(MAZIDEX audit 2026-09-29). This module lists them all from the same checklist tables the linker uses; the beta
exporter builds catalog rows from it, and a version with no confirmed sale is a reference-only row: no price, never
MAZIFIED, never used for valuation.

variant_id = mazi_card_id for the Base version, else mazi_card_id + '~' + slug(parallel, 40) -- the same rule as the
Neon view veefriends_guaranteed_sales. Products that are not minted (2022 zerocool Series 2, 2025 Sapphire Selections
until their SEL codes are sourced) are not listed.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import veefriends_link as V
import veefriends_stickers as VS

# Sapphire checklists: 2025 = the official veefriends.com Sapphire Edition page; 2026 = checklistinsider Sapphire.
SAPPHIRE_PARALLELS = {
    "2025-topps-chrome-sapphire": [("Orange Sapphire", 25), ("Red Sapphire", 5), ("Padparadscha Sapphire", 1)],
    "2026-topps-chrome-sapphire": [("Gold Sapphire", 50), ("White Sapphire", 30), ("Orange Sapphire", 25),
                                   ("Black Sapphire", 10), ("Red Sapphire", 5), ("Padparadscha Sapphire", 1)],
}
HIDDEN_GEMS = [("Onyx", 10), ("Ruby", 5), ("Padparadscha Sapphire", 1)]   # base = Emerald


def parallels_for(card):
    s, sec = card["set_key"], card["section"]
    if (s, sec) in V.TOPPS_PARALLELS:
        return [("Base", V.BASE_RUN.get((s, sec)))] + list(V.TOPPS_PARALLELS[(s, sec)])
    if s in SAPPHIRE_PARALLELS and sec == "base":
        return [("Base", None)] + SAPPHIRE_PARALLELS[s]
    if sec == "hidden-gems":
        return [("Base", None)] + HIDDEN_GEMS
    if sec in ("infinite-sapphire", "promo"):
        return [("Base", None)]
    if s == VS.SET_KEY:
        return list(VS.SECTIONS[sec][2].items())
    return []


def variant_id(mazi_card_id, parallel):
    return mazi_card_id if parallel == "Base" else f"{mazi_card_id}~{V.mazi_slug(parallel, 40)}"


def matrix(catalog=None):
    cat = catalog or V.build_catalog()
    out = []
    for c in cat["cards"]:
        mid = V.mazi_card_id(c)
        if not mid:
            continue
        for par, run in parallels_for(c):
            out.append({"variant_id": variant_id(mid, par), "mazi_card_id": mid, "vf_card_id": c["card_id"],
                        "set_key": c["set_key"], "set_name": c["set_name"], "section": c["section"],
                        "section_name": c["section_name"], "code": c["code"], "character": c["character"],
                        "parallel": par, "print_run": run})
    return out


if __name__ == "__main__":
    import collections
    m = matrix()
    ids = [r["variant_id"] for r in m]
    print(f"{len(m):,} checklist versions, {len(set(ids)):,} distinct ids")
    for k, v in sorted(collections.Counter((r["set_key"], r["section"]) for r in m).items()):
        print(f"  {v:6,}  {k[0]:32s} {k[1]}")
