#!/usr/bin/env python3
"""veefriends_link.py -- VeeFriends card catalog + STRICT sale -> card linker (2026-09-26, EXTERNAL COMPS lane).

For the mazidex.com VeeFriends tab. VeeFriends cards are on NO catalog site we scrape (not SportsCardsPro,
not PriceCharting, not TCGplayer), so the catalog is built from saved public checklist pages in
~/mazi_veefriends/checklists:
  * 2025 + 2026 Topps Chrome VeeFriends -- full numbered checklists (base, inserts, 2026 GaryVee autos).
  * 2022 zerocool Series 2 "Compete & Collect" -- no parseable public checklist, so it is keyed by SET +
    CHARACTER (names from the Topps Chrome checklists), numbers_verified=False, never minted.
  * 2026 Super Stickers Spectacular Series -- the official checklist (tools/veefriends_stickers.py). The set
    prints NO card numbers, so identity is set + section + character (id_basis 'checklist_name').
VeeFriends cards are identified by CHARACTER NAME (only ~26% of sale titles carry a number).

A sale links only when set, section and character resolve to exactly ONE catalog card and any number in the
title agrees with it. Lots, boxes, packs, breaks and multi-character titles are rejected. Everything that does
not resolve stays unlinked WITH A REASON -- nothing is guessed.

  python3 tools/veefriends_link.py catalog                     # -> ~/mazi_veefriends/veefriends_catalog.json
  python3 tools/veefriends_link.py link --since 2025-01-01     # read-only Neon -> linked JSONL + report
"""
from __future__ import annotations

import argparse, html as H, json, os, re, sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import veefriends_stickers as VS   # 2026 Super Stickers: official checklist + its own strict linker (2026-09-27)

HOME = os.path.expanduser("~/mazi_veefriends")
CHECKLISTS = os.path.join(HOME, "checklists")
CATALOG = os.path.join(HOME, "veefriends_catalog.json")

# ---------------------------------------------------------------- checklist parsing
CARD_RE = re.compile(r"^(?P<code>(?:[A-Z]{1,6}-)?\d{1,3}[a-z]?)\s+(?P<name>[A-Z0-9][^\n]{1,80})$")
NOISE_RE = re.compile(r"(Total Cards|Refractor\s*/|\bodds\b|\d:\d|Hobby|Blaster|Mega Box|Pack)", re.I)
SKIP_HEAD = re.compile(r"^(Buy on eBay|Buy on|Shop|Parallels?:?|Pack odds|Autograph Parallels?:?)", re.I)


def _lines(h):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r'(?is)<td class="n">\s*([^<]+?)\s*</td>\s*<td class="pcell">\s*<span class="pl">\s*([^<]+?)\s*</span>',
               r"<br>\1 \2<br>", h)
    h = re.sub(r"(?i)<(h[1-4]|strong|b)(\s[^>]*)?>", "\n@@HEAD@@", h)
    h = re.sub(r"(?i)</(h[1-4]|strong|b)>", "\n", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|td|h[1-6])>", "\n", h)
    for l in H.unescape(re.sub(r"<[^>]+>", " ", h)).splitlines():
        l = re.sub(r"\s+", " ", l).strip()
        if l:
            yield l


def extract(path):
    sec, out = "?", []
    for l in _lines(open(path, errors="ignore").read()):
        if l.startswith("@@HEAD@@"):
            s = l[8:].strip()
            if s and len(s) < 90 and not SKIP_HEAD.match(s):
                sec = s
            continue
        m = CARD_RE.match(l)
        if m and not NOISE_RE.search(l):
            out.append((sec, m.group("code"), m.group("name").strip()))
    return out


# heading -> section key (checklistinsider 2026 / cardsmiths 2025 headings, normalised)
SECTION_OF_HEADING = [
    (r"^base garyvee autographs", "auto-garyvee", "GaryVee Autographs"),
    (r"^base (variations|gritty ghost|last glass)", None, None),          # variations: not separate cards
    (r"^base", "base", "Base"),
    (r"^sketch to screen", "sketch-to-screen", "Sketch to Screen"),
    (r"^manga speckle", "manga-speckle", "Manga Speckle"),
    (r"^erupt", "erupt", "Erupt"),
    (r"^game on", "game-on", "Game On!"),
    (r"^iconics", "iconics", "Iconics"),
    (r"^comic clippings", "comic-clippings", "Comic Clippings"),
    (r"^balance battles", "balance-battles", "Balance Battles"),
    (r"^chalkboard", "chalkboard", "Chalkboard"),
    (r"^mega heads", "mega-heads", "Mega Heads"),
    (r"^neon lights", "neon-lights", "Neon Lights"),
    (r"^original sketch", "original-sketch", "Original Sketch Selections"),
    (r"^stellar haze", "stellar-haze", "Stellar Haze"),
    (r"^hidden gems", "hidden-gems", "Hidden Gems"),
    (r"^infinite sapphire", "infinite-sapphire", "Infinite Sapphire"),
]
SECTION_KEYWORDS = {  # how titles name a section
    "manga-speckle": ["manga speckle", "manga"], "mega-heads": ["mega heads", "megaheads", "mega head"],
    "neon-lights": ["neon lights"], "iconics": ["iconics", "iconic"], "erupt": ["erupt"], "chalkboard": ["chalkboard"],
    "stellar-haze": ["stellar haze"], "comic-clippings": ["comic clippings", "comic clipping", "comic cuts?"], "balance-battles": ["balance battles"],
    "original-sketch": ["original sketch", "sketch selections?"], "sketch-to-screen": ["sketch to screen", "sketch screen", "to screen"],
    "game-on": ["game on"],
    "hidden-gems": ["hidden gems", "hidden gem"], "infinite-sapphire": ["infinite sapphire"],
    "sapphire-selections": ["sapphire selections", "sapphire selection"],
}
TOPPS_SETS = {
    "2025-topps-chrome": ("2025 Topps Chrome VeeFriends", "2025_topps_chrome_cardsmiths.html"),
    "2026-topps-chrome": ("2026 Topps Chrome VeeFriends", "2026_topps_chrome_checklistinsider.html"),
    "2026-topps-chrome-sapphire": ("2026 Topps Chrome Sapphire VeeFriends", "2026_topps_chrome_sapphire_checklistinsider.html"),
}
# 2025 Sapphire Edition: official guide = "the same 100 characters from Topps Chrome 2025"; numbering = the 2025 base
# numbers, VERIFIED on sale titles 2026-09-27 (190 numbered Sapphire titles agree; the 4 "disagreements" were serials).
SAPPHIRE_2025_SELECTIONS = ["Alpha Alligator", "Ambitious Angel", "Entrepreneur Elf", "Empathy Elephant", "Fearless Fairy",
                            "Heart-Trooper", "Patient Pig", "Skilled Skeleton", "Sweet Swan",
                            "Very, Very, Very, Very, Lucky Black Cat"]   # numbers unknown -> keyed by character, not minted
