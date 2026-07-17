# MockBuilder — Session Handoff / Working State

> **Purpose of this file:** reload full project direction into a fresh session
> after a context reset. Read this first, then `PLAN_v2.md`. Do not re-derive the
> diagnosis — it's settled below.

---

## What this project is

Given any web-app URL, generate a deterministic, backend-free, **agent-testable
static UI mockup** whose quality matches a hand-built one. It's a take-home for
Omnisavant.ai (they build voice agents and test them against static mockups of
client apps). Deploy target: Vercel.

The full spec and property table (P1–P8) live in `PLAN.md`. The corrected
architecture lives in `PLAN_v2.md`. **This file is the current working state.**

---

## The settled diagnosis (do not re-litigate)

The pipeline (crawler → reasoning → generator → verifier) works but produces
recognizable-but-flawed output. Root cause, confirmed from on-disk evidence:

1. **The LLM is instructed to invent seed DATA** ("generate 4–8 rows"). The
   crawler had already captured ~30 real HN stories; the model used 3 and
   fabricated `Story 4…8 / example.com`. That filler is the visible defect.
2. **Structure and data are fused into one LLM call.** Judgment (what kind of
   app, what's the repeating unit) belongs in the LLM; data transcription (the
   actual rows) does not. Fusing them forces fabrication or token-limit blowup.
3. Design tokens, evidence capture, and the generator are **fine** — not the
   problem. Do not rebuild from scratch.

## The fix (the direction — do not drift from this)

**Extract real records deterministically in the crawler → LLM decides only
structure → generator fills seed data from real records → add stateful stores so
Instamart-class harnesses are expressible.**

Proven by a working PoC (`repeating_extractor_poc.py`): one detector, zero
app-specific selectors, correctly extracts all 5 HN stories AND all 6 shop
products (rank/title/domain/score/age/comments; and image/name/price/unit).
The PoC IS the blueprint for Phase 1′.

---

## Build order (phases from PLAN_v2)

1. **Phase 1′ — `crawler/records.py`** ✅ DONE: repeating-unit extraction → emit
   `records.json` per crawl state. (De-risked by the PoC.)
2. **Phase 2′ — reasoning** ✅ DONE: STRUCTURE-ONLY. seed removed from schema
   (rejected, not just discouraged); prompt asks for shape not data; payload is
   text-only (no screenshot — the model is text-only), one representative record
   per collection + tokens. LLM selects primary collection (sourceCollection).
3. **Phase 3′ — generator** (IN PROGRESS): seed injection from records.json via
   sourceCollection/sourceRole ✅; images ✅; accent ✅; stateful capture +
   provenance ✅ (10-pre). REMAINING: cart/collection store + header badge
   (10a/10b), detail-screen template + reachable edge variants.
4. **Re-run HN** (confirm zero filler) → then **grocery demo** (confirm the same
   pipeline builds a stateful cart harness with no per-app code).
5. **Verifier checks** (P-data: every seed row traces to a real record; P-state:
   mutateState causes an asserted DOM change) + **Vercel deploy** + **README**.

---

## Working rules for this collaboration

- **One step at a time.** Never batch phases.
- Each step is scoped and self-contained; **don't touch unrelated files**.
- After each step: run the stated **verification**; the human confirms manually.
- On confirmation: commit (human commits manually with a provided message).
- **If a step fails, fix before advancing.** No moving on with a red gate.
- Prefer validating new modules in **isolation first** (as the PoC did) before
  wiring them into the live pipeline.

---

## CURRENT POSITION — HISTORICAL, SUPERSEDED

> This section records the position as of Step 5c and its "NEXT: Step 6" is long
> done. **For where the project actually stands, read "CURRENT POSITION (update)"
> near the end of this file.** Kept because the per-step notes below are still
> the settled record of WHY each step is shaped the way it is.

Phase 1′ (crawler extraction) and Phase 2′ (structure-only reasoning) are
COMPLETE and committed.

Completed and committed:
- Step 1  — crawler/records.py: app-agnostic repeating-unit extractor + role
            inference. 32 tests. Frozen (see freeze note below).
- Step 2  — async seam on records.py (extract_records / extract_records_async
            over one pure build_result_from_raw); wired into the async crawl
            loop, emits evidence/<hash>_records.json per state.
