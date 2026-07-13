"""Tests for seed injection (Phase 3' Step 6b) — the DATA/STRUCTURE zip.

`inject_seed` fills entity.seed from real extracted records via the
sourceCollection + sourceRole linkage. These lock the behavior that kills the
fabrication defect (every real record becomes a row; nothing is invented) and the
two loud guards (uniqueness, resolution). Fixture-backed tests use the real
extractor; synthetic records exercise the duplicate-meta and guard paths.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from mockbuilder.crawler.records import extract_records
from mockbuilder.generator.inject import SeedInjectionError, inject_seed

FIXTURES = Path(__file__).parent / "fixtures"
HN_FIXTURE = FIXTURES / "hn_fixture.html"
MULTI_FIXTURE = FIXTURES / "multi_collection_fixture.html"


# --------------------------------------------------------------------------- #
# Model / record builders
# --------------------------------------------------------------------------- #
_HN_FIELDS = [
    {"name": "rank", "type": "string", "sourceRole": "rank"},
    {"name": "title", "type": "string", "sourceRole": "title"},
    {"name": "domain", "type": "string", "sourceRole": "domain"},
    {"name": "score", "type": "string", "sourceRole": "score"},
    {"name": "author", "type": "string", "sourceRole": "meta"},
    {"name": "age", "type": "string", "sourceRole": "age"},
    {"name": "commentCount", "type": "string", "sourceRole": "comment_count"},
]


def _model(fields: list[dict], name: str = "story", source_collection: int = 0) -> dict:
    return {
        "entities": [
            {"name": name, "fields": fields, "sourceCollection": source_collection}
        ]
    }


def _collection(records: list[dict], rank: int = 0) -> dict:
    return {"collections": [{"rank": rank, "count": len(records), "records": records}]}


def _hn_record(rank, title, domain, score, author, age, comments, with_hide=True):
    """A synthetic HN story record mirroring the extractor's output, including the
    two-meta duplication (author + hide) that the first-occurrence rule must
    resolve. `with_hide=False` mimics a shortened (jobs-style) row."""
    fields = [
        {"tag": "td", "text": rank, "role": "rank"},
        {"tag": "a", "text": title, "role": "title", "href": "item?id=1"},
        {"tag": "span", "text": domain, "role": "domain"},
        {"tag": "span", "text": score, "role": "score"},
        {"tag": "a", "text": author, "role": "meta", "href": "user?id=" + author},
        {"tag": "a", "text": age, "role": "age"},
    ]
    if with_hide:
        fields.append({"tag": "a", "text": "hide", "role": "meta"})
    fields.append({"tag": "a", "text": comments, "role": "comment_count"})
    return {"index": 0, "fields": fields}


# --------------------------------------------------------------------------- #
# Playwright browser (real extraction for the fixture-backed happy paths)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def _extract(browser, fixture: Path):
    page = browser.new_page()
    try:
        page.goto(fixture.resolve().as_uri(), wait_until="load")
        return extract_records(page).to_dict()
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# Happy path — every record becomes a row (ALL of them, never a sample)
# --------------------------------------------------------------------------- #
def test_hn_fixture_injects_all_records(browser):
    records = _extract(browser, HN_FIXTURE)
    out = inject_seed(_model(_HN_FIELDS), records)
    seed = out["entities"][0]["seed"]

    # The fixture has 5 stories -> 5 real rows, each keyed by entity field name.
    assert len(seed) == 5
    for i, row in enumerate(seed):
        assert row["id"] == i
        assert row["title"] and row["domain"] and row["score"]
    # every seeded title is a real one the extractor found (no invented rows)
    assert {row["title"] for row in seed} == {
        f["text"]
        for rec in records["collections"][0]["records"]
        for f in rec["fields"]
        if f["role"] == "title"
    }


def test_anti_fabrication_no_story_n_no_example_com(browser):
    """The headline assertion: the defect that started this project is dead."""
    records = _extract(browser, HN_FIXTURE)
    seed = inject_seed(_model(_HN_FIELDS), records)["entities"][0]["seed"]
    import json

    blob = json.dumps(seed, ensure_ascii=False)
    assert re.search(r'"Story \d+"', blob) is None
    assert "example.com" not in blob
    # every title traces to a real extracted record
    real = {
        f["text"]
        for rec in records["collections"][0]["records"]
        for f in rec["fields"]
        if f["role"] == "title"
    }
    assert all(row["title"] in real for row in seed)


def test_first_occurrence_picks_author_meta_not_hide():
    """The one load-bearing rule: on HN's different-valued meta duplicate, the
    author is the FIRST meta leaf, not the `hide` one."""
    records = _collection(
        [
            _hn_record("1.", "A real headline", "github.com", "640 points", "vforno", "13 hours ago", "147 comments"),
            _hn_record("2.", "Another headline", "arxiv.org", "88 points", "alice", "3 hours ago", "12 comments"),
        ]
    )
    seed = inject_seed(_model(_HN_FIELDS), records)["entities"][0]["seed"]
    assert [row["author"] for row in seed] == ["vforno", "alice"]
    assert "hide" not in [row["author"] for row in seed]


# --------------------------------------------------------------------------- #
# The two guards — loud, never silent
# --------------------------------------------------------------------------- #
def test_uniqueness_guard_raises_and_names_both_fields():
    fields = _HN_FIELDS + [{"name": "hideLabel", "type": "string", "sourceRole": "meta"}]
    records = _collection([_hn_record("1.", "t", "d.com", "5 points", "bob", "1 hour ago", "2 comments")])
    with pytest.raises(SeedInjectionError) as exc:
        inject_seed(_model(fields), records)
    msg = str(exc.value)
    assert "author" in msg and "hideLabel" in msg  # names BOTH colliding fields
    assert "meta" in msg


def test_resolution_guard_invented_role_raises():
    """A sourceRole present in NO record of the collection is an invented role."""
    fields = copy.deepcopy(_HN_FIELDS)
    fields[4]["sourceRole"] = "username"  # no 'username' leaf exists anywhere
    records = _collection([_hn_record("1.", "t", "d.com", "5 points", "bob", "1 hour ago", "2 comments")])
    with pytest.raises(SeedInjectionError) as exc:
        inject_seed(_model(fields), records)
    assert "username" in str(exc.value)


def test_graceful_shortening_omits_key_no_raise():
    """A role present in SOME records but missing from a given record (jobs-style
    shortening) -> that row omits the key; the build does NOT fail."""
    records = _collection(
        [
            _hn_record("1.", "Full story", "github.com", "640 points", "vforno", "13 hours ago", "147 comments"),
            # a shortened row: no score, no comments, no hide-meta — just title/age/author
            {
                "index": 1,
                "fields": [
                    {"tag": "td", "text": "2.", "role": "rank"},
                    {"tag": "a", "text": "YC startup is hiring", "role": "title"},
                    {"tag": "span", "text": "ycombinator.com", "role": "domain"},
                    {"tag": "a", "text": "somefounder", "role": "meta"},
                    {"tag": "a", "text": "4 days ago", "role": "age"},
                ],
            },
        ]
    )
    seed = inject_seed(_model(_HN_FIELDS), records)["entities"][0]["seed"]
    assert len(seed) == 2
    # full row has score + commentCount; shortened row omits them (graceful)
    assert "score" in seed[0] and "commentCount" in seed[0]
    assert "score" not in seed[1] and "commentCount" not in seed[1]
    assert seed[1]["title"] == "YC startup is hiring"  # the row still renders


# --------------------------------------------------------------------------- #
# Determinism, purity
# --------------------------------------------------------------------------- #
def test_determinism():
    records = _collection([_hn_record("1.", "t", "d.com", "5 points", "bob", "1 hour ago", "2 comments")])
    model = _model(_HN_FIELDS)
    assert inject_seed(model, records) == inject_seed(model, records)


def test_purity_does_not_mutate_input():
    records = _collection([_hn_record("1.", "t", "d.com", "5 points", "bob", "1 hour ago", "2 comments")])
    model = _model(_HN_FIELDS)
    before = copy.deepcopy(model)
    inject_seed(model, records)
    assert model == before  # input untouched
    assert "seed" not in model["entities"][0]


# --------------------------------------------------------------------------- #
# Multi fixture — product seeds from the GRID (sourceCollection 0), with coercion
# --------------------------------------------------------------------------- #
def test_multi_fixture_seeds_grid(browser):
    records = _extract(browser, MULTI_FIXTURE)
    fields = [
        {"name": "imageUrl", "type": "imageUrl", "sourceRole": "image"},
        {"name": "title", "type": "string", "sourceRole": "title"},
        {"name": "price", "type": "currency", "sourceRole": "price"},
        {"name": "unit", "type": "string", "sourceRole": "meta"},
    ]
    out = inject_seed(_model(fields, name="product", source_collection=0), records)
    seed = out["entities"][0]["seed"]
    assert len(seed) == 6  # the grid's 6 products, not the strip
    for row in seed:
        assert row["price"].startswith(("₹", "$", "€", "£"))  # currency kept as text
        assert row["imageUrl"]  # image src resolved, non-empty
        assert row["title"]


# --------------------------------------------------------------------------- #
# Zero-collection — model with no data entity passes through unchanged
# --------------------------------------------------------------------------- #
def test_zero_collection_passthrough():
    model = {"entities": [], "screens": [], "flows": []}
    out = inject_seed(model, {"collections": []})
    assert out == model


def test_number_coercion_extracts_digits():
    """A field typed 'number' pulls digits out; unparseable text stays raw."""
    records = _collection(
        [
            {
                "index": 0,
                "fields": [
                    {"tag": "span", "text": "640 points", "role": "score"},
                    {"tag": "span", "text": "N/A", "role": "rank"},
                ],
            }
        ]
    )
    fields = [
        {"name": "score", "type": "number", "sourceRole": "score"},
        {"name": "rank", "type": "number", "sourceRole": "rank"},
    ]
    row = inject_seed(_model(fields), records)["entities"][0]["seed"][0]
    assert row["score"] == 640  # parsed to int
    assert row["rank"] == "N/A"  # unparseable -> kept raw, not null
