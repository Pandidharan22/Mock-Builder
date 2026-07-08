"""Wiring tests: the async crawler emits ``<hash>_records.json`` per state.

These drive the *real* async crawl entrypoint (``Crawler.crawl``) against the
hermetic HN fixture, writing into a throwaway evidence dir. They assert three
things Step 2 promised:

  * a valid ``<hash>_records.json`` (count == 5) lands beside the other evidence;
  * record extraction is failure-isolated — if it raises, the crawl still
    completes, a valid *empty* file is written, and an ERROR is logged;
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

pytest.importorskip("playwright.async_api")

FIXTURES = Path(__file__).parent / "fixtures"
HN_FIXTURE = FIXTURES / "hn_fixture.html"


def _crawl(evidence_dir: Path) -> list[dict]:
    """Run one real async crawl of the HN fixture into ``evidence_dir``."""
    crawler = Crawler(evidence_dir=evidence_dir)
    url = HN_FIXTURE.resolve().as_uri()
    return asyncio.run(crawler.crawl(url, max_states=1))


def _records_files(evidence_dir: Path) -> list[Path]:
    return sorted(evidence_dir.glob("*_records.json"))


# --------------------------------------------------------------------------- #
# Happy path — a real crawl emits a valid records.json with the 5 stories
# --------------------------------------------------------------------------- #
def test_crawl_emits_records_json(tmp_path):
    captured = _crawl(tmp_path)
    assert len(captured) == 1

    files = _records_files(tmp_path)
    assert len(files) == 1, f"expected one records.json, got {files}"

    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["count"] == 5
    assert len(payload["records"]) == 5
    # a real extraction records a structural signature (not the error sentinel)
    assert payload["signature"], "happy-path signature should be non-empty"

    # the state record references the records file it wrote
    assert Path(captured[0]["records"]).name == files[0].name


def test_crawl_does_not_regress_other_captures(tmp_path):
    """Screenshot, elements, and design tokens must still be produced."""
    captured = _crawl(tmp_path)
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
# Failure isolation — extraction blowing up must not abort the crawl
# --------------------------------------------------------------------------- #
def test_extraction_failure_is_isolated(tmp_path, monkeypatch, caplog):
    async def _boom(page):  # noqa: ARG001 - signature must match the real fn
        raise RuntimeError("simulated extraction failure")

    monkeypatch.setattr(
        "mockbuilder.crawler.crawler.extract_records_async", _boom
    )

    with caplog.at_level(logging.ERROR, logger="mockbuilder.crawler.crawler"):
        captured = _crawl(tmp_path)

    # The crawl still completed and captured its state.
    assert len(captured) == 1

    # A valid, empty records.json was still written...
    files = _records_files(tmp_path)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload == {
        "count": 0,
        "field_count": 0,
        "records": [],
        "signature": None,
    }

    # ...and the failure was logged at ERROR (the error path's fingerprint,
    # distinct from a legitimately-empty page which logs nothing at ERROR).
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected an ERROR log for the extraction failure"
    assert any("record extraction failed" in r.getMessage() for r in error_records)

    # Other captures are untouched by the extraction failure.
    state_hash = captured[0]["state_hash"]
    assert (tmp_path / f"{state_hash}.png").exists()
    assert (tmp_path / f"{state_hash}_elements.json").exists()