# Sapphire parallels have ONE print run each, so an unnamed "/25" IS Orange Sapphire (checklist-deterministic, not a guess).
SAPPHIRE_RUNS = {"2026-topps-chrome-sapphire": {50: "Gold Sapphire", 30: "White Sapphire", 25: "Orange Sapphire",
                                                10: "Black Sapphire", 5: "Red Sapphire", 1: "Padparadscha Sapphire"},
                 "2025-topps-chrome-sapphire": {25: "Orange Sapphire", 5: "Red Sapphire", 1: "Padparadscha Sapphire"}}
PROMO_SETS = {"2026-topps-industry-conference": ("2026 Topps Industry Conference VeeFriends", [("ICGV", "GaryVee & Gary Bee")])}
ICGV_RE = re.compile(r"\bicgv\b|\bindustry (?:conference|summit)\b")
CHAR_SETS = {
    "2022-zerocool-series-2": "2022 zerocool VeeFriends Series 2 Compete & Collect",
}


def norm(s: str) -> str:
    s = (s or "").lower().replace("\u2019", "'").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str) -> str:
    return norm(s).replace(" ", "-")


def build_catalog(checklists=CHECKLISTS):
    cards, characters = [], {}
    for set_key, (set_name, fname) in TOPPS_SETS.items():
        seen = set()
        for heading, code, name in extract(os.path.join(checklists, fname)):
            h = re.sub(r"\s*(checklist|\d+ cards|set)\s*", " ", heading, flags=re.I).strip().lower()
            h = h.replace("erupti", "erupt")
            sec = next(((k, n) for pat, k, n in SECTION_OF_HEADING if re.match(pat, h)), ("?", heading))
            if sec[0] is None or sec[0] == "?":
                continue
            key = (sec[0], code)
            if key in seen:
                continue
            seen.add(key)
            card_id = f"vf:{set_key}:{sec[0]}:{code}"
            cards.append({"card_id": card_id, "set_key": set_key, "set_name": set_name, "section": sec[0],
                          "section_name": sec[1], "code": code, "character": name, "numbers_verified": True,
                          "id_basis": "number"})
            if sec[0] != "balance-battles":
                characters.setdefault(norm(name), name)
    for c in [c for c in cards if c["set_key"] == "2025-topps-chrome" and c["section"] == "base"]:
        cards.append({**c, "card_id": c["card_id"].replace("2025-topps-chrome", "2025-topps-chrome-sapphire", 1),
                      "set_key": "2025-topps-chrome-sapphire", "set_name": "2025 Topps Chrome Sapphire VeeFriends"})
    for name in SAPPHIRE_2025_SELECTIONS:
        cards.append({"card_id": f"vf:2025-topps-chrome-sapphire:sapphire-selections:{slug(name)}",
                      "set_key": "2025-topps-chrome-sapphire", "set_name": "2025 Topps Chrome Sapphire VeeFriends",
                      "section": "sapphire-selections", "section_name": "Sapphire Selections", "code": None,
                      "character": name, "numbers_verified": False, "id_basis": None})
        characters.setdefault(norm(name), name)
    for set_key, set_name in CHAR_SETS.items():
        for n, name in sorted(characters.items()):
            cards.append({"card_id": f"vf:{set_key}:char:{slug(name)}", "set_key": set_key, "set_name": set_name,
                          "section": "char", "section_name": "Character", "code": None, "character": name,
                          "numbers_verified": False, "id_basis": None})
    for set_key, (set_name, promos) in PROMO_SETS.items():
        for code, name in promos:
            cards.append({"card_id": f"vf:{set_key}:promo:{code}", "set_key": set_key, "set_name": set_name, "section": "promo",
                          "section_name": "Promo", "code": code, "character": name, "numbers_verified": True, "id_basis": "number"})
    cards += VS.catalog_cards()
    return {"version": "vf-catalog-2026-09-29", "cards": cards}


# ---------------------------------------------------------------- title -> card
NOT_SINGLE_RE = re.compile(r"\b(lot|lots|bundle|you pick|pick your|choose your|u pick|complete set|full set|team set|"
                           r"factory sealed|sealed pack|box|boxes|packs|pack of|case|break|breaks|"
                           r"\d+\s*x|x\s*\d+|\d+\s*cards)\b")
SINGLE_PHRASES_RE = re.compile(r"\b(pack fresh|fresh (from|out of) (the )?pack|pulled from (a )?pack|case hit|case hits)\b")  # singles
# Products/inserts that exist but are NOT in the catalog yet: a title naming one must NOT fall through to the base card.
UNCATALOGED_RE = re.compile(r"\b(y2k|favou?rite entrepreneurs?|entrepreneur elfs favou?rite|stained glass|sketch card|sketch cards|sketch artist|alpha drawings|treasure chest|"
                            r"die cuts?|variation|variations|variants?|var|1975|1986|printing plate|uno|nycc|promo|original drawing|"
                            r"book games|content condors|condor autos?|condor autographs?|industry summit|summit)\b")
# The same, on the raw title: 1986 Topps TF- letter codes, Content Condors CC- codes, Last Glass Standing Rose/White Wine.
UNCATALOGED_RAW_RE = re.compile(r"\b(?:TF|CCA?)-[A-Z]{2}\b|ros[e\u00e9]\b|white wine", re.I)   # CC-1..CC-10 = Comic Clippings
# The Content Condors insert features real creators; "Content Condor" + one of them is the insert, not base #57.
CONDOR_PEOPLE_RE = re.compile(r"\b(brickley|clix|conrod|vaynerchuk|mr ?beast|donaldson|mongraal|kyle jackson|mel robbins|"
                              r"jake paul|kam patterson|livvy|dunne)\b")
