# MockBuilder — Engineering Plan

> **One-line brief:** Given any web application URL, automatically generate a
> deterministic, backend-free, **agent-testable static UI harness** — output
> whose quality matches a hand-built mockup.

This document is the build spec. It is written to be handed to an engineer (or
to Claude Code) and executed phase by phase. Each phase has explicit
**deliverables** and **acceptance criteria**; do not advance until a phase's
acceptance criteria pass.

---

## 1. Problem framing (why the obvious approach fails)

The naive reading — "crawl the site, save the HTML" — produces a dead page:
buttons do nothing, `fetch()` calls 404 against no backend, nothing is
addressable, and edge states (empty cart, no search results) simply don't exist
in a single captured snapshot.

A hand-built mockup has four properties a raw crawl does not yield:

1. **Semantic structure** — `ProductCard`, `Cart`, `CategoryNav` — not `<div class="_xb3f">`.
2. **A clean data model** — products/prices as local typed data, not scraped DOM text.
3. **Flows as a connected graph** — Home → Search → Listing → Cart → Checkout.
4. **Edge states** — empty / error / unavailable, which often aren't in the live DOM at crawl time.

Recovering 1–4 for an *arbitrary* app requires **judgment**, not pattern
matching. That judgment is the one task we delegate to an LLM. Everything else
is deterministic engineering.

---

## 2. The harness properties (our real specification)

The benchmark is **not** "does the output look like a specific app." It is:
*does the output exhibit the properties that make a mockup a good agent-testable
static UI harness.* These are app-agnostic, measurable, and what the brief
actually cares about. They are simultaneously the generator's target and the
verifier's checklist.

| # | Property | Definition | Verifier check |
|---|----------|------------|----------------|
| P1 | **Self-contained** | No backend/DB/network at runtime; all data local. | Runtime network panel shows zero external calls after build. |
| P2 | **Deterministic** | Same action → same result, every run. No randomness/clock dependence. | Same agent script run twice → identical end states. |
| P3 | **Addressable** | Every interactive element has a stable, semantic `data-testid`. | All actionable nodes carry ids; ids stable across rebuilds. |
| P4 | **Navigable** | Core flows are complete and connected as a graph. | Agent walks every `flow` in the manifest with no dead ends. |
| P5 | **Edge-state complete** | Empty/error/unavailable states exist where meaningful. | Each eligible screen declares + renders its edge variants. |
| P6 | **Faithful** | Layout, design tokens, flows recognizably match the source. | Structural + visual diff vs. source screenshots; score threshold. |
| P7 | **Documented** | A manifest describes screens, components, flows, selectors. | `manifest.json` validates against schema; covers all screens/edges. |
| P8 | **Readable** | Generated code is clean, conventional, human-reviewable. | Lint passes; file/component structure matches template conventions. |

---

## 3. Architecture

Five stages. The first two **gather evidence**, the middle one **makes
decisions** (the only non-deterministic stage), the last two are **deterministic
machinery**.

```
        CAPTURE                       REASONING                 SYNTHESIS                VERIFICATION
 ┌──────────────────┐        ┌────────────────────────┐   ┌──────────────────┐   ┌──────────────────┐
 │ 1. Crawler        │        │ 3. Reasoning layer      │   │ 4. Generator      │   │ 5. Verifier       │
URL─▶ Playwright BFS  │──evid─▶│ LLM fills AppModel       │──▶│ AppModel→React     │──▶│ build/lint/        │─▶ ./out
 │   over UI states   │ ence   │ schema (judgment only)   │   │ +testids+manifest  │   │ agent smoke-test   │  (Vercel-ready)
 ├──────────────────┤        │                          │   │ via deterministic  │   │ scores P1–P8       │
 │ 2. Recorder        │        │ validate ⇄ retry         │   │ templates          │   │                    │
 │   net/css/assets   │        └────────────────────────┘   └──────────────────┘   └──────────────────┘
 └──────────────────┘
```

