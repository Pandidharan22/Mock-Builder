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

1. **Phase 1′ — `crawler/records.py`**: repeating-unit extraction → emit
   `records.json` per crawl state. (De-risked by the PoC.)
2. **Phase 2′ — reasoning**: rewrite prompt to STRUCTURE-ONLY, feed one sample
   record + screenshot; drop "generate rows"; swap to stronger model (payload
   now fits the TPM budget).
3. **Phase 3′ — generator**: seed `seed[]` from `records.json` (never the model);
   add cart/collection store template + persistent header badge; add
   detail-screen template + reachable edge variants.
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

## CURRENT POSITION

- `PLAN_v2.md` committed to the repo.
- PoC (`repeating_extractor_poc.py`) + fixtures exist as reference (may live
  outside the repo; they are the blueprint, not production code).
- **Next action: Phase 1′, Step 1** — create `crawler/records.py` as a
  standalone module with the repeating-unit extractor + role inference, and a
  test that runs it against the two fixtures, BEFORE wiring it into the crawler.

## Key files

- `PLAN.md` — original spec + property table (P1–P8)
- `PLAN_v2.md` — corrected architecture (the plan we follow)
- `repeating_extractor_poc.py` — working proof of the core extractor
- `mockbuilder/crawler/` — where records.py will live
- `mockbuilder/reasoning/prompts.py`, `reason.py` — Phase 2′ targets
- `mockbuilder/generator/generate.py` + `templates/` — Phase 3′ targets
- `app_model.schema.json` — the contract; gets store/state additions in Phase 3′

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