# Lots the single-word filter misses: "20 Card Base Set", "18 Card ...", "(23) ... Set", "(3). ...", "X-Fractor(2)And Base".
LOT_RE = re.compile(r"(?<![/\d#])\b(?:[2-9]|[1-9]\d{1,2})\s*-?\s*cards?\b|(?<![/\d#])\b\d{1,3}\s+(?:card\s+)?base\s+set\b|"
                    r"^\W*\(\s*[2-9]\d*\s*\)|\(\s*\d+\s*\)\s*and\b|\(\s*\d+\s*\).*\b(?:set|collection|lot)\b", re.I)
# Every spelling of "Very, Very, Very, Very, Lucky Black Cat" ("Very, Very Lucky", "VVVV Very Luck", "V V V Lucky", plain
# "Lucky Black Cat") -> the checklist name, so its "Black" is never read as a Black parallel (MAZIDEX review 09-27).
ALIASES = [(re.compile(r"\b(?:(?:very|v{1,4})\s+)*(?:lucky|luck)\s+black\s+cat\b"), "very very very very lucky black cat")]
# More than one item in the sale ("... + Base Card", "plus TCG card", "& Free CGC card"). "MINT+" is a grade, not an item.
EXTRA_ITEM_RE = re.compile(r"\s\+\s*\w|\b(plus|bonus|extras?|includes?|including|comes with)\b|"
                           r"\bfree\b(?!\s*(shipping|ship|s/?h|postage|returns?))", re.I)
# Graded by a company we cannot name ("GMA GEM MT 10", "Graded Pristine 10"): its price is not a Raw price.
GRADE_HINT_RE = re.compile(r"\b(gem ?(mint|mt)|gem 10|graded|slabbed|slab|pristine|black label|gma|mint cards|bccg|ksa|isa)\b", re.I)
# A grader named with no readable grade ("... PADPARADSCHA 1/1 PSA", "CGC AUTH"): slabbed, grade unknown.
GRADER_WORD_RE = re.compile(r"\b(PSA|BGS|SGC|CGC|TAG|BVG|CSG|HGA|beckett)\b", re.I)
# 2025 Very Lucky Black Cat #3 eye variations (White/Yellow/Green/Red/Blue/Purple Eyes SSP): not separate catalog
# cards. "Green Eyes" once read as a Green Refractor ($1,575 PSA 10) -- any title about eyes is refused.
EYE_VARIATION_RE = re.compile(r"\beyes?\b")
NUM_RE = re.compile(r"#\s*([A-Za-z]{1,4}-)?\s*(\d{1,3})\b(?!\s*/)")   # "#37/99" is a serial, not card #37
# Insert codes written WITHOUT '#' ("... Hidden Gems /5 HG-2"). Grader prefixes are not insert codes.
BARE_CODE_RE = re.compile(r"(?<![A-Za-z0-9#/-])([A-Z]{1,4})-(\d{1,3})\b")
GRADER_PREFIXES = {"PSA", "BGS", "SGC", "CGC", "TAG", "BVG", "CSG", "HGA"}
# A serial number on the title: "/25", "8/10", "1/1", or the words "serial numbered".
NUMBERED_RE = re.compile(r"(\b\d{1,4}\s*/\s*\d{1,4}\b|(?<![\w/])/\s*\d{1,4}\b|\bserial(?:ly)? numbered\b)", re.I)
GRADE_RE = re.compile(r"\b(PSA|BGS|SGC|CGC|TAG|BVG|CSG|HGA)[\s-]*(?:GEM\s*(?:MT|MINT)\s*|MINT\s*|PRISTINE\s*|GEM\s*)?"
                      r"(10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b", re.I)
SPECIAL_PAR = [("superfractor", "SuperFractor"), ("blast off", "Blast Off Refractor"), ("catfractor", "Black CatFractor"),
               ("x fractor", "X-Fractor"), ("xfractor", "X-Fractor"), ("shimmer", "Shimmer Refractor")]
COLORS = ["aqua", "pink", "yellow", "blue", "vf green", "green", "purple", "gold", "pearl", "orange", "black", "red"]
MODIFIERS = [("mini diamond", "Mini-Diamond"), ("raywave", "RayWave"), ("ray wave", "RayWave"), ("wave", "Wave")]
S2_FOILS = [("bubble gum", "Bubble Gum"), ("very rare", "Very Rare"), ("epic", "Epic"), ("diamond", "Diamond"),
            ("gold", "Gold"), ("hologram", "Hologram"), ("holo", "Hologram"), ("lava", "Lava"), ("rare", "Rare")]
# Every parallel on the checklists (2026: checklistinsider; 2025: the official veefriends.com collector's guide), with
# its print run. A Topps Chrome sale links only to a parallel ON its section's list, and a serial must equal that run.
_REF26 = [("VF Green Refractor", 99), ("Gold Refractor", 50), ("Orange Refractor", 25), ("Black Refractor", 10),
          ("Red Refractor", 5), ("SuperFractor", 1)]
