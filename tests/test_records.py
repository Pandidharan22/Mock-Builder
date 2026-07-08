"""Isolation tests for the DATA-track record extractor (``crawler/records.py``).

These run the *real* detector against hermetic HTML fixtures that live in
``tests/fixtures/`` — a link-aggregator front page (split title/subtext rows), a
product grid (single cards), and a page with two unrelated repeating groups. The
same detector must handle all three with no app-specific code. The extractor now
returns *all* collections ranked by score; ``collections[0]`` is the highest
scorer. The strict assertions below are the Phase 1' gate:

  * regression — ``collections[0]`` is byte-identical to the previous
    single-winner output (proven by the pure-transform anchor);
  * anti-fabrication — extracted titles match the real ones exactly; no
    "example.com" / "Story N" filler can sneak in;
  * genericity — the module source contains no site-specific selector strings;
  * determinism — extraction twice yields identical output;
  * no ``primary`` verdict — the extractor ranks but never classifies.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mockbuilder.crawler.records import (
    Collection,
    ExtractionResult,
    Field,
    Record,
    build_result_from_raw,
    extract_records,
    extract_records_async,
    infer_role,
)

FIXTURES = Path(__file__).parent / "fixtures"
HN_FIXTURE = FIXTURES / "hn_fixture.html"
SHOP_FIXTURE = FIXTURES / "shop_fixture.html"
MULTI_FIXTURE = FIXTURES / "multi_collection_fixture.html"

HN_TITLES = {
    "Show HN: I built a deterministic mockup generator for agents",
    "The hidden cost of fusing structure and data in LLM pipelines",
    "Playwright is quietly becoming the default browser automation tool",
    "Why your crawler should extract records, not just screenshots",
    "A practical guide to structural signatures for DOM extraction",
}

SHOP_NAMES = {
    "Amul Taaza Toned Milk 500 ml Pouch",
    "Aashirvaad Whole Wheat Atta 5 kg Bag",
    "Tata Salt Iodized Vacuum Evaporated 1 kg",
    "Fortune Sunlite Refined Sunflower Oil 1 L",
    "Nescafe Classic Instant Coffee 100 g Jar",
    "Britannia Good Day Cashew Cookies 250 g",
}

CURRENCY_SYMBOLS = ("₹", "$", "€", "£")


# --------------------------------------------------------------------------- #
# Playwright helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def browser():
    """A single headless Chromium instance shared across the browser tests."""
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def _extract(browser, fixture: Path) -> ExtractionResult:
    page = browser.new_page()
    try:
        page.goto(fixture.resolve().as_uri(), wait_until="load")
        return extract_records(page)
    finally:
        page.close()


def _top(result: ExtractionResult) -> Collection:
    """The highest-scored collection — the regression-critical one."""
    return result.collections[0]


def _roles(record) -> set[str]:
    return {f.role for f in record.fields}


def _texts_with_role(collection: Collection, role: str) -> list[str]:
    return [f.text for r in collection.records for f in r.fields if f.role == role]


# --------------------------------------------------------------------------- #
# HN fixture — link aggregator with split rows
# --------------------------------------------------------------------------- #
def test_hn_count(browser):
    assert _top(_extract(browser, HN_FIXTURE)).count == 5


def test_hn_every_record_has_nonempty_title(browser):
    top = _top(_extract(browser, HN_FIXTURE))
    for record in top.records:
        titles = [f.text for f in record.fields if f.role == "title"]
        assert titles, f"record {record.index} has no title role: {record.fields}"
        assert all(t.strip() for t in titles)


def test_hn_every_record_has_domain(browser):
    top = _top(_extract(browser, HN_FIXTURE))
    for record in top.records:
        assert "domain" in _roles(record), (
            f"record {record.index} missing domain role: {record.fields}"
        )


def test_hn_titles_match_reality_exactly(browser):
    top = _top(_extract(browser, HN_FIXTURE))
    extracted = set(_texts_with_role(top, "title"))
    assert extracted == HN_TITLES


def test_hn_no_fabrication(browser):
    """The whole point of Phase 1': no invented filler can appear."""
    top = _top(_extract(browser, HN_FIXTURE))
    story_pat = re.compile(r"^Story \d+$")
    for record in top.records:
        for f in record.fields:
            assert "example.com" not in f.text
            assert "example.com" not in (f.href or "")
            assert not story_pat.match(f.text)


