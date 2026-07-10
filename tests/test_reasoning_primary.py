"""Offline tests for Phase 2' Step 5 — primary-collection selection.

The live "which collection did the model pick" gate is exercised as a verification
run against the multi_collection fixture and a synthetic override payload (both
need the LLM and are reported separately). These offline tests lock the
deterministic contract: the payload now carries `count` (and only count), the
schema requires `sourceCollection` on every entity, the zero-collection case
validates with no entity, and the Step-4 seed prohibition still holds.
"""

from __future__ import annotations

import copy

import jsonschema
import pytest

from mockbuilder.models import validate_app_model
from mockbuilder.reasoning.reason import build_sample_collections


# --------------------------------------------------------------------------- #
# Payload: count is forwarded; score / field_count are not
# --------------------------------------------------------------------------- #
_RECORDS = {
    "collections": [
        {
            "rank": 0,
            "score": 24,
            "count": 6,
            "field_count": 24,
            "records": [
                {
                    "index": 0,
                    "fields": [
                        {"tag": "img", "text": "[img]", "role": "image"},
                        {"tag": "h3", "text": "Amul Milk", "role": "title"},
                        {"tag": "span", "text": "Rs52", "role": "price"},
                    ],
                }
            ],
        },
        {
            "rank": 1,
            "score": 7,
            "count": 7,
            "field_count": 7,
            "records": [
                {"index": 0, "fields": [{"tag": "span", "text": "Fruits", "role": "title"}]}
            ],
        },
    ]
}


def test_payload_forwards_count_only():
    out = build_sample_collections(_RECORDS)
    assert [c["collection"] for c in out] == [0, 1]  # order preserved (rank asc)
    assert [c["count"] for c in out] == [6, 7]
    for c in out:
        assert set(c.keys()) == {"collection", "count", "fields"}
        assert "score" not in c
        assert "field_count" not in c
        for f in c["fields"]:
            assert set(f.keys()) == {"role", "text"}  # still no seed/extra data


def test_payload_empty_for_zero_collections():
    assert build_sample_collections({"collections": []}) == []


# --------------------------------------------------------------------------- #
# Schema: sourceCollection required; seed still forbidden; empty entities ok
# --------------------------------------------------------------------------- #
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
            "name": "product",
            "fields": [{"name": "title", "type": "string"}],
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
    "flows": [{"id": "main-flow", "name": "Main", "steps": [
        {"screen": "home", "testId": "x", "expectScreen": "home"}]}],
    "components": [
        {
            "name": "Row",
            "role": "row",
            "interactiveElements": [
                {"testId": "x", "kind": "link", "label": "go",
                 "action": {"type": "navigate", "targetScreen": "home"}}
            ],
        }
    ],
}


def test_valid_model_with_source_collection_validates():
    validate_app_model(_VALID_MODEL)


def test_entity_without_source_collection_is_rejected():
    """sourceCollection is now required on every entity the model emits."""
    bad = copy.deepcopy(_VALID_MODEL)
    del bad["entities"][0]["sourceCollection"]
    with pytest.raises(jsonschema.ValidationError) as exc:
        validate_app_model(bad)
    assert "sourceCollection" in exc.value.message


def test_seed_key_still_rejected():
    """Step-4 negative guard must still hold: a seed key fails validation."""
    bad = copy.deepcopy(_VALID_MODEL)
    bad["entities"][0]["seed"] = [{"title": "x"}]
    with pytest.raises(jsonschema.ValidationError):
        validate_app_model(bad)


def test_source_collection_must_be_nonnegative_integer():
    bad = copy.deepcopy(_VALID_MODEL)
    bad["entities"][0]["sourceCollection"] = -1
    with pytest.raises(jsonschema.ValidationError):
        validate_app_model(bad)


def test_zero_collection_empty_entities_validates():
    """The zero-collection shell: an entity-less model must validate (no data
    entity invented)."""
    shell = copy.deepcopy(_VALID_MODEL)
    shell["entities"] = []
    validate_app_model(shell)