_SPECKLE = [("Orange Speckle", 25), ("Purple Speckle", 10), ("Red Speckle", 5), ("Black Speckle", 1)]
TOPPS_PARALLELS = {
    ("2026-topps-chrome", "base"): [("Refractor", None), ("Yellow Refractor", None), ("X-Fractor", None), ("Pink Refractor", 250),
        ("Aqua RayWave Refractor", 199), ("Blue Refractor", 150), ("VF Green Refractor", 99), ("Purple Refractor", 75),
        ("Purple Mini-Diamond Refractor", 75), ("Gold Refractor", 50), ("Gold Mini-Diamond Refractor", 50), ("Pearl Refractor", 30),
        ("Orange Refractor", 25), ("Orange Mini-Diamond Refractor", 25), ("Black Refractor", 10), ("Black Mini-Diamond Refractor", 10),
        ("Black Wave Refractor", 10), ("Black CatFractor", 7), ("Red Refractor", 5), ("Red Mini-Diamond Refractor", 5),
        ("Red Wave Refractor", 5), ("Blast Off Refractor", 1), ("SuperFractor", 1)],
    ("2025-topps-chrome", "base"): [("Refractor", None), ("Yellow Refractor", None), ("Shimmer Refractor", 250), ("RayWave Refractor", 199),
        ("Wave Refractor", 150), ("VF Green Refractor", 99), ("Pink Refractor", 75), ("Gold Refractor", 50), ("Orange Refractor", 25),
        ("Purple Refractor", 20), ("Black Refractor", 10), ("Red Refractor", 5), ("Blast Off Refractor", 1), ("SuperFractor", 1)],
    ("2025-topps-chrome", "manga-speckle"): _SPECKLE, ("2026-topps-chrome", "manga-speckle"): _SPECKLE,
    ("2025-topps-chrome", "erupt"): [("Black Lava", 10), ("SuperFractor", 1)],
    ("2026-topps-chrome", "erupt"): [("Black Lava Refractor", 10), ("SuperFractor", 1)],
    ("2025-topps-chrome", "iconics"): [], ("2026-topps-chrome", "iconics"): [],
    ("2025-topps-chrome", "game-on"): [("Purple", 50), ("Green", 25), ("Yellow", 10), ("Teal", 5), ("SuperFractor", 1)],
    ("2025-topps-chrome", "sketch-to-screen"): _REF26,
    ("2026-topps-chrome", "auto-garyvee"): [], ("2026-topps-chrome", "comic-clippings"): [],
    ("2026-topps-chrome", "balance-battles"): _REF26[1:], ("2026-topps-chrome", "chalkboard"): _REF26,
    ("2026-topps-chrome", "neon-lights"): _REF26, ("2026-topps-chrome", "mega-heads"): [("Red Refractor", 5), ("SuperFractor", 1)],
    ("2026-topps-chrome", "original-sketch"): _REF26[1:], ("2026-topps-chrome", "stellar-haze"): _REF26[1:],
}
BASE_RUN = {("2026-topps-chrome", "auto-garyvee"): 5, ("2026-topps-chrome", "comic-clippings"): 1,
            ("2025-topps-chrome", "game-on"): 99}   # "Game On Base (Blue)" is itself numbered /99   # the card itself is numbered
# 2026 base cards that also exist as Purple/Green Variant SSPs (and Gritty Ghost / Last Glass Standing image variations):
# an unnumbered "Purple"/"Green"/"SSP" title on one of these could be the variation, so it is refused.
VARIATION_CODES_2026 = {"3", "7", "11", "15", "16", "39", "54", "109", "111", "143", "175", "200"}
_SPECIALS = [("catfractor", re.compile(r"\bcat ?fractor\b|\bblack cat (?:refractor|rf)\b")),
             ("superfractor", re.compile(r"\bsuper ?fractor\b")), ("blast off", re.compile(r"\bblast ?off\b")),
             ("x-fractor", re.compile(r"\bx ?fractor\b")), ("shimmer", re.compile(r"\bshimmer\b"))]
_COLORS = [("green", re.compile(r"\b(?:vf )?green\b")), ("aqua", re.compile(r"\baqua\b")), ("pink", re.compile(r"\bpink\b")),
           ("yellow", re.compile(r"\byellow\b")), ("blue", re.compile(r"\bblue\b")), ("purple", re.compile(r"\bpurple\b")),
           ("gold", re.compile(r"\bgold\b")), ("pearl", re.compile(r"\bpearl\b")), ("orange", re.compile(r"\borange\b")),
           ("black", re.compile(r"\bblack\b")), ("red", re.compile(r"\bred\b")), ("teal", re.compile(r"\bteal\b")),
           ("white", re.compile(r"\bwhite\b")), ("silver", re.compile(r"\bsilver\b")), ("bronze", re.compile(r"\bbronze\b")),
           ("rainbow", re.compile(r"\brainbow\b")), ("platinum", re.compile(r"\bplatinum\b")), ("sapphire", re.compile(r"\bsapphire\b"))]
_MODS = [("mini-diamond", re.compile(r"\bmini ?diamond\b")), ("raywave", re.compile(r"\bray ?wave\b")),
         ("wave", re.compile(r"(?<!ray )(?<!ray)\bwave\b"))]


def parallel_features(t):
    """(special, colors, mod) named in normalised text t. 'Refractor', 'Speckle' and 'Lava' are descriptors."""
    specials = []
    for name, rx in _SPECIALS:
        if rx.search(t):
            specials.append(name)
            t = rx.sub(" ", t)                       # "black cat refractor" must not also read as the colour Black
    colors = {name for name, rx in _COLORS if rx.search(t)}
    mods = [name for name, rx in _MODS if rx.search(t)]
    return specials, colors, (mods[0] if len(mods) == 1 else ("?" if mods else None))


_PAR_FEATS = {}


def _feats_of(name):
    if name not in _PAR_FEATS:
        sp, co, mo = parallel_features(norm(name))
        _PAR_FEATS[name] = (sp[0] if sp else None, next(iter(co)) if co else None, mo)
    return _PAR_FEATS[name]