# --------------------------------------------------------------------------- #
# Shop fixture — product grid
# --------------------------------------------------------------------------- #
def test_shop_count(browser):
    assert _top(_extract(browser, SHOP_FIXTURE)).count == 6


def test_shop_every_record_has_image(browser):
    top = _top(_extract(browser, SHOP_FIXTURE))
    for record in top.records:
        assert "image" in _roles(record), (
            f"record {record.index} missing image role: {record.fields}"
        )


def test_shop_every_record_has_currency_price(browser):
    top = _top(_extract(browser, SHOP_FIXTURE))
    for record in top.records:
        prices = [f.text for f in record.fields if f.role == "price"]
        assert prices, f"record {record.index} has no price role: {record.fields}"
        assert all(p.startswith(CURRENCY_SYMBOLS) for p in prices)


def test_shop_names_match_reality_exactly(browser):
    top = _top(_extract(browser, SHOP_FIXTURE))
    extracted = set(_texts_with_role(top, "title"))
    assert extracted == SHOP_NAMES


# --------------------------------------------------------------------------- #
# Multi-collection fixture — two ranked collections, dedup suppresses the third
# --------------------------------------------------------------------------- #
def test_multi_yields_exactly_two_collections(browser):
    """The strip + grid page has a redundant per-card wrapper that would form a
    third (nested) group; dedup must suppress it, leaving exactly two."""
    result = _extract(browser, MULTI_FIXTURE)
    assert len(result.collections) == 2


def test_multi_order_grid_then_strip(browser):
    """collections[0] is the higher-scored product grid; [1] is the strip.
    Rank is a position, asserted explicitly — not a semantic verdict."""
    result = _extract(browser, MULTI_FIXTURE)
    grid, strip = result.collections

    # grid: 6 product cards, 4 fields each, image + price present
    assert grid.rank == 0
    assert grid.count == 6
    assert {r_len for r_len in (len(r.fields) for r in grid.records)} == {4}
    assert set(_texts_with_role(grid, "title")) == SHOP_NAMES
    for record in grid.records:
        assert "image" in _roles(record)
        assert "price" in _roles(record)

    # strip: 7 category items, exactly 1 field each
    assert strip.rank == 1
    assert strip.count == 7
    assert {r_len for r_len in (len(r.fields) for r in strip.records)} == {1}

    # ranking really is by score, and the two scores are visibly different
    assert grid.score > strip.score


def test_multi_has_no_primary_flag_anywhere(browser):
    """The extractor ranks but never classifies: no ``primary`` key may appear
    anywhere in the wire format."""
    import json

    result = _extract(browser, MULTI_FIXTURE)
    assert "primary" not in json.dumps(result.to_dict())


# --------------------------------------------------------------------------- #
# Genericity guard — mechanically enforce app-agnosticism
# --------------------------------------------------------------------------- #
def test_records_module_has_no_app_specific_strings():
    source = (
        Path(__file__).parent.parent
        / "mockbuilder" / "crawler" / "records.py"
    ).read_text(encoding="utf-8")
    for banned in ("athing", "titleline", "sitestr", "card", "subtext"):
        assert banned not in source, f"app-specific string {banned!r} in records.py"


# --------------------------------------------------------------------------- #
# Determinism guard
# --------------------------------------------------------------------------- #
def test_extraction_is_deterministic(browser):
    # Use the multi-collection fixture so determinism covers the full ranked
    # collections list, not just a single winner.
    first = _extract(browser, MULTI_FIXTURE)
    second = _extract(browser, MULTI_FIXTURE)
    assert first == second
    assert len(first.collections) == 2