- Step 3  — extractor returns ranked collections[] (not a single winner); no
            `primary` flag (primacy is the LLM's call); nesting + absorption
            dedup.
- Step 4  — reasoning is STRUCTURE-ONLY. seed removed from the entity schema
            entirely; additionalProperties:false now REJECTS any model-emitted
            seed (proven by a negative-guard test). Prompt asks for entity
            shape/screens/flows, never data. Payload dropped the dead vision
            path + elements dump: ~21K → ~3.2K tokens, text-only model.
- Step 4b — cache key is now content-addressed over (system prompt + user
            payload + model name): {state_hash}_{inputs_hash}_model.json. A
            prompt change now MISSES stale entries (was silently serving stale
            reasoning). Legacy state-hash-only files are inert.
- Step 5  — LLM selects the primary collection by semantic judgment. Payload
            forwards `count` per collection; prompt frames rank as an
            overridable prior. Chosen index recorded as entity.sourceCollection
            (integer, required on entities; added to schema). PROVEN by the
            override test: model picks a lower-ranked, richer collection over a
            high-count nav strip — primacy genuinely moved to the LLM.
- Step 5c — entity shape sampled from the MOST-COMPLETE record (most distinct
            non-empty roles, tie-break lowest index), not records[0]. Fixes the
            order-dependence where a short first-row title corrupted the entity
            shape. One real record, never a union.

Test count at Step 5c: 70 passing. (Now 121 — see CURRENT POSITION (update).)

THE DEFECT IS DEAD: seed data is now structurally impossible for the model to
emit (schema rejects it), and primacy is a semantic LLM decision, not the
extractor's scoring arithmetic.

Step 6 — the injection zip — is DONE. Its open fork (entity fields carry
model-chosen camelCase NAMES, records are keyed by ROLE, and nothing recorded the
mapping) was RESOLVED by adding `sourceRole` per field rather than recovering it
heuristically. See "LINKAGE CONTRACT" below for the settled result.

## Key files

- `PLAN.md` — original spec + property table (P1–P8)
- `PLAN_v2.md` — corrected architecture (the plan we follow)
- `repeating_extractor_poc.py` — working proof of the core extractor
- `mockbuilder/crawler/records.py` — the extractor (FROZEN, see below)
- `mockbuilder/crawler/crawler.py` — BFS + clickability filter + affordance
  synthesis (`synthesize_cart_path`) + provenance writing
- `mockbuilder/crawler/dom.py` — `normalize_dom` (the state hash) +
  `discover_elements` (the affordance capture 10a will consume)
- `mockbuilder/reasoning/prompts.py`, `reason.py` — Phase 2′ targets; also 10a's
  target (the affordance channel lands here)
- `mockbuilder/generator/generate.py` + `templates/` — Phase 3′ targets
- `app_model.schema.json` — the contract; gets store/state additions in Phase 3′
- records.py is FROZEN: _DETECTOR_JS, infer_role, grouping, adjacent-sibling
  merge, dataclass shapes, to_dict(). Changes only via a dedicated,
  explicitly-scoped step. The genericity guard (source contains no
  athing/titleline/sitestr/card/subtext) and build_result_from_raw purity anchor
  enforce this.

## Known quantities (observed, not bugs)
- Records carry a duplicated `domain` field on HN (domain link renders twice
  in the DOM). Harmless. Normalize at field level in Phase 3′, not in the
  extractor's detection logic.
- The structural `signature` is per-page, not a stable app-level identity.
  Two states of the same app legitimately produce different signatures.
  Nothing downstream may key off signature equality to identify an entity.
- Record API is dataclasses (attribute access: `f.role`, `rec.fields`).
  `records.json` is written via a `to_dict()` serialization boundary.
- Live HN: front page → 30 records; /jobs → 28 records, shortening gracefully
  (score/author/comment_count simply absent; no field-stealing, no corruption).

- records.py has TWO entrypoints over ONE pure transform:
  extract_records (sync) / extract_records_async (async) both call
  build_result_from_raw(raw). The crawler is async and uses the async one.
  build_result_from_raw is the browser-free regression anchor — test the
  algorithm against it, not through a browser.
- records.json sentinels are distinguishable BY VALUE, not just by log level:
    signature: ""    -> legitimate page with no repeating collection (INFO only)
    signature: null  -> extraction crashed, isolated so the crawl survives (ERROR)
  Anything downstream that reads records.json must treat both as "no records"
  but must not conflate them when diagnosing.
- Crawl output (evidence/) is derived data, not tracked. Live crawls go to a
  scratch dir. tests/fixtures/ is the only tracked thing under test paths.

- ExtractionResult is `collections[]`, ranked by score desc. `rank` is a
  POSITION, not a verdict. There is deliberately NO `primary` flag: choosing the
  primary entity is semantic judgment and belongs to the reasoning step. A test
  enforces the absence of that key in the wire format.
- Two dedup rules keep the candidate list honest:
    nesting     — if every member of A sits inside some member of B, keep the
                  higher-scored one. (Kills redundant per-item wrappers.)
    absorption  — drop any collection whose members were already folded into a
                  higher-scored collection by the adjacent-sibling merge.
                  (Without this, the merge and collections[] contradict: HN's
                  subtext rows would be emitted both merged AND standalone.
                  Real HN yields ~10 raw candidates; absorption collapses to 1.)
- LATENT RISK, not yet a bug: absorption drops by score order. A genuinely
  interesting sub-collection nested inside a *lower*-scored parent could be
  suppressed. No current fixture exercises this. If a real page ever returns
  fewer collections than expected, suspect absorption first.
- Sentinels at top level:
    {"collections": []}                  -> legitimate: no repeating collection (INFO)
    {"collections": [], "error": true}   -> extraction crashed, crawl survived (ERROR)

    - entity.sourceCollection (integer) records which collection[] index the entity
  was derived from. Required on every data-bearing entity. It is the linkage
  Step 6's injection uses to find the right records. rank/index, not data.
- The negative guard is load-bearing: a candidate with a `seed` key on any
  entity now FAILS schema validation. If you ever need the model to emit data,
  you'd have to reopen the schema — don't, that's the defect.
- Entity field NAMES are model-chosen camelCase (commentCount, author); record
  fields are keyed by ROLE (comment_count, meta, title). These namespaces do NOT
  match — any code joining entity fields to record data must bridge them. This
  is the core open problem for Step 6 injection.
- DEFERRED cleanups (do deliberately, not incidentally):
    * infer_role assigns `title` only when len(text) > 20; short titles fall
      through to meta and corrupt entity shape. Length is the wrong signal;
      needs a dedicated extractor pass (position/tag/href), NOT a threshold bump.
      records.py is frozen until then.
    * meta.crawlEvidenceHash and generatedAt are model-INVENTED placeholders;
      should be stamped programmatically from the real state_hash in a reason.py
      provenance pass.
    * cli.py:89 logs the old {state_hash}_model.json filename — stale since 4b's
      key change. One-line fix in a later pass.
    * Entity fields are all typed `string` even where number/currency fits
      (score, commentCount). Optional prompt nudge later.

- LINKAGE CONTRACT (entity → record data), settled after investigation:
    entity.sourceCollection : int    -> which collections[] index the entity came from
    field.sourceRole        : string -> which record ROLE this field reads from
  Field NAME and record ROLE are DIFFERENT namespaces and do not match:
    author       <- meta            (semantic rename; no `author` role exists)
    commentCount <- comment_count   (case/format change)
    domain       <- domain (x2)     (many-to-one dedup; both leaves same value)
  Name-based matching WILL mis-zip. Always join via sourceRole.
- Injection resolves a field to the FIRST record leaf whose role == sourceRole
  ("first-occurrence"). Chosen over a model-emitted roleIndex because the model
  is bad at positional counting, and an injection-assigned index just collapses
  back into first-occurrence anyway. Residual risk (accepted, never observed):
  breaks only if a model derives a field from a NON-first occurrence of a
  different-valued duplicate role while dropping the first.
- Duplicate roles in records are common (HN: domain x2 same-value, meta x2
  DIFFERENT-value: author vs hide). The model reliably keeps <=1 field per role,
  so collisions don't reach the entity — but injection still GUARDS for it.
- Guards are split across the two boundaries where the model reaches into data:
    6a (reasoning) -> RESOLVABILITY: every sourceRole is a real role. Invented
                      roles / typos rejected into the retry loop.
    6b (injection) -> UNIQUENESS + RESOLUTION: two fields sharing a sourceRole is
                      a loud failure (not a guess); a sourceRole with no matching
                      leaf is a loud failure. Never silently produce a hole.
- id is a SYNTHETIC ROW INDEX. Deliberately boring: guaranteed unique,
  zero-heuristic, deterministic within a build. HN records DO carry a real story
  id in hrefs (item?id=NNN) but recovering it needs a param heuristic that
  collides with user?id=<author> — a silent-corruption risk in the one field the
  reducer depends on. Cross-crawl id stability is not a requirement (each build
  regenerates from one crawl). If it ever becomes one, recovery needs its own
  uniqueness guard — it is NOT free.

- ROLE INFERENCE IS TWO-PASS (records.py, unfrozen in Step 7 and re-stable now):
    pass 1  pure per-leaf -> a SPECIFIC role (image/price/age/score/domain/
            comment_count/rank/vote/number) or UNCLAIMED. Patterns are
            fullmatch/whole-text, NOT substring — a title containing "points"
            or "year" must not be eaten.
    pass 2  record-level -> first UNCLAIMED leaf in document order becomes
            `title`; the rest become `meta`.
  WHY negative definition: there is no positive feature meaning "title" across
  apps (HN uses a bare <a>, shop a <span>, multi an <h3>). The title is the
  free-text payload — the thing that is NOT typed data. Do not reintroduce a
  length threshold or a "heading tag" heuristic; both were tried and both
  relocate the failure.
  DELIBERATE TRADE: stricter pass 1 -> unanticipated formats fall through
  UNTYPED (graceful) rather than being mistyped (corrupting). If a real site's
  age/price format goes unrecognized, WIDEN THE ANCHORED PATTERN — never loosen
  the anchor back to substring matching.
- The purity anchor (_CANNED_RAW/_EXPECTED_RESULT) survived Step 7 UNMODIFIED
  and is the independent regression net for the specific roles. Its invariance
  is a property of its canned data (titles that are first-unclaimed) — if
  _CANNED_RAW is ever edited, re-verify the invariance holds.
- Role-vocabulary changes propagate WITHOUT touching reasoning: a changed role
  stream changes build_sample_collections' payload -> changes the 4b cache key
  -> cache MISS -> fresh reasoning against the new vocabulary. Verified in
  Step 7. Always CONFIRM the miss rather than assume it — a perfect extractor
  with a stale cached model still renders wrong.
  
## GENERICITY PROVEN (grocery diagnostic, unseen site)

Ran the full pipeline against scrapingcourse.com/ecommerce — a real WooCommerce
storefront the pipeline had never seen. Zero per-app code.

RESULT — the two-track thesis generalizes past HN:
- Extractor found 3 collections: the 16-product grid (rank 0), a 20-item nav
  menu (rank 1), 4-item pagination (rank 2). Step-3 absorption did NOT suppress
  the grid (the latent risk didn't bite).
- Reasoning chose the grid as primary among THREE+ candidates (sourceCollection
  0) — Step 5's semantic selection generalizes past the two-candidate case. It
  also declared a product-detail screen.
- 16 real products injected. Real names, prices, image URLs. No fabrication.
It is a mockup builder, not an HN mockup builder. This is the headline result.

FOUR GAPS SURFACED (the spec for what remains):
1. NO STATEFUL STORES (generator gap, Phase 3′ as originally specced).
   No cart, no badge, no add button. An agent cannot ACT on the page — which is
   the entire point of Omnisavant's use case. Highest-value remaining work.
2. NO IMAGE RENDERING (generator gap).
   type: imageUrl renders as a text URL string, not an <img src=>. The generator
   treats every field as a text span. HN had no images so this never surfaced;
   every commerce app is image-first.
3. NO THEMING ON THIS SITE (harvester gap).
   design_tokens came back EMPTY (colors: None) on this site's CSS, so the mock
   is unstyled. HN's tokens harvested cleanly — the harvester works on some CSS
   shapes and not others. Visual fidelity currently cannot be claimed on
   arbitrary sites.
4. DOM-FUSED FIELDS (a fidelity CEILING, not a bug — do not "fix" with a
   heuristic). This site renders "Abominable Hoodie $69.00" as ONE leaf, so the
   price rides inside the title. The extractor faithfully reflected the markup.
   Splitting it needs app knowledge — exactly what we refuse to hardcode. A
   "price-looking suffix" heuristic would misfire on product names ending in
   numbers. State this as a known limit of structural extraction.

WHAT AN AGENT CAN / CANNOT DO ON THE GROCERY MOCK TODAY:
  CAN:    read all 16 real products (names, prices); navigate nav + detail
          screen; target rows by testid.
  CANNOT: add to cart (no store); see images (URLs render as text); query price
          as a field (fused into title); filter by category (nav is inert).

SEQUENCE FROM HERE (runway: weeks):
  Step 8  — image rendering (generator honors type: imageUrl)
  Step 9  — theming (harvester works on arbitrary CSS)
  Step 10 — stateful stores (cart/collection + header badge + mutateState) ←
            the flagship: this is what makes it agent-TESTABLE
  Then    — verifier checks (P-data, P-state), Vercel deploy, README

- The generator now has TWO declaration-driven tag seams, and they are symmetric:
    elements    -> render_el switches on el.kind (input/select/link/button)
    data fields -> render_image / span branch on field.type
  Both emit based on what the MODEL DECLARED, not on field names. Any future tag
  treatment (currency, number) slots into the same branch. Do NOT name-match.
- THE STORE WRITE-PATH ALREADY EXISTS (discovered in Step 8's read):
    * a mutateState action already renders onClick={() => mutate({type, store,
      payload})} (Component.jsx.jinja)
    * GlobalContext's reducer already implements add/remove/increment/toggle
  So stores are NOT "build a state system" — they are "give the existing system
  something to target (schema stores) and something that READS it (a badge with
  a stateBinding)". Smaller than it looks.
- KNOWN, not fixed: if a model ever gives an imageUrl field uiHint: metadata,
  the <img> renders in the cramped meta row. Grocery got uiHint: content and
  looked right. If it bites, the fix is a PROMPT nudge (imageUrl -> sensible
  uiHint), NOT a template special-case for image placement.

- THEMING IS TWO HALVES. Half 1 (DONE, Step 9): the harvester detects the real
  accent (grocery #7f54b3) and the model maps it to designTokens.primary.
  Half 2 (OUTSTANDING, deferred): the generator templates barely USE `primary`
  — mostly on hover: states and the HN votearrow. So a correct accent doesn't
  visibly brand a static page (grocery renders gray-on-white, purple only on
  hover). Making branding VISIBLE (titles/prices/header in primary) is a
  contained TEMPLATE task, deliberately deferred as lower-value than stores.
  It does NOT block stores. Pick it up only if runway remains after stores +
  verifier + deploy. Risk if skipped: the demo IS faithful but doesn't LOOK
  branded in the first-glance test a human reviewer applies.

## CURRENT POSITION (update) — THIS SECTION IS AUTHORITATIVE

The "CURRENT POSITION" section near the top of this file is HISTORICAL: its
"NEXT: Step 6" is long superseded. Read this one.

Phases 1′ and 2′ complete. Phase 3′ in progress:
- Step 8 (image rendering) — DONE, committed.
- Step 9 (accent detection) — DONE. Theming half 2 (templates barely use
  `primary`) OUTSTANDING, deferred below stores.
- Step 10-pre-fix (clickable + self-link branch filter) — DONE, committed.
  Multi-state crawling now works on accessible sites; was silently broken on
  the whole modern web.
- Step 10-pre (synthesis + edge provenance) — DONE. The AJAX-race defect it
  shipped with is now FIXED by 10-pre-fix2 (below); live capture works.
- 10c-vet — ABORTED, premise false (scrapingcourse's cart was never broken).
  Left behind a keeper: tools/vet_storefront.py.
- Step 10-pre-fix2 (AJAX-settle predicate) — DONE (this commit), 129 tests.
  The race is dead: `_visit` now calls `_settle_after_click` instead of bare
  networkidle, and AJAX adds are captured WITH their effect. LIVE PROOF:
  scrapingcourse's populated cart a7ccbf5b33908333 (the exact 07-10 hash — item
  row, qty control, Proceed to Checkout) captured as a genuinely NEW state, TRUE
  `via:'Add to cart'`, deterministic across two crawls; HN byte-identical.
  See "THE SETTLE PREDICATE" below for the shape and the one judgment call.

NEXT: Step 10a — route a DISTILLED, PER-ELEMENT, LABEL-PRESERVING affordance
channel into reasoning, so the model can declare stores/mutations at all (it
emits 0 mutateState today because it never sees an affordance). Part A already
made labels survive capture; 10a is the reasoning half. Then 10b (render the
add->badge->cart loop), then 10c (the full loop; terminus is the CART, since
/checkout/ genuinely redirects to shop — re-confirmed 07-17 from a POPULATED
cart, so that limit is real and not another measurement artifact).

## STORES — THE PLAN (settled across four reads; do not re-derive)

- Goal: agent-testable stateful mockups (add-to-cart -> badge -> cart -> qty).
  This chain IS available on scrapingcourse: its cart really populates (item rows
  + quantity control, hash a7ccbf5b...), so add->badge->cart->qty is faithful
  there. Only the CHECKOUT leg is absent (/checkout/ redirects to shop). A 07-17
  note here briefly claimed the cart had no rows and no qty — FALSE, retracted;
  that was the AJAX race, not the site. The race is now FIXED (10-pre-fix2), and
  the populated cart is captured live and deterministically, so P-state IS
  demonstrable on this site.
- THE WRITE PATH ALREADY EXISTS: mutateState -> mutate({type,store,payload}) ->
  reducer (add/remove/increment/toggle). store is a real slice key; payload can
  be the whole row (payloadFrom: boundEntity). Reducer LAZY-INITS unknown stores
  (store:'cart' springs into existence on first add). READ path: useMockState()
  exists; Screen already reads state. Missing: schema `stores`, a component
  `stateBinding`, and a badge that renders a store-derived value.
- FAITHFULNESS LINE (the core discipline — a fabricated affordance is the
  Story 4..8 sin one layer up):
    * Transcribe WHICH products have add-to-cart (scrapingcourse: 2 of 16, NOT
      16). Never add buttons to products that lack them.
    * Complete the PLUMBING of affordances that exist (add->badge->cart->qty must
      actually work), but never invent screens/affordances the page lacks.
- REASONING WAS BLIND TO AFFORDANCES: Step 4 made the payload structure-only
  (records only), so the model never saw "Add to cart" / the cart badge and
  emitted zero mutations. 10a's prerequisite is routing a DISTILLED, PER-ELEMENT,
  LABEL-PRESERVING affordance channel back into reasoning (NOT the 34KB dump Step
  4 killed). Labels carry the signal ("Add to cart"->add, "Select options"->nav,
  "cart-contents"->read); dropping labels would collapse the distinction and
  invite fabrication.
- FLOW-SEQUENCE APPROACH = SYNTHESIS (chosen over inferred/declared): derive the
  action from the REAL captured affordance, resolve into an ordered click-path,
  hand to the existing replay (which already runs a full path in ONE persistent
  context, so [add, cart-link] reaches the populated cart with NO architectural
  change). No add button on page -> no action synthesized -> honest absence.
- DETERMINISM UNDER MUTATION = ALREADY SAFE (measured): normalize_dom strips text
  and all attrs but class/role/data-testid, so cart totals / session ids / cart
  ids never reach the hash. Two fresh sessions -> byte-identical cart hash. Only
  requirement: choose the action deterministically (first "Add to cart" in doc
  order). No hash surgery needed.
- SCOPE of 10-pre: execute ONE captured affordance's action, capture the
  resulting state, deterministically, for a single linear flow. No planner, no
  branching, no multi-item carts. — DONE; see "10-pre DELIVERED" below.

## 10-pre DELIVERED (settled; do not re-derive)

- EVIDENCE NOW RECORDS EDGES, not just states. evidence/{hash}_provenance.json:
    { url, from_state: <parent hash|null>, clicks: [sel,...], via: <element|null> }
  Each state has exactly ONE incoming edge (BFS-first-wins), so provenance is a
  FIELD ON THE STATE, not a separate edges file — there is no many-to-one to
  model. `clicks` is the FULL path from `url`, not the last hop.
- `via` IS THE FIRST PATH, NOT THE CAUSE — a LATENT trap for 10a. It records the
  path that FIRST reached a state, not the only path nor the cause. Synthesis is
  queue-PREPENDED (appending lets cheap link-follows exhaust max_states and starve
  the cart), so it wins the dedup race; if an action's effect were not structural,
  `via` would attribute a state to an affordance that didn't produce it. The
  scrapingcourse example once recorded here was an ARTIFACT OF THE AJAX RACE, not
  a real instance — with the add awaited, the populated cart IS distinct and
  via:'Add to cart' is causally right. So: latent and unobserved, not demonstrated.
  Proving causation needs a control (reach the state WITHOUT the action). Not built.
- THE AJAX RACE — FIXED (10-pre-fix2). Kept here because the SHAPE of the bug is
  load-bearing: bare `wait_for_load_state('networkidle')` after a click does NOT
  wait for an AJAX effect — networkidle is a per-document lifecycle event that has
  already fired, so it returns instantly and the crawl reads state before the XHR
  even lands. Any action whose effect is an XHR was LOST, with the action's
  provenance stamped on the state it never changed. A navigation-based action was
  unaffected (networkidle waits for the load), which is why it hid for a week.
  The replacement is `_settle_after_click`; see "THE SETTLE PREDICATE" below.
- _pick_branch_selectors -> _pick_branch_elements, returns element DICTS. The
  selector<->label association already exists at selection time; narrowing to a
  string discards it, and recovering it by lookup-by-selector MIS-ATTRIBUTES when
  two elements share a selector — a false provenance record.
- discover_elements now returns {tag, text, selector, testid, href}. href is
  absolute (resolved against baseURI); testid/href are None when absent. Nothing
  reads elements.json yet — it is written for 10a.
- _CLICKABLE_JS takes {selectors, requireNewTarget}. ONE definition of
  "clickable", two callers: branching requires a new target (a self-link wastes
  budget); SYNTHESIS MUST NOT (an ACTION is not a navigation — an add-to-cart
  legitimately has href="#"). Never write a second clickability check.
- SYNTHESIS MUST FILTER BY CLICKABILITY — learned the hard way. Unfiltered, the
  first "Cart" in DOM order is inside a COLLAPSED HAMBURGER: real box, unclickable,
  click times out, path abandoned, storefront reported cartless. "An unclickable
  match degrades to honest absence" is WRONG: it is a FALSE absence,
  indistinguishable from a real one. The fixture reproduces this trap.
- DETECTION RULES (anchored, same discipline as records.py role inference):
    ACTION      -> LABEL only. Its href is site-private query vocabulary
                   (?add-to-cart=2765) and must not be matched.
    NAVIGATION  -> LABEL **or** TARGET. A link's intent lives in either, and real
                   headers express it only in the target: the one clickable cart
                   link on scrapingcourse is labelled "$0.00 0 items" — a price,
                   containing no word for what it is. Label-only detection is
                   blind to it and calls a cart site cartless.
    Targets match a whole PATH SEGMENT, query IGNORED. ?add-to-cart=2765 contains
    the substring "cart" but its path is /ecommerce/ — segment equality is what
    stops the add being mistaken for the cart it feeds ([add, add]).
  Widen by ADDING anchored alternatives; never loosen to substring.
- SYNTHESIS OFFERS A PATH; it does not assert the path leads anywhere new. If the
  action has no structural effect, the replay lands on an already-seen state and
  dedup rejects it — the crawl is exactly what BFS alone produces. Honest, not a
  bug: the action really happened and the page really did not change.
- HONEST ABSENCE IS PROVEN, live and hermetic: no add-to-cart -> no path -> no
  cart state. HN's captured state set is byte-identical to a BFS-only baseline
  (synthesis seam stubbed). Guard this in any future change.

## THE SETTLE PREDICATE (10-pre-fix2; do not re-derive, four wrong designs were tried)

- `_visit` replaced bare `wait_for_load_state('networkidle')` with
  `_settle_after_click(page, inflight)`. `_Inflight` is attached BEFORE the click
  (a click handler fires its XHR synchronously; a counter attached after misses
  it). SHAPE:
    STAGE 1  `load` then `networkidle` — waits a NAVIGATION's post-load XHRs
             (WooCommerce's cart block fetches contents AFTER load); no-ops for an
             in-place click (the state already fired). networkidle was never
             broken at THIS — only as a general "did the page react" test.
    STAGE 2  poll until busy()==0 AND the NORMALIZED-DOM fingerprint has held
             _SETTLE_QUIET_MS(500ms), else _SETTLE_CEILING_MS(12000ms) ceiling
             (LOGGED, never silent). busy() ignores requests older than
             _SETTLE_STALE_MS(1500ms) as fire-and-forget.
- THE FOUR WRONG DESIGNS (each passed the hermetic suite; each caught ONLY live —
  the tests could not gate them, which is the standing lesson):
    1. Dropped networkidle -> captured the cart MID-RENDER (client-rendered block
       not yet fetched). Keep networkidle for navigations; it composes with (2).
    2. Quiescence via MutationObserver -> never closed. scrapingcourse mutates
       text/attrs endlessly, but normalize_dom STRIPS those, so they can't move
       the hash. Measure quiescence of the NORMALIZED DOM (the artifact we hash),
       not raw mutations.
    3. Wait for zero in-flight -> unreachable. A GA beacon (google.com/g/collect)
       + ~20 SAME-ORIGIN prefetches never complete. So the predicate degenerated
       into a fixed 5s sleep and captured non-deterministically (17 vs 25
       elements run-to-run). Origin filtering would NOT help (same-origin). Hence
       _SETTLE_STALE_MS drops stale requests.
    4. Ceiling for hung requests -> wrong. Staleness handles those; the ceiling is
       for a DOM that never STABILIZES. hang_fixture must SETTLE (<6s);
       churn_fixture (appends ELEMENTS, since text wouldn't survive normalize)
       hits the ceiling.
- THE ONE JUDGMENT CALL — _SETTLE_STALE_MS=1500 is NOT derived. It must exceed the
  slowest request whose response we need AND stay under how long a never-returning
  request may block us; no value fits every site. A legit action slower than 1.5s
  is UNDER-waited, and unlike a ceiling hit that failure is SILENT. Residual risk.
- COST: HN +3.8s (~25%), states identical — the predicate runs after every click.
  Judged worth it. FIXTURE IS STILL CLEANER THAN REALITY (no prefetch storm / no
  analytics / no client-rendered block), so _SETTLE_STALE_MS is gated only by the
  live check, never the suite. If a future site under-waits, this is the first
  suspect.
- TERMINUS on scrapingcourse: /checkout/ REDIRECTS TO SHOP — no form, no
  confirmation. 10b must not build checkout/confirmation here; that would
  FABRICATE two screens the site lacks. The full
  add->cart->checkout->confirmation loop is a 10c goal on a DIFFERENT storefront
  whose checkout is real — VET ITS CHECKOUT BEFORE COMMITTING, the way we vetted
  scrapingcourse.
- SCRAPINGCOURSE'S CART IS FINE. IT POPULATES. The 07-10 terminus read above is
  CORRECT and still reproduces — cart hash a7ccbf5b33908333, item rows, the works.
  A 07-17 entry briefly claimed "the site changed and its cart no longer
  populates". THAT CLAIM WAS FALSE and has been retracted (see JOURNAL 07-17
  "10c-vet ABORTED"). The site never changed; the MEASUREMENT was broken by the
  AJAX race below. Do not resurrect it. Measured proof:
    add awaited 0ms    -> 07e8a64d...  cart body "currently empty"
    add awaited 3000ms -> a7ccbf5b...  cart body has the item   <- matches 07-10
  The demo storefront question is CLOSED: scrapingcourse stays. 10c-vet was
  aborted because its premise (a stub cart) did not exist.

- PROVENANCE `via` IS REACHABILITY, NOT CAUSATION — but LATENT, never observed.
  Because synthesis is prepended, the synthesized [add, cart] path wins the
  first-wins dedup race, so a captured cart WOULD be attributed to "Add to cart"
  even if the cart link alone reached the identical state. 10a MUST NOT read `via`
  as "this affordance caused this screen." Establishing causation needs a CONTROL
  — reach the state without the action and compare — which 10-pre did not do.
  RETRACTED: the "(true on scrapingcourse, whose cart is a stub)" example that
  stood here was an ARTIFACT OF THE AJAX RACE, not a real instance. With the add
  awaited, scrapingcourse's populated cart IS a distinct state and via:'Add to
  cart' is causally correct there. The trap is real in principle; no site has yet
  exhibited it. Faithful default: `via` tells you which affordance-path first
  reached a state, nothing stronger.

- RETRACTED — "scrapingcourse commerce is STUBBED" WAS FALSE (my error, 07-17).
  Its cart POPULATES: item rows + quantity control, hash a7ccbf5b33908333, exactly
  as the 07-10 read recorded. The "stub" reading came from measuring the cart
  BEFORE the AJAX add landed. Only /checkout/ is genuinely absent (redirects to
  shop) — re-confirmed 07-17 FROM A POPULATED CART, so THAT limit is real and not
  another measurement artifact. scrapingcourse DOES host a live
  add->populated-cart demo (captured deterministically since 10-pre-fix2) and CAN
  satisfy P-state. Terminus is the CART, as 07-10 said all along.

- DEMO SITE = SCRAPINGCOURSE. Question CLOSED; 10c-vet aborted, no replacement
  needed. If one is ever wanted, demowebshop.tricentis.com/books was vetted and
  PASSES (its cart populates); note its /-root FAILS because the first add is a
  gift card that navigates rather than adds.

- FREEZE-THE-EVIDENCE STRATEGY — OPEN DECISION, re-opened by the retraction. Its
  original rationale ("scrapingcourse changed in 7 days, so don't depend on a live
  site") WAS FALSE — the site never changed. An honest case still exists: live
  sites really do vanish mid-project (magento.softwaretestingboard.com was DOWN on
  07-17 — Cloudflare 526, invalid origin SSL), and the pipeline is
  deterministic-from-evidence, so building from a committed capture is honest and
  deadline-safe. But it is no longer FORCED. Decide it on its merits, not on my
  retracted finding.

- P-STATE NEEDS A REAL CART — and it HAS one. PLAN_v2's verifier property "a
  declared mutateState produces an asserted DOM change" cannot pass where the
  action has no observable effect; scrapingcourse qualifies (a7ccbf5b... != the
  empty cart), now that the AJAX race is fixed. For ANY future site check the
  criterion
  is a HASH DELTA, never a visual check (a text-only badge looks populated but is
  stripped by normalize_dom) — AND both paths must land on the SAME page, or the
  delta just compares a product page to a cart page and "passes" meaninglessly
  (demowebshop fooled the probe exactly that way). tools/vet_storefront.py
  implements both halves.

- 10-pre COMPLETE AND VERIFIED LIVE. The AJAX-race defect is fixed
  (10-pre-fix2). scrapingcourse's populated cart (a7ccbf5b33908333) is captured
  as a new state with a true via:"Add to cart" edge. All prior corrections stand;
  the cart populates, checkout redirects to shop (terminus = cart), P-state is
  demonstrable on scrapingcourse. NO site replacement needed.
- STANDING LIMITATION (do not try to "fix" with a better fixture — 5th recurrence
  of the lesson): settle-timing correctness against real network conditions
  CANNOT be unit-tested; the hermetic suite went green for four DIFFERENT broken
  settle designs. This class of correctness is established by LIVE measurement and
  guarded by a re-runnable live check against a known control hash, not by tests.
- _SETTLE_STALE_MS=1500 is a JUDGMENT CONSTANT, not derived. Failure mode is
  SILENT under-waiting (an action slower than the window captured empty with a
  false via) — unlike a ceiling hit, which logs. Guarded by: (a) a log line
  distinguishing stale-stop from ceiling-hit, (b) vet_storefront.py re-run against
  scrapingcourse's control a7ccbf5b before trusting a dependent run. Do not tune
  blind.
- NEXT: 10a — the affordance channel. Route distilled, per-element, label-
  preserving affordances (the provenance `via` edges + the page's real nav
  elements) into reasoning. DELETE the hardcoded HN nav list in prompts.py:40.
  Gate: grocery/storefront Navbar comes from real nav (not "Home"/HN pattern),
  and the model declares the cart mutation from the real add-to-cart affordance.