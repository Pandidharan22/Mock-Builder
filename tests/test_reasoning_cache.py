"""Tests for the reasoning cache key (Phase 2' Step 4b).

The cache is content-addressed over the reasoning INPUTS: ``state_hash`` plus a
hash of (system prompt, constructed user payload, pinned model name). A change to
any of those must yield a different key — a cache *miss* (a different file), never
a stale hit. The miss-on-change test is the regression that was silently broken.
"""

from __future__ import annotations

import asyncio
import copy
import json
import types
from pathlib import Path

import pytest

from mockbuilder.models import validate_app_model
from mockbuilder.reasoning import cache as C
from mockbuilder.reasoning import reason as R


# --------------------------------------------------------------------------- #
# Key-level unit tests (pure, no model, no I/O)
# --------------------------------------------------------------------------- #
def test_inputs_hash_deterministic():
    a = C._inputs_hash("sys", "usr", "model")
    b = C._inputs_hash("sys", "usr", "model")
    assert a == b
    assert len(a) == 16


def test_prompt_change_changes_key():
    assert C._inputs_hash("sys", "usr", "m") != C._inputs_hash("sys-CHANGED", "usr", "m")


def test_payload_change_changes_key():
    assert C._inputs_hash("sys", "usr", "m") != C._inputs_hash("sys", "usr-CHANGED", "m")


def test_model_change_changes_key():
    assert C._inputs_hash("sys", "usr", "m") != C._inputs_hash("sys", "usr", "m-CHANGED")


def test_delimiter_prevents_boundary_collision():
    # Without a separator, ('ab','c') and ('a','bc') would hash identically.
    assert C._inputs_hash("ab", "c", "m") != C._inputs_hash("a", "bc", "m")


def test_save_get_roundtrip_and_filename(tmp_path):
    model = {"marker": "A"}
    path = C.save_cached_model("STATE", model, "sys", "usr", "mod", tmp_path)
    assert path.name.startswith("STATE_")
    assert path.name.endswith("_model.json")
    assert C.get_cached_model("STATE", "sys", "usr", "mod", tmp_path) == model
    # Any input change -> different key -> miss (the old file is never looked up).
    assert C.get_cached_model("STATE", "sys2", "usr", "mod", tmp_path) is None
    assert C.get_cached_model("STATE", "sys", "usr2", "mod", tmp_path) is None
    assert C.get_cached_model("STATE", "sys", "usr", "mod2", tmp_path) is None


# --------------------------------------------------------------------------- #
# synthesize_model-level: HIT skips the model; MISS-on-change forces a fresh call
# --------------------------------------------------------------------------- #
# A minimal AppModel that passes validate_app_model + the graph-integrity gate.
_VALID_MODEL = {
    "meta": {
        "sourceUrl": "https://example.test/",
        "appName": "AppA",
        "appType": "other",
        "generatedAt": "2026-01-01T00:00:00+00:00",
        "modelVersion": "1.0.0",
    },
    "designTokens": {
        "colors": {
            "primary": "#ff6600",
            "background": "#ffffff",
            "surface": "#f6f6ef",
            "text": "#828282",
        },
        "typography": {"fontFamily": "Verdana"},
        "spacing": {},
        "radii": {},
    },
    "entities": [
        {
            "name": "story",
            "fields": [{"name": "title", "type": "string", "sourceRole": "title"}],
            "sourceCollection": 0,
        }
    ],
    "screens": [
        {
            "id": "home",
            "name": "Home",
            "route": "/",
            "variants": [{"id": "default", "kind": "default"}],
        }
    ],
    "components": [
        {
            "name": "StoryRow",
            "role": "row",
            "boundToEntity": "story",
            "interactiveElements": [
                {
                    "testId": "story-title",
                    "kind": "link",
                    "label": "{title}",
                    "action": {"type": "navigate", "targetScreen": "home"},
                }
            ],
        }
    ],
    "flows": [
        {
            "id": "main-flow",
            "name": "Main",
            "steps": [
                {"screen": "home", "testId": "story-title", "expectScreen": "home"}
            ],
        }
    ],
}


def test_valid_model_fixture_is_actually_valid():
    """Guard: the canned model must validate, else the cache tests prove nothing."""
    validate_app_model(_VALID_MODEL)


def _seed_evidence(tmp_path: Path, state_hash: str = "STATE") -> str:
    """Write the minimal evidence synthesize_model reads: a records.json (one
    collection, one record) and a design_tokens.json."""
    records = {
        "collections": [
            {
                "rank": 0,
                "score": 40,
                "signature": "X",
                "count": 1,
                "field_count": 2,
                "records": [
                    {
                        "index": 0,
                        "fields": [
                            {"tag": "a", "text": "Hello world headline", "role": "title"},
                            {"tag": "span", "text": "github.com", "role": "domain"},
                        ],
                    }
                ],
            }
        ]
    }
    (tmp_path / f"{state_hash}_records.json").write_text(
        json.dumps(records), encoding="utf-8"
    )
    (tmp_path / "design_tokens.json").write_text(
        json.dumps({"colors": {}}), encoding="utf-8"
    )
    return state_hash


def _fake_response(content: str):
    """Mimic the Groq chat-completion response shape reason.py reads."""
    message = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _ModelSpy:
    """Stands in for reason._create_completion: counts calls, returns a canned
    model, and captures the messages it was actually sent."""

    def __init__(self, model: dict):
        self.model = model
        self.calls = 0
        self.last_messages = None

    async def __call__(self, client, messages):
        self.calls += 1
        self.last_messages = messages
        return _fake_response(json.dumps(self.model))


