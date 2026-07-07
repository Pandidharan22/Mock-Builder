"""LLM reasoning stage: crawl evidence -> validated AppModel.

Takes the evidence captured for one crawl state (the discovered actionable
elements plus harvested design tokens) and asks an LLM to synthesize an
``AppModel`` that conforms to ``app_model.schema.json``. The model's only job is
to fill the contract; the (deterministic) generator consumes it downstream.

Reasoning runs on **Groq** via the OpenAI-compatible chat-completions API. To fit
the free tier's token budget, the prompt is deliberately slim and TEXT-ONLY: the
elements are compressed (repeating rows -> a few samples + counts), the schema is
minified (descriptions stripped), and the base64 screenshot is dropped entirely.
This headroom lets us run a stronger model for better schema adherence.

Two guarantees wrap the non-deterministic call:
  * **Cache** — keyed by ``state_hash`` (see :mod:`.cache`); an unchanged crawl
    reuses the saved model instead of re-calling the LLM.
  * **Validate-retry** — every candidate is validated against the JSON Schema
    via ``validate_app_model`` *and* a referential-integrity gate
    (``verify_graph_integrity``, which the schema can't express); on failure the
    error is fed back to the model to fix, up to a small retry budget. Only a
    model that passes both gates is cached/returned.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
from pathlib import Path
from typing import Any

import groq
import jsonschema
from groq import AsyncGroq

from ..models import SCHEMA_PATH, validate_app_model
from .cache import get_cached_model, save_cached_model
from .prompts import SYSTEM_PROMPT

# Groq-hosted reasoning model. Now that the prompt is TEXT-ONLY (we dropped the
# screenshot), we no longer need a *vision* model — so we use Groq's strongest
# general text model, Llama 3.3 70B. It is far stronger than the 17B Scout on
# strict-JSON instruction-following, and (unlike Qwen 3) is NOT a reasoning model,
# so it emits clean JSON with no <think> preamble and no token-hungry reasoning
# that blew the 8000 TPM budget. (Fallbacks: "meta-llama/llama-4-scout-17b-16e-instruct".)
MODEL = "llama-3.3-70b-versatile"

# Max attempts through the validate-retry loop. Kept generous so the model has
# room to resolve several independent schema/integrity errors sequentially
# without failing the build (avoids the "whack-a-mole" convergence trap).
MAX_RETRIES = 5


def _minify_schema(node: Any) -> Any:
    """Recursively strip human-facing ``description`` keys from the schema.

    Descriptions are ~2/3 of the file but carry no machine constraint; dropping
    them leaves types/enums/required/patterns intact.
    """
    if isinstance(node, dict):
        return {k: _minify_schema(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_minify_schema(x) for x in node]
    return node


# Dense, description-free rendering of the AppModel schema, injected so the model
# targets the exact contract. Minify + compact separators cut it from ~15.7KB to
# a few KB while preserving every structural constraint.
MINIFIED_SCHEMA = json.dumps(
    _minify_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))),
    separators=(",", ":"),
)

# --- Elements compression -------------------------------------------------
_NTH_OF_TYPE = re.compile(r":nth-of-type\(\d+\)")
_NUMBER = re.compile(r"\b\d+\b")


def _selector_signature(selector: str) -> str:
    """Collapse a CSS selector to a structural signature so repeated rows
    (differing only by ``:nth-of-type`` / numeric ids) map to the same key."""
    sig = _NTH_OF_TYPE.sub(":nth-of-type(*)", selector or "")
    return _NUMBER.sub("#", sig)


def compress_elements(elements: list) -> list:
    """Deduplicate repeating structural patterns in the discovered elements.

    Groups elements by (tag + normalized-selector) signature. The first 3 of each
    group are kept intact (enough to model a story/comment row as seed data); the
    rest collapse into one summary entry carrying the count. This slashes the
    elements payload (e.g. Hacker News ~34KB -> <4KB) so it fits the token budget
    while preserving both representative samples and the full picture (counts).
    """
    groups: dict[str, list] = {}
    order: list[str] = []
    for el in elements:
        sig = el.get("tag", "") + "|" + _selector_signature(el.get("selector", ""))
        if sig not in groups:
            groups[sig] = []
            order.append(sig)
        groups[sig].append(el)

    compressed: list = []
    for sig in order:
        members = groups[sig]
        compressed.extend(members[:3])
        extra = len(members) - 3
        if extra > 0:
            sample = members[0]
            compressed.append(
                {
                    "tag": sample.get("tag"),
                    "text": f"[+{extra} more similar '{sample.get('tag')}' elements]",
                    "selector": _selector_signature(sample.get("selector", "")),
                    "repeatedCount": len(members),
                }
            )
    return compressed


def _strip_json_fences(text: str) -> str:
    """Extract the JSON payload from a model reply.

    Reasoning models (Qwen 3) emit a ``<think>...</think>`` preamble before the
    answer; strip it, then remove any Markdown code fences.
    """
    # Drop a complete <think>...</think> block, or everything up to a lone
    # closing </think> (truncated/partial reasoning).
    text = re.sub(r"(?is)<think>.*?</think>", "", text)
    text = re.sub(r"(?is)^.*?</think>", "", text)

    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the closing fence.
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    return stripped.strip()


async def _create_completion(client: AsyncGroq, messages: list) -> Any:
    """Call the model, waiting out the per-minute token window on a rate limit.

    The free tier's 8000 TPM ceiling is tight enough that a retry within the same
    minute can trip it (413/429). On a rate-limit status we pause for the window
    to reset and retry the same request rather than failing the build.
    """
    for attempt in range(3):
        try:
            return await client.chat.completions.create(
                model=MODEL, messages=messages, temperature=0
            )
        except groq.APIStatusError as exc:
            if getattr(exc, "status_code", None) in (413, 429) and attempt < 2:
                print(
                    "  [reasoning] token-per-minute limit hit; "
                    "waiting 60s for the window to reset ..."
                )
                await asyncio.sleep(60)
                continue
            raise
    raise RuntimeError("unreachable")


def _format_error(exc: Exception) -> str:
    """Render a validation/parse failure as a short, targeted hint for the model.

    ``str(jsonschema.ValidationError)`` embeds the entire schema and instance,
    which is huge and unhelpful to feed back; use the location + message only.
    Note ``json.JSONDecodeError`` subclasses ``ValueError``, so it is checked
    first — the plain ``ValueError`` branch carries our graph-integrity errors.
    """
    if isinstance(exc, jsonschema.ValidationError):
        location = exc.json_path  # e.g. "$.screens" or "$" for the root
        hint = f"At {location}: {exc.message}"
        # Regex-pattern violations are cryptic; add a plain-English fix so the
        # model stops thrashing on identifier casing (a recurring failure).
        if exc.validator == "pattern" and isinstance(exc.instance, str):
            val = exc.instance
            if "_" in val or " " in val or val != val.lower():
                hint += (
                    " (identifiers must be camelCase — no underscores, spaces, "
                    "or leading capitals; e.g. 'commentCount' not 'comment_count')"
                )
        return hint
    if isinstance(exc, json.JSONDecodeError):
        return f"Invalid JSON: {exc}"
    if isinstance(exc, ValueError):
        return str(exc)  # concatenated graph-integrity violations
    return f"{exc}"


def _testid_matches(flow_testid: str, component_testids: set[str]) -> bool:
    """True if a flow-step ``testId`` maps to a declared component element.

    Component testIds may carry a ``{id}`` placeholder for per-instance elements
    (e.g. ``story-link-{id}``). A flow step may reference either the literal
    placeholder form or a concrete resolution of it (``story-link-5``), so match
    exact strings first, then treat ``{id}`` as a single-segment wildcard.
    """
    if flow_testid in component_testids:
        return True
    for pattern in component_testids:
        if "{id}" in pattern:
            regex = "^" + re.escape(pattern).replace(re.escape("{id}"), r"[A-Za-z0-9_-]+") + "$"
            if re.fullmatch(regex, flow_testid):
                return True
    return False


def verify_graph_integrity(model: dict) -> list[str]:
    """Return a list of referential-integrity violations in the AppModel graph.

    The JSON Schema enforces *shape* but not *cross-references*: a flow can point
    at a screen id or a ``testId`` that was never defined. This walks every flow
    step and confirms each ``expectScreen`` resolves to a declared screen and each
    ``testId`` maps to a declared component interactive element. Empty list = clean.
    """
    violations: list[str] = []

    screen_ids = {s.get("id") for s in model.get("screens", [])}
    component_names = {c.get("name") for c in model.get("components", [])}
    component_testids = {
        el.get("testId")
        for comp in model.get("components", [])
        for el in comp.get("interactiveElements", [])
        if el.get("testId")
    }

    # A screen layout may only place components that are actually defined — the
    # generator imports `../components/{name}`, so an undefined name breaks the
    # build (`Could not resolve "../components/Navbar"`).
    for screen in model.get("screens", []):
        layout = screen.get("layout") or {}
        for region in layout.get("regions", []):
            for comp_name in region.get("components", []):
                if comp_name not in component_names:
                    violations.append(
                        f"Screen '{screen.get('id')}' region references an "
                        f"undefined component: '{comp_name}'."
                    )

    for flow in model.get("flows", []):
        flow_id = flow.get("id", "<unknown>")
        for step in flow.get("steps", []):
            expect_screen = step.get("expectScreen")
            if expect_screen is not None and expect_screen not in screen_ids:
                violations.append(
                    f"Flow '{flow_id}' step references an undefined screen: "
                    f"'{expect_screen}'."
                )
            test_id = step.get("testId")
            if test_id is not None and not _testid_matches(test_id, component_testids):
                violations.append(
                    f"Flow '{flow_id}' step references an undefined testId: "
                    f"'{test_id}'."
                )

    # A component element that navigates must target a screen that exists — a
    # nav to an undeclared screen is a dead click in the generated harness.
    for comp in model.get("components", []):
        comp_name = comp.get("name", "<unknown>")
        for el in comp.get("interactiveElements", []):
            action = el.get("action") or {}
            if action.get("type") in ("navigate", "navigateAndMutate"):
                target = action.get("targetScreen")
                if target is not None and target not in screen_ids:
                    violations.append(
                        f"Component '{comp_name}' element '{el.get('testId')}' "
                        f"navigates to an undefined screen: '{target}'."
                    )

    return violations


async def synthesize_model(evidence_dir: Path, state_hash: str) -> dict[str, Any]:
    """Synthesize (or load from cache) a validated AppModel for one crawl state.

    Reads ``{state_hash}_elements.json`` and ``{state_hash}.png`` from
    ``evidence_dir``, calls the LLM, validates the output against the AppModel
    schema, retries on validation failure, then caches and returns the result.
    """
    evidence_dir = Path(evidence_dir)

    # 1. Cache hit short-circuits the whole (expensive, non-deterministic) call.
    cached = get_cached_model(state_hash, evidence_dir)
    if cached is not None:
        return cached

    # 2. Load + compress the evidence for this state. Grouping repeating rows to
    #    3 samples + a count shrinks the elements payload ~10x so the request fits
    #    the free-tier token budget (and a stronger model).
    elements_path = evidence_dir / f"{state_hash}_elements.json"
    raw_elements = json.loads(elements_path.read_text(encoding="utf-8"))
    elements_json = json.dumps(compress_elements(raw_elements), separators=(",", ":"))

    # Design tokens are harvested by the crawler into a single shared file. Feed
    # them to the model so it maps *extracted* colors/fonts to semantic roles
    # rather than inventing them (the schema says tokens are extracted, not made up).
    tokens_path = evidence_dir / "design_tokens.json"
    tokens_json = (
        tokens_path.read_text(encoding="utf-8") if tokens_path.exists() else "{}"
    )

    # 3. Build a TEXT-ONLY prompt. The compressed elements + design tokens carry
    #    the structural and stylistic facts, so we drop the heavy base64
    #    screenshot entirely — the single biggest token cost.
    client = AsyncGroq()  # picks up GROQ_API_KEY from the environment
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": (
                "Your output MUST validate against this exact AppModel JSON "
                "Schema (note: `entities`, `components`, `screens`, and `flows` "
                "are ARRAYS; the top-level object requires `meta`, `designTokens`, "
                "`entities`, `screens`, `flows`; no extra top-level keys are "
                "allowed):\n\n"
                f"{MINIFIED_SCHEMA}\n\n"
                f"Here is the (compressed) elements JSON:\n{elements_json}\n\n"
                f"Here are the extracted design tokens:\n{tokens_json}\n\n"
                "Return ONLY valid AppModel JSON."
            ),
        },
    ]

    # 4. Validate-retry loop. Groq supports temperature=0 for determinism.
    #    `base_messages` is the fixed [system, user] prefix; on each failure we
    #    rebuild a BOUNDED conversation (prefix + latest attempt + error) so the
    #    request never grows past the tight per-minute token budget.
    base_messages = list(messages)
    last_error: Exception | None = None
    for _ in range(MAX_RETRIES):
        response = await _create_completion(client, messages)
        raw_text = response.choices[0].message.content or ""

        try:
            candidate = json.loads(_strip_json_fences(raw_text))
            # Never trust the model for the timestamp — stamp it programmatically
            # for strict determinism/provenance. (Guarded so malformed output
            # still flows to the validator and the retry loop.)
            if isinstance(candidate, dict) and isinstance(candidate.get("meta"), dict):
                candidate["meta"]["generatedAt"] = datetime.datetime.now(
                    datetime.UTC
                ).isoformat()
            validate_app_model(candidate)  # raises on schema violation

            # Referential-integrity gate: the schema can't enforce cross-refs, so
            # reject dangling flow -> screen / flow -> testId references here. This
            # turns a graph invariant into a hard, deterministic compilation gate.
            violations = verify_graph_integrity(candidate)
            if violations:
                raise ValueError("\n".join(violations))
        except (json.JSONDecodeError, jsonschema.ValidationError, ValueError) as exc:
            last_error = exc
            hint = _format_error(exc)
            print(f"  [reasoning] candidate rejected:\n    {hint}\n  retrying ...")
            # Rebuild a bounded conversation: fixed prefix + only the latest
            # (think-stripped) attempt + the error. This keeps every retry request
            # roughly the same size instead of accumulating past the TPM ceiling.
            correction = (
                "That output failed validation:\n"
                f"{hint}\n\n"
                "Fix ONLY what the error points to, keep the rest, and return the "
                "corrected AppModel as raw JSON only — no code fences, no prose."
            )
            messages = base_messages + [
                {"role": "assistant", "content": _strip_json_fences(raw_text)},
                {"role": "user", "content": correction},
            ]
            continue

        # 5. Success: cache and return.
        save_cached_model(state_hash, candidate, evidence_dir)
        return candidate

    raise RuntimeError(
        f"Failed to synthesize a valid AppModel for state {state_hash} "
        f"after {MAX_RETRIES} attempts."
    ) from last_error
