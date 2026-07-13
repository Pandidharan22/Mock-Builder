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
from mockbuilder.reasoning.reason import build_sample_collections, verify_source_roles


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
# Step 5c: representative-record selection (most-complete, deterministic, no union)
# --------------------------------------------------------------------------- #
def _one_collection(records: list[dict]) -> dict:
    return {"collections": [{"rank": 0, "count": len(records), "records": records}]}


def test_representative_is_most_complete_not_first():
    """records[0] is impoverished; a later record is complete -> sample the
    complete one, so the entity shape covers every field the collection has."""
    records = [
        {"index": 0, "fields": [{"tag": "a", "text": "x", "role": "meta"}]},  # 1 role
        {
            "index": 1,
            "fields": [
                {"tag": "a", "text": "Long title here", "role": "title"},
                {"tag": "span", "text": "github.com", "role": "domain"},
                {"tag": "span", "text": "10 points", "role": "score"},
            ],
        },  # 3 distinct roles -> most complete
        {"index": 2, "fields": [{"tag": "a", "text": "y", "role": "meta"}]},
    ]
    out = build_sample_collections(_one_collection(records))
    assert [f["role"] for f in out[0]["fields"]] == ["title", "domain", "score"]


def test_tiebreak_lowest_index_and_deterministic():
    """Two records tie on distinct-role count -> the lower index wins, and
    repeated runs are byte-identical."""
    records = [
        {
            "index": 0,
            "fields": [
                {"tag": "a", "text": "A", "role": "title"},
                {"tag": "span", "text": "d.com", "role": "domain"},
            ],
        },
        {
            "index": 1,
            "fields": [
                {"tag": "a", "text": "B", "role": "title"},
                {"tag": "span", "text": "e.com", "role": "domain"},
            ],
        },
    ]
    coll = _one_collection(records)
    out = build_sample_collections(coll)
    assert [f["text"] for f in out[0]["fields"]] == ["A", "d.com"]  # index 0 wins
    assert build_sample_collections(coll) == build_sample_collections(coll)


def test_representative_is_one_real_record_not_a_union():
    """The sampled fields must be exactly ONE real record's fields — never a
    role-union across records (which would fabricate a shape no instance has)."""
    records = [
        {"index": 0, "fields": [{"role": "title", "text": "T0"}, {"role": "domain", "text": "d0"}]},
        {
            "index": 1,
            "fields": [
                {"role": "title", "text": "T1"},
                {"role": "score", "text": "s1"},
                {"role": "age", "text": "a1"},
            ],
        },  # 3 distinct -> chosen
        {"index": 2, "fields": [{"role": "price", "text": "p2"}]},
    ]
    out = build_sample_collections(_one_collection(records))
    sampled = [(f["role"], f["text"]) for f in out[0]["fields"]]
    # The sampled pairs co-occur in exactly one actual record (no 'price' leaks in).
    real_projections = [[(f["role"], f["text"]) for f in r["fields"]] for r in records]
    assert sampled in real_projections
    assert sampled == real_projections[1]


def test_hn_short_title_wrinkle_recovers_title_role():
    """The concrete Step-5 wrinkle: row 0 had a SHORT title mis-roled as `meta`
    while a later row carries a proper `title` role. The representative must now
    carry `title`, so the entity regains its title field regardless of ordering."""
    records = [
        {  # row 0 — short title "GPT-5.6" fell through infer_role to `meta`
            "index": 0,
            "fields": [
                {"role": "rank", "text": "1."},
                {"role": "meta", "text": "GPT-5.6"},
                {"role": "domain", "text": "openai.com"},
                {"role": "score", "text": "1255 points"},
                {"role": "meta", "text": "author"},
                {"role": "age", "text": "16 hours ago"},
                {"role": "meta", "text": "hide"},
                {"role": "comment_count", "text": "897 comments"},
            ],
        },
        {  # a later row — long title correctly roled `title`
            "index": 1,
            "fields": [
                {"role": "rank", "text": "2."},
                {"role": "title", "text": "A sufficiently long descriptive headline"},
                {"role": "domain", "text": "example.org"},
                {"role": "score", "text": "88 points"},
                {"role": "meta", "text": "author2"},
                {"role": "age", "text": "3 hours ago"},
                {"role": "meta", "text": "hide"},
                {"role": "comment_count", "text": "12 comments"},
            ],
        },
    ]
    row0_roles = [f["role"] for f in records[0]["fields"]]
    out = build_sample_collections(_one_collection(records))
    rep_roles = [f["role"] for f in out[0]["fields"]]

    assert "title" not in row0_roles  # records[0] lacked a title role
    assert "title" in rep_roles  # representative recovered it