**The central design principle:** the LLM never writes code. It only fills the
`AppModel` JSON schema (see `app_model.schema.json`). Deterministic templates
turn that validated schema into a React app. This split is what lets us claim
both *generality* (LLM handles arbitrary apps) and *defensibility*
(determinism, verification, clean code).

### Why this contains the LLM's two real risks
- **Non-determinism** → low temperature + cache keyed on `crawlEvidenceHash`;
  the model output is a reviewable artifact, re-used when evidence is unchanged.
- **Hallucination** → schema validation rejects malformed output; the verifier
  stage refuses to ship a harness that doesn't build, lint, and navigate.

---

## 4. Tech stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Pipeline orchestrator | **Python 3.11+** | Strongest language for the author; great Playwright + tooling story. |
| Browser automation | **Playwright (async)** | Reliable JS-app rendering, network interception, screenshots. |
| Reasoning | **Groq API (Llama 3.2 Vision)** | Open-weights vision+text in one call; low-cost, high-throughput, no proprietary-API lock-in. |
| Schema validation | **jsonschema** (Python) | Enforce the AppModel contract before generation. |
| Generated app | **React + Vite + Tailwind + React Router + Context** | Conventional, Vercel-native, clean to read. |
| Verification | **Playwright** (agent walk) + **eslint** + `vite build` | Turns each property into an executable check. |

The **pipeline** (Python) and the **generated mock** (React) are separate
artifacts with a hard boundary. Don't blur them — that boundary is the
architecture.

---

## 5. Repository layout

```
mockbuilder/
├── README.md                      # property table, architecture, results, how-to
├── PLAN.md                        # this document
├── pyproject.toml
├── app_model.schema.json          # THE CONTRACT (Phase 0)
│
├── mockbuilder/                   # the PIPELINE (Python)
│   ├── cli.py                     # `mockbuilder build <url> -o ./out`
│   ├── config.py                  # budgets, timeouts, model + temperature
│   ├── models.py                  # python dataclasses mirroring the schema
│   │
│   ├── crawler/                   # Phase 1
│   │   ├── crawler.py             # BFS over UI states
│   │   └── dom.py                 # normalize-DOM fingerprint + element discovery (in-page JS)
│   ├── recorder/                  # Phase 1
│   │   ├── network.py             # intercept + save API payloads as fixtures
│   │   └── design_tokens.py       # harvest computed colors/fonts/spacing/radii
│   │
│   ├── reasoning/                 # Phase 2  (the centerpiece)
│   │   ├── reason.py              # orchestrates LLM calls, validate⇄retry loop
│   │   ├── prompts.py             # versioned system + user prompt templates
│   │   └── cache.py               # content-addressed cache keyed on evidence hash
│   │
│   ├── generator/                 # Phase 3
│   │   ├── generate.py            # AppModel → file tree
│   │   ├── testids.py             # stable data-testid synthesis + injection
│   │   ├── manifest.py            # emit manifest.json for agents
│   │   └── templates/             # React/Vite/Tailwind template files (Jinja2)
│   │
│   └── verifier/                  # Phase 4
│       ├── verify.py              # runs all property checks, emits a scorecard
│       └── agent_walk.py          # Playwright agent that walks manifest flows
│
├── benchmarks/                    # Phase 5: runs + scorecards on contrasting apps
│   ├── instamart/
│   ├── blog/
│   └── dashboard/
│
└── examples/                      # a committed generated mock = the live Vercel demo
```

---

## 6. Build phases

> Rule: each phase ends with something runnable and an acceptance gate. Never
> advance on a red gate.

### Phase 0 — Foundations & the contract
**Goal:** lock the interface everything depends on, before any feature code.

- Author `app_model.schema.json` (done — review it first).
- Mirror it as Python dataclasses in `models.py` with a `validate()` that runs
  `jsonschema` against the file. Schema is the source of truth; dataclasses are
  ergonomics.
