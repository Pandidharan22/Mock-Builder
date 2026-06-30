# MockBuilder Development Journal

## Phase 0: Foundations

### Date: 2026-06-30
* **Objective:** Initialize repository, define environments, and establish the AppModel schema contract.
* **Implementation Notes:**
  >
* **Challenges & Solutions:**
  >
* **Verification:**
  >

### Date: 2026-06-30
* **Objective:** Establish the AppModel schema contract in code: create the `mockbuilder` package, mirror the JSON Schema as Python dataclasses, and implement the validation gate.
* **Implementation Notes:**
  > Created the `mockbuilder/` package (`__init__.py`, `models.py`, `cli.py`).
  > `models.py` defines stdlib dataclasses mirroring every schema definition — `Meta`, `DesignTokens` (with `Typography`), `Entity` (with `EntityField`), `Component` (with `Prop`, `InteractiveElement`, `Action`, `Mutation`), `Screen` (with `Region`, `Variant`), `Flow` (with `FlowStep`), and the root `AppModel`. Each carries a `from_dict` classmethod for building typed objects from a validated payload.
  > `validate_app_model(data: dict) -> None` loads `app_model.schema.json` from disk and validates the dict with `jsonschema.validate`, raising `ValidationError` on the first violation.
  > `cli.py` uses `argparse` to expose `build <url> -o <out_dir>`, wired to a `run_build` stub that prints `Pipeline stubbed for <url> -> <out_dir>`.
* **Challenges & Solutions:**
  > The schema allows `additionalProperties` on `colors`, `spacing`, `radii`, and `shadows`. Stdlib dataclasses don't model open-ended key sets cleanly, so these are kept as plain `dict[str, str]` fields rather than forcing a rigid dataclass — fidelity to the contract over rigidity in the mirror.
  > **Architectural decision:** `app_model.schema.json` is the *single source of truth*. The dataclasses are a typed convenience mirror for the Python pipeline and are deliberately NOT used for validation — all contract checking goes through the JSON Schema via `validate_app_model`. This keeps one authoritative definition and avoids a second, drifting source of truth (and is why we chose stdlib dataclasses over Pydantic).
* **Verification:**
  > Imported `mockbuilder.models` and ran `validate_app_model` against a minimal payload (catches schema-violating dicts via `ValidationError`); exercised the CLI with `python -m mockbuilder.cli build <url> -o out` to confirm the stub prints as expected.

## Phase 1: Evidence Capture

### Date: 2026-06-30
* **Objective:** Stand up the crawler + recorder: a headless Playwright browser that visits a URL, captures a denoised structural snapshot of the DOM, derives a deterministic `state_hash`, and persists evidence (screenshot + actionable elements) to `evidence/`.
* **Implementation Notes:**
  > Created the virtualenv (`python -m venv .venv`), `pip install -e .`, and `playwright install chromium`.
  > `mockbuilder/crawler/dom.py` holds two browser-side captures run via `page.evaluate()`:
  >   - **DOM normalization** — clones `document.body`, removes all text nodes plus `<script>`/`<style>`/`<iframe>`/`<svg>` tags, and strips every attribute except `class`, `role`, and `data-testid`, returning the cleaned `outerHTML`.
  >   - **Element discovery** — queries `a, button, input, select, [role="button"], [role="tab"]` and returns `{tag, text, selector}` per element, where `selector` prefers `id`/`data-testid` and otherwise builds an `nth-of-type` path; `text` falls back through `innerText` → `value` → `aria-label`.
  > `mockbuilder/crawler/crawler.py` defines `async class Crawler` with `crawl(url, max_states=1)`: launches headless Chromium, `goto(url, wait_until="networkidle")`, runs both captures, hashes the normalized DOM with `hashlib.sha256().hexdigest()`, creates `evidence/` at the project root, and writes `evidence/{state_hash}.png` (full-page) and `evidence/{state_hash}_elements.json`. `max_states` is the seam for multi-state crawling later; only the landing state is captured now.
  > `cli.py`'s `build` command now calls `asyncio.run(Crawler().crawl(url))` instead of the stub. Added `evidence/` to `.gitignore`.
* **Challenges & Solutions:**
  > **Why hash the *structural* DOM?** Live apps are full of volatile noise — timestamps, ad/tracking markup, randomized class hashes, injected `<script>`/`<svg>`, A/B copy, per-request ids. If we hashed raw HTML, every reload would look like a brand-new state and the crawl graph would explode with near-duplicates. By first normalizing (drop text + noise tags, keep only the structurally meaningful attributes `class`/`role`/`data-testid`) and *then* hashing, two renders that are the *same screen* collapse to the *same* `state_hash`, while a genuinely different layout produces a different one. This makes the state hash the natural unique key (de-dupe, screenshot/elements filenames) and is what will keep multi-state crawling convergent rather than infinite.
  > The sandbox has no outbound network (`ERR_NAME_NOT_RESOLVED`), so end-to-end verification used a local `file://` HTML fixture instead of a public URL.
* **Verification:**
  > Crawled a local fixture containing noise (`<script>`, `<style>`, `<svg>`) and six actionable elements. Result: a stable 64-char `state_hash`, a full-page PNG, and an elements JSON with exactly 6 entries — `data-testid` selector used for the tagged button, `nth-of-type` paths for siblings, and the `aria-label` fallback correctly capturing the search input's label.
