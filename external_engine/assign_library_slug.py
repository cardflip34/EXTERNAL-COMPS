#!/usr/bin/env python3
"""Assign a SportsCardsPro library slug to comps that have none. DRY RUN ONLY -- measures, never writes.

WHY: the front end keys every external comp to a card by raw->>'slug' (the SCP console/product slug,
set at ingest by the SCP bridge). Measured 2026-09-22: sportscardspro rows are 100% keyed; ebay,
fanatics, tcgplayer and goldin rows are 0% -- 4,784,180 comps (34.5%) invisible to every card page.
Re-normalizing external_transaction_normalizations does not touch this; the front end never reads it.

HOW: parse (year, set, #number) out of the title with the same set dictionary the normalizer uses,
look them up in the 1.7M-card library (~/mazi_scp_broad/targets*.jsonl), and accept ONLY a unique
hit. Base vs parallel is decided by the product slug: a title with no parallel word must land on the
base product; a title naming a parallel must land on a product that names it. Anything else is
'ambiguous', never assigned -- a wrong card attached to a sale is worse than a sale with no card.

Usage: python3 external_engine/assign_library_slug.py --source ebay --sample 20000 [--out report.csv]
"""
from __future__ import annotations
import time
import argparse, csv, json, os, re, sys, collections, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LIB = [os.path.expanduser("~/mazi_scp_broad/targets.jsonl"), os.path.expanduser("~/mazi_scp_broad/targets_tier2.jsonl")]
CATALOG = "/Volumes/MAZI_EVIDENCE_6TB/comp_images/_backfill/refmap/card_reference_images_by_slug.csv"   # 393K slugs the July target lists lack
SETS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mazi_db", "normalization", "card_sets.json")
PARALLEL_WORDS_LEGACY = ("refractor","prizm","holo","gold","silver","blue","red","green","orange","purple","pink","black",
                  "wave","mojo","shimmer","cracked ice","zebra","tiger","camo","disco","velocity","hyper","neon",
                  "sepia","negative","xfractor","x-fractor","atomic","rainbow","laser","chrome","1st")
YEAR = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")
NUM = re.compile(r"#\s*([A-Za-z0-9][A-Za-z0-9-]*)")

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

PARALLEL_VOCAB: set = set()
PREFIX_SET: dict = {}
SET_SLUGS_BY_YEAR: dict = {}
DISCRIMINATORS: set = set()
CORPUS_SPELLING: dict = {}
HARD_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mazi_db", "normalization", "library_hard_parallels.json")
HARD_PARALLELS: set = set()   # label-calibrated: tokens that, when a TITLE says them, the true product carries   # normalized slug -> the spelling the SCP corpus / front end uses   # vocab tokens that split sibling products (pink, refractor, wave) -- hard-gated   # year -> set of set-slugs present that year (for the parent/child walk)          # 'bd' -> 'bowman-draft', 'bcp' -> 'bowman-chrome-prospects' (mined)
COIN = re.compile(r"\b(pcgs|ngc|morgan|peace dollar|standing liberty|walking liberty|seated liberty|indian head|capped bust|barber|"
                  r"half dollar|silver eagle|bullion|mint state|ms ?6\d|proof ?6\d|\d+ ?oz|wheat cent|buffalo nickel|mercury dime)\b", re.I)
TCG = re.compile(r"\b(pok[eé]mon|tcg|mtg|magic the gathering|yu-?gi-?oh|one piece|lorcana|digimon|dragon ball|weiss|flesh and blood|metazoo)\b", re.I)

