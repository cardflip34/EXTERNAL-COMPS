# VeeFriends → MAZI ID plan (2026-09-27)

> **Update 2026-09-27:** the Super Stickers are no longer held. The official checklist prints **no numbers**, so they are minted with the `nn-<section>` rule. See `MAZI_ID_FORMAT_AUDIT.md`: 215 IDs, 836 guaranteed sales. Series 2 stays held until its numbered checklist is sourced.


**Goal:** VeeFriends cards join the **same** MAZI global ID spine as sports and Pokémon, so the VeeFriends tab works like
the Pokémon tab.

## Status: done today (Andy: GO)
- **20,251 eBay VeeFriends sales** (Jul–Sep 2026, signed-in pull) imported to Neon `external_transactions` (run 53218).
  They are deduped the same way as existing eBay rows. A 200-row parity check against the proven insert path showed
  0 mismatches.
- New Neon tables, read-only for `mazidex_ro` (verified from that login; writes refused):
  - `public.veefriends_cards`: 1,426 cards, with an empty `mazi_card_id` column waiting for this plan.
  - `public.veefriends_sale_links`: 17,917 sales, each linked to exactly one card, with parallel, grade and price_trust.
- Rows with `price_trust = best_offer_context_only` must never be averaged or charted.

## The rules we keep (mazidex-data `001_catalog_spine.sql`)
- IDs are **permanent** and **deterministic**. Merges are redirects, never deletes.
- A public card is **variety × parallel**. Parallels are `catalog_parallel` rows, **never** part of the card id.
  A serial numerator (27/99) is a per-sale observation.
- Year is an attribute, taken from the set name.
- External ids are `catalog_xref` attributes, never identity.
- The catalog never rejects a sale: unmatched sales go to a review lane.

## Proposed ID rule (the same `slug()` as `loaders/load_scp.py`)
| thing | format | example |
|---|---|---|
| category code | `vf` in the sport slot, next to `bb` / `fb` / `bk` | |
| set | `mazi:vf:<year>-<slug(set line)>` | `mazi:vf:2026-topps-chrome` |
| card | `mazi:vf:<year>-<slug(set line)>:<slug(character)>:<number>` | `mazi:vf:2026-topps-chrome:skilled-skeleton:159` |
| insert card | keeps its code as the number | `mazi:vf:2025-topps-chrome:stunned-sun:mss-96` |
| autograph variant | `~` suffix, like SCP bracket variants | `mazi:vf:2026-topps-chrome:gratitude-gorilla:89~garyvee-auto` |
| parallel | `catalog_parallel(card_id, slug(parallel), print_run)` | `yellow-refractor`, `pink-refractor` /250 |

The full preview is `veefriends_mazi_id_preview.csv`. **902 cards are ready, with 0 ID collisions.**

## What mints now, and what waits
| set | cards | linked sales | usable prices |
|---|---|---|---|
| 2026 Topps Chrome | 650 (483 with sales) | 10,275 | 7,445 |
| 2025 Topps Chrome | 252 (232 with sales) | 3,455 | 2,481 |
| 2022 zerocool Series 2 (**held**) | 262 characters | 1,862 | 1,168 |
| 2026 Super Stickers Spectacular (**held**) | 262 characters | 2,325 | 1,384 |

**Held sets:** they have no verified card numbers yet. Series 2 is skip-numbered to #268 and the Stickers set has 100
cards. IDs are permanent, so we will not mint character-only IDs. We mint once the numbered checklists are sourced.

**4,152 more sales** are waiting on products that aren't in the catalog yet: 2026 variations, Sapphire, Y2K, Entrepreneur
Elf, and the other sticker series. They join the catalog through the same rule, from published checklists.

## Who does what, in order
1. **Youssef confirms the format:**
   - the `vf` code;
   - the set and card format above;
   - the `~garyvee-auto` variant;
   - the EMR question in step 7.
2. **EXTERNAL COMPS (Claude):**
   - fill `veefriends_cards.mazi_card_id` in Neon with the deterministic IDs;
   - add the parallel slug to the links, so every VeeFriends sale carries its MAZI ID;
   - write `loaders/load_veefriends.py` for mazidex-data (tests pin byte-identical IDs to the preview).
3. **Andy:** OK the mazidex-data PR push.
4. **Youssef runs the loader** against the product DB (he holds its credentials). It fills
   `catalog_set` / `catalog_card` (provenance `imported`) / `catalog_parallel` / `catalog_xref` (our `vf:` id and the
   checklist URL) / `catalog_alias` (e.g. "Very Lucky Black Cat"). It is idempotent (`ON CONFLICT DO NOTHING`).
5. **Youssef wires the tab:**
   - cards come from `catalog_card WHERE set_id LIKE 'mazi:vf:%'`;
   - sales come through the pipe from Neon (`veefriends_sale_links ⨝ external_transactions`, joined on `mazi_card_id`);
   - a page activates when data attaches.
6. **Claude:** numbered checklists for Series 2 and Stickers, then mint; extend the catalog for the 4,152; publish
   daily after the eBay and Fanatics refreshes.
7. **DECIDED (Andy, 2026-09-27): NO market ranges for VeeFriends. Show guaranteed sales only:** one card, one exact
   sold price, like every other MAZI sale.
   - The list to show is ONE Neon view, `public.veefriends_guaranteed_sales` (`mazidex_ro` can read it). A sale is in it
     only if:
     - it has a real sold price (no eBay Best Offer asking prices, no Goldin premium-basis rows);
     - it is on a checklist-verified card;
     - it passes a price fence: a sale more than 10× above or below its card × parallel × grade median (when there
       are at least 5 sales), or a thin graded sale more than 500× the raw median, is withheld as likely mislabeled.
   - The linker also refuses serial-numbered cards whose parallel it cannot name (Topps Chrome base is unnumbered),
     so a 1/1's price never lands on the base card.
   - Today: **9,251 guaranteed sales on 645 cards** (165 withheld by the fence; 873 unnamed numbered parallels refused).
   - Youssef: show these rows, and don't compute an EMR for `mazi:vf:%` cards.
