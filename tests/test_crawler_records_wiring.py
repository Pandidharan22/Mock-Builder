"""Wiring tests: the async crawler emits ``<hash>_records.json`` per state.

These drive the *real* async crawl entrypoint (``Crawler.crawl``) against
hermetic fixtures, writing into a throwaway evidence dir. They assert:

  * a valid ``<hash>_records.json`` with the new ``collections`` shape lands
    beside the other evidence — one collection for HN, two for the multi fixture;
  * record extraction is failure-isolated — a crash still completes the crawl,
    writes the ``{"collections": [], "error": true}`` sentinel, and logs ERROR,
    while a legitimately-empty page writes ``{"collections": []}`` and logs no
    ERROR (the two empty paths stay distinguishable by value);
  * the existing captures (screenshot / elements / design tokens) are unchanged,
    so wiring in extraction caused no capture regression.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from mockbuilder.crawler.crawler import Crawler
from mockbuilder.crawler.records import ExtractionResult

pytest.importorskip("playwright.async_api")

FIXTURES = Path(__file__).parent / "fixtures"
HN_FIXTURE = FIXTURES / "hn_fixture.html"
MULTI_FIXTURE = FIXTURES / "multi_collection_fixture.html"


def _crawl(evidence_dir: Path, fixture: Path = HN_FIXTURE) -> list[dict]:
    """Run one real async crawl of ``fixture`` into ``evidence_dir``."""
    crawler = Crawler(evidence_dir=evidence_dir)
    return asyncio.run(crawler.crawl(fixture.resolve().as_uri(), max_states=1))


def _records_files(evidence_dir: Path) -> list[Path]:
    return sorted(evidence_dir.glob("*_records.json"))


def _only_payload(evidence_dir: Path) -> dict:
    files = _records_files(evidence_dir)
    assert len(files) == 1, f"expected one records.json, got {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Happy path — a real crawl emits a valid records.json in the collections shape
# --------------------------------------------------------------------------- #
def test_crawl_emits_records_json_hn(tmp_path):
    captured = _crawl(tmp_path, HN_FIXTURE)
    assert len(captured) == 1

    payload = _only_payload(tmp_path)
    # HN's front page is one repeating collection of 5 stories.
    assert len(payload["collections"]) == 1
    top = payload["collections"][0]
    assert top["rank"] == 0
    assert top["count"] == 5
    assert len(top["records"]) == 5
    assert top["signature"], "happy-path signature should be non-empty"
    assert "error" not in payload  # not the crash sentinel

    # the state record references the records file it wrote
    assert Path(captured[0]["records"]).name == _records_files(tmp_path)[0].name


def test_crawl_emits_two_collections_for_multi(tmp_path):
    _crawl(tmp_path, MULTI_FIXTURE)
    payload = _only_payload(tmp_path)
    assert len(payload["collections"]) == 2
    assert [c["rank"] for c in payload["collections"]] == [0, 1]
    assert "primary" not in json.dumps(payload)


def test_crawl_does_not_regress_other_captures(tmp_path):
    """Screenshot, elements, and design tokens must still be produced."""
    captured = _crawl(tmp_path, HN_FIXTURE)
    state_hash = captured[0]["state_hash"]

    assert (tmp_path / f"{state_hash}.png").exists()
    assert (tmp_path / f"{state_hash}_elements.json").exists()
    assert (tmp_path / "design_tokens.json").exists()

    # elements.json is still valid and non-trivial
    elements = json.loads(
        (tmp_path / f"{state_hash}_elements.json").read_text(encoding="utf-8")
    )
    assert isinstance(elements, list) and elements


# --------------------------------------------------------------------------- #
# Failure isolation — a crash must not abort the crawl (crash sentinel + ERROR)
# --------------------------------------------------------------------------- #
def test_extraction_failure_is_isolated(tmp_path, monkeypatch, caplog):
    async def _boom(page):  # noqa: ARG001 - signature must match the real fn
        raise RuntimeError("simulated extraction failure")

    monkeypatch.setattr("mockbuilder.crawler.crawler.extract_records_async", _boom)

    with caplog.at_level(logging.ERROR, logger="mockbuilder.crawler.crawler"):
        captured = _crawl(tmp_path, HN_FIXTURE)

    # The crawl still completed and captured its state.
    assert len(captured) == 1

    # The crash sentinel was written — distinguishable by the "error" key.
    assert _only_payload(tmp_path) == {"collections": [], "error": True}

    # ...and the failure was logged at ERROR.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected an ERROR log for the extraction failure"
    assert any("record extraction failed" in r.getMessage() for r in error_records)

    # Other captures are untouched by the extraction failure.
    state_hash = captured[0]["state_hash"]
    assert (tmp_path / f"{state_hash}.png").exists()
    assert (tmp_path / f"{state_hash}_elements.json").exists()


def test_legitimate_empty_writes_plain_sentinel_no_error(tmp_path, monkeypatch, caplog):
    """A page with no repeating collection returns an empty result WITHOUT
    raising: the writer emits ``{"collections": []}`` (no ``error`` key) and logs
    nothing at ERROR — the value-level distinction from the crash path."""

    async def _empty(page):  # noqa: ARG001 - signature must match the real fn
        return ExtractionResult(collections=[])

    monkeypatch.setattr("mockbuilder.crawler.crawler.extract_records_async", _empty)

    with caplog.at_level(logging.ERROR, logger="mockbuilder.crawler.crawler"):
        _crawl(tmp_path, HN_FIXTURE)

    assert _only_payload(tmp_path) == {"collections": []}
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
