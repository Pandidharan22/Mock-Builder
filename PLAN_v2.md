# MockBuilder — Engineering Plan v2

> **Revision of the original PLAN.md.** Same architecture, same property table,
> same tech stack. This version fixes the *one structural mistake* that caps
> output quality no matter how good the model is, and adds what's needed to reach
> hand-built (Instamart-class) fidelity.

---

## 0. What changed and why (read this first)

The original plan is sound. Evidence capture, design-token harvesting, the
deterministic generator, the verifier, the LLM-fills-schema principle — all
correct and worth keeping. **Do not rebuild from scratch.**

Diagnosis from the actual HN runs (evidence + app-model on disk):

1. **Design tokens were never the problem.** The crawler correctly harvested
   `#ff6600`, `#f6f6ef`, `Verdana`. They reached the model intact.

2. **The real defect: the LLM is asked to invent DATA.** The prompt says
   *"You MUST generate 4 to 8 realistic, deterministic rows of seed data."* The
   crawler had already captured ~30 real stories. The model used 3 and
   fabricated `Story 4 … Story 8 / example.com` to satisfy the rule. That
   fabrication is the visible "40% fake" in the output — and it is **instructed**,
   not a model weakness.

3. **The fusion is the root cause.** One LLM call is asked to do two jobs at
   once: (a) *judgment* — "this is a link aggregator, the unit is a story with
   these parts" (LLMs are good at this), and (b) *data transcription* — "here are
   the exact 30 rows" (LLMs are bad at this and shouldn't do it). Fusing them
   forces fabrication or token-limit blowup.

4. **HN is a deceptively weak test.** It's a single flat list, so nothing forces
   multi-screen, multi-entity, stateful structure to emerge. Instamart is a good
   harness because an agent can *act and observe consequences* (add to cart →
   badge changes). The current schema can barely express that, so a
   more complex app would expose the gap immediately — exactly the worry.

**The fix, in one sentence:** *Extract real records deterministically; let the
LLM decide only structure; make the schema able to express stateful multi-screen
apps.*

This is validated by a working PoC (`repeating_extractor_poc.py`) — see §5.

---

## 1. The corrected architecture (one boundary moved)

Original five stages stay. The **crawler↔reasoning boundary moves**: structured
data extraction moves *out* of the LLM and *into* the crawler.

```
   CAPTURE                         REASONING                   SYNTHESIS
┌───────────────────┐      ┌────────────────────────┐   ┌──────────────────┐
│ 1. Crawler         │      │ 3. Reasoning (LLM)      │   │ 4. Generator      │
│  - BFS UI states   │      │  Input: ONE sample      │   │  AppModel + REAL  │
│  - screenshots     │──▶   │  record's typed shape   │──▶│  records → React  │
│  - design tokens   │      │  + screenshot + tokens  │   │  seed[] from data │
│  ★ RECORD EXTRACT  │      │  Output: STRUCTURE only │   │  (never invented) │
│    (repeating-unit │      │  (entities/components/  │   └──────────────────┘
│     detection)     │      │   screens/flows/state)  │
└───────────────────┘      └────────────────────────┘
        │                                                        ▲
        └──────────── extracted records[] ──────────────────────┘
             (bypass the LLM entirely; go straight to seed data)
```

**Why this also fixes the token-budget problem you documented in `reason.py`:**
the LLM no longer receives 30 rows of elements or a huge flat element list. It
gets *one* sample record's shape. The request shrinks by an order of magnitude,
which unblocks a stronger model within the free-tier TPM cap.

---

## 2. The two-track data model

Split every screen's evidence into two tracks with a hard boundary:

| Track | Produced by | Consumed by | Contains |
|-------|-------------|-------------|----------|
| **Structure** | LLM (judgment) | Generator templates | entity field *names+types*, component shape, `uiHint` roles, screens, flows, state stores, edge variants |
| **Data** | Crawler (deterministic) | Generator `seed[]` | the actual extracted records — real titles, prices, domains, ages |

The LLM defines the *schema of a story*; the crawler supplies the *stories*. The
generator zips them: `entity.fields` (from LLM) × `records[]` (from crawler) →
`seed[]`. Fabrication becomes structurally impossible.

---

## 3. New/changed phases

### Phase 1′ — Crawler: add repeating-unit extraction (the key change)
**Goal:** for each captured state, emit `records.json` alongside the existing
screenshot + elements + tokens.

- Add `crawler/records.py`: an in-page detector (proven in the PoC) that:
  - assigns each element a **structural signature** (data-independent subtree
    tag-shape);
  - groups structurally-identical siblings; the largest text-rich group is the
    **primary collection**;
  - merges adjacent non-group siblings to handle split rows (HN's title-row +
    subtext-row);
  - emits ordered, **role-typed** leaves per instance (title / price / age /
    domain / count / image / meta) via deterministic Python inference.
- Detect **multiple** repeating groups per page when present (e.g. a category
  strip *and* a product grid) — rank by score, keep the top N as candidate
  collections/entities.