# --------------------------------------------------------------------------- #
# Pure transform — build_result_from_raw is the browser-free regression anchor
# --------------------------------------------------------------------------- #
# The records that the previous SINGLE-winner detector produced for this payload.
# Multi-collection is a re-PACKAGING, not a re-tuning, so collections[0] must be
# byte-identical to these. This list is frozen: if it changes, the algorithm did.
_OLD_WINNER_RECORDS = [
    Record(
        index=0,
        fields=[
            Field(tag="td", text="1.", role="rank"),
            Field(
                tag="a",
                text="A sufficiently long headline here",
                role="title",
                href="/x",
            ),
            Field(tag="span", text="github.com", role="domain"),
            Field(tag="span", text="100 points", role="score"),
        ],
    ),
    Record(
        index=1,
        fields=[
            Field(tag="img", text="[img]", role="image", src="p.png"),
            Field(
                tag="span",
                text="Amul Taaza Toned Milk 500 ml Pouch",
                role="title",
            ),
            Field(tag="span", text="₹52", role="price"),
        ],
    ),
]

# A canned detector payload in the NEW shape (exactly what the detector now
# returns): a list of already-ranked collections, each with signature + score +
# raw records. Collection 0 carries the old winner's records; collection 1 is a
# lower-scored strip, present to prove rank assignment and ordering.
_CANNED_RAW = {
    "collections": [
        {
            "signature": "DIV>DIV(IMG,SPAN,SPAN)",
            "score": 24,
            "records": [
                [
                    {"tag": "td", "text": "1."},
                    {"tag": "a", "text": "A sufficiently long headline here", "href": "/x"},
                    {"tag": "span", "text": "github.com"},
                    {"tag": "span", "text": "100 points"},
                ],
                [
                    {"tag": "img", "text": "[img]", "src": "p.png"},
                    {"tag": "span", "text": "Amul Taaza Toned Milk 500 ml Pouch"},
                    {"tag": "span", "text": "₹52"},
                ],
            ],
        },
        {
            "signature": "UL>LI(SPAN)",
            "score": 7,
            "records": [[{"tag": "span", "text": "Fruits and Vegetables"}]],
        },
    ],
}

_EXPECTED_RESULT = ExtractionResult(
    collections=[
        Collection(
            rank=0,
            score=24.0,
            signature="DIV>DIV(IMG,SPAN,SPAN)",
            count=2,
            field_count=7,
            records=_OLD_WINNER_RECORDS,
        ),
        Collection(
            rank=1,
            score=7.0,
            signature="UL>LI(SPAN)",
            count=1,
            field_count=1,
            records=[
                Record(
                    index=0,
                    fields=[Field(tag="span", text="Fruits and Vegetables", role="title")],
                )
            ],
        ),
    ]
)


def test_build_result_from_raw_is_pure_and_exact():
    """No browser: the transform must produce exactly this ExtractionResult.
    This is the algorithm's regression anchor from here on."""
    assert build_result_from_raw(_CANNED_RAW) == _EXPECTED_RESULT


def test_collection_zero_is_the_old_single_winner():
    """THE regression gate: multi-collection only re-packages. collections[0]
    must equal the previous single-winner output — same records, same roles,
    same signature, same count."""
    top = build_result_from_raw(_CANNED_RAW).collections[0]
    assert top.records == _OLD_WINNER_RECORDS
    assert top.signature == "DIV>DIV(IMG,SPAN,SPAN)"
    assert top.count == 2
    assert top.rank == 0


def test_build_result_from_raw_empty():
    """A payload with no collections yields a valid empty result, never raises."""
    result = build_result_from_raw({"collections": []})
    assert result == ExtractionResult(collections=[])


# --------------------------------------------------------------------------- #
# Async parity — extract_records_async must equal the sync path exactly
# --------------------------------------------------------------------------- #
async def _extract_async(fixture: Path) -> ExtractionResult:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        try:
            page = await b.new_page()
            await page.goto(fixture.resolve().as_uri(), wait_until="load")
            return await extract_records_async(page)
        finally:
            await b.close()


