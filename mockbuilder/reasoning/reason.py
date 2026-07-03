"""LLM reasoning stage: crawl evidence -> validated AppModel.

Takes the evidence captured for one crawl state (a full-page screenshot plus the
discovered actionable elements) and asks Claude to synthesize an ``AppModel`` that
conforms to ``app_model.schema.json``. The model's only job is to fill the
contract; the (deterministic) generator consumes it downstream.

Two guarantees wrap the non-deterministic call:
  * **Cache** — keyed by ``state_hash`` (see :mod:`.cache`); an unchanged crawl
    reuses the saved model instead of re-calling the LLM.
  * **Validate-retry** — every candidate is validated against the JSON Schema
    via ``validate_app_model``; on failure the error is fed back to the model to
    fix, up to a small retry budget. Only a schema-valid model is cached/returned.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import anthropic
import jsonschema

from ..models import validate_app_model
from .cache import get_cached_model, save_cached_model
from .prompts import SYSTEM_PROMPT

# The requested model, `claude-3-5-sonnet-20240620`, is retired (returns 404).
# `claude-sonnet-5` is its documented drop-in replacement — Sonnet-tier, current.
MODEL = "claude-sonnet-5"

# Generous output budget: an AppModel carries seed data for several entities.
# Streaming avoids SDK HTTP timeouts at this size.
MAX_TOKENS = 32000

# Max attempts through the validate-retry loop.
MAX_RETRIES = 3

_INSTRUCTIONS = (
    "You are given a screenshot of a web app screen and a JSON list of the "
    "actionable elements discovered on it. Produce a single AppModel JSON "
    "document that captures this app's structure, per the AppModel contract.\n\n"
    "Discovered elements:\n{elements}\n\n"
    "Respond with ONLY the raw AppModel JSON object. Do not wrap it in Markdown "
    "code fences, and do not include any prose before or after it."
)


def _extract_text(response: "anthropic.types.Message") -> str:
    """Concatenate the text blocks of a response (ignoring thinking blocks)."""
    return "".join(block.text for block in response.content if block.type == "text")


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

    elements = json.loads(elements_path.read_text(encoding="utf-8"))
    image_b64 = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")

    # 3. Build the initial message: screenshot + elements + instructions.
    # NOTE: no `temperature` — sampling params are rejected on this model, and
    # the state_hash cache is the real determinism guarantee for the pipeline.
    client = anthropic.AsyncAnthropic()
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    },
                },
                {
                    "type": "text",
                    "text": _INSTRUCTIONS.format(
                        elements=json.dumps(elements, indent=2)
                    ),
                },
            ],
        }
    ]

    # 4. Validate-retry loop.
    last_error: Exception | None = None
    for _ in range(MAX_RETRIES):
        async with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
        ) as stream:
            response = await stream.get_final_message()

        raw_text = _extract_text(response)
        try:
            candidate = json.loads(_strip_json_fences(raw_text))
            validate_app_model(candidate)  # raises on schema violation
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last_error = exc
            # Feed the failure back to the model and ask it to fix the JSON.
            messages.append({"role": "assistant", "content": raw_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That output failed validation with the following error:\n"
                        f"{exc}\n\n"
                        "Return the corrected AppModel as raw JSON only — no code "
                        "fences, no prose."
                    ),
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
