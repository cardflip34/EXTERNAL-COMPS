#!/usr/bin/env python3
"""veefriends_stickers.py -- 2026 VeeFriends Super Stickers: Spectacular Series catalog + STRICT title linker.

SOURCE: the official checklist linked from veefriends.com/about/explore/super-stickers-spectacular-series
("CHECKLIST" -> Google Drive file 1aZu5iIYTHQEdbY_Tq7xL6XV4EhxgTQTV, 6 pages, image-only). Transcribed by
hand 2026-09-27 and saved as ~/mazi_veefriends/checklists/super_stickers_spectacular_2026_official.pdf.

THE SET HAS NO PRINTED CARD NUMBERS. The checklist names characters per section, so a sticker's identity is
set + section + character (id_basis 'checklist_name') and its MAZI ID carries 'nn-<section>' in the number
slot. The only numbers on the checklist are the comic issue numbers ("VeeFriends #3: Decisive Duck"); they
are used as a cross-check against the title, not as identity.

Parallels come from the checklist's own parallel lines. A sale links only when the title names ONE sticker
and ONE parallel that exists for it. The /55 "Full Art Spectacular Background" parallels are finish ON
background ("Lava on Diamond", "Gold on Gold" = a Match): a title must name both, or say Match with one
finish. Anything else -- a /55 with one colour, an unnamed finish, a colour the section doesn't have -- is
refused with a reason. Nothing is guessed.
"""
from __future__ import annotations

import re

SET_KEY = "2026-super-stickers"
SET_NAME = "2026 VeeFriends Super Stickers Spectacular Series"
SOURCE = "veefriends.com official checklist (Google Drive 1aZu5iIYTHQEdbY_Tq7xL6XV4EhxgTQTV), transcribed 2026-09-27"
ID_BASIS = "checklist_name"

# ------------------------------------------------------------------ the official checklist (page order)
SPECTACULAR = [  # "Spectacular Stickers" -- 88 characters, pages 1-2
    "Adaptable Alien", "Adventurous Astronaut", "Alpha Alligator", "Ambitious Angel", "Amped Aye Aye",
    "Articulate Armadillo", "Aspiring Alpaca", "Bad Intentions", "Bashful Blobfish", "Benevolent Barn Owl",
    "Big Game Bandicoot", "Boisterous Beaver", "Bold As Heck Bat", "Bullish Bull",
    "Chill Chinchilla", "Compassionate Catfish", "Competitive Clown", "Considerate Cowboy", "Consistent Cougar",
    "Courageous Cockatoo", "Curious Crane", "Decisive Duck", "Determined Dolphin", "Dialed In Dog",
    "Driven Dragon", "Eager Eagle", "Empathy Elephant", "Entrepreneur Elf",
    "Fearless Fairy", "Flex'n Fox", "Fly Firefly", "Focused Falcon", "Forever Phoenix", "Forthright Flamingo",
    "GaryVee", "Genuine Giraffe", "Gifted Gopher", "Gracious Grizzly Bear", "Gritty Ghost", "Happy Hermit Crab",
    "Headstrong Honey Badger", "Heart-Trooper",
    "Helpful Hippo", "Hungry Hammerhead", "Hustling Hamster", "Hype Horse", "Innovative Impala",
    "Insightful Irish Terrier", "Jolly Jack-O", "Kind-Warrior", "Kindred Kangaroo", "Likable Leopard",
    "Logical Lion", "Mint Mink", "Motivated Monster", "Nifty Narwhal", "Noble Numbat", "Notorious Ninja",
    "O.G. Ox", "Passionate Parrot", "Patient Pig", "Persistent Penguin", "Perspective Pigeon", "Poised Pug",
    "Positive Porcupine", "Protective Panther", "Rare Robot", "Resilient Red Devil", "Sensible Sommelier",
    "Shrewd Shark", "Skilled Skeleton", "Smooth Spider", "Tenacious Turkey", "Thoughtful Three Horned Harpik",
    "Tidy Troll", "Tolerant Tuna", "Tranquil Toad", "Tremendous Tiger", "Trusting Tarantula", "Turnt Tick",
    "Versatile Viking", "Very, Very, Very, Very, Lucky Black Cat", "Vibe'n Vampire", "Warm Wolverine",
    "Well-Connected Werewolf", "Willful Wizard", "Witty Weasel", "Zealous Zombie",
]
DEBUT = ["Arbitraging Admiral", "Brave Bison", "Conviction Cockroach", "Cynical Cat", "Daring Dragonfly", "Gary Bee",
         "Hard-Working Wombat", "Hot Shot Hornet", "Juicy Jaguar", "Knowing Gnome", "Perfect Persian Cat",
         "Respectful Racoon"]