def load_index():
    """(year, set-slug, number) -> [(product-slug, full slug)], deduped (targets + tier2 overlap).

    Also mines PARALLEL_VOCAB from the library: every token a product slug carries beyond the player
    and the number IS a parallel/insert name ('fire-burst', 'dragon-scale', 'zebra'). A hand list
    missed those, so a 'Fire Burst' title fell through to the base card -- the worst failure mode.
    Numbers are indexed under the trailing token AND the trailing two ('m1b-13', 'bcp-125'), because
    modern inserts carry a prefix and the title writes it as #M1B-13."""
    idx: dict = collections.defaultdict(set)
    tok_count: collections.Counter = collections.Counter()
    lead_count: collections.Counter = collections.Counter()   # how often a token STARTS a product (a name)
    prefix_sets: dict = collections.defaultdict(collections.Counter)
    def slugs():
        for p in LIB:
            if not os.path.exists(p): continue
            with open(p, errors="ignore") as f:
                for ln in f:
                    try: yield (json.loads(ln).get("slug") or "")
                    except ValueError: continue
        if os.path.exists(CATALOG):                        # cards catalogued since the target lists were built
            with open(CATALOG) as f:
                next(f, None)
                for ln in f:
                    sl = ln.split(",", 1)[0]
                    CORPUS_SPELLING[re.sub(r"[^a-z0-9/]+", "", urllib.parse.unquote(sl).lower())] = sl
                    yield sl
    for slug in slugs():
        if True:
            if True:
                m = re.match(r"^([a-z]+)-cards-(\d{4})(?:-\d{2})?-([^/]+)/(.+)$", slug)
                if not m: continue
                prod, toks = m.group(4), m.group(4).split("-")
                if len(toks) < 2: continue
                for key in (toks[-1], "-".join(toks[-2:])):
                    idx[(m.group(2), m.group(3), key)].add((prod, slug))
                lead_count.update(toks[:2])
                for t in toks[2:-1]:                          # beyond first-last name, before the number
                    if not t.isdigit(): tok_count[t] += 1
                if len(toks) >= 2 and re.match(r"^[a-z]{1,4}$", toks[-2]) and toks[-1].isdigit():
                    prefix_sets[toks[-2]][m.group(3)] += 1    # '#BD-50' style prefix names the set
    # a parallel token is one that appears mid-slug often AND never leads a product: 'gold' qualifies,
    # 'jordan' does not. The first cut without this exclusion swallowed jordan/james/curry/mahomes/
    # lebron/davis/smith and rejected perfectly good titles for "no player found".
    # X2: 'green' leads ~50 products (Draymond, A.J.) but sits mid-slug 10,000+ times: a parallel. 'jordan' is
    # the reverse: a name. "Never leads a product" threw out every colour that is also a surname, and a
    # title's GREEN then passed the player check against kyler-murray-green-1.
    PARALLEL_VOCAB.update(t for t, c in tok_count.items() if c >= 25 and len(t) > 2 and c > 3 * lead_count.get(t, 0))
    split_ct: collections.Counter = collections.Counter(); seen_ct: collections.Counter = collections.Counter()
    for (yy, ss, _n), prods in idx.items():
        SET_SLUGS_BY_YEAR.setdefault(yy, set()).add(ss)
        if len(prods) < 2: continue
        toksets = [set(p.split("-")[2:-1]) & PARALLEL_VOCAB for p, _ in prods]
        allt = set().union(*toksets); common = set.intersection(*toksets) if toksets else set()
        for t in allt:
            seen_ct[t] += 1
            if t not in common: split_ct[t] += 1
    # Q1: a colour/finish splits siblings nearly every time it appears ('pink' is on some #1s, not all);
    # 'rookie' or 'auto' tends to ride on every product of its group. Only the splitters are hard-gated.
    DISCRIMINATORS.update(t for t, c in split_ct.items() if seen_ct[t] >= 20 and c / seen_ct[t] >= 0.6)
    # 'rookie' splits library siblings, so the sibling test called it a discriminator -- but sellers write
    # "Rookie" on nearly every rookie card whatever the slug says, and gating on it rejected everything.
    # The gate uses the LABEL-calibrated list (P(product has t | title says t) >= 0.7) when it exists.
    if os.path.exists(HARD_JSON):
        HARD_PARALLELS.update(json.load(open(HARD_JSON))["hard"])
    else:
        HARD_PARALLELS.update(DISCRIMINATORS)
    # P1: a token that sits directly before the number in >=20 products is a NUMBER PREFIX ('mf', 'pp',
    # 'sccua'), not a parallel. Left in the vocab, every card of that insert looked like a parallel and
    # a plain title's "must be a base product" filter emptied the candidate list -- 73% of all misses.
    # ...but only when prefix-position use DOMINATES the token: 'bcp' is only ever a prefix; 'pink' sits
    # before the number in a fraction of its 20,000 uses. The first cut stripped every 1-4 letter colour
    # and finish (pink, red, ice, gold, wave) and 796 of 928 wrong-parallel picks were that.
    PARALLEL_VOCAB.difference_update(p for p, c in prefix_sets.items()
                                     if sum(c.values()) >= 20 and sum(c.values()) >= 0.8 * tok_count.get(p, 0))
    for pre, c in prefix_sets.items():
        top, n = c.most_common(1)[0]
        if n >= 20 and n / sum(c.values()) >= 0.8: PREFIX_SET[pre] = top
    return {k: sorted(v) for k, v in idx.items()}

