"""Disk cache for synthesized AppModels.

The reasoning stage is the one non-deterministic step in the pipeline (an LLM
call). Caching its output keyed by the crawl's ``state_hash`` restores
determinism and controls cost: the same normalized DOM always maps to the same
model file, so re-running the pipeline on an unchanged crawl reuses the cached
AppModel instead of paying for another LLM call.

Cache entries live alongside the rest of the evidence at
``evidence/{state_hash}_model.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Project root is three levels above this file:
# <root>/mockbuilder/reasoning/cache.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = PROJECT_ROOT / "evidence"


def _model_path(state_hash: str, evidence_dir: Path | str = EVIDENCE_DIR) -> Path:
    return Path(evidence_dir) / f"{state_hash}_model.json"


def get_cached_model(
    state_hash: str, evidence_dir: Path | str = EVIDENCE_DIR
) -> dict[str, Any] | None:
    """Return the cached AppModel dict for ``state_hash``, or ``None`` if absent."""
    path = _model_path(state_hash, evidence_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_cached_model(
    state_hash: str,
    model_dict: dict[str, Any],
    evidence_dir: Path | str = EVIDENCE_DIR,
) -> Path:
    """Persist ``model_dict`` as the cached AppModel for ``state_hash``."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = _model_path(state_hash, evidence_dir)
    path.write_text(
        json.dumps(model_dict, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path