MINI = ["Bad Intentions", "Courageous Cockatoo", "Fly Firefly", "Happy Hermit Crab", "Knowing Gnome", "Notorious Ninja",
        "Persistent Penguin", "Perspective Pigeon", "Versatile Viking", "Willful Wizard"]
HAUNTED = ["Bold As Heck Bat", "Gritty Ghost", "Jolly Jack-O", "Motivated Monster", "Skilled Skeleton", "Vibe'n Vampire",
           "Well-Connected Werewolf", "Zealous Zombie"]
SIXTH = ["Articulate Armadillo", "Bashful Blobfish", "Competitive Clown", "Curious Crane", "Dialed In Dog", "Eager Eagle",
         "Flex'n Fox", "Focused Falcon", "Forever Phoenix", "Heart-Trooper", "Hustling Hamster", "Noble Numbat",
         "Poised Pug", "Protective Panther", "Rare Robot", "Sensible Sommelier", "Smooth Spider", "Tidy Troll",
         "Tranquil Toad", "Turnt Tick"]
SHOWDOWNS = [("Amped Aye Aye", "Chill Chinchilla"), ("Bullish Bull", "Considerate Cowboy"),
             ("Compassionate Catfish", "Brave Bison"), ("Entrepreneur Elf", "Arbitraging Admiral"),
             ("Fearless Fairy", "Bad Intentions"), ("Nifty Narwhal", "Hungry Hammerhead"),
             ("Resilient Red Devil", "Ambitious Angel"), ("Shrewd Shark", "Determined Dolphin"),
             ("Tremendous Tiger", "Likable Leopard"), ("Well-Connected Werewolf", "Vibe'n Vampire")]
DIE_CUTS = ["Alpha Alligator", "Aspiring Alpaca", "Big Game Bandicoot", "Boisterous Beaver", "Consistent Cougar",
            "Courageous Cockatoo", "Empathy Elephant", "Genuine Giraffe", "Gifted Gopher", "Gracious Grizzly Bear",
            "Happy Hermit Crab", "Headstrong Honey Badger", "Hype Horse", "Insightful Irish Terrier", "Kind-Warrior",
            "Kindred Kangaroo", "Mint Mink", "O.G. Ox", "Persistent Penguin", "Turnt Tick", "Trusting Tarantula",
            "Warm Wolverine"]
COMIC = {1: "Cynical Cat", 2: "Fearless Fairy", 3: "Decisive Duck", 4: "Thoughtful Three Horned Harpik",
         5: "Motivated Monster", 6: "Versatile Viking", 7: "Notorious Ninja", 8: "Gary Bee"}
#          #9 is "To Be Revealed" on the checklist: not a named card yet, so not catalogued.
OG_ART = ["Adaptable Alien", "Adventurous Astronaut", "Benevolent Barn Owl", "Chill Chinchilla", "Decisive Duck",
          "Driven Dragon", "Fly Firefly", "Forthright Flamingo", "Helpful Hippo", "Innovative Impala", "Jolly Jack-O",
          "Logical Lion", "Passionate Parrot", "Patient Pig", "Perspective Pigeon", "Positive Porcupine", "Shrewd Shark",
          "Tenacious Turkey", "Willful Wizard", "Witty Weasel"]
SCRATCH = [("Accountable Ant", "Bubble Gum"), ("Accountable Anteater", "Gold"), ("Articulate Armadillo", "Diamond"),
           ("Big Game Bandicoot", "Lava"), ("Capable Caterpillar", "Gold"), ("Magnanimous Maltese", "Bubble Gum"),
           ("Magnanimous Maltese", "Diamond"), ("O.G. Ox", "Bubble Gum"), ("Passionate Parrot", "Emerald"),
           ("Practical Peacock", "Hologram"), ("Resourceful Robin", "Bubble Gum"), ("Selfless Sloth", "Lava"),
           ("Slay'n Slug", "Bubble Gum"), ("Sufficient Shrimp", "Emerald"), ("Woke Walrus", "Hologram")]