LIB_SETS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mazi_db", "normalization", "library_set_slugs.json")
_SETMAP: dict = {}

def set_regex():
    """Match the library's own set vocabulary, INCLUDING brand-only sets ('Topps', 'Bowman') and
    brand-dropped variants ('Prizm' for 'Panini Prizm'). card_sets.json deliberately excludes
    brand-only names because the normalizer handles brands separately -- correct there, wrong here:
    first run of this tool read 51% of eBay orphans as 'missing a set' and 29% as 'no library card',
    almost entirely because '2024 Topps ...' had no set and 'Prizm' slugified to prizm, not
    panini-prizm. The surface text is mapped to the library slug, never slugified directly."""
    global _SETMAP
    cfg = json.load(open(LIB_SETS_JSON))["entries"]
    _SETMAP = {k.lower(): v for k, v in cfg.items()}
    names = sorted(_SETMAP, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in names) + r")\b", re.I)

def set_slugs_for(surface: str) -> list[str]:
    e = _SETMAP.get(re.sub(r"\s+", " ", surface.lower()).strip())
    return ([e["slug"]] + list(e.get("alts", []))) if e else [slugify(surface)]

def _player_tokens(title: str, setre, span=None) -> list[str]:
    """Alpha tokens left after removing year/set/number/grade -- the player is what remains.
    Only the set span actually USED is removed: setre.sub() stripped every match, and a player-named
    console ('Topps Shohei Ohtani ...') made 'SHOHEI OHTANI' vanish from its own title."""
    t = title if span is None else title[:span[0]] + " " + title[span[1]:]
    t = YEAR.sub(" ", t); t = NUM.sub(" ", t)
    if span is None: t = setre.sub(" ", t)
    t = re.sub(r"\b(psa|bgs|sgc|cgc|gem|mint|mt|rc|rookie|auto|graded|card|lot)\b", " ", t, flags=re.I)
    t = re.sub(r"['\u2019]", "", t)
    # keep colour words: 'AJ Green' lost 'green' here and could never match aj-green. The slug side
    # already ends a name at a parallel token from position 2, so 'green' cannot hit kyler-murray-green.
    return re.findall(r"[a-z]{2,}", t.lower())

def assign(title: str, idx, setre, explain: bool = False):
    r = _assign(title, idx, setre, explain)
    return r if explain else r[:2]