def test_representative_change_preserves_payload_shape_and_order():
    """Selecting a different in-collection record must not change collection
    identity, count, or ordering — only which record fills `fields`."""
    records_a = [
        {"index": 0, "fields": [{"role": "title", "text": "a"}]},
        {"index": 1, "fields": [{"role": "title", "text": "b"}, {"role": "price", "text": "9"}]},
    ]
    records_b = [{"index": 0, "fields": [{"role": "title", "text": "nav"}]}]
    data = {
        "collections": [
            {"rank": 0, "count": 20, "records": records_a},
            {"rank": 1, "count": 3, "records": records_b},
        ]
    }
    out = build_sample_collections(data)
    assert [c["collection"] for c in out] == [0, 1]  # order + rank unchanged
    assert [c["count"] for c in out] == [20, 3]  # counts unchanged
    # collection 0 sampled the fuller record (index 1), not records[0]
    assert {f["role"] for f in out[0]["fields"]} == {"title", "price"}


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


# --------------------------------------------------------------------------- #
# Step 6a: sourceRole integrity — every sourceRole must resolve to a real role
# --------------------------------------------------------------------------- #
# An HN-shaped source collection (with the duplicate domain + meta leaves).
_HN_RECORDS = {
    "collections": [
        {
            "rank": 0,
            "count": 30,
            "records": [
                {
                    "index": 0,
                    "fields": [
                        {"role": "rank", "text": "1."},
                        {"role": "title", "text": "A sufficiently long headline"},
                        {"role": "domain", "text": "github.com"},
                        {"role": "domain", "text": "github.com"},
                        {"role": "score", "text": "100 points"},
                        {"role": "meta", "text": "alice"},
                        {"role": "age", "text": "3 hours ago"},
                        {"role": "meta", "text": "hide"},
                        {"role": "comment_count", "text": "42 comments"},
                    ],
                }
            ],
        }
    ]
}


def _entities_model(fields: list[dict], source_collection: int = 0) -> dict:
    """verify_source_roles only reads model['entities']."""
    return {
        "entities": [
            {"name": "story", "fields": fields, "sourceCollection": source_collection}
        ]
    }


# The correct HN linkage — the table 6b will consume. author->meta is the
# semantic-rename case name-matching would have missed.
_HN_FIELDS_OK = [
    {"name": "rank", "type": "string", "sourceRole": "rank"},
    {"name": "title", "type": "string", "sourceRole": "title"},
    {"name": "domain", "type": "string", "sourceRole": "domain"},
    {"name": "score", "type": "string", "sourceRole": "score"},
    {"name": "author", "type": "string", "sourceRole": "meta"},
    {"name": "age", "type": "string", "sourceRole": "age"},
    {"name": "commentCount", "type": "string", "sourceRole": "comment_count"},
]


def test_source_roles_all_resolve():
    """Valid linkage (incl. author<-meta, commentCount<-comment_count) passes."""
    assert verify_source_roles(_entities_model(_HN_FIELDS_OK), _HN_RECORDS) == []


def test_source_role_invented_is_rejected():
    """A role the extractor never produced ('username') is caught, with an
    actionable message listing the valid roles."""
    fields = copy.deepcopy(_HN_FIELDS_OK)
    fields[4]["sourceRole"] = "username"  # author field points at a fake role
    violations = verify_source_roles(_entities_model(fields), _HN_RECORDS)
    assert len(violations) == 1
    assert "username" in violations[0]
    assert "Valid roles are" in violations[0]
    assert "meta" in violations[0]  # the real role it should have used