@pytest.fixture()
def _no_real_client(monkeypatch):
    """Stub AsyncGroq so a cache MISS (which constructs the client) needs no key."""
    monkeypatch.setattr(R, "AsyncGroq", lambda *a, **k: object())


def test_cache_hit_skips_model_call(tmp_path, monkeypatch, _no_real_client):
    state_hash = _seed_evidence(tmp_path)
    spy = _ModelSpy(_VALID_MODEL)
    monkeypatch.setattr(R, "_create_completion", spy)

    first = asyncio.run(R.synthesize_model(tmp_path, state_hash))
    second = asyncio.run(R.synthesize_model(tmp_path, state_hash))

    # Second run served from cache: the model was called exactly once.
    assert spy.calls == 1
    assert first == second


def test_key_hashes_exactly_what_is_sent(tmp_path, monkeypatch, _no_real_client):
    """The saved filename's hash must be computed from the EXACT system/user
    content sent to the model — not an independently re-derived copy."""
    state_hash = _seed_evidence(tmp_path)
    spy = _ModelSpy(_VALID_MODEL)
    monkeypatch.setattr(R, "_create_completion", spy)

    asyncio.run(R.synthesize_model(tmp_path, state_hash))

    sent = spy.last_messages
    expected = C._inputs_hash(sent[0]["content"], sent[1]["content"], R.MODEL)
    saved = list(tmp_path.glob(f"{state_hash}_*_model.json"))
    assert len(saved) == 1
    assert saved[0].name == f"{state_hash}_{expected}_model.json"


def test_prompt_change_misses_stale_cache(tmp_path, monkeypatch, _no_real_client):
    """THE regression that was silently broken: change the prompt and the prior
    cached model must NOT be returned — a different key is a miss, a fresh call."""
    state_hash = _seed_evidence(tmp_path)

    model_a = copy.deepcopy(_VALID_MODEL)
    model_a["meta"]["appName"] = "AppA"
    spy_a = _ModelSpy(model_a)
    monkeypatch.setattr(R, "_create_completion", spy_a)
    first = asyncio.run(R.synthesize_model(tmp_path, state_hash))
    assert spy_a.calls == 1
    assert first["meta"]["appName"] == "AppA"

    # Change ONE word of the system prompt -> different key -> stale entry unhit.
    monkeypatch.setattr(R, "SYSTEM_PROMPT", R.SYSTEM_PROMPT + "\nEXTRA RULE.")
    model_b = copy.deepcopy(_VALID_MODEL)
    model_b["meta"]["appName"] = "AppB"
    spy_b = _ModelSpy(model_b)
    monkeypatch.setattr(R, "_create_completion", spy_b)
    second = asyncio.run(R.synthesize_model(tmp_path, state_hash))

    # The model was called again (fresh), and we got the NEW model — not the
    # stale cached one. If the key ignored the prompt, spy_b.calls would be 0.
    assert spy_b.calls == 1
    assert second["meta"]["appName"] == "AppB"
    assert second != first

    # Two distinct cache files now exist — one per prompt (nothing overwritten).
    assert len(list(tmp_path.glob(f"{state_hash}_*_model.json"))) == 2


def test_legacy_statehash_only_file_is_inert(tmp_path, monkeypatch, _no_real_client):
    """A pre-change ``{state_hash}_model.json`` (the Step-4 hazard, possibly
    seed-bearing) must never be looked up under the new key — proving the manual
    ``unlink`` workaround from Step 4 is no longer necessary."""
    state_hash = _seed_evidence(tmp_path)
    legacy = tmp_path / f"{state_hash}_model.json"
    legacy.write_text(
        json.dumps({"stale": True, "entities": [{"seed": [1]}]}), encoding="utf-8"
    )

    spy = _ModelSpy(_VALID_MODEL)
    monkeypatch.setattr(R, "_create_completion", spy)
    result = asyncio.run(R.synthesize_model(tmp_path, state_hash))

    assert spy.calls == 1  # fresh call — the legacy file was not a hit
    assert "stale" not in result
    assert legacy.exists()  # left inert: not read, not overwritten, not deleted


def test_payload_change_misses_stale_cache(tmp_path, monkeypatch, _no_real_client):
    """Same guarantee for the payload: change the records (hence the sample
    record sent) and the prior cached model must not be reused."""
    state_hash = _seed_evidence(tmp_path)

    spy_a = _ModelSpy(_VALID_MODEL)
    monkeypatch.setattr(R, "_create_completion", spy_a)
    asyncio.run(R.synthesize_model(tmp_path, state_hash))
    assert spy_a.calls == 1

    # Rewrite records.json so the constructed user payload differs. Keep a
    # `title` role (so _VALID_MODEL's sourceRole still resolves under the 6a
    # gate); only the text/count change, which is enough to change the cache key.
    changed = {
        "collections": [
            {
                "rank": 0,
                "score": 12,
                "signature": "Y",
                "count": 9,
                "field_count": 1,
                "records": [
                    {
                        "index": 0,
                        "fields": [{"tag": "a", "text": "A different headline", "role": "title"}],
                    }
                ],
            }
        ]
    }
    (tmp_path / f"{state_hash}_records.json").write_text(
        json.dumps(changed), encoding="utf-8"
    )
    spy_b = _ModelSpy(_VALID_MODEL)
    monkeypatch.setattr(R, "_create_completion", spy_b)
    asyncio.run(R.synthesize_model(tmp_path, state_hash))

    # A changed payload is a miss -> the model was called again.
    assert spy_b.calls == 1
    assert len(list(tmp_path.glob(f"{state_hash}_*_model.json"))) == 2