def _assign(title: str, idx, setre, explain: bool):
    if TCG.search(title): return "non_sports_tcg", None, []       # the library is SportsCardsPro; Pokemon is not in it
    if COIN.search(title): return "coins", None, []                # nor are Morgan dollars
    y = YEAR.search(title)
    # the library never spells a set with '&' ('Allen & Ginter' is 'allen-and-ginter' or 'allen-ginter'):
    # match the title both ways and keep the longest set span
    s = setre.search(title)
    if "&" in title:                                                    # only pay for the variants when there is an '&'
        for v in (re.sub(r"\s*&\s*", " and ", title), re.sub(r"\s*&\s*", " ", title)):
            m = setre.search(v)
            if m and (s is None or len(m.group(0)) > len(s.group(0))): s = m
        if s is not None and s.string is not title:
            s = setre.search(s.string)                                  # span is on the variant; keep it consistent
            title = s.string
    # '#10/25' is a print run, not card #10 -- first cut matched Rhys Hoskins #10 off a serial number
    k = next((m for m in NUM.finditer(title) if not re.match(r"\s*/\s*\d", title[m.end():m.end()+4])), None)
    if not (y and s and k): return "missing_field", None, []
    num = k.group(1).lower()
    numkeys = [num] + ([num.split("-", 1)[1]] if "-" in num else [])          # '#bcp-125' -> also '125'
    g = re.fullmatch(r"([a-z]+)(\d+)", num)                                    # '#PP25' is written pp-25 in the slug
    if g: numkeys += [f"{g.group(1)}-{g.group(2)}", g.group(2)]
    if num.isalpha() and "-" not in num:                                       # '#CPARC' is cpa-rc: split a known prefix off
        for pfx in sorted(PREFIX_SET, key=len, reverse=True):
            if num.startswith(pfx) and len(num) > len(pfx): numkeys.append(f"{pfx}-{num[len(pfx):]}"); break
    years = [y.group(1), str(int(y.group(1)) - 1)]                             # '2020 Prizm' may be the 2019-20 set
    set_slugs = set_slugs_for(s.group(0))
    pre = num.split("-", 1)[0] if "-" in num else None
    if pre and pre in PREFIX_SET and PREFIX_SET[pre] not in set_slugs:
        set_slugs = [PREFIX_SET[pre]] + set_slugs                              # '#BD-50' says Bowman Draft, whatever the words say
    # Collect EVERY hit across years x set-slugs x number forms. The first cut stopped at the first
    # non-empty key: a card at #BCP-125 or in the y-1 season fell through to "no library card" (34%).
    hits = []
    for yy in years:
        for ss in set_slugs:
            for nk in numkeys:
                for c in idx.get((yy, ss, nk), []): hits.append((yy, ss, c))
    cands = [c for _, _, c in hits]
    ptoks = _player_tokens(title, setre, (s.start(), s.end()))
    def name_toks(prod):
        # the name is every leading token that is not a parallel word or a number: 'ja-marr-chase-207' has
        # three, 'kyler-murray-green-1' has two. Reading a fixed two rejected every three-token name.
        out = []
        for i, tk in enumerate(prod.split("-")):
            if tk.isdigit() or tk in PREFIX_SET: break
            if tk in PARALLEL_VOCAB and i >= 2: break                    # 'aj-green' keeps green; 'kyler-murray-green' stops
            out.append(tk)
        return out[:4]
    def by_player(cs):
        def hit(c, minlen):
            nt = name_toks(c[0]); joined = "".join(nt)                     # 'jamarr' == 'ja'+'marr'
            # the joined-substring rule exists for apostrophe names ("jamarr" in "jamarrchase"); a parallel
            # colour must not use it, or a title's GREEN finds any player whose name contains "green"
            return any((len(w) >= minlen and w in nt) or (len(w) >= 5 and w not in PARALLEL_VOCAB and w in joined) for w in ptoks)
        keep = [c for c in cs if hit(c, 4)]
        if not keep: keep = [c for c in cs if hit(c, 3)]                   # 'Bo Nix'
        return keep
    low_toks = set(slugify(title[:s.start()] + " " + title[s.end():]).split("-"))
    wants_auto = bool(re.search(r"\b(auto|autograph|autographed|signatures?)\b", title, re.I))
    def auto_ok(ss, prod):
        has = bool(re.search(r"auto|signature", ss + "-" + prod))
        return has == wants_auto
    # ONE pool: direct hits (support 1.0) plus every family child in the year, each child ranked by how
    # much of its extra name the title carries. Filters run on the whole pool, so a rejected direct hit
    # can still fall back to the child that actually matches ("Rookie of the Year Favorites" -> the
    # roy-favorites insert, not base #4). Previously the first path to return won and later filters
    # had nothing to fall back to.
    primary = set_slugs[0]
    pool = [((1.0 if ss0 == primary else 0.9), 1 if (pre and ss0 == PREFIX_SET.get(pre)) else 0, yy, ss0, c) for yy, ss0, c in hits]
    for yy in years:
        for ss in SET_SLUGS_BY_YEAR.get(yy, ()):
            if ss in set_slugs or not any(ss.startswith(b) or b.startswith(ss) or ss.endswith("-" + b) or b.endswith("-" + ss) for b in set_slugs): continue
            extra = set(ss.split("-")) - set("-".join(set_slugs).split("-"))
            support = len(extra & low_toks) / max(len(extra), 1)
            for nk in numkeys:
                for c in idx.get((yy, ss, nk), []): pool.append((support, 1 if (pre and ss == PREFIX_SET.get(pre)) else 0, yy, ss, c))
    if not pool: return "no_library_card", None, []
    pool = [p for p in pool if p[4] in by_player([p[4]])]
    if not pool: return "player_mismatch", None, []
    pool = [p for p in pool if auto_ok(p[3], p[4][0])]
    if not pool: return "no_library_card", None, []
    named = {w for w in low_toks if w in PARALLEL_VOCAB}
    hard = named & HARD_PARALLELS
    pool = [p for p in pool if hard <= (set(p[4][0].split("-")[2:-1]) & PARALLEL_VOCAB)]
    if not pool: return "no_library_card", None, []
    both = [p for p in pool if all(w in ptoks for w in p[4][0].split("-")[:2])]
    if both and len({p[4][0] for p in both}) == 1: pool = both
    # a child the title actually names beats a plain base hit; an unnamed child never beats it
    top_support = max(p[0] for p in pool)
    if top_support >= 0.5: pool = [p for p in pool if p[0] >= 0.5]
    else: pool = [p for p in pool if p[0] == top_support]
    # season: the same product under both y and y-1. Measured on 52/52 labeled cases, the year the seller
    # wrote was the library's year every time -- so prefer it; y-1 stays a fallback, not a tie.
    by_name = collections.defaultdict(set)
    for p in pool: by_name[p[4][0]].add(p[2])
    if any(len(v) > 1 for v in by_name.values()):
        stated = [p for p in pool if p[2] == y.group(1)]
        if stated: pool = stated
    def pscore(p):
        pt = set(p[4][0].split("-")[2:-1]) & PARALLEL_VOCAB
        return len(named & pt) - len(pt - named)
    def pcover(p):                                                        # 3-player team card: the product naming
        return sum(1 for w in set(ptoks) if len(w) >= 4 and w in name_toks(p[4][0]))   # three of them beats one naming none
    key = lambda p: (p[0], p[1], pcover(p), pscore(p))
    ranked = sorted(pool, key=key, reverse=True)
    best = [p for p in ranked if key(p) == key(ranked[0])]
    if len({(p[2], p[3]) for p in best}) > 1: return "ambiguous", None, best   # equal rank across different (year,set) -> refuse
    # a child that won on support must carry a title token its runner-up LACKS: 'Downtown' beat
    # 'Optic Downtown' only because it had fewer extra tokens to match -- that is a guess, not evidence.
    runners = [p for p in ranked if p not in best and p[3] != best[0][3]]
    if runners and best[0][0] < 1.0:
        w_extra = set(best[0][3].split("-")) & low_toks; r_extra = set(runners[0][3].split("-")) & low_toks
        if w_extra <= r_extra and runners[0][0] >= 0.5: return "ambiguous", None, best + runners[:1]
    if len(best) > 1:
        best.sort(key=lambda p: len(p[4][0]))                            # within one set, the tightest product
        if len(best[0][4][0]) == len(best[1][4][0]): return "ambiguous", None, best
    top = best[0][4]
    chosen = top[1]
    # confidence: a title that names NO parallel while siblings exist is choosing the base on silence
    siblings = [p for p in pool if p[4][0] != top[0]]
    conf = "low" if (not named and siblings) else "high"
    return "assigned", CORPUS_SPELLING.get(re.sub(r"[^a-z0-9/]+", "", urllib.parse.unquote(chosen).lower()), chosen), (conf, [p[4][0] for p in siblings][:6])