def topps_parallel(t_wo, set_key, section, code, raw):
    """-> (parallel, print_run, basis) or (None, None, reason). Only parallels on the section's checklist list."""
    table = TOPPS_PARALLELS[(set_key, section)]
    runs = dict(table)
    m = PRINT_RUN_RE.search(raw)
    run = int(m.group(1)) if m else (1 if re.search(r"\b1\s*/\s*1\b|\bone of one\b", raw, re.I) else None)
    specials, colors, mod = parallel_features(t_wo)
    if (set_key, section) == ("2025-topps-chrome", "game-on") and colors == {"blue"}:
        colors = set()                                 # Blue is the Game On base, not a parallel
    if section != "manga-speckle" and re.search(r"\bspeckle\b", t_wo):
        return None, None, "ambiguous_parallel"         # a "Purple Speckle /75" base card could be Purple or Purple Mini-Diamond
    if len(specials) > 1 or len(colors) > 1 or mod == "?":
        return None, None, "ambiguous_parallel"
    named = bool(specials or colors or mod)
    if specials:
        if colors and specials[0] != "catfractor":
            return None, None, "ambiguous_parallel"
        cands = [n for n, _ in table if _feats_of(n)[0] == specials[0]]
    elif colors or mod:
        c = next(iter(colors)) if colors else None
        cands = [n for n, _ in table if _feats_of(n)[0] is None and _feats_of(n)[1] == c and _feats_of(n)[2] == mod]
        if not cands and mod is None:                     # "Aqua /199" = the only Aqua on the list (Aqua RayWave)
            cands = [n for n, _ in table if _feats_of(n)[0] is None and _feats_of(n)[1] == c]
        if not cands and c is None:                       # "RayWave /199" in 2026 = Aqua RayWave (the only RayWave)
            cands = [n for n, _ in table if _feats_of(n)[0] is None and _feats_of(n)[2] == mod]
    else:
        cands = ["Refractor"] if re.search(r"\b(?:refractor|rf)\b", t_wo) and "Refractor" in runs else ["Base"]
    if len(cands) != 1:
        return None, None, ("parallel_not_on_checklist" if not cands else "ambiguous_parallel")
    par = cands[0]
    pr = runs.get(par) if par != "Base" else BASE_RUN.get((set_key, section))
    basis = "named_in_title" if named or par == "Refractor" else "default_none_named"
    if run is not None and pr is None:
        # A serial on an unnumbered name ("Refractor 204/250"): the run names the parallel only when the title names no
        # colour at all and exactly one parallel on this list has that run.
        by_run = [n for n, r in table if r == run]
        if named or len(by_run) != 1 or section != "base":   # insert lists disagree between 2025 sources: no inference
            return None, None, "numbered_parallel_unnamed"
        par, pr, basis = by_run[0], run, "inferred_from_print_run"
    elif run is not None and run != pr:
        return None, None, ("print_run_conflict" if named else "numbered_parallel_unnamed")
    if (set_key == "2026-topps-chrome" and section == "base" and code in VARIATION_CODES_2026 and run is None
            and (colors & {"purple", "green"} or re.search(r"\bssp\b|\bshort print\b", t_wo))):
        return None, None, "variation_possible"
    return par, pr, basis


PRINT_RUN_RE = re.compile(r"(?:\b\d{1,4}\s*/\s*|(?<![\d/])/\s*)(\d{1,4})\b")   # "27/99" or "/99" -> 99


