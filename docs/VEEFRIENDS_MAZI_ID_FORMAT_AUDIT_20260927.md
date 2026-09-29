# MAZI ID format audit, and IDs for cards with no printed number (2026-09-27)

EXTERNAL COMPS lane. Scope: the MAZI global ID rules as written and as actually minted. Then the rule for
cards whose set prints no number, applied to the 2026 VeeFriends Super Stickers.

## 1. The format as written

The format is locked in `mazi-hq/mazidex-data` `migrations/001_catalog_spine.sql` and its README:

| thing | format | example |
|---|---|---|
| Pokémon card | `ptcgio:<setid>-<number>` | `ptcgio:base1-4` |
| other card (variety) | `mazi:<category>:<year>-<set-slug>:<name-slug>:<number>` | `mazi:bk:2023-topps-chrome:...` |
| set | `ptcgio:<setid>` or `mazi:<category>:<year>-<set-slug>` | `mazi:vf:2026-topps-chrome` |

Rules the spine locks:

- IDs are deterministic and permanent. Corrections are redirects (`catalog_redirect` / `beta_redirects`), never
  a delete or a re-mint.
- A parallel is a variant of a variety. Year is an attribute. A serial numerator (23/99) is a per-sale
  observation, never identity.
- External IDs (SCP SKU, tcgdex) are `catalog_xref` attributes, never identity.

What the site accepts (`mazidex-web-prototype` `lib/import-contract.mjs`): the ID must start with `mazi:` or
`ptcgio:`, be at most 300 characters, and contain no control characters.

## 2. What is actually minted: 5 findings

1. **The public ID carries the parallel.** The spine says parallels are never part of the card ID. But the SCP
   loader and the beta both key the public card as `<variety id>~<slug(parallel, 40)>`, for example
   `mazi:bk:1989-hoops:detroit-pistons-champions:353~error`. So in practice:
   - the part before `~` is the permanent variety;
   - the full string is the public variant ID.

   VeeFriends follows the practice (`mazi_variant_id`). **Recommendation:** write `~<parallel>` into the
   contract, so the doc matches what is minted.
2. **SCP cards with no number carry a provider SKU in the number slot.** `load_scp.py` mints `...:sku<scp_id>`,
   which puts an external ID inside identity; that is what the spine forbids. We can't count these from here:
   this lane has no product-DB access.

   **Recommendation:**
   - keep every existing `sku` ID, because IDs are permanent;
   - mint no new ones;
   - use the `nn-` rule below instead.
3. **Curly and straight apostrophes slug differently.** The slug step (NFKD, then ASCII) drops `’` but turns `'`
   into a hyphen, so "Flex’n Fox" gives `flexn-fox` and "Flex'n Fox" gives `flex-n-fox`. The same character
   could mint twice from differently typed source text.
   - VeeFriends normalises to straight apostrophes. This was verified against the existing Topps slugs
     (`flex-n-fox`, `vibe-n-vampire`, `o-g-ox`, `jolly-jack-o`).
   - **Recommendation:** add `’ → '` to the spine's `slug()`.
4. **The category code sits in the sport slot.** VeeFriends uses `vf`, next to `bb`, `fb` and `bk`.
   `beta_scp_slug()` only maps sport codes, which is fine because it only runs on `scp:` sets.