# ---------------------------------------------------------------------------------------------
# GRADE LABEL, in SportsCardsPro's exact dialect.
#
# The front end reads raw->>'grade_label' verbatim to pick the grade tab (its loader query, seen in
# pg_stat_statements 2026-09-23: `raw->>$1 AS slug, raw->>$2 AS grade_label ...`). Every label SCP
# writes, measured over a 1% sample of its 9M rows: 'Ungraded'; 'Grade 1'..'Grade 9' and half grades
# ('Grade 9.5') with NO company; the company appears only at 10 -- 'PSA 10', 'SGC 10', 'BGS 10',
# 'CGC 10', 'TAG 10', plus 'BGS 10 Black' and 'CGC 10 Prist.'. An assigned comp must speak this
# dialect: an eBay PSA 10 written with a slug but no label would be filed under RAW.
_GRADE = None


def grade_label(title: str) -> str:
    global _GRADE
    if _GRADE is None:
        from mazi_db.scripts.normalize_external_transactions import GRADE_RE, GRADE_COMPANY_CHOICES, canonical_from_choices
        _GRADE = (GRADE_RE, GRADE_COMPANY_CHOICES, canonical_from_choices)
    grade_re, choices, canon = _GRADE
    m = grade_re.search(title or "")
    if not m or not m.group(2):
        return "Ungraded"
    company = (canon(m.group(1), choices) or "").upper()
    try:
        gv = float(m.group(2).rstrip("."))
    except ValueError:
        return "Ungraded"
    if gv == 10 and company:
        if company == "BGS" and re.search(r"\bblack\s*label\b", title, re.I):
            return "BGS 10 Black"
        if company == "CGC" and re.search(r"\bpristine\b", title, re.I):
            return "CGC 10 Prist."
        return f"{company} 10"
    return f"Grade {int(gv) if gv.is_integer() else gv}"