class Linker:
    def __init__(self, catalog):
        self.stickers = None
        self.by_code = {}                       # (set, section, code) -> card
        self.by_char = defaultdict(list)        # (set, section, normchar) -> [card]
        self.prefix = defaultdict(dict)         # set -> code prefix -> section
        self.sections = defaultdict(set)        # set -> sections it has
        self.chars = {}                         # normchar -> display
        for c in catalog["cards"]:
            if c["set_key"] == VS.SET_KEY or c["set_key"] in PROMO_SETS:
                continue                        # stickers and promos have their own routes in link()
            n = norm(c["character"])
            if c["code"]:
                self.by_code[(c["set_key"], c["section"], c["code"].upper())] = c
                m = re.match(r"([A-Z]+)-", c["code"])
                if m:
                    self.prefix[c["set_key"]][m.group(1)] = c["section"]
            self.by_char[(c["set_key"], c["section"], n)].append(c)
            self.sections[c["set_key"]].add(c["section"])
            if c["section"] != "balance-battles":
                self.chars[n] = c["character"]
        self.char_list = sorted(self.chars, key=len, reverse=True)
        self.stickers = VS.StickerLinker(roster=self.chars)

    def characters_in(self, t):
        """Distinct catalog characters named in normalised title t (longest match wins, no overlaps)."""
        found, taken = [], [False] * len(t)
        for n in self.char_list:
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", t):
                if not any(taken[m.start():m.end()]):
                    found.append((n, m.start(), m.end()))
                    for k in range(m.start(), m.end()):
                        taken[k] = True
        return found

    @staticmethod
    def sets_for(t, raw):
        if re.search(r"\bseries 2\b|compete (and )?collect|zerocool|zero cool", t):
            return ["2022-zerocool-series-2"]
        if re.search(r"\bseries 1\b", t):
            return []                                        # not in the catalog yet
        if "sapphire" in t or re.search(r"\bhidden gems?\b", t):     # Hidden Gems exists only in 2026 Sapphire
            if "sapphire" not in t:
                return ["2026-topps-chrome-sapphire"]
            yrs = [y for y in ("2025", "2026") if re.search(r"\b" + y + r"\b", raw)]
            return [f"{y}-topps-chrome-sapphire" for y in yrs] if len(yrs) == 1 else \
                ["2025-topps-chrome-sapphire", "2026-topps-chrome-sapphire"]
        if "chrome" in t or "topps" in t:
            yrs = [y for y in ("2025", "2026") if re.search(r"\b" + y + r"\b", raw)]
            return [f"{y}-topps-chrome" for y in yrs] if len(yrs) == 1 else ["2025-topps-chrome", "2026-topps-chrome"]
        return []

    @staticmethod
    def parallel(t, set_key):
        if set_key == "2022-zerocool-series-2":
            return next((lab for k, lab in S2_FOILS if re.search(r"\b" + k + r"\b", t)), "Base")
        if set_key.endswith("-sapphire"):
            if "padparadscha" in t:
                return "Padparadscha Sapphire"
            if "superfractor" in t:
                return "SuperFractor"
            col = next((c for c in ("gold", "white", "orange", "black", "red") if re.search(r"\b" + c + r"\b", t)), None)
            return f"{col.title()} Sapphire" if col else "Base"
        for k, lab in SPECIAL_PAR:
            if k in t:
                return lab
        color = next((c for c in COLORS if re.search(r"\b" + c + r"\b", t)), None)
        mod = next((lab for k, lab in MODIFIERS if re.search(r"\b" + k + r"\b", t)), None)
        if color:
            label = "VF Green" if color == "vf green" else color.title()
            return f"{label}{' ' + mod if mod and mod != 'RayWave' else ''}{' RayWave' if mod == 'RayWave' else ''} Refractor"
        if mod == "RayWave":
            return "RayWave Refractor"
        return "Refractor" if re.search(r"\brefractor\b", t) else "Base"

    def link(self, title):
        raw = title or ""
        t = norm(raw)
        if "veefriend" not in t.replace(" ", ""):
            return None, "not_veefriends"
        if NOT_SINGLE_RE.search(SINGLE_PHRASES_RE.sub(" ", t)) or EXTRA_ITEM_RE.search(raw) or LOT_RE.search(GRADE_RE.sub(" ", raw)):
            return None, "not_single"
        if not GRADE_RE.search(raw) and (GRADE_HINT_RE.search(raw) or GRADER_WORD_RE.search(raw)):
            return None, "grade_unknown"
        if ICGV_RE.search(t):
            if not re.search(r"\bgary ?bee\b", t) or PRINT_RUN_RE.search(raw) or re.search(r"\b(refractor|gold|red|black|superfractor|auto)\b", t):
                return None, "parallel_not_on_checklist"   # the promo has one version
            g = GRADE_RE.search(raw)
            return {"card_id": "vf:2026-topps-industry-conference:promo:ICGV", "set_key": "2026-topps-industry-conference",
                    "section": "promo", "code": "ICGV", "character": "GaryVee & Gary Bee", "parallel": "Base",
                    "parallel_basis": "default_none_named", "print_run": None,
                    "grade": f"{g.group(1).upper()} {g.group(2)}" if g else "Raw", "numbers_verified": True, "id_basis": "number",
                    "rule": "code+character"}, "linked"
        if "sticker" in t or "spectacular" in t:
            # Several sticker series exist (2024 originals, Manga Series, Magazine Exclusive, Road to VeeCon,
            # Treasure Chest...). Only the 2026 Spectacular Series is in the catalog, and only a title that
            # SAYS "spectacular" is provably that series.
            if "spectacular" not in t and not (VS.FINISH_ON_FINISH_RE.search(t) and "sticker" in t):
                return None, "set_unknown"
            if UNCATALOGED_RE.search(re.sub(r"\bdie ?cuts?\b", " ", t)):
                return None, "product_not_in_catalog"
            return self.stickers.link(raw)
        if UNCATALOGED_RE.search(t) or UNCATALOGED_RAW_RE.search(raw) or EYE_VARIATION_RE.search(t) or \
                ("content condor" in t and CONDOR_PEOPLE_RE.search(t)):
            return None, "product_not_in_catalog"
        for rx, full in ALIASES:
            t = rx.sub(full, t)
        sets = self.sets_for(t, raw)
        if not sets:
            return None, "set_unknown"
        num = NUM_RE.search(raw)
        code = ((num.group(1) or "").upper().replace(" ", "") + num.group(2)) if num else None
        if not code:
            bare = next((m for m in BARE_CODE_RE.finditer(raw) if m.group(1) not in GRADER_PREFIXES), None)
            if bare:
                code = f"{bare.group(1)}-{bare.group(2)}"
        chars = self.characters_in(t)
        if len({c for c, _, _ in chars}) > 1:
            return None, "multiple_characters"
        char = chars[0] if chars else None
        hits = []
        for s in sets:
            if s in CHAR_SETS:
                if char:
                    hits += [(c, s) for c in self.by_char.get((s, "char", char[0]), [])]
                continue
            sec = None
            if code and "-" in code:
                sec = self.prefix[s].get(code.split("-")[0])
                if sec is None:
                    continue
            if sec is None:
                sec = next((k for k, kws in SECTION_KEYWORDS.items()
                            if k in self.sections[s] and any(re.search(r"\b" + kw + r"\b", t) for kw in kws)), None)
            if sec is None:
                auto = re.search(r"\b(auto|autograph|autographed|signed)\b", t)
                if auto and re.search(r"\bgary ?vee\b|\bgaryvee\b", t) and s == "2026-topps-chrome":
                    sec = "auto-garyvee"
                elif auto:
                    continue                                   # other autographs are not in the catalog
                else:
                    sec = "base"
            if code and "-" not in code:                   # "Manga Speckle #9" -> MSS-9 within that section
                pre = next((p for p, k in self.prefix[s].items() if k == sec), None)
                if pre:
                    code_in = f"{pre}-{code}"
                else:
                    code_in = code
            else:
                code_in = code
            if code_in:
                c = self.by_code.get((s, sec, code_in))
                # Comic Clippings are catalogued by issue title ("VeeFriends #10 - Rare Robot"): the character may sit inside it
                if c and (not char or norm(c["character"]) == char[0] or (sec == "comic-clippings" and char[0] in norm(c["character"]))):
                    hits.append((c, s))
            elif char:
                hits += [(c, s) for c in self.by_char.get((s, sec, char[0]), [])]
        if len(hits) != 1:
            if not hits:
                return None, ("number_character_conflict" if code and char else "character_unknown" if not char and not code
                              else "no_catalog_match")
            return None, "ambiguous"
        card, s = hits[0]
        t_wo = t
        if char:
            t_wo = t[:char[1]] + " " + t[char[2]:]
        else:   # linked by code alone: the card's own name words ("Resilent Red Devil") are not parallel words
            name_words = set(norm(card["character"]).split())
            t_wo = " ".join(w for w in t.split() if w not in name_words)
        g = GRADE_RE.search(raw)
        pr = PRINT_RUN_RE.search(raw)
        if (s, card["section"]) in TOPPS_PARALLELS:
            par, run, basis = topps_parallel(t_wo, s, card["section"], card["code"], raw)
            if par is None:
                return None, basis
            return {"card_id": card["card_id"], "set_key": card["set_key"], "section": card["section"],
                    "code": card["code"], "character": card["character"], "parallel": par, "parallel_basis": basis,
                    "print_run": run if run is not None else (int(pr.group(1)) if pr else None),
                    "grade": f"{g.group(1).upper()} {g.group(2)}" if g else "Raw",
                    "numbers_verified": card["numbers_verified"], "id_basis": card.get("id_basis"),
                    "rule": "code+character" if code and char else "code" if code else "character"}, "linked"
        if (s, card["section"]) == ("2026-topps-chrome-sapphire", "hidden-gems"):
            # Hidden Gems has its OWN parallels (Sapphire checklist): Emerald, Onyx /10, Ruby /5, Padparadscha 1/1.
            # "Emerald as the base" (Sapphire checklist): an Emerald title is the Hidden Gems base card
            gems = [("Padparadscha Sapphire", r"\bpadparadscha\b", 1),
                    ("Onyx", r"\bonyx\b|\bblack\b", 10), ("Ruby", r"\bruby\b|\bred\b", 5)]
            named = [(n, r) for n, rx, r in gems if re.search(rx, t_wo)]
            run = int(pr.group(1)) if pr else (1 if re.search(r"\b1\s*/\s*1\b", raw) else None)
            if len(named) > 1 or re.search(r"\b(gold|white|orange|superfractor)\b", t_wo):
                return None, "parallel_not_on_checklist"
            par, prun = named[0] if named else ("Base", None)
            if run is not None and run != prun:
                return None, ("numbered_parallel_unnamed" if par == "Base" else "print_run_conflict")
            return {"card_id": card["card_id"], "set_key": card["set_key"], "section": card["section"], "code": card["code"],
                    "character": card["character"], "parallel": par, "parallel_basis": "named_in_title" if named else "default_none_named",
                    "print_run": prun, "grade": f"{g.group(1).upper()} {g.group(2)}" if g else "Raw",
                    "numbers_verified": card["numbers_verified"], "id_basis": card.get("id_basis"),
                    "rule": "code+character" if code and char else "code" if code else "character"}, "linked"
        if s.endswith("-sapphire") and card["section"] == "infinite-sapphire" and (
                PRINT_RUN_RE.search(raw) or re.search(r"\b(gold|white|orange|black|red|padparadscha|superfractor)\b", t_wo)):
            return None, "parallel_not_on_checklist"      # Infinite Sapphire has no parallels
        par = self.parallel(t_wo, s)
        basis = "named_in_title" if par != "Base" else "default_none_named"
        if s.endswith("-sapphire") and card["section"] == "base" and par != "Base" and par not in SAPPHIRE_RUNS.get(s, {}).values():
            return None, "parallel_not_on_checklist"     # e.g. SuperFractor, or Gold on 2025 Sapphire (Orange/Red/Padparadscha only)
        if s.endswith("-sapphire") and pr and par in SAPPHIRE_RUNS.get(s, {}).values():
            if {v: k for k, v in SAPPHIRE_RUNS[s].items()}[par] != int(pr.group(1)):
                return None, "print_run_conflict"          # "Gold Sapphire /25": the run and the colour disagree
        # GUARANTEED-SALES RULE (Andy 2026-09-27): a serial-numbered card whose parallel we cannot name is NOT the base
        # card (base is unnumbered). Refuse it rather than put a 1/1's price on the base card -- except in Sapphire sets,
        # where each print run belongs to exactly one parallel, so the run itself names it.
        if par == "Base" and s not in CHAR_SETS and NUMBERED_RE.search(raw):
            run = int(pr.group(1)) if pr else (1 if re.search(r"\b1\s*/\s*1\b", raw) else None)
            if card["section"] == "base" and run in SAPPHIRE_RUNS.get(s, {}):
                par, basis = SAPPHIRE_RUNS[s][run], "inferred_from_print_run"
            else:
                return None, "numbered_parallel_unnamed"
        return {"card_id": card["card_id"], "set_key": card["set_key"], "section": card["section"],
                "code": card["code"], "character": card["character"], "parallel": par, "parallel_basis": basis,
                "print_run": int(pr.group(1)) if pr else None,
                "grade": f"{g.group(1).upper()} {g.group(2)}" if g else "Raw",
                "numbers_verified": card["numbers_verified"], "id_basis": card.get("id_basis"),
                "rule": "code+character" if code and char else "code" if code else "character"}, "linked"


