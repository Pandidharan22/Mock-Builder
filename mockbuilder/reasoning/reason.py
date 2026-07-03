"""LLM reasoning stage: crawl evidence -> validated AppModel.

Takes the evidence captured for one crawl state (a full-page screenshot plus the
discovered actionable elements) and asks a vision LLM to synthesize an
``AppModel`` that conforms to ``app_model.schema.json``. The model's only job is
to fill the contract; the (deterministic) generator consumes it downstream.

Reasoning runs on **Groq** (Llama 4 Scout, multimodal) via the OpenAI-compatible
chat-completions API — an open-weights, low-cost, high-throughput alternative to
proprietary vision APIs. The full JSON Schema is injected into the prompt so the
model targets the exact contract instead of inferring it from prose.

Two guarantees wrap the non-deterministic call:
  * **Cache** — keyed by ``state_hash`` (see :mod:`.cache`); an unchanged crawl
    reuses the saved model instead of re-calling the LLM.
  * **Validate-retry** — every candidate is validated against the JSON Schema
    via ``validate_app_model``; on failure the error is fed back to the model to
    fix, up to a small retry budget. Only a schema-valid model is cached/returned.
"""

from __future__ import annotations

import base64
import datetime
import json
from pathlib import Path
from typing import Any

import jsonschema
from groq import AsyncGroq

from ..models import SCHEMA_PATH, validate_app_model
from .cache import get_cached_model, save_cached_model
from .prompts import SYSTEM_PROMPT

# Groq-hosted Llama 4 Scout (multimodal): the current open-weights Llama vision
# model, succeeding the decommissioned llama-3.2-*-vision-preview ids. Cost-
# effective and high-throughput; retargeting is a one-line change here.
# (Groq alternative with vision: "qwen/qwen3.6-27b".)
MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# Max attempts through the validate-retry loop.
MAX_RETRIES = 3

# The actual JSON Schema, injected into the prompt so the model targets the exact
# contract (key names, arrays-vs-objects, required fields) rather than guessing
# from prose. Smaller open models can't infer the shape without seeing it.
SCHEMA_TEXT = SCHEMA_PATH.read_text(encoding="utf-8")


def _strip_json_fences(text: str) -> str:
    """Best-effort removal of Markdown code fences around a JSON payload."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the closing fence.
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    return stripped.strip()


def _format_error(exc: Exception) -> str:
    """Render a validation/parse failure as a short, targeted hint for the model.

    ``str(jsonschema.ValidationError)`` embeds the entire schema and instance,
    which is huge and unhelpful to feed back; use the location + message only.
    """
    if isinstance(exc, jsonschema.ValidationError):
        location = exc.json_path  # e.g. "$.screens" or "$" for the root
        return f"At {location}: {exc.message}"
    return f"Invalid JSON: {exc}"


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

    # 2. Load the evidence for this state.
    elements_path = evidence_dir / f"{state_hash}_elements.json"
    image_path = evidence_dir / f"{state_hash}.png"

    elements_json = elements_path.read_text(encoding="utf-8")
    base64_image = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")

    # Design tokens are harvested by the crawler into a single shared file. Feed
    # them to the model so it maps *extracted* colors/fonts to semantic roles
    # rather than inventing them (the schema says tokens are extracted, not made up).
    tokens_path = evidence_dir / "design_tokens.json"
    tokens_json = (
        tokens_path.read_text(encoding="utf-8") if tokens_path.exists() else "{}"
    )

    # 3. Build the initial message (OpenAI vision format used by Groq):
    #    a system prompt plus a user turn carrying the elements text and the
    #    screenshot as an inline base64 data URL.
    client = AsyncGroq()  # picks up GROQ_API_KEY from the environment
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Your output MUST validate against this exact AppModel "
                        "JSON Schema (note: `entities`, `components`, `screens`, "
                        "and `flows` are ARRAYS; the top-level object requires "
                        "`meta`, `designTokens`, `entities`, `screens`, `flows`; "
                        "no extra top-level keys are allowed):\n\n"
                        f"{SCHEMA_TEXT}\n\n"
                        f"Here is the elements JSON:\n{elements_json}\n\n"
                        f"Here are the extracted design tokens:\n{tokens_json}\n\n"
                        "Return ONLY valid AppModel JSON."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64_image}"},
                },
            ],
        },
    ]

    # 4. Validate-retry loop. Groq supports temperature=0 for determinism.
    last_error: Exception | None = None
    for _ in range(MAX_RETRIES):
        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0,
        )
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
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last_error = exc
            # Feed the failure back to the model and ask it to fix the JSON.
            messages.append({"role": "assistant", "content": raw_text})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "That output failed schema validation:\n"
                                f"{_format_error(exc)}\n\n"
                                "Fix ONLY what the error points to, keep the rest, "
                                "and return the corrected AppModel as raw JSON only "
                                "— no code fences, no prose."
                            ),
                        }
                    ],
                }
            )
            continue

        # 5. Success: cache and return.
        save_cached_model(state_hash, candidate, evidence_dir)
        return candidate

    raise RuntimeError(
        f"Failed to synthesize a valid AppModel for state {state_hash} "
        f"after {MAX_RETRIES} attempts."
    ) from last_error