def test_source_role_typo_is_rejected():
    fields = copy.deepcopy(_HN_FIELDS_OK)
    fields[1]["sourceRole"] = "titel"  # typo of 'title'
    violations = verify_source_roles(_entities_model(fields), _HN_RECORDS)
    assert len(violations) == 1
    assert "titel" in violations[0]


def test_legal_same_role_collision_passes_6a():
    """Two fields both sourceRole 'meta' is LEGAL at 6a (uniqueness is 6b's
    guard) — verify_source_roles must NOT reject it, since both resolve."""
    fields = copy.deepcopy(_HN_FIELDS_OK)
    # add a second meta-derived field (author + a hide label, both from meta)
    fields.append({"name": "hideLabel", "type": "string", "sourceRole": "meta"})
    assert verify_source_roles(_entities_model(fields), _HN_RECORDS) == []


def test_source_collection_out_of_range_is_rejected():
    """A sourceCollection that isn't a detected collection is flagged."""
    violations = verify_source_roles(
        _entities_model(_HN_FIELDS_OK, source_collection=5), _HN_RECORDS
    )
    assert len(violations) == 1
    assert "not a detected collection" in violations[0]


def test_source_roles_validated_against_representative_not_row0():
    """The valid-role set is the representative record's roles (what the model
    saw), so a title present only in a later, fuller record still counts."""
    records = {
        "collections": [
            {
                "rank": 0,
                "count": 2,
                "records": [
                    # row 0 impoverished: title mis-roled as meta (short title)
                    {"index": 0, "fields": [{"role": "meta", "text": "X"}]},
                    # row 1 complete -> representative; carries a real title role
                    {
                        "index": 1,
                        "fields": [
                            {"role": "title", "text": "A long proper headline here"},
                            {"role": "domain", "text": "d.com"},
                        ],
                    },
                ],
            }
        ]
    }
    fields = [
        {"name": "title", "type": "string", "sourceRole": "title"},
        {"name": "domain", "type": "string", "sourceRole": "domain"},
    ]
    assert verify_source_roles(_entities_model(fields), records) == []


def test_invented_sourcerole_triggers_retry_then_recovers(tmp_path, monkeypatch):
    """End-to-end: a candidate with an invented sourceRole is rejected INTO the
    retry loop (not accepted); a corrected candidate on the next attempt is
    accepted. Proves the 6a gate fires at the reasoning boundary."""
    import asyncio
    import json
    import types

    from mockbuilder.reasoning import reason as R

    records = {
        "collections": [
            {
                "rank": 0,
                "count": 1,
                "records": [
                    {"index": 0, "fields": [{"tag": "a", "text": "A long headline here", "role": "title"}]}
                ],
            }
        ]
    }
    (tmp_path / "S_records.json").write_text(json.dumps(records), encoding="utf-8")
    (tmp_path / "design_tokens.json").write_text(json.dumps({"colors": {}}), encoding="utf-8")
    monkeypatch.setattr(R, "AsyncGroq", lambda *a, **k: object())

    good = copy.deepcopy(_VALID_MODEL)  # field sourceRole 'title' resolves
    bad = copy.deepcopy(_VALID_MODEL)
    bad["entities"][0]["fields"][0]["sourceRole"] = "username"  # invented role
    sequence = [bad, good]
    calls = {"n": 0}

    async def _spy(client, messages):
        model = sequence[min(calls["n"], len(sequence) - 1)]
        calls["n"] += 1
        content = json.dumps(model)
        message = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(R, "_create_completion", _spy)
    result = asyncio.run(R.synthesize_model(tmp_path, "S"))

    assert calls["n"] == 2  # the invented sourceRole forced a retry
    assert result["entities"][0]["fields"][0]["sourceRole"] == "title"  # recovered