FINISHES = ["Lava", "Bubble Gum", "Diamond", "Hologram", "Gold", "Emerald"]   # the base set's rarities
DUAL_AUTO_NAME = "GaryVee & Gary Bee"
SPECTACULAR_CAT = "The Spectacular Cat"

# section key -> (display name, characters, {parallel: print_run or None})
SECTIONS = {
    "spectacular-stickers": ("Spectacular Stickers", SPECTACULAR,
                             {**{f: None for f in FINISHES}, "White Ice": None, "GaryVee Autograph": 2,
                              **{f"{a} on {b}": 55 for a in FINISHES for b in FINISHES}}),
    "debut-stickers": ("Debut Stickers", DEBUT,
                       {"Red": None, "Orange": None, "Yellow": 499, "Green": 399, "Blue": 299, "Indigo": 99,
                        "Violet": 55, "Double Rainbow": 1, "Autograph": 1}),
    "mini-stickers": ("Mini Stickers", MINI, {"Base": None, "Tie Dye": None, "Gold": None, "Autograph": 1}),
    "haunted-holograms": ("Haunted Holograms", HAUNTED, {"Base": None}),
    "sixth-dimension": ("Sixth Dimension", SIXTH, {"Base": None, "Black": None}),
    "spectacular-showdowns": ("Spectacular Showdowns", [f"{a} vs {b}" for a, b in SHOWDOWNS], {"Base": None}),
    "diamond-die-cuts": ("Diamond Die-Cuts", DIE_CUTS, {"Base": None}),
    "garyvee-gary-bee-dual-autograph": ("GaryVee & Gary Bee Dual Autograph", [DUAL_AUTO_NAME],
                                        {"Green": 55, "Purple": 5, "Gold": 1}),
    "comic-inserts": ("Comic Inserts", list(COMIC.values()),
                      {"Base": None, "Gold": 99, "DJ Coffman Autograph": 55, "GaryVee & DJ Coffman Dual Autograph": 10}),
    "the-spectacular-cat": ("The Spectacular Cat", [SPECTACULAR_CAT], {"Base": 55, "GaryVee Autograph": 1}),
    "5-year-og-art-inserts": ("5 Year OG Art Inserts", OG_ART, {"Base": None, "GaryVee Autograph": 5}),
    "sweepstakes-scratch-offs": ("Sweepstakes Scratch-Offs", [f"{c} - {f}" for c, f in SCRATCH], {"Base": None}),
}


def norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str) -> str:
    return norm(s).replace(" ", "-")


def catalog_cards():
    """One catalog card per (section, sticker). code=None: the set prints no numbers."""
    out = []
    for sec, (sec_name, names, _pars) in SECTIONS.items():
        for name in names:
            out.append({"card_id": f"vf:{SET_KEY}:{sec}:{slug(name)}", "set_key": SET_KEY, "set_name": SET_NAME,
                        "section": sec, "section_name": sec_name, "code": None, "character": name,
                        "numbers_verified": False, "id_basis": ID_BASIS})
    return out


def parallel_print_run(section, parallel):
    return SECTIONS[section][2].get(parallel)


# ------------------------------------------------------------------ title -> sticker
# Title spellings -> the checklist's normalised name. Applied to norm(title).
T_ALIASES = [
    (re.compile(r"\b(?:very ){1,4}lucky black cat\b|\bv{2,4} lucky black cat\b|(?<!very )\blucky black cat\b"),
     "very very very very lucky black cat"),
    (re.compile(r"\bgary vee\b"), "garyvee"),
    (re.compile(r"\bflex n fox\b"), "flexn fox"), (re.compile(r"\bvibe n vampire\b"), "viben vampire"),
    (re.compile(r"\bslay n slug\b"), "slayn slug"), (re.compile(r"\bog ox\b"), "o g ox"),
    (re.compile(r"\bjolly jacko\b"), "jolly jack o"),
]
# Other sticker products and things this catalog cannot price as a single sticker.
EXCLUDE_RE = re.compile(r"\b(manga|jolly rancher|crossovers?|topps|chrome|zerocool|zero cool|sketch|printing plates?|"
                        r"custom|proxy|reprint|promo|variations?|errors?|misprints?|samples?|nft|sealed|unopened|wax|"
                        r"hanger box|blaster|display|binder|album|sheet|uncut|magazine|treasure chest|veecon|"
                        r"graded lot|mystery|repack)\b")
