"""Tests for accent-color detection in the design-token harvester (Step 9).

The harvester's structural pass samples container backgrounds; the accent pass
recovers a brand color that lives on interactive text/borders (a link-colored
accent the structural pass is blind to). The load-bearing test here is the
ABSENCE GUARD: a near-monochrome page — even with one stray saturated link — must
report NO accent, so a false brand color is never manufactured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mockbuilder.recorder.design_tokens import _HARVEST_JS

FIXTURES = Path(__file__).parent / "fixtures"
ACCENT_FIXTURE = FIXTURES / "accent_fixture.html"
MONO_FIXTURE = FIXTURES / "monochrome_fixture.html"
HN_FIXTURE = FIXTURES / "hn_fixture.html"


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def _harvest(browser, fixture: Path) -> dict:
    page = browser.new_page()
    try:
        page.goto(fixture.resolve().as_uri(), wait_until="load")
        return page.evaluate(_HARVEST_JS)
    finally:
        page.close()


def test_accent_detected_from_interactive_text(browser):
    """The brand accent lives only on link/button color — never a background —
    yet the accent pass recovers it."""
    tokens = _harvest(browser, ACCENT_FIXTURE)
    assert tokens.get("accentColor") == "#7f54b3"
    # and the structural pass still only saw neutral chrome (accent isn't a bg)
    assert "#7f54b3" not in tokens["structuralColors"]


def test_no_false_accent_on_monochrome(browser):
    """THE guard: a near-monochrome page with one stray saturated link must
    report NO accent — the stray must not be promoted to a brand color."""
    tokens = _harvest(browser, MONO_FIXTURE)
    assert "accentColor" not in tokens or tokens.get("accentColor") is None


def test_accent_pass_is_additive(browser):
    """The accent pass must ADD `accentColor` without removing or altering any
    existing structural token — it's additive by construction. (Real-HN
    byte-identical designTokens is verified live; the HN fixture is a bare table
    with no styled orange, so it isn't a representative structural sample.)"""
    tokens = _harvest(browser, HN_FIXTURE)
    for key in (
        "fontFamily",
        "baseSize",
        "bodyBackground",
        "bodyColor",
        "structuralBackgrounds",
        "structuralColors",
        "divBackgrounds",
    ):
        assert key in tokens, f"accent pass dropped existing token {key!r}"
    assert tokens["fontFamily"] == "Verdana, sans-serif"  # unchanged by accent pass


def test_harvest_is_deterministic(browser):
    assert _harvest(browser, ACCENT_FIXTURE) == _harvest(browser, ACCENT_FIXTURE)
    assert _harvest(browser, MONO_FIXTURE) == _harvest(browser, MONO_FIXTURE)
