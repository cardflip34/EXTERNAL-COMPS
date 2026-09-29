#!/usr/bin/env python3
"""Unit tests for tools/veefriends_link.py against the REAL catalog built from the saved checklists.
Every expected card below was checked against the checklist AND against a real sale title in Neon."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # the modules next to this file
import veefriends_link as V

L = V.Linker(V.build_catalog())
PASS = FAIL = 0
def check(title, card_id=None, why="linked", parallel=None, grade=None):
    global PASS, FAIL
    rec, w = L.link(title)
    got = (rec or {}).get("card_id")
    ok = w == why and got == card_id and (parallel is None or (rec or {}).get("parallel") == parallel) \
         and (grade is None or (rec or {}).get("grade") == grade)
    PASS += ok; FAIL += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {title[:78]}" + ("" if ok else f"\n        got={got} why={w} par={(rec or {}).get('parallel')} grade={(rec or {}).get('grade')}"))

print("-- exact links (checklist-verified numbers)")
check("2025 Topps Chrome VeeFriends Alpha Alligator Yellow Refractor #26", "vf:2025-topps-chrome:base:26", parallel="Yellow Refractor", grade="Raw")
check("2025 Topps Chrome VeeFriends Stunned Sun Manga Speckle #MSS-96", "vf:2025-topps-chrome:manga-speckle:MSS-96")
check("2025 Topps Chrome VeeFriends Courteous Coyote Refractor #68", "vf:2025-topps-chrome:base:68", parallel="Refractor")
check("2026 Topps Chrome VeeFriends Refractor Skilled Skeleton #159 CGC 9 MINT", "vf:2026-topps-chrome:base:159", parallel="Refractor", grade="CGC 9")
check("2026 Topps Chrome VeeFriends Neon Lights Competitive Clown #NE-6 CGC 9 MINT", "vf:2026-topps-chrome:neon-lights:NE-6", grade="CGC 9")
check("2026 Topps Chrome Veefriends Megaheads Superfractor Very Lucky Black Cat 1/1 #M-25 PSA 7 N", "vf:2026-topps-chrome:mega-heads:M-25", parallel="SuperFractor", grade="PSA 7")
check("2025 Topps Chrome VeeFriends Alpha Alligator Refractor #26 pack fresh", "vf:2025-topps-chrome:base:26")
print("-- year-less titles: the number decides, or it stays ambiguous")
check("Topps Chrome VeeFriends Skilled Skeleton #159", "vf:2026-topps-chrome:base:159")     # 2025 base has only 100
check("Topps Chrome VeeFriends Cynical Cat Refractor", None, "ambiguous")                   # base in both years
print("-- character-keyed sets (numbers not verified)")
check("Skilled Skeleton VeeFriends Series 2 Compete & Collect CGC 10 Pre Topps Chrome", "vf:2022-zerocool-series-2:char:skilled-skeleton", grade="CGC 10")
print("-- 2026 GaryVee autographs")
check("2026 Topps Chrome VeeFriends Cynical Cat Gary Vee Auto #1", "vf:2026-topps-chrome:auto-garyvee:1")
print("-- rejections, never guessed")
check("2025 Topps Chrome VeeFriends Alpha Alligator #68", None, "number_character_conflict")
check("VeeFriends Topps Chrome lot of 10 base refractors", None, "not_single")
check("2025 Topps Chrome VeeFriends Hobby Box Factory Sealed", None, "not_single")
check("2026 Topps Chrome VeeFriends Cynical Cat Decisive Duck Refractor", None, "multiple_characters")
check("VeeFriends Series 1 Trading Card Adventurous Astronaut", None, "set_unknown")
check("Pokemon Charizard PSA 10", None, "not_veefriends")
print("-- fixes from the first dry run")
check("2025 Veefriends Topps Chrome - Heart Trooper Game On /10 CASE HIT", None, "numbered_parallel_unnamed")  # insert base is unnumbered per checklists; /10 = unnamed parallel -> refused (guaranteed rule)
check("2025 Topps Chrome Sapphire VeeFriends Empathy Elephant #9 PSA 10", "vf:2025-topps-chrome-sapphire:base:9", grade="PSA 10")  # Sapphire catalogued 09-27
check("2026 Topps Chrome VeeFriends Y2K Balanced Beetle Case Hit PSA 8", None, "product_not_in_catalog")
check("2026 Topps Chrome VeeFriends Gritty Ghost Variation #16", None, "product_not_in_catalog")
check("2025 Topps Chrome Veefriends Manga Speckle Set Empathy Elephant #9 PSA 10 GEM", "vf:2025-topps-chrome:manga-speckle:MSS-9", grade="PSA 10")
check("2026 Topps Chrome Veefriends Megaheads Very Lucky Black Cat #M-25", "vf:2026-topps-chrome:mega-heads:M-25")
print("-- sticker series: only 'Spectacular' is provably the 2026 set")
check("2024 Veefriends Super Stickers Happy Hermit Crab Black Ice /55 CGC 10", None, "set_unknown")
check("2026 VeeFriends Spectacular Series Super Sticker Skilled Skeleton Lava", "vf:2026-super-stickers:spectacular-stickers:skilled-skeleton", parallel="Lava")
print("-- guaranteed-sales rules (2026-09-27)")
check("Topps Chrome VeeFriends 2026 Gratitude Gorilla #89 VF First Chrome 1/1", None, "numbered_parallel_unnamed")
check("Topps 2026 Topps Chrome VeeFriends Hidden Gems Resilient Red Devil /5 HG-2", None, "numbered_parallel_unnamed")  # HG parallels unnamed in the checklist
check("2026 Topps VeeFriends Chrome Very,Very,Very,Very,Lucky Black Cat Stained Glass", None, "product_not_in_catalog")
check("2026 Topps Chrome VeeFriends Resilient Red Devil Erupt E-16", "vf:2026-topps-chrome:erupt:E-16")
check("2026 Topps Chrome VeeFriends Mystery Card XY-3", None, "no_catalog_match")
check("2025 Topps Chrome VeeFriends Mature Mule Orange /25", "vf:2025-topps-chrome:base:92", parallel="Orange Refractor")
check("2025 Topps Chrome VeeFriends Alpha Alligator #26 PSA-10", "vf:2025-topps-chrome:base:26", grade="PSA 10")   # was recorded Raw before 09-27
print("-- Sapphire (2025 + 2026), catalogued 2026-09-27")
check("2025 Topps Chrome Sapphire VeeFriends Cynical Cat #44", "vf:2025-topps-chrome-sapphire:base:44", parallel="Base")
check("2026 Topps Chrome Sapphire VeeFriends Skilled Skeleton #159 Gold /50", "vf:2026-topps-chrome-sapphire:base:159", parallel="Gold Sapphire")
check("2026 Topps Chrome Sapphire VeeFriends Skilled Skeleton #159 /25", "vf:2026-topps-chrome-sapphire:base:159", parallel="Orange Sapphire")
check("2026 Topps Chrome Sapphire VeeFriends Skilled Skeleton #159 1/1", "vf:2026-topps-chrome-sapphire:base:159", parallel="Padparadscha Sapphire")
check("2026 Topps Chrome Sapphire VeeFriends Infinite Sapphire Rare Robot IS-1", "vf:2026-topps-chrome-sapphire:infinite-sapphire:IS-1")
check("Topps Chrome Sapphire VeeFriends Skilled Skeleton #159", "vf:2026-topps-chrome-sapphire:base:159")   # 2025 Sapphire has only 100
check("2026 Topps Chrome VeeFriends Skilled Skeleton #159 /25", None, "numbered_parallel_unnamed")         # regular Chrome: runs not unique
check("Kevin Hart Entrepreneur Elf's Favorite 2025 Topps Chrome VeeFriends #EE-KH", None, "product_not_in_catalog")
check("2025 Topps Chrome VeeFriends Entrepreneur Elf #16", "vf:2025-topps-chrome:base:16")   # the character, not the insert
S = "vf:2026-super-stickers:"
print("-- 2026 Super Stickers Spectacular Series (official checklist; the set prints no card numbers)")
check("2026 VeeFriends Super Stickers Spectacular Series Empathy Elephant Emerald", S + "spectacular-stickers:empathy-elephant", parallel="Emerald")
check("VeeFriends Spectacular Stickers Big Game Bandicoot 31/55 Diamond On Emerald", S + "spectacular-stickers:big-game-bandicoot", parallel="Diamond on Emerald")
check("VeeFriends Super Stickers Spectacular Positive Porcupine 46/55 Diamond Match!", S + "spectacular-stickers:positive-porcupine", parallel="Diamond on Diamond")
check("Veefriends Super Stickers Spectacular Series Adaptable Alien Gary Vee Auto 2/2", S + "spectacular-stickers:adaptable-alien", parallel="GaryVee Autograph")
check("Veefriends Super Stickers Spectacular Series VVVV Lucky Black Cat WHITE ICE", S + "spectacular-stickers:very-very-very-very-lucky-black-cat", parallel="White Ice")
check("2026 VeeFriends Spectacular Super Stickers GaryVee Lava", S + "spectacular-stickers:garyvee", parallel="Lava")
check("VeeFriends Super Stickers Spectacular Series Flex'N Fox 40/55 Gold On Bubblegum!", S + "spectacular-stickers:flexn-fox", parallel="Gold on Bubble Gum")
check("2026 VeeFriends Spectacular Super Stickers Conviction Cockroach Debut Violet /55", S + "debut-stickers:conviction-cockroach", parallel="Violet")
check("VeeFriends Spectacular Stickers Debut Conviction Cockroach 323/499", S + "debut-stickers:conviction-cockroach", parallel="Yellow")   # /499 is Yellow only
check("GARY BEE - Veefriends Spectacular Super Stickers 56/99 Comic Insert", S + "comic-inserts:gary-bee", parallel="Gold")                    # comic /99 is Gold only
check("Veefriends Super Sticker Spectacular Decisive Duck Comic #3 DJ Coffman Auto #/55", S + "comic-inserts:decisive-duck", parallel="DJ Coffman Autograph")
check("2026 VeeFriends Super Stickers Spectacular - Haunted Holograms Zealous Zombie", S + "haunted-holograms:zealous-zombie", parallel="Base")
check("CGC 10 Smooth Spider 6th Dimension Veefriends Spectacular Super Sticker", S + "sixth-dimension:smooth-spider", parallel="Base", grade="CGC 10")
check("BAD INTENTIONS VeeFriends MINI Super Stickers TIE DYE Spectacular Series SP Rare", S + "mini-stickers:bad-intentions", parallel="Tie Dye")
check("VeeFriends Super Stickers Spectacular Showdowns Ambitious Angel Resilient Devil", S + "spectacular-showdowns:resilient-red-devil-vs-ambitious-angel", parallel="Base")
check("Veefriends Spectacular Super Stickers - HYPE HORSE CASE HIT DIAMOND DIE CUTS", S + "diamond-die-cuts:hype-horse", parallel="Base")
check("VeeFriends Spectacular Series Super Sticker 5 Year OG Art Chill Chinchilla", S + "5-year-og-art-inserts:chill-chinchilla", parallel="Base")
print("-- stickers: refused, never guessed")
check("VeeFriends Super Stickers Spectacular Insightful Irish Terrier #34/55", None, "numbered_parallel_unnamed")   # a /55 names finish AND background
check("2026 VeeFriends Super Stickers Spectacular Hustling Hamster Gold Lava #2/55", None, "numbered_parallel_unnamed")  # which one is the background?
check("Vee Friends Super Stickers Spectacular Fly Firefly Emerald/Gum", None, "ambiguous_parallel")                   # precision audit error #48
check("2026 VeeFriends Spectacular Series Super Sticker Skilled Skeleton", None, "finish_unnamed")                    # the base set has no plain version
check("VeeFriends Spectacular Series Super Sticker - Gary Bee Debut 14/99 plus TCG card", None, "not_single")         # precision audit error #77
check("Gem 10 Diamond - VeeFriends Spectacular Series Super Stickers Boisterous Beaver", None, "grade_unknown")       # precision audit error #20
check("Veefriends Super Stickers Spectacular Series Kind Warrior Comic Insert", None, "sticker_character_not_in_section")  # comic #9 is 'To Be Revealed'
check("2026 VeeFriends Spectacular Super Stickers Decisive Duck Comic Inserts #5", None, "number_character_conflict")       # #5 is Motivated Monster
check("VeeFriends Spectacular Series Super Sticker - Shrewd Shark & Likable Leopard /55", None, "multiple_characters")
check("2026 VeeFriends Spectacular Series Super Sticker Patient Pig Blue Diamond", None, "unknown_parallel_word")      # no Blue in the base set
check("HAPPY HERMIT CRAB 2025 VeeFriends Manga Super Stickers SP Card LIMITED/399 CHASE", None, "set_unknown")
check("2026 VeeFriends Super Stickers Spectacular #55 Patient Pig Insert Hologram!!!", None, "numbered_parallel_unnamed")  # no card numbers: '#55' is a serial         # Manga series: no checklist yet
print("-- guards added 2026-09-27 (every set)")
check("2025 VEEFRIENDS TOPPS REFRACTOR #3 VERY LUCKY BLACK CAT YELLOW EYES + Base Card", None, "not_single")
check("Topps Chrome 2025 VeeFriends Very Very Very Very Lucky Black Cat w/Yellow Eyes", None, "product_not_in_catalog")
check("3957 Very, Lucky Black Cat 2025 Topps Chrome VeeFriends #3 Green Eyes SSP PSA 10", None, "product_not_in_catalog")  # not a Green Refractor
check("2025 Topps Chrome VeeFriends Red Eyes SSP VVVV LUCKY BLACK CAT PSA 10 #3", None, "product_not_in_catalog")
check("Topps 2025 Chrome VeeFriends Cynical Cat #44 First Chrome GMA GEM MT 10", None, "grade_unknown")
check("DIAMOND HANDS HEN PSA 2025 TOPPS CHROME VEEFRIENDS SAPPHIRE #47 PADPARADSCHA 1/1", None, "grade_unknown")   # slabbed, grade unreadable
check("2025 Topps Chrome VeeFriends Yellow Refractor Decisive Duck #14 CGC AUTH", None, "grade_unknown")
check("DIAMOND HANDS HEN PSA 10 2025 TOPPS CHROME VEEFRIENDS 1975 VAR SUPERFRACTOR 1/1", None, "product_not_in_catalog")
check("2025 Topps Chrome VeeFriends Refractor Bullish Bull #46 CGC 8.5 NM-MT+", "vf:2025-topps-chrome:base:46", parallel="Refractor", grade="CGC 8.5")  # MINT+ is a grade
print("-- MAZIDEX review 2026-09-27: checklist parallels only, every serial checked")
T6 = "vf:2026-topps-chrome:"
check("2026 TOPPS CHROME ERUPT EMPATHY ELEPHANT VEEFRIENDS GREEN", None, "parallel_not_on_checklist")            # Erupt: Black Lava /10, SuperFractor only
check("2026 Topps Chrome VeeFriends ERUPT! Jolly Jack-O Red Refractor SSP Case Hit", None, "parallel_not_on_checklist")
check("2026 Topps Chrome VeeFriends Erupt! Motivated Monster Red Raywave Refractor SSP", None, "parallel_not_on_checklist")
check("2026 Topps Chrome VeeFriends Erupt! Adventurous Astronaut Black Lava /10", T6 + "erupt:E-7", parallel="Black Lava Refractor")
check("2026 Topps Chrome VeeFriends Iconics SSP #I-5 Very, Very, Very, Lucky Black Cat", T6 + "iconics:I-5", parallel="Base")   # Iconics: no parallels
check("2026 Topps Chrome VeeFriends Very Very Lucky Black Cat Mega Heads M-25 Refractor", T6 + "mega-heads:M-25", parallel="Base")
check("BAD INTENTIONS 2026 TOPPS CHROME VEEFRIENDS 1ST BLACK CAT REFRACTOR /7 #26 Q6608", T6 + "base:26", parallel="Black CatFractor")
check("2026 Topps Chrome Veefriends #134 Perfect Persian Cat Black Cat Refractor 1/7", T6 + "base:134", parallel="Black CatFractor")
check("2026 Topps Chrome VeeFriends Gritty Ghost Black Cat Refractor 2/7!!!", T6 + "base:16", parallel="Black CatFractor")
check("2025 Topps Chrome Veefriends VVVV Very Luck Black Cat Refractor #3 CGC 8", "vf:2025-topps-chrome:base:3", parallel="Refractor", grade="CGC 8")
check("Topps Chrome VeeFriends 2026 Knowing Gnome Purple Variant #109 VF First Chrome", None, "product_not_in_catalog")
check("2026 Topps Chrome VeeFriends Last Glass Standing SSP ROSE VARIANT #200", None, "product_not_in_catalog")
check("LAST GLASS STANDING 2026 TOPPS CHROME VEEFRIENDS ROS\u00c9 SSP #200", None, "product_not_in_catalog")
check("2026 Topps Chrome VeeFriends Knowing Gnome Purple #109", None, "variation_possible")       # could be the Purple Variant SSP
check("2026 Topps Chrome VeeFriends Knowing Gnome Purple Refractor 12/75 #109", T6 + "base:109", parallel="Purple Refractor")
check("2026 Topps Chrome VeeFriends Very, Lucky Black Cat Sketch Selections Gold #/50", T6 + "original-sketch:OSS-4", parallel="Gold Refractor")  # was base #55
check("Headstrong Honey Badger 2025 Topps Chrome VeeFriends Green Sketch Screen 16/99", "vf:2025-topps-chrome:sketch-to-screen:SK-8", parallel="VF Green Refractor")  # was base #45
check("2026 Topps Chrome VeeFriends SUPERFRACTOR 1/1 Noble Numbat TF-NN", None, "product_not_in_catalog")         # 1986 Topps insert
check("2026 Topps Chrome Sapphire VeeFriends Content Condors CC-3", None, "product_not_in_catalog")
check("5555 Fan 2025 Topps Chrome VeeFriends #1 Wave Refractor /150", "vf:2025-topps-chrome:base:1", parallel="Wave Refractor")
check("2025 Topps Chrome VeeFriends Alpha Alligator #26 Refractor 204/250", "vf:2025-topps-chrome:base:26", parallel="Shimmer Refractor")  # 2025 base: /250 is Shimmer only
check("2026 Topps Chrome VeeFriends Skilled Skeleton #159 Refractor 204/250", T6 + "base:159", parallel="Pink Refractor")  # 2026 /250 = Pink only
check("2026 Topps Chrome VeeFriends Skilled Skeleton #159 White Refractor /30", None, "parallel_not_on_checklist")        # no White; /30 is Pearl
check("2026 Topps Chrome VeeFriends Skilled Skeleton #159 Gold Refractor 12/25", None, "print_run_conflict")
check("2025 VeeFriends Topps Chrome 20 Card Base Set #1 5555 Fan", None, "not_single")
check("VeeFriends Topps Chrome 2025 20 Base Set #71 Methodical Mammoth", None, "not_single")
check("(23) 2026 Topps Chrome VeeFriends Mega Head Collection Very Lucky Black Cat Set", None, "not_single")
check("2026 Topps Chrome VeeFriends #88 Grateful Gar X-Fractor(2)And Base", None, "not_single")
check("Bullish Bull Shimmer Refractor 021/250 Card #46 2025 Topps Chrome VeeFriends", "vf:2025-topps-chrome:base:46", parallel="Shimmer Refractor")  # '250 Card' is not a lot
check("2026 Topps Chrome Sapphire VeeFriends Content Condor #57", "vf:2026-topps-chrome-sapphire:base:57")                  # the base character stays
print("-- errors found in the fresh random 200 (2026-09-27)")
check('Jimmy "MrBeast" Donaldson 2026 Topps Chrome VeeFriends Content Condor', None, "product_not_in_catalog")   # Content Condors insert
check("GaryVee & Gary Bee 2026 Topps Industry Summit Exclusive Veefriends ICGV SSP", "vf:2026-topps-industry-conference:promo:ICGV", parallel="Base")
check("2026 Topps Chrome VeeFriends Caring Camel Purple Speckle Refractor 27/75", None, "ambiguous_parallel")      # Purple or Purple Mini-Diamond
check("2026 Topps Chrome VeeFriends Sapphire Content Condor White /30 1st Chrome", "vf:2026-topps-chrome-sapphire:base:57", parallel="White Sapphire")
print("-- $500+ census (2026-09-27): Hidden Gems parallels are Emerald / Onyx /10 / Ruby /5 / Padparadscha 1/1")
HG = "vf:2026-topps-chrome-sapphire:hidden-gems:"
check("GRATITUDE GORILLA 2026 TOPPS CHROME VEEFRIENDS HIDDEN GEMS RED 5/5 Q7255", HG + "HG-4", parallel="Ruby")
check("Very Lucky Black Cat Hidden Gem Emerald 2026 Topps Chrome Sapphire VeeFriends", HG + "HG-5", parallel="Base")   # Emerald IS the base
check("2026 Topps Chrome Sapphire VeeFriends Gratitude Gorilla Hidden Gem HG-4", HG + "HG-4", parallel="Base")
check("GRATITUDE GORILLA 2026 TOPPS CHROME VEEFRIENDS HIDDEN GEMS GOLD /50", None, "parallel_not_on_checklist")
check("2026 Topps Chrome VeeFriends Rare Robot Infinite Sapphire Gold /50", None, "parallel_not_on_checklist")
print("-- MAZIDEX scan audit 2026-09-29")
check("Topps 2026 Chrome VeeFriends Authentic Comic Cut CC-8 Gary Bee Insert**1 of 1**", T6 + "comic-clippings:CC-8", parallel="Base")
check("2026 Topps Chrome VeeFriends #10 - Rare Robot Comic Clippings CGC 9.5 MINT+", T6 + "comic-clippings:CC-10", grade="CGC 9.5")
check("2026 Topps Chrome Sapphire VeeFriends Content Condors CC-MB MrBeast", None, "product_not_in_catalog")   # Condors are CC-<letters>
check("2025 Topps Chrome VeeFriends Legendary Lemur Game On Blue Refractor GO-7 #37/99", "vf:2025-topps-chrome:game-on:GO-7", parallel="Base")  # base IS Blue /99
check("2025 TOPPS CHROME VEEFRIENDS GAME ON! BLUE #GO7 LEGENDARY LEMUR 52/99 PSA 8", "vf:2025-topps-chrome:game-on:GO-7", parallel="Base", grade="PSA 8")
check("VeeFriends Super Stickers Series Gold on Lava Logical Lion /55 Card", "vf:2026-super-stickers:spectacular-stickers:logical-lion", parallel="Gold on Lava")
check("2025 Veefriends Super Stickers Manga Gold on Lava Logical Lion /55", None, "sticker_other_product")          # the X-on-Y rule never admits Manga
check("2026 Topps Chrome Sapphire VeeFriends Skilled Skeleton #159 Superfractor 1/1", None, "parallel_not_on_checklist")
check("2025 Topps Chrome Sapphire VeeFriends Juicy Jaguar #42 Gold", None, "parallel_not_on_checklist")   # 2025 Sapphire: Orange/Red/Padparadscha
check("2026 Topps Industry Conference Garyvee & Gary Bee Veefriends #ICGV", "vf:2026-topps-industry-conference:promo:ICGV", parallel="Base")
import veefriends_variants as VV, export_veefriends_beta as XB
MAT = VV.matrix(); MIDS = [m["variant_id"] for m in MAT]
ok = len(MIDS) == len(set(MIDS)); PASS += ok; FAIL += (not ok); print(f"  {'PASS' if ok else 'FAIL'}  checklist matrix {len(MIDS):,} versions, all ids distinct")
want = {"mazi:vf:2026-topps-chrome:gary-bee:8~black-catfractor": 7, "mazi:vf:2025-topps-chrome:alpha-alligator:26~wave-refractor": 150,
        "mazi:vf:2025-topps-chrome:legendary-lemur:go-7": 99, "mazi:vf:2026-topps-chrome-sapphire:very-very-very-very-lucky-black-cat:hg-5": None}
got = {m["variant_id"]: m["print_run"] for m in MAT if m["variant_id"] in want}
ok = got == want; PASS += ok; FAIL += (not ok); print(f"  {'PASS' if ok else 'FAIL'}  matrix has the scan-audit versions with checklist print runs  {'' if ok else got}")
ok = (XB.number_key("OSS-2"), XB.number_key("E-5"), XB.number_key("159"), XB.number_key(None)) == ("oss2", "e5", "159", None)
PASS += ok; FAIL += (not ok); print(f"  {'PASS' if ok else 'FAIL'}  number_key strips to letters+digits (oss2, e5)")
print("-- MAZI IDs: deterministic; numbered IDs unchanged; stickers carry nn-<section>")
CARDS = {c["card_id"]: c for c in V.build_catalog()["cards"]}
def idcheck(vf_id, expect):
    global PASS, FAIL
    got = V.mazi_card_id(CARDS[vf_id]) if vf_id in CARDS else "MISSING"
    ok = got == expect; PASS += ok; FAIL += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {vf_id} -> {got}" + ("" if ok else f"   (want {expect})"))
X = "mazi:vf:2026-super-stickers-spectacular-series:"
idcheck("vf:2025-topps-chrome:base:26", "mazi:vf:2025-topps-chrome:alpha-alligator:26")
idcheck("vf:2025-topps-chrome:manga-speckle:MSS-96", "mazi:vf:2025-topps-chrome:stunned-sun:mss-96")
idcheck(S + "spectacular-stickers:patient-pig", X + "patient-pig:nn-spectacular-stickers")
idcheck(S + "spectacular-stickers:flexn-fox", X + "flex-n-fox:nn-spectacular-stickers")          # same slug as the Topps Flex'n Fox
idcheck(S + "spectacular-stickers:o-g-ox", X + "o-g-ox:nn-spectacular-stickers")
idcheck(S + "mini-stickers:fly-firefly", X + "fly-firefly:nn-mini-stickers")                       # same character, other section
idcheck(S + "5-year-og-art-inserts:fly-firefly", X + "fly-firefly:nn-5-year-og-art-inserts")
idcheck(S + "spectacular-showdowns:amped-aye-aye-vs-chill-chinchilla", X + "amped-aye-aye-vs-chill-chinchilla:nn-spectacular-showdowns")
idcheck(S + "sweepstakes-scratch-offs:magnanimous-maltese-bubble-gum", X + "magnanimous-maltese-bubble-gum:nn-sweepstakes-scratch-offs")
idcheck(S + "the-spectacular-cat:the-spectacular-cat", X + "the-spectacular-cat:nn-the-spectacular-cat")
idcheck("vf:2022-zerocool-series-2:char:skilled-skeleton", None)
idcheck("vf:2026-topps-industry-conference:promo:ICGV", "mazi:vf:2026-topps-industry-conference:garyvee-gary-bee:icgv")                                   # no checklist numbers: never minted
ALL = [V.mazi_card_id(c) for c in CARDS.values() if V.mazi_card_id(c)]
ok = len(ALL) == len(set(ALL)); PASS += ok; FAIL += (not ok)
print(f"  {'PASS' if ok else 'FAIL'}  {len(ALL)} minted ids, {len(set(ALL))} distinct")
ok = all(len(i) <= 220 and i.startswith("mazi:vf:") and not any(ch.isupper() for ch in i) for i in ALL); PASS += ok; FAIL += (not ok)
print(f"  {'PASS' if ok else 'FAIL'}  every id is lowercase mazi:vf:..., <= 220 chars (beta limit 300)")
print(f"\nRESULT: {PASS} passed, {FAIL} failed"); sys.exit(1 if FAIL else 0)