SERIES2_RE = re.compile(r"\bseries 2\b")
SEC_RX = [
    ("sweepstakes-scratch-offs", re.compile(r"\bscratch(?: ?offs?|ers?)?\b|\bsweepstakes?\b")),
    ("spectacular-showdowns", re.compile(r"\bshowdowns?\b|\bvs\b|\bversus\b")),
    ("haunted-holograms", re.compile(r"\bhaunted\b")),
    ("sixth-dimension", re.compile(r"\b(?:sixth|6th) dimension\b")),
    ("diamond-die-cuts", re.compile(r"\bdie ?cuts?\b|\bdiecuts?\b")),
    ("comic-inserts", re.compile(r"\bcomics?\b|\bcoffman\b|\bdjc\b|\bdj c\b")),
    ("5-year-og-art-inserts", re.compile(r"\bog art\b|\b5 ?(?:year|yr)s?\b|\bfive year\b")),
    ("debut-stickers", re.compile(r"\bdebuts?\b")),
    ("mini-stickers", re.compile(r"\bminis?\b")),
    ("garyvee-gary-bee-dual-autograph", re.compile(r"\bgary bee\b.*\bdual\b|\bdual\b.*\bgary bee\b")),
    ("the-spectacular-cat", re.compile(r"\bspectacular cat\b")),
]
# Section names that contain parallel words -- removed before reading the parallel.
SEC_PHRASE_RE = re.compile(r"\bhaunted holograms?\b|\bdiamond die ?cuts?\b|\bdiamond diecuts?\b|\bspectacular cat\b|"
                           r"\bsixth dimension\b|\b6th dimension\b|\bog art\b")
FINISH_RX = [("Bubble Gum", r"bubble ?gum|bubblegum|bubble|gum"), ("Hologram", r"holograms?|holos?"), ("Lava", r"lava"),
             ("Diamond", r"diamonds?"), ("Gold", r"gold"), ("Emerald", r"emeralds?")]
# "<finish> on <finish>" -- the /55 finish-on-background parallels exist only in the Spectacular Series
FINISH_ON_FINISH_RE = re.compile(r"\b(?:lava|bubble ?gum|bubblegum|gum|diamond|holo|hologram|gold|emerald) on "
                                 r"(?:lava|bubble ?gum|bubblegum|bubble|gum|diamond|holo|hologram|gold|emerald)\b")
FINISH_RE = re.compile(r"\b(" + "|".join(f"(?P<f{i}>{p})" for i, (_, p) in enumerate(FINISH_RX)) + r")\b")
OTHER_COLOR_RE = re.compile(r"\b(red|orange|yellow|green|blue|indigo|violet|purple|pink|silver|black|rainbow|tie ?dye|"
                            r"platinum|ruby|sapphire|white|chrome|refractor|prizm|black ice)\b")
AUTO_RE = re.compile(r"\b(auto|autos|autograph|autographed|signed|signature|on card auto)\b")
MATCH_RE = re.compile(r"\bmatch(?:ing|ed|es)?\b|\bcolou?r ?match\b|\bcolou?rmatch\b")
NUMBERED_WORD_RE = re.compile(r"\b(numbered|serial|serialized|serial numbered)\b")
RUN_RE = re.compile(r"(?:\b\d{1,4}\s*/\s*|(?<![\d/])/\s*)(\d{1,4})\b")        # "27/55" or "/55" -> 55
OF_RUN_RE = re.compile(r"\b\d{1,4}\s+of\s+(\d{1,4})\b", re.I)               # "1 of 2" -> 2
COMIC_NO_RE = re.compile(r"#\s*(\d{1,2})(?!\s*/)\b")                         # "Comic #3", never "#7/55"
GRADE_RE = re.compile(r"\b(PSA|BGS|SGC|CGC|TAG|BVG|CSG|HGA)[\s-]*(?:GEM\s*(?:MT|MINT)\s*|MINT\s*|PRISTINE\s*|GEM\s*)?"
                      r"(10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b", re.I)
