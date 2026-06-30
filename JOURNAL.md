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