# ---------------------------------------------------------------------------------------------
# FULL PASS + WRITER
#
# --all streams every orphan of a source in primary-key pages (the front end reads this table the
# same way), so a page is an index range scan however sparse the source is among 13.9M rows, and the
# pass survives a dropped connection: the last id is checkpointed next to the report. Output is the
# same CSV as sample mode plus grade_label; it is the manifest for --apply AND for --undo.
#
# --apply writes ONLY rows the manifest marks assigned, merges into raw (never replaces it), and tags
# each write with slug_source/slug_assigned_at so it is auditable and reversible. WHERE ... NOT
# (raw ? 'slug') means a row that gained a slug by any other route since the manifest is left alone.
SLUG_SOURCE = "library_assign_v1"
PAGE = 10000


def iter_orphan_pages(conn, source: str, ckpt_path: str):
    last = 0
    try:
        last = int(open(ckpt_path).read().strip() or 0)
    except (OSError, ValueError):
        pass
    page = PAGE
    cur = conn.cursor()
    while True:
        cur.execute("SET statement_timeout='240s';")
        try:
            cur.execute("SELECT id, title FROM external_transactions WHERE source_code=%s AND id>%s "
                        "AND title IS NOT NULL AND NOT (raw ? 'slug') ORDER BY id LIMIT %s;", (source, last, page))
            rows = cur.fetchall()
        except Exception as e:  # noqa: BLE001 -- a dense region can time the page out; shrink and retry
            conn.rollback()
            if page > 500:
                page //= 2; print(f"   page timed out ({str(e)[:60]}); retrying with page={page}", flush=True); continue
            raise
        if not rows:
            return
        yield rows
        last = rows[-1][0]
        with open(ckpt_path, "w") as f:
            f.write(str(last))