- Stand up `cli.py` with the full stage sequence wired to **stubs**, so
  `mockbuilder build <url>` runs end-to-end and produces an empty-but-valid
  output tree.
- Groq (AsyncGroq) client wrapper in `reasoning/` with: pinned model,
  `temperature=0`, and the content-addressed cache (`cache.py`) keyed on
  evidence hash.

**Acceptance:** `mockbuilder build https://example.com -o ./out` exits 0,
creates the output tree, and a hand-written sample `AppModel` validates against
the schema (and an intentionally-broken one fails).

---

### Phase 1 — Crawler + Recorder (evidence capture)
**Goal:** from a URL, produce a structured `evidence/` bundle. No interpretation.

- `crawler.py`: launch Chromium, BFS over UI states.
  - **State identity** = hash of normalized DOM (`dom.py`): strip volatile
    text/data so the same screen with different data collapses to one node.
    This heuristic is the single most important tunable in the crawler —
    document it inline.
  - Budgeted: `--max-states`, `--max-actions-per-state`, settle timeouts.
  - Per state capture: full screenshot, structural DOM, list of interactive
    elements (with best available selector: testid → role+name → css path).
- `recorder/network.py`: intercept responses; persist JSON/text payloads as
  fixtures keyed by `METHOD URL`.
- `recorder/design_tokens.py`: harvest computed `color`, `font-family`,
  spacing, `border-radius` across sampled nodes; reduce to a small palette/scale.

**Acceptance:** pointing the crawler at the benchmark grocery app yields an
`evidence/` folder with ≥1 screenshot per discovered state, a fixtures dir, and
a `design_tokens.json` with a non-empty primary color and font family.

**Risk note:** real production apps may have bot-protection/login walls. Keep a
fallback public demo target so a blocked crawl never blocks development. The
pipeline must be URL-agnostic — that's the whole point.

---

### Phase 2 — Reasoning layer (the centerpiece — de-risk early)
**Goal:** turn `evidence/` into a **valid** `AppModel`.

- `prompts.py`: a versioned system prompt instructing the vision model to act as
  a senior engineer reverse-engineering an app into the AppModel schema. The prompt:
  - receives, per screen, the **screenshot + structural DOM + any fixtures**;
  - must **classify screens, identify repeated components, infer entities +
    deterministic seed data, map flow edges**;
  - must **enumerate edge-state variants the live app didn't show** (this is
    where regeneration beats snapshotting — the model reasons from a screen's
    *purpose*);
  - must derive `data-testid`s from **role/semantics**, never source CSS classes;
  - must output **only** JSON conforming to the schema.
- `reason.py`: the **validate ⇄ retry loop**. Call the model → validate against
  schema → on failure, feed the validation errors back for a bounded number of
  repair attempts → cache the final valid model by evidence hash.

**Acceptance:** for the benchmark app, the produced `app-model.json` validates,
and a human review confirms screens/components/flows are correctly identified
and at least the obvious edge states (empty primary collection, error) are
present. Re-running with unchanged evidence is a cache hit (proves determinism +
cost control).

---

### Phase 3 — Generator (AppModel → clean React)
**Goal:** deterministic schema → working, readable React project.

- `generate.py`: walk the validated `AppModel` and emit, via Jinja2 templates:
  - Vite + Tailwind + Router scaffold; `tailwind.config.js` themed from
    `designTokens`.
  - One data file per entity from `seed` (pure local data → P1).
  - One component per `component` (DRY, role-named → P8).
  - One route per screen; render the default variant, and expose **every edge
    variant behind a deterministic switch** (e.g. `?state=empty`) so agents can
    force any state (→ P5, P2).
  - Context stores for declared `mutateState` actions (e.g. cart).
- `testids.py`: inject the schema's `data-testid`s, interpolating `{id}` per
  entity instance for per-record addressability (→ P3).