# ---------------------------------------------------------------- MAZI IDs (deterministic, permanent)
MAZI_SET_LINE = {"2025-topps-chrome": (2025, "Topps Chrome"), "2026-topps-chrome": (2026, "Topps Chrome"),
                 "2025-topps-chrome-sapphire": (2025, "Topps Chrome Sapphire"),
                 "2026-topps-chrome-sapphire": (2026, "Topps Chrome Sapphire"),
                 VS.SET_KEY: (2026, "Super Stickers Spectacular Series"),
                 "2026-topps-industry-conference": (2026, "Topps Industry Conference")}
NO_NUMBER_PREFIX = "nn-"   # number slot of a card whose set prints no numbers: 'nn-<section>' (see MAZI_ID_FORMAT_AUDIT)


def mazi_slug(s, maxlen=60):
    """VERBATIM from mazi-hq/mazidex-data loaders/load_scp.py slug() -- IDs must match the spine's rule exactly."""
    import unicodedata
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-').lower()
    return s[:maxlen].strip('-') or 'x'


def mazi_card_id(card):
    """mazi:vf:<year>-<set line>:<character>:<number>[~garyvee-auto]; None when the card has no verified number.
    A set that PRINTS no numbers (official checklist, id_basis 'checklist_name') puts 'nn-<section>' in the number
    slot: mazi:vf:2026-super-stickers-spectacular-series:patient-pig:nn-spectacular-stickers."""
    if card["set_key"] not in MAZI_SET_LINE:
        return None
    year, line = MAZI_SET_LINE[card["set_key"]]
    if card.get("id_basis") == VS.ID_BASIS:
        return f"mazi:vf:{year}-{mazi_slug(line)}:{mazi_slug(card['character'])}:{NO_NUMBER_PREFIX}{mazi_slug(card['section'])}"
    if not card["numbers_verified"] or not card["code"]:
        return None
    mid = f"mazi:vf:{year}-{mazi_slug(line)}:{mazi_slug(card['character'])}:{card['code'].lower()}"
    return mid + "~" + mazi_slug("GaryVee Auto", 40) if card["section"] == "auto-garyvee" else mid


def fetch_rows(c, since):
    c.execute("SET statement_timeout='300s'")
    return c.execute("""SELECT id, source_code, source_item_id, title, sold_price, sold_date, best_offer
                        FROM public.external_transactions
                        WHERE (title ILIKE '%%veefriend%%' OR title ILIKE '%%vee friend%%') AND sold_date >= %s
                        ORDER BY sold_date""", (since,)).fetchall()