def run_all(a) -> int:
    import psycopg
    t_idx = time.time(); idx = load_index(); setre = set_regex()
    print(f"library index built in {time.time()-t_idx:.0f}s", flush=True)
    ckpt = a.out + ".ckpt"; resume = os.path.exists(ckpt)
    f = open(a.out, "a" if resume else "w", newline=""); w = csv.writer(f)
    if not resume:
        w.writerow(["transaction_id", "slug", "status", "grade_label"])
    out = collections.Counter(); n = 0; t0 = time.time()
    with psycopg.connect(os.environ["MAZI_DB_URL"], connect_timeout=20) as c:
        for rows in iter_orphan_pages(c, a.source, ckpt):
            for tid, title in rows:
                st, slug = assign(title, idx, setre); out[st] += 1; n += 1
                w.writerow([tid, slug or "", st, grade_label(title) if st == "assigned" else ""])
            f.flush()
            el = time.time() - t0
            print(f"   {a.source}: {n:,} done  assigned={out['assigned']:,} ({100*out['assigned']/n:.1f}%)  "
                  f"{n/max(el,1e-9):,.0f}/s  last_id={rows[-1][0]}", flush=True)
    f.close()
    print(f"{a.source}: FULL PASS COMPLETE  {n:,} orphans in {(time.time()-t0)/60:.1f} min")
    for k, v in out.most_common():
        print(f"   {v:>8,}  {100*v/max(n,1):5.1f}%  {k}")
    return 0


