"""Disk cache for synthesized AppModels.

The reasoning stage is the one non-deterministic step in the pipeline (an LLM
call). Caching its output restores determinism and controls cost: identical
inputs always map to the same model file, so re-running on unchanged inputs
reuses the cached AppModel instead of paying for another LLM call.

The cache key is **content-addressed over everything that determines the output**:
the crawl ``state_hash`` *and* a short hash of the exact reasoning inputs — the
system prompt, the constructed user payload, and the pinned model name. Changing
any of those yields a different key, so a prompt/payload/model change is a cache
*miss* (a different file) rather than a stale hit. Keeping ``state_hash`` in the
filename keeps entries greppable per crawl state.

Cache entries live alongside the rest of the evidence at
``evidence/{state_hash}_{inputs_hash}_model.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Project root is three levels above this file:
# <root>/mockbuilder/reasoning/cache.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = PROJECT_ROOT / "evidence"


def _inputs_hash(system_prompt: str, user_payload: str, model_name: str) -> str:
    """A short, stable hash of the exact reasoning inputs.

    Any change to the system prompt, the constructed user payload, or the pinned
    model name changes this hash — and therefore the cache key — so stale
    pre-change entries are simply never looked up. A NUL separator makes the
    concatenation unambiguous (so 'ab'+'c' and 'a'+'bc' can't collide). No
    timestamps or randomness: identical inputs → identical hash.
    """
    digest = hashlib.sha256()
    digest.update(system_prompt.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(user_payload.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(model_name.encode("utf-8"))
    return digest.hexdigest()[:16]


def _model_path(
    state_hash: str,
    inputs_hash: str,
    evidence_dir: Path | str = EVIDENCE_DIR,
) -> Path:
    return Path(evidence_dir) / f"{state_hash}_{inputs_hash}_model.json"


def get_cached_model(
    state_hash: str,
    system_prompt: str,
    user_payload: str,
    model_name: str,
    evidence_dir: Path | str = EVIDENCE_DIR,
) -> dict[str, Any] | None:
    """Return the cached AppModel for these exact inputs, or ``None`` if absent.

    The key hashes ``(system_prompt, user_payload, model_name)`` so a change to
    any of them is a miss (a different file), never a stale hit.
    """
    path = _model_path(
        state_hash, _inputs_hash(system_prompt, user_payload, model_name), evidence_dir
    )
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_cached_model(
    state_hash: str,
    model_dict: dict[str, Any],
    system_prompt: str,
    user_payload: str,
    model_name: str,
    evidence_dir: Path | str = EVIDENCE_DIR,
) -> Path:
    """Persist ``model_dict`` under the inputs-addressed key for these inputs."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = _model_path(
        state_hash, _inputs_hash(system_prompt, user_payload, model_name), evidence_dir
    )
    path.write_text(
        json.dumps(model_dict, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path