def price_trust(src, bo):
    # MAZI hard gate: eBay Best Offer rows are context-only (the price is usually the LIST price).
    return "best_offer_context_only" if bo else "goldin_basis_unverified" if src == "goldin" else "sold_price"


def cmd_publish(args):
    """Write the catalog + links to Neon (public.veefriends_cards / veefriends_sale_links) in ONE transaction."""
    import psycopg
    cat = build_catalog()
    L = Linker(cat)
    with psycopg.connect(os.environ["MAZI_DB_URL"], connect_timeout=20) as c:
        rows = fetch_rows(c, args.since)
        c.rollback()
        links, reasons = [], Counter()
        for rid, src, sid, title, price, sold, bo in rows:
            rec, why = L.link(title)
            reasons[why] += 1
            if rec:
                links.append((rid, rec["card_id"], rec["parallel"], rec["parallel_basis"], rec["print_run"], rec["grade"],
                              price_trust(src, bo), rec["rule"], cat["version"]))
        with c.transaction():
            c.execute("SET LOCAL lock_timeout = '10s'")
            with c.cursor() as cur:
                cur.executemany("""INSERT INTO public.veefriends_cards (card_id, set_key, set_name, section, section_name, code,
                                     character, numbers_verified, id_basis, catalog_version)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT (card_id) DO UPDATE SET set_name=EXCLUDED.set_name, section_name=EXCLUDED.section_name,
                                     character=EXCLUDED.character, numbers_verified=EXCLUDED.numbers_verified,
                                     id_basis=EXCLUDED.id_basis, catalog_version=EXCLUDED.catalog_version, updated_at=now()""",
                                [(x["card_id"], x["set_key"], x["set_name"], x["section"], x["section_name"], x["code"],
                                  x["character"], x["numbers_verified"], x.get("id_basis"), cat["version"]) for x in cat["cards"]])
                # mint MAZI IDs for newly verified cards; never change one that exists (IDs are permanent)
                cur.executemany("""UPDATE public.veefriends_cards SET mazi_card_id = %s, updated_at = now()
                                   WHERE card_id = %s AND mazi_card_id IS NULL""",
                                [(mazi_card_id(x), x["card_id"]) for x in cat["cards"] if mazi_card_id(x)])
                cur.execute("DELETE FROM public.veefriends_sale_links")
                # cards that left the catalog (e.g. the 262-name sticker placeholder) go, unless they carry a MAZI ID:
                # minted IDs are permanent and are only ever redirected, never deleted.
                cur.execute("DELETE FROM public.veefriends_cards WHERE mazi_card_id IS NULL AND card_id <> ALL(%s)",
                            ([x["card_id"] for x in cat["cards"]],))
                one = "(%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                for i in range(0, len(links), 1000):
                    chunk = links[i:i + 1000]
                    cur.execute("""INSERT INTO public.veefriends_sale_links (external_transaction_id, card_id, parallel,
                                     parallel_basis, print_run, grade, price_trust, rule, catalog_version) VALUES """
                                + ", ".join([one] * len(chunk)), [v for row in chunk for v in row])
        n_cards = c.execute("select count(*) from public.veefriends_cards").fetchone()[0]
        n_links = c.execute("select count(*) from public.veefriends_sale_links").fetchone()[0]
    print(f"published: {n_cards:,} cards, {n_links:,} links from {len(rows):,} VeeFriends sales "
          f"({100 * len(links) / max(len(rows), 1):.1f}% linked)")
    for k, v in reasons.most_common():
        print(f"  {v:7,}  {k}")


def cmd_link(args):
    import psycopg
    cat = json.load(open(CATALOG))
    L = Linker(cat)
    with psycopg.connect(os.environ["MAZI_DB_URL"], connect_timeout=20) as c:
        rows = fetch_rows(c, args.since)
    out = os.path.join(HOME, "veefriends_sales_linked.jsonl")
    reasons, by_set, examples = Counter(), Counter(), defaultdict(list)
    linked = 0
    with open(out + ".tmp", "w") as f:
        for rid, src, sid, title, price, sold, bo in rows:
            rec, why = L.link(title)
            reasons[why] += 1
            if rec:
                linked += 1
                by_set[(rec["set_key"], rec["section"])] += 1
                rec.update({"external_transaction_id": rid, "source_code": src, "source_item_id": sid, "title": title,
                            "sold_price": str(price), "sold_date": sold.isoformat(), "best_offer": bool(bo),
                            "price_basis": "hammer_x_bp_unverified" if src == "goldin" else "sold_price",
                            # MAZI hard gate: eBay Best Offer rows are context-only (the price is usually the LIST price).
                            "price_trust": ("best_offer_context_only" if bo else "goldin_basis_unverified" if src == "goldin"
                                            else "sold_price"),
                            "catalog_version": cat["version"]})
                f.write(json.dumps(rec) + "\n")
            elif len(examples[why]) < 6:
                examples[why].append(f"[{src}] {title[:100]}")
    os.replace(out + ".tmp", out)
    print(f"VeeFriends sales since {args.since}: {len(rows):,} | linked {linked:,} ({100 * linked / max(len(rows), 1):.1f}%) -> {out}")
    for k, v in reasons.most_common():
        print(f"  {v:6,}  {k}")
    print("linked by set/section:")
    for k, v in by_set.most_common():
        print(f"  {v:6,}  {k[0]:24s} {k[1]}")
    for k, ex in examples.items():
        print(f"-- {k}:"); [print("     ", e) for e in ex]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog")
    lk = sub.add_parser("link"); lk.add_argument("--since", default="2025-01-01")
    pb = sub.add_parser("publish"); pb.add_argument("--since", default="2025-01-01")
    a = ap.parse_args()
    if a.cmd == "catalog":
        cat = build_catalog()
        tmp = CATALOG + ".tmp"; json.dump(cat, open(tmp, "w"), indent=1); os.replace(tmp, CATALOG)
        c = Counter((x["set_key"], x["section"]) for x in cat["cards"])
        print(f"catalog: {len(cat['cards']):,} cards -> {CATALOG}")
        for k, v in sorted(c.items()):
            print(f"  {v:4d}  {k[0]:24s} {k[1]}")
    elif a.cmd == "publish":
        cmd_publish(a)
    else:
        cmd_link(a)


if __name__ == "__main__":
    main()