DEBUT_COLORS = [("Double Rainbow", re.compile(r"\b(?:double )?rainbow\b")), ("Red", re.compile(r"\bred\b")),
                ("Orange", re.compile(r"\borange\b")), ("Yellow", re.compile(r"\byellow\b")),
                ("Green", re.compile(r"\bgreen\b")), ("Blue", re.compile(r"\bblue\b")),
                ("Indigo", re.compile(r"\bindigo\b")), ("Violet", re.compile(r"\bviolet\b"))]
DEBUT_BY_RUN = {499: "Yellow", 399: "Green", 299: "Blue", 99: "Indigo", 55: "Violet"}


class StickerLinker:
    def __init__(self, roster=()):
        """roster: extra normalised character names (the whole VeeFriends cast from the other checklists), so a title
        naming a character that has no sticker is refused instead of silently ignored."""
        self.by_sec = {}                      # (section, normname) -> card
        self.names = set(roster)
        for c in catalog_cards():
            n = norm(c["character"])
            self.by_sec[(c["section"], n)] = c
            if c["section"] == "sweepstakes-scratch-offs":
                n = norm(c["character"].rsplit(" - ", 1)[0])
            elif c["section"] == "spectacular-showdowns":
                for side in c["character"].split(" vs "):
                    self.names.add(norm(side))
                continue
            elif c["section"] in ("garyvee-gary-bee-dual-autograph", "the-spectacular-cat"):
                continue
            self.names.add(n)
        self.names.update(("garyvee", "gary bee"))
        self.name_list = sorted(self.names, key=len, reverse=True)
        self.showdown_of = {}
        for a, b in SHOWDOWNS:
            pair = frozenset((norm(a), norm(b)))
            for side in pair:
                self.showdown_of[side] = pair
        self.scratch = {}
        for ch, fin in SCRATCH:
            self.scratch.setdefault(norm(ch), {})[fin] = f"{ch} - {fin}"

    def names_in(self, t):
        found, taken = [], [False] * len(t)
        for n in self.name_list:
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", t):
                if not any(taken[m.start():m.end()]):
                    found.append((n, m.start(), m.end()))
                    for k in range(m.start(), m.end()):
                        taken[k] = True
        return found

    @staticmethod
    def _run(raw):
        m = RUN_RE.search(raw) or OF_RUN_RE.search(raw)
        if m:
            return int(m.group(1))
        return 1 if re.search(r"\bone of one\b|\b1\s*/\s*1\b", raw, re.I) else None

    @staticmethod
    def _finishes(t):
        out = []
        for m in FINISH_RE.finditer(t):
            name = next(FINISH_RX[i][0] for i in range(len(FINISH_RX)) if m.group(f"f{i}"))
            out.append((name, m.start(), m.end()))
        return out

    def link(self, raw):
        """-> (record, 'linked') or (None, reason). raw = the full listing title."""
        t = norm(raw)
        if EXCLUDE_RE.search(t):
            return None, "sticker_other_product"
        for rx, full in T_ALIASES:
            t = rx.sub(full, t)
        secs = [k for k, rx in SEC_RX if rx.search(t)]
        if "comic-inserts" in secs and "garyvee-gary-bee-dual-autograph" in secs:
            secs.remove("garyvee-gary-bee-dual-autograph")          # comic dual autos are GaryVee & DJ Coffman
        if SERIES2_RE.search(t) and secs != ["sweepstakes-scratch-offs"]:
            return None, "sticker_other_product"
        if len(secs) > 1:
            return None, "sticker_ambiguous_section"
        sec = secs[0] if secs else "spectacular-stickers"
        auto = bool(AUTO_RE.search(t))
        found = self.names_in(t)
        names = {n for n, _, _ in found}
        # "GaryVee Auto" names the SIGNER, not the character, when another character is on the title.
        if auto and "garyvee" in names and len(names) > 1:
            names.discard("garyvee")
            found = [f for f in found if f[0] != "garyvee"]

        # ---- which sticker
        if sec == "spectacular-showdowns":
            pairs = {self.showdown_of.get(n) for n in names}
            if not names or None in pairs or len(pairs) != 1:
                return None, "sticker_character_not_in_section"
            a, b = next(p for p in SHOWDOWNS if frozenset((norm(p[0]), norm(p[1]))) == next(iter(pairs)))
            character = f"{a} vs {b}"
        elif sec == "garyvee-gary-bee-dual-autograph":
            if not names or names - {"garyvee", "gary bee"}:
                return None, "sticker_character_not_in_section"
            character = DUAL_AUTO_NAME
        elif sec == "the-spectacular-cat":
            if names - {"garyvee"}:
                return None, "multiple_characters"
            character = SPECTACULAR_CAT
        else:
            if len(names) != 1:
                return None, "character_unknown" if not names else "multiple_characters"
            n = next(iter(names))
            if sec == "sweepstakes-scratch-offs":
                fins = {f for f, _, _ in self._finishes(t[:found[0][1]] + " " + t[found[0][2]:])}
                opts = self.scratch.get(n, {})
                hit = [opts[f] for f in fins if f in opts]
                if len(fins) != 1 or len(hit) != 1:
                    return None, "sticker_character_not_in_section"
                character = hit[0]
            else:
                card = self.by_sec.get((sec, n))
                if not card:
                    return None, "sticker_character_not_in_section"
                character = card["character"]
        card = self.by_sec[(sec, norm(character))]

        # ---- which parallel (title without the character names and section phrases)
        rest = t
        for _, s, e in sorted(found, key=lambda f: -f[1]):
            rest = rest[:s] + " " + rest[e:]
        rest = SEC_PHRASE_RE.sub(" ", rest)
        run = self._run(raw)
        numbered = run is not None or bool(NUMBERED_WORD_RE.search(t)) or bool(re.search(r"#\s*/|#\s*['\u2019]?d\b", raw))
        # The set prints no card numbers, so outside the comic inserts a "#55" on a sticker title is a serial number.
        numbered = numbered or (sec != "comic-inserts" and bool(re.search(r"#\s*\d", raw)))
        par, why = self._parallel(sec, rest, run, numbered, auto, character, raw)
        if par is None:
            return None, why
        pr = parallel_print_run(sec, par)
        if pr is not None and run is not None and run != pr:
            return None, "print_run_conflict"
        g = GRADE_RE.search(raw)
        return {"card_id": card["card_id"], "set_key": SET_KEY, "section": sec, "code": None, "character": character,
                "parallel": par, "parallel_basis": why, "print_run": pr if pr is not None else run,
                "grade": f"{g.group(1).upper()} {g.group(2)}" if g else "Raw", "numbers_verified": False,
                "id_basis": ID_BASIS, "rule": "checklist_name"}, "linked"

    def _parallel(self, sec, rest, run, numbered, auto, character, raw):
        fins = self._finishes(rest)
        fset = {f for f, _, _ in fins}
        others = {m.group(1) for m in OTHER_COLOR_RE.finditer(rest)}
        if sec == "spectacular-stickers":
            white_ice = bool(re.search(r"\bwhite ice\b", rest))
            if re.search(r"\bblack ice\b", rest) or others - ({"white"} if white_ice else set()):
                return None, "unknown_parallel_word"
            if auto:
                if character == "GaryVee" or fins or run not in (None, 2):
                    return None, "numbered_parallel_unnamed"
                return "GaryVee Autograph", "named_in_title"
            if white_ice:
                return (None, "numbered_parallel_unnamed") if (fins or numbered) else ("White Ice", "named_in_title")
            for (a, _, ea), (b, sb, _) in zip(fins, fins[1:]):
                if rest[ea:sb].strip() == "on":
                    return (f"{a} on {b}", "named_in_title") if len(fins) == 2 else (None, "ambiguous_parallel")
            if MATCH_RE.search(rest):
                return (f"{next(iter(fset))} on {next(iter(fset))}", "named_in_title") if len(fset) == 1 \
                    else (None, "numbered_parallel_unnamed")
            if numbered:
                return None, "numbered_parallel_unnamed"           # a /55 names finish AND background, or it's refused
            if len(fins) == 1:
                return fins[0][0], "named_in_title"
            return None, ("finish_unnamed" if not fins else "ambiguous_parallel")
        if sec == "debut-stickers":
            if fins or others - {"red", "orange", "yellow", "green", "blue", "indigo", "violet", "rainbow"}:
                return None, "unknown_parallel_word"
            cols = [name for name, rx in DEBUT_COLORS if rx.search(rest)]
            if auto:
                return (None, "ambiguous_parallel") if cols or run not in (None, 1) else ("Autograph", "named_in_title")
            if len(cols) > 1:
                return None, "ambiguous_parallel"
            if cols:
                return cols[0], "named_in_title"
            if run in DEBUT_BY_RUN:
                return DEBUT_BY_RUN[run], "inferred_from_print_run"  # each debut run belongs to exactly one colour
            return None, ("numbered_parallel_unnamed" if numbered else "finish_unnamed")
        if sec == "mini-stickers":
            tie = re.search(r"\btie ?dye\b|\btiedye\b", rest)
            gold = fset == {"Gold"}
            if (fset and not gold) or (others - {"tie dye", "tiedye", "tie  dye"} if tie else others):
                return None, "unknown_parallel_word"
            if auto:
                return (None, "ambiguous_parallel") if (tie or gold or run not in (None, 1)) else ("Autograph", "named_in_title")
            if numbered:
                return None, "numbered_parallel_unnamed"
            if tie and gold:
                return None, "ambiguous_parallel"
            return ("Tie Dye" if tie else "Gold" if gold else "Base"), ("named_in_title" if (tie or gold) else "default_none_named")
        if sec == "sixth-dimension":
            black = re.search(r"\bblack\b", rest)
            if fins or auto or numbered or others - {"black"}:
                return None, "unknown_parallel_word"
            return ("Black", "named_in_title") if black else ("Base", "default_none_named")
        if sec in ("haunted-holograms", "spectacular-showdowns", "diamond-die-cuts", "sweepstakes-scratch-offs"):
            if sec == "sweepstakes-scratch-offs":
                fins = []                                          # the finish is part of the scratch-off's name
            if fins or others or auto or numbered:
                return None, "unknown_parallel_word"
            return "Base", "default_none_named"
        if sec == "garyvee-gary-bee-dual-autograph":
            cols = [c for c in ("green", "purple", "gold") if re.search(r"\b" + c + r"\b", rest)]
            if fset - {"Gold"} or others - {"green", "purple"}:
                return None, "unknown_parallel_word"
            if len(cols) > 1:
                return None, "ambiguous_parallel"
            if cols:
                return cols[0].title(), "named_in_title"
            by_run = {55: "Green", 5: "Purple", 1: "Gold"}
            return (by_run[run], "inferred_from_print_run") if run in by_run else (None, "numbered_parallel_unnamed")
        if sec == "comic-inserts":
            m = COMIC_NO_RE.search(raw)
            no = next(k for k, v in COMIC.items() if v == character)
            if m and int(m.group(1)) != no:
                return None, "number_character_conflict"
            coff = re.search(r"\bcoffman\b|\bdjc\b|\bdj c\b", rest)
            if others or fset - {"Gold"}:
                return None, "unknown_parallel_word"
            if re.search(r"\bdual\b", rest) and (coff or "garyvee" in norm(raw).replace("gary vee", "garyvee")):
                return "GaryVee & DJ Coffman Dual Autograph", "named_in_title"
            if auto and coff:
                return "DJ Coffman Autograph", "named_in_title"
            if auto:
                return None, "ambiguous_parallel"
            if "Gold" in fset:
                return "Gold", "named_in_title"
            by_run = {99: "Gold", 55: "DJ Coffman Autograph", 10: "GaryVee & DJ Coffman Dual Autograph"}
            if run is not None:
                return (by_run[run], "inferred_from_print_run") if run in by_run else (None, "numbered_parallel_unnamed")
            return (None, "numbered_parallel_unnamed") if numbered else ("Base", "default_none_named")
        if sec == "the-spectacular-cat":
            if fins or others:
                return None, "unknown_parallel_word"
            if auto:
                return (None, "ambiguous_parallel") if run not in (None, 1) else ("GaryVee Autograph", "named_in_title")
            return (None, "numbered_parallel_unnamed") if run not in (None, 55) else ("Base", "default_none_named")
        if sec == "5-year-og-art-inserts":
            if fins or others:
                return None, "unknown_parallel_word"
            if auto or run == 5:
                return (None, "ambiguous_parallel") if run not in (None, 5) else ("GaryVee Autograph", "named_in_title")
            return (None, "numbered_parallel_unnamed") if numbered else ("Base", "default_none_named")
        return None, "section_unhandled"