5. **The site's category list is hard-coded.** `beta_discovery()` returns
   `['basketball', ..., 'wrestling', 'pokemon']`, with no `veefriends`. Browsing with the category filter works
   (it's generic), but **the tab list won't show VeeFriends until that array includes it.** This is a one-line
   change in Youssef's `beta_discovery()`.

Checks on the VeeFriends IDs:

- all 1,427 are distinct;
- all are lowercase, start with `mazi:vf:`, and are at most 220 characters;
- all 1,212 IDs minted on 09-26 are reproduced byte-identical by the new code.

## 3. The rule for cards with no printed number

```
mazi:<category>:<year>-<set-slug>:<name-slug>:nn-<section-slug>
```

- `nn` means **no number**. It is set only when the **official checklist prints no numbers**
  (`id_basis = 'checklist_name'`). It is never used for a set whose numbers we simply haven't found:
  2022 zerocool Series 2 stays unminted until its numbered checklist is sourced.
- The section takes the number's place because the same character can appear in several sections of one set.
  Fly Firefly, for example, is a Spectacular sticker, a Mini sticker and an OG Art insert. It does the same
  job an insert code (`MSS-96`) does for numbered sets.
- The name and section are slugged exactly as the spine slugs them. The result is deterministic from the
  checklist: same inputs, same ID, always.
- If a manufacturer ever adds numbers, the card gets a new ID and a redirect. The old ID is never re-minted.
- Parallels stay out of the variety ID and follow as `~<parallel>`, like every other card.

Examples (all live in Neon `veefriends_cards`):

| sticker | MAZI ID |
|---|---|
| Patient Pig (Spectacular) | `mazi:vf:2026-super-stickers-spectacular-series:patient-pig:nn-spectacular-stickers` |
| Fly Firefly (Mini) | `mazi:vf:2026-super-stickers-spectacular-series:fly-firefly:nn-mini-stickers` |
| Fly Firefly (OG Art) | `mazi:vf:2026-super-stickers-spectacular-series:fly-firefly:nn-5-year-og-art-inserts` |
| Amped Aye Aye vs Chill Chinchilla | `...:amped-aye-aye-vs-chill-chinchilla:nn-spectacular-showdowns` |
| Positive Porcupine, Diamond on Diamond /55 | `...:positive-porcupine:nn-spectacular-stickers~diamond-on-diamond` |

## 4. The 2026 Super Stickers catalog (215 IDs)

**Source.** The official checklist: veefriends.com, "Super Stickers: Spectacular Series", then CHECKLIST, which
opens Google Drive `1aZu5iIYTHQEdbY_Tq7xL6XV4EhxgTQTV`. It is 6 image pages, transcribed by hand and saved on
the Mini as `super_stickers_spectacular_2026_official.pdf`.

- It prints **no card numbers**.
- The old placeholder (262 names, the whole VeeFriends cast) was replaced. The product has 100 characters:
  88 Spectacular and 12 Debut.

| section | stickers | parallels on the checklist |
|---|---|---|
| Spectacular Stickers | 88 | Lava, Bubble Gum, Diamond, Hologram, Gold, Emerald (the base set); White Ice (hanger); GaryVee Auto /2; *finish on background* /55 (36 combinations) |
| Debut Stickers | 12 | Red, Orange, Yellow /499, Green /399, Blue /299, Indigo /99, Violet /55, Double Rainbow 1/1, Autograph 1/1 |
| Mini Stickers | 10 | Base, Tie Dye, Gold, Autograph 1/1 |
| Haunted Holograms | 8 | one version |
| Sixth Dimension | 20 | Base, Black (short print) |
| Spectacular Showdowns | 10 | one version |
| Diamond Die-Cuts | 22 | one version (case hit) |
| GaryVee & Gary Bee Dual Auto | 1 | Green /55, Purple /5, Gold 1/1 |
| Comic Inserts | 8 | Base, Gold /99, DJ Coffman Auto /55, GaryVee & DJ Coffman Dual Auto /10. #9 is "To Be Revealed", so it is not minted. |
| The Spectacular Cat | 1 | Base (55 made), GaryVee Auto 1/1 |
| 5 Year OG Art Inserts | 20 | Base, GaryVee Auto /5 |
| Sweepstakes Scratch-Offs | 15 | one version each |

The full list is in `super_stickers_mazi_ids.csv`: 215 rows, with sales per ID.

## 5. How a sticker sale earns a guaranteed price

A sale links only when the title names **one** sticker, **one** section and **one** parallel that exists for
that sticker on the checklist. Everything else is refused with a reason:

- **The /55 parallels.** These must name finish AND background ("Lava on Diamond"), or say "Match" with one
  finish. A /55 with a single colour is refused: it could be any of 6 backgrounds.
- **Serial-only colours.** A Debut or Comic serial infers the colour only where the checklist gives that print
  run to exactly one parallel (/499 = Yellow; comic /99 = Gold).
- **Stickers print no numbers,** so a `#55` on a sticker title is read as a serial. A comic's `#3` must match the
  character on the comic checklist (#3 = Decisive Duck).

**Precision audit.** I read 100 random links by hand; 97 were exact. The 3 misses became rules:

- a two-colour title ("Emerald/Gum") read as one colour;
- a "plus TCG card" two-item sale;
- a "Gem 10" slab recorded as Raw.

After the fixes, a second sample of 40 turned up one more risk ("#55 ... Hologram"), which is now refused.

**Result:** 836 guaranteed sticker sales on 500 variants of 172 stickers.

## 6. Guards the audit added to ALL VeeFriends sets (Topps Chrome too)

Reading the most expensive ready sales found errors that were already in the 09-26 set:

| guard | example it stops | sales removed |
|---|---|---|
| eye variations | "Very Lucky Black Cat #3 **Green Eyes** SSP PSA 10" at $1,575, priced as a *Green Refractor* | 66 |
| grader named, no readable grade | "... PADPARADSCHA 1/1 **PSA**", "... **CGC AUTH**" | 44 |
| unnamed grading company | "**GMA GEM MT 10**", "Graded Pristine 10" | 25 |
| more than one item | "... YELLOW EYES **+ Base Card**", "**plus** TCG card" | ~40 |
| '75 variation | "**1975 VAR** SUPERFRACTOR 1/1" at $3,383 | 2 |
| hyphenated grade | "PSA-10" is now read as PSA 10 (it was recorded Raw) | (fixed) |

**Tests:** `tools/test_veefriends_link.py` passes 92 of 92, including every example above and the ID parity check.

## 7. Where it stands

- **Neon.** `veefriends_guaranteed_sales` has **12,499 exact sold prices on 4,944 card variants (1,088 cards)**:
  - 2026 Topps Chrome: 7,058
  - 2026 Sapphire: 2,331
  - 2025 Topps Chrome: 2,127
  - 2026 Super Stickers: 836
  - 2025 Sapphire: 147

  The daily 07:20 job republishes with the new rules automatically.
- **Beta.** A canary of 20 cards and 36 sales is live and verified through `public_catalog` and `public_sales`.
  The full load (`tools/export_veefriends_beta.py`, insert-only) is ready, and all 6 fixture files pass the
  site's own `validateFixture`. It is waiting on Andy's go.
- **The tab** also needs `'veefriends'` added to `beta_discovery()`'s category array (finding 5).