**Acceptance:** pointing at HN yields `records.json` with **all** visible
stories (not 3), each carrying title/domain/age/score/comments. Pointing at a
grid app (shop fixture / real grocery demo) yields product records with
name/price/unit — *using the identical detector, no per-app code.* (PoC already
passes both.)

### Phase 2′ — Reasoning: structure only, one sample record
**Goal:** LLM outputs the AppModel *shape*; no seed data.

- Rewrite the prompt: remove "generate 4–8 rows." Replace with: *"Here is one
  representative record with typed fields, plus a screenshot. Define the entity
  (field names + types), the component (with uiHint per field), screens, flows,
  and any stateful stores. Do NOT emit seed data — it is supplied separately."*
- Feed **one** sample record + screenshot + tokens. Payload drops from ~12.5K
  tokens to ~2–3K → a stronger model (Qwen-class) now fits the TPM budget.
- Keep the validate⇄retry loop and referential-integrity gate as-is (they're
  good).

**Acceptance:** `app-model.json` has zero seed data, correct field
names/types/uiHints, and — for HN — declares the story entity, StoryRow, home +
detail screens, an upvote `mutateState`, and edge variants. Request stays under
the account TPM cap on the stronger model.

### Phase 3′ — Generator: seed from real records + stateful templates
**Goal:** clean React whose data is 100% real, and that can express stateful
apps.

- `generate.py`: build `seed[]` by mapping `entity.fields` over
  `records.json` (crawler data). Coerce types per field (`points`→number,
  `age`→string). No model data path remains.
- Add a **cart/collection store template**: a Context store that any
  `mutateState` op (`add`/`remove`/`increment`) targets, plus a persistent
  header badge component that reflects store size/total. This is what makes an
  agent-testable harness (Instamart-class): actions have observable
  consequences.
- Add a **detail-screen template** per entity so `home → detail` is a real
  navigation, and edge variants (`?state=empty|error|loading`) are reachable.

**Acceptance:** generated HN mock shows all real stories, no `Story N` filler;
upvote increments a visible count deterministically; `?state=empty` renders the
empty variant. Generated grocery mock: add-to-cart updates a header badge; cart
screen lists added items; remove empties it.

### Phases 4–5 — unchanged in spirit
Verifier and benchmark/deploy stay as originally planned. Add two verifier
checks: **(P-data)** every seed row traces to an extracted record (no
fabrication regression), and **(P-state)** a declared `mutateState` produces an
asserted DOM change in the agent walk.

---

## 4. Schema additions (for Instamart-class harnesses)

Minimal additions to `app_model.schema.json`:

- `stores[]`: named stateful stores (`cart`, `savedItems`) with an item shape
  and ops (`add`/`remove`/`increment`/`setQty`).
- `component.stateBinding`: lets a component (header badge, cart line) read a
  store's derived value (count, sum).
- `screen.reachableVia`: ties edge variants to the *actions* that reach them, so
  the verifier can walk to them (empty-cart is reached by remove-all, not just a
  query param).

Keep the "LLM never sets seed data" invariant: `entity.seed` is **generator-
owned**, populated from `records.json`, never from the model.

---

## 5. Proof of concept (already working)

`repeating_extractor_poc.py` demonstrates the core fix end-to-end against two
structurally different fixtures, with **one detector and no app-specific code**:

```
HN fixture   -> 5 real stories: rank, vote, title, domain, score, author, age, comments
Shop fixture -> 6 real products: image, name, price, unit
```

Run:
```
python repeating_extractor_poc.py file:///.../hn_fixture.html
python repeating_extractor_poc.py file:///.../shop_fixture.html
python repeating_extractor_poc.py https://news.ycombinator.com/
```

This is the whole thesis in 120 lines: real data out, app-agnostic, LLM-free.
Productionizing it = Phase 1′.

---

## 6. Suggested build order

1. **Phase 1′ first** (records.py) — it's the linchpin and already de-risked by
   the PoC. Wire it into the crawler; emit `records.json` per state.
2. **Phase 2′** — rewrite the prompt to structure-only; feed one sample record;
   swap to the stronger model now that the payload fits.
3. **Phase 3′** — seed-from-records + cart store + detail screen templates.
4. Re-run HN → confirm zero filler. Then run the grocery demo → confirm a
   stateful cart harness emerges with *no HN-specific and no grocery-specific
   code*. That contrast is the deliverable that proves genericity.
5. Verifier checks + Vercel deploy + README (lead with the two-track principle
   and the HN-vs-grocery contrast).

---

## 7. What to tell Omnisavant in the README

Lead with the insight, not the code: *"A raw crawl is a dead page; a naive
LLM-clone hallucinates data. MockBuilder splits the job — deterministic
extraction supplies real content, the LLM supplies only structure, and a
deterministic generator produces an agent-testable stateful harness. The same
pipeline builds Hacker News and a grocery app with no per-app logic."* That
framing directly answers their use case (testing voice agents on faithful,
stateful, deterministic mockups of arbitrary client apps).
