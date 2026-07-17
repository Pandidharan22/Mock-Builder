"""Tests for branch-selection clickability filtering (Step 10-pre-fix).

The bug: `_pick_branch_elements` took the first two `a`/`button` in DOM order.
On any accessible site those are hidden skip-links; `page.click()` times out on
them, `_visit` abandons the path, and the crawl dies at ONE state. Multi-state
crawling was silently broken on essentially every modern accessible site — HN was
spared only by having no skip-links.

The fix filters candidates to genuinely clickable elements BY PROPERTY (never by
name): not hidden/aria-hidden, not disabled/aria-disabled, not display:none /
visibility:hidden / opacity:0, and a box larger than 1x1 (the universal clipped
sr-only pattern is exactly 1x1 with an offsetParent and nonzero size, so only a
meaningful box separates it from a real target).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mockbuilder.crawler.crawler import Crawler
from mockbuilder.crawler.dom import discover_elements

FIXTURES = Path(__file__).parent / "fixtures"
SKIPLINK_FIXTURE = FIXTURES / "skiplink_fixture.html"


async def _pick_for(fixture: Path) -> tuple[list[dict], list[dict]]:
    """Run the real discovery + branch selection against a fixture page."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(fixture.resolve().as_uri(), wait_until="load")
            elements = await discover_elements(page)
            picks = await Crawler._pick_branch_elements(page, elements)
            return picks, elements
        finally:
            await browser.close()


def _selectors(picks: list[dict]) -> str:
    """Join picked elements' selectors for substring assertions."""
    return " ".join(p["selector"] for p in picks)


def _run(coro):
    """Run an async coroutine from a sync test (no loop is running here)."""
    return asyncio.run(coro)


def test_skiplinks_are_not_selected_real_links_are():
    """THE bug, as a test: the first two anchors in DOM order are hidden
    skip-links. Selection must skip them and pick the real, visible links."""
    picks, elements = _run(_pick_for(SKIPLINK_FIXTURE))

    # the skip-links ARE discovered (they're real anchors) ...
    discovered = [e.get("selector") for e in elements]
    assert any("skip-nav" in (s or "") for s in discovered)
    # ... but they must NOT be branched on.
    joined = _selectors(picks)
    assert "skip-nav" not in joined
    assert "skip-main" not in joined
    # the real links win instead
    assert len(picks) == 2
    assert "real-products" in joined
    assert "real-cart" in joined


def test_disabled_and_aria_hidden_are_excluded():
    """Excluded by property (attribute), not by name."""
    picks, _ = _run(_pick_for(SKIPLINK_FIXTURE))
    joined = _selectors(picks)
    assert "hidden-link" not in joined  # aria-hidden="true"
    assert "none-link" not in joined  # display:none
    # neither disabled button is picked (they'd be the first buttons in DOM order)
    assert "Disabled" not in joined


def test_branch_selection_is_deterministic():
    """Same DOM -> same eligible set -> same picks."""
    first, _ = _run(_pick_for(SKIPLINK_FIXTURE))
    second, _ = _run(_pick_for(SKIPLINK_FIXTURE))
    assert first == second


def test_branch_selection_preserves_labels():
    """Selection returns the ELEMENT, not a bare selector, so the label survives.

    A selector says where an element is; only the label says what it DOES. Edge
    provenance records "this state came from clicking 'Cart'", which is
    unrecoverable from `nth-of-type` paths — and reconstructing it by matching on
    selector would mis-attribute whenever two elements share one.
    """
    picks, _ = _run(_pick_for(SKIPLINK_FIXTURE))

    assert all(isinstance(p, dict) for p in picks)
    for p in picks:
        assert p.keys() >= {"tag", "text", "selector"}

    by_selector = {p["selector"]: p for p in picks}
    assert by_selector["#real-products"]["text"] == "Products"
    assert by_selector["#real-cart"]["text"] == "Cart"


def test_filter_is_property_based_not_name_based():
    """Guard the principle: the filter's LOGIC must not enumerate known offenders.
    If it matched on 'skip'/'sr-only' strings it would break on the next site that
    names its hidden elements differently — a property generalizes, a name list
    doesn't. (Explanatory comments may of course name the motivating case; only
    executable lines are checked, since a comment can't misbehave at runtime.)"""
    from mockbuilder.crawler.crawler import _CLICKABLE_JS

    executable = "\n".join(
        line
        for line in _CLICKABLE_JS.splitlines()
        if not line.strip().startswith("//")
    )
    for banned in ("skip", "sr-only", "screen-reader", "visually-hidden"):
        assert banned not in executable, (
            f"the filter's logic matches on a name ({banned!r}); it must filter by "
            f"property (visibility/size/disabled) so it generalizes"
        )