def run_apply(a, undo: bool = False) -> int:
    import psycopg
    reader = csv.DictReader(open(a.apply or a.undo))
    if "grade_label" not in (reader.fieldnames or []):
        # A manifest without grade labels predates 2026-09-23 (sample mode wrote 3 columns). Applying
        # it would default every comp to 'Ungraded' -- a PSA 10 filed under RAW on the card page.
        print("REFUSED: manifest has no grade_label column; regenerate it with --all before applying.")
        return 2
    rows, seen = [], set()
    for r in reader:
        # a pass that died mid-page re-processes that page on resume, so an id can appear twice
        if r.get("status") == "assigned" and r.get("slug") and r["transaction_id"] not in seen:
            seen.add(r["transaction_id"]); rows.append(r)
    verb = "UNDO" if undo else "WRITE"
    print(f"manifest: {len(rows):,} assigned rows -> {verb}")
    if not a.yes_i_understand_prod:
        print("dry: pass --yes-i-understand-prod to execute. Nothing written.")
        return 0
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    done = 0; relabelled = [0]
    with psycopg.connect(os.environ["MAZI_DB_URL"], connect_timeout=20) as c:
        cur = c.cursor(); cur.execute("SET statement_timeout='300s';")
        for i in range(0, len(rows), 1000):
            chunk = rows[i:i+1000]
            if undo:
                cur.executemany("UPDATE external_transactions SET raw = raw - 'slug' - 'grade_label' - 'slug_source' - 'slug_assigned_at' "
                                "WHERE id=%s AND raw->>'slug_source'=%s;", [(int(r["transaction_id"]), SLUG_SOURCE) for r in chunk])
            else:
                # The label is computed HERE, from the live title and the current parser -- never
                # copied from the manifest. A manifest written before a parser fix would otherwise
                # carry the old mistake into production (the Goldin manifest of 2026-09-23 labelled
                # "PSA VG 3" as Ungraded). The manifest's column is kept for review only.
                ids = [int(r["transaction_id"]) for r in chunk]
                cur.execute("SELECT id, title FROM external_transactions WHERE id = ANY(%s);", (ids,))
                titles = dict(cur.fetchall())
                params = []
                for r in chunk:
                    tid = int(r["transaction_id"]); fresh = grade_label(titles.get(tid) or "")
                    if fresh != (r.get("grade_label") or ""): relabelled[0] += 1
                    params.append((r["slug"], fresh, SLUG_SOURCE, ts, tid))
                cur.executemany("UPDATE external_transactions SET raw = raw || jsonb_build_object("
                                "'slug', %s::text, 'grade_label', %s::text, 'slug_source', %s::text, 'slug_assigned_at', %s::text) "
                                "WHERE id=%s AND NOT (raw ? 'slug');", params)
            c.commit(); done += len(chunk)
            if done % 20000 == 0 or done == len(rows):
                print(f"   {verb}: {done:,}/{len(rows):,} committed", flush=True)
    print(f"{verb} COMPLETE: {done:,} rows processed" + ("" if undo else f"; {relabelled[0]:,} labels differed from the manifest and were written fresh"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--source", default="ebay"); ap.add_argument("--sample", type=int, default=20000)
    ap.add_argument("--out"); ap.add_argument("--pct", type=float, default=0.3, help="TABLESAMPLE SYSTEM percent; raise for large --sample")
    ap.add_argument("--all", action="store_true", help="full resumable pass over every orphan of --source (dry: writes the manifest CSV only)")
    ap.add_argument("--apply", help="manifest CSV from --all: write slug+grade_label into raw for its assigned rows")
    ap.add_argument("--undo", help="manifest CSV: strip what --apply wrote (only rows tagged slug_source=%s)" % SLUG_SOURCE)
    ap.add_argument("--yes-i-understand-prod", action="store_true")
    a = ap.parse_args()
    if a.apply or a.undo:
        return run_apply(a, undo=bool(a.undo))
    if a.all:
        if not a.out: ap.error("--all needs --out")
        return run_all(a)
    import psycopg
    t_idx = time.time(); idx = load_index(); setre = set_regex(); t_idx = time.time() - t_idx
    print(f"library index: {sum(len(v) for v in idx.values()):,} products under {len(idx):,} (year,set,#) keys  (built in {t_idx:.0f}s)", flush=True)
    with psycopg.connect(os.environ["MAZI_DB_URL"], connect_timeout=20) as c:
        cur = c.cursor(); cur.execute("SET statement_timeout='240s';")
        # TABLESAMPLE, not a bare LIMIT: physical order puts an old Pokemon corpus at the head of the
        # table, so `LIMIT n` read as 87% non-sports TCG on 2026-09-23 and reported 0.8% assignable
        # against a fairly-sampled ~27%. SYSTEM samples pages (BERNOULLI would scan all 13.9M rows and
        # hit the statement timeout); 0.3% of pages is ~40K rows before the source/orphan filter.
        cur.execute(f"SELECT id, title FROM external_transactions TABLESAMPLE SYSTEM ({a.pct}) "
                    "WHERE source_code=%s AND title IS NOT NULL AND NOT (raw ? 'slug') LIMIT %s;", (a.source, a.sample))
        rows = cur.fetchall()
    out = collections.Counter(); w = None
    if a.out: f = open(a.out, "w", newline=""); w = csv.writer(f); w.writerow(["transaction_id", "slug", "status", "grade_label"])
    t_m = time.time()
    for tid, title in rows:
        st, slug = assign(title, idx, setre); out[st] += 1
        if w: w.writerow([tid, slug or "", st, grade_label(title) if st == "assigned" else ""])
    n = len(rows); t_m = time.time() - t_m
    print(f"{a.source}: {n:,} orphaned comps sampled; matched in {t_m:.1f}s = {n/max(t_m,1e-9):,.0f} titles/sec")
    for k, v in out.most_common(): print(f"   {v:>6}  {100*v/n:5.1f}%  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