- `manifest.py`: emit `manifest.json` — the agent-facing contract: screen graph,
  every selector, every flow, and the variant switches (→ P7).

**Acceptance:** `npm install && npm run build` on the generated output succeeds;
opening it shows the happy path; `?state=empty` (etc.) renders edge states;
`manifest.json` validates and lists every screen + testid.

---

### Phase 4 — Verifier (properties → executable tests)
**Goal:** turn P1–P8 into an automated scorecard; nothing ships red.

- `verify.py` runs, per generated mock:
  - **P1**: load built app, assert zero outbound network requests post-load.
  - **P2**: run an agent script twice; assert identical end DOM/state.
  - **P3**: assert every interactive node has a testid; diff testids across two
    builds for stability.
  - **P4**: `agent_walk.py` walks every `flow` in the manifest, asserting each
    step lands on `expectScreen` with no dead ends.
  - **P5**: assert every eligible screen renders its declared edge variants.
  - **P6**: structural diff (component tree) + optional visual diff vs. source
    screenshots → similarity score with a threshold.
  - **P7**: validate `manifest.json` against its schema; assert coverage.
  - **P8**: run eslint; assert clean.
- Emit `scorecard.json` + a human-readable report per run.

**Acceptance:** verifier produces a scorecard for the benchmark app with P1–P5,
P7, P8 green and P6 above threshold; a deliberately corrupted output makes the
relevant check go red (proves the checks have teeth).

---

### Phase 5 — Benchmark, polish, deploy
**Goal:** prove genericity, ship the demo.

- Run the full pipeline on **three structurally different apps**: the grocery
  benchmark + a blog + a dashboard. The contrast *proves* the generator has no
  category-specific logic baked in.
- Record each scorecard in `benchmarks/`.
- Pick the strongest output, deploy to **Vercel**, commit its source under
  `examples/`.
- Write `README.md`: lead with the property table + architecture diagram, then
  results (scorecards), then how-to-run, then honest limitations & future work
  (full visual fidelity, auth/destructive-action handling, snapshot/restore
  crawling for deeper flows).

**Acceptance:** a public Vercel URL of a generated mock; `README.md` lets a
stranger reproduce a run; three scorecards demonstrate breadth.

---

## 7. Cross-cutting concerns

- **Determinism budget (P2):** temperature 0, no `Math.random`/`Date.now` in
  generated runtime code, content-addressed LLM cache. Determinism is the
  product's core promise; treat any nondeterminism as a bug.
- **Cost control:** cache by evidence hash; batch per-screen reasoning; the
  expensive stage never re-runs on unchanged input. (For a tool whose buyers
  sell AI agents, inference cost is a managed line item, not a reason to avoid
  LLMs.)
- **Failure handling:** every stage degrades gracefully — a flaky element is
  skipped and logged, a blocked crawl falls back, a malformed model triggers
  bounded repair. The pipeline should never hard-crash on one bad input.
- **Observability:** each stage writes its artifacts to disk (`evidence/`,
  `app-model.json`, generated tree, `scorecard.json`) so any run is fully
  inspectable and debuggable — and so the LLM's decisions are auditable.

## 8. Explicit non-goals (scope honesty)

- **Pixel-perfect cloning.** Goal is *recognizable* fidelity (P6), not a
  byte-identical copy; a human made dozens of taste calls we don't replicate.
- **Auth / destructive flows.** Out of scope; clean hooks left
  (`--seed-cookies`, an action allow/deny list).
- **Arbitrarily deep flows.** We BFS within a budget; very deep multi-step
  state requires snapshot/restore crawling — noted as future work.

## 9. Suggested build order for Claude Code

Feed this file, then build **strictly in phase order**, pausing at each
acceptance gate. Start every phase by writing its acceptance test first, then
make it pass. Phase 2 (reasoning) is the highest-risk/highest-leverage stage —
get its prompt + validate⇄retry loop solid before investing in the generator
templates, because the generator is only as good as the AppModel it's fed.