def _run_async(fixture: Path) -> ExtractionResult:
    """Run the async extraction on its own thread. The module-scoped sync
    ``browser`` fixture keeps a Playwright asyncio loop live on the main thread,
    so ``asyncio.run`` must execute where no loop is already running."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_extract_async(fixture))).result()


@pytest.mark.parametrize("fixture", [HN_FIXTURE, SHOP_FIXTURE, MULTI_FIXTURE])
def test_async_matches_sync_exactly(browser, fixture):
    """Same page, same transform → identical collections, records, roles, sigs."""
    sync_result = _extract(browser, fixture)
    async_result = _run_async(fixture)
    assert async_result == sync_result


# --------------------------------------------------------------------------- #
# Serialization boundary — to_dict() is the records.json wire format
# --------------------------------------------------------------------------- #
def test_to_dict_shape_and_drops_none(browser):
    """``to_dict()`` must be JSON-serializable, mirror the dataclasses, and omit
    absent optional keys (no ``href: null`` / ``src: null`` noise)."""
    import json

    result = _extract(browser, HN_FIXTURE)
    d = result.to_dict()

    # round-trips through JSON unchanged (proves it is pure data)
    assert json.loads(json.dumps(d)) == d
    assert list(d.keys()) == ["collections"]
    assert len(d["collections"]) == len(result.collections)

    for col_d, col in zip(d["collections"], result.collections):
        assert col_d["rank"] == col.rank
        assert col_d["score"] == col.score
        assert col_d["signature"] == col.signature
        assert col_d["count"] == col.count == len(col_d["records"])
        assert col_d["field_count"] == col.field_count
        for rec_d, rec in zip(col_d["records"], col.records):
            assert rec_d["index"] == rec.index
            assert len(rec_d["fields"]) == len(rec.fields)
            for f_d, f in zip(rec_d["fields"], rec.fields):
                assert f_d["tag"] == f.tag
                assert f_d["text"] == f.text
                assert f_d["role"] == f.role
                # None-valued optionals are dropped, not serialized as null
                if f.href is None:
                    assert "href" not in f_d
                else:
                    assert f_d["href"] == f.href
                if f.src is None:
                    assert "src" not in f_d
                else:
                    assert f_d["src"] == f.src


# --------------------------------------------------------------------------- #
# infer_role — pure-function table-driven unit tests (no browser)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,expected",
    [
        ({"tag": "img", "text": "[img]", "src": "x.png"}, "image"),
        ({"tag": "td", "text": "1."}, "rank"),
        ({"tag": "td", "text": "42."}, "rank"),
        ({"tag": "a", "text": "▲"}, "vote"),
        ({"tag": "span", "text": "₹40"}, "price"),
        ({"tag": "span", "text": "$9.99"}, "price"),
        ({"tag": "span", "text": "€ 5"}, "price"),
        ({"tag": "span", "text": "40"}, "number"),
        ({"tag": "span", "text": "3.5"}, "number"),
        ({"tag": "a", "text": "234 comments"}, "comment_count"),
        ({"tag": "a", "text": "1 comment"}, "comment_count"),
        ({"tag": "span", "text": "100 points"}, "score"),
        ({"tag": "span", "text": "1 point"}, "score"),
        ({"tag": "a", "text": "9 hours ago"}, "age"),
        ({"tag": "a", "text": "1 day ago"}, "age"),
        ({"tag": "span", "text": "github.com"}, "domain"),
        ({"tag": "span", "text": "blog.mozilla.org"}, "domain"),
        (
            {"tag": "a", "text": "A sufficiently long headline here", "href": "/x"},
            "title",
        ),
        ({"tag": "a", "text": "short", "href": "/x"}, "meta"),
        ({"tag": "span", "text": "by"}, "meta"),
    ],
)
def test_infer_role(field, expected):
    assert infer_role(field) == expected
