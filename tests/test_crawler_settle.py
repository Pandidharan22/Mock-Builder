"""Tests for the post-click settle predicate (Step 10-pre-fix2).

THE BUG THIS EXISTS TO KILL. `_visit` clicked, then called
`wait_for_load_state("networkidle")`. Load states are per-DOCUMENT LIFECYCLE
events, not a live read of the network: once `networkidle` fired for the current
document — which `goto(wait_until="networkidle")` guarantees on arrival — asking
again returns instantly regardless of what is in flight. So an AJAX action's
effect was never waited for. On scrapingcourse the crawler captured the EMPTY
cart and stamped `via: "Add to cart"` on it: a false record of what happened.

WHY IT SHIPPED. `storefront_fixture.html`'s add is a synchronous `onclick` with no
network, so the hermetic suite could not fail. The fixture was cleaner than
reality. `ajax_storefront.html` exists to be as messy as reality: its add issues a
REAL request that the test server answers slowly and only mutates the DOM once it
resolves.

The first test below pins that down by running the fixture against the OLD logic
and asserting it captures the empty cart. A fixture that cannot fail without the
fix does not test the fix.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import socketserver
import threading
import time
from pathlib import Path

import pytest

import mockbuilder.crawler.crawler as crawler_mod
from mockbuilder.crawler.crawler import Crawler, _Inflight, _settle_after_click

FIXTURES = Path(__file__).parent / "fixtures"
HN_FIXTURE = FIXTURES / "hn_fixture.html"

# How long the server stalls the add. Comfortably longer than any incidental
# pause, so "read the DOM immediately" reliably reads the PRE-add page.
_SLOW_ADD_SECONDS = 0.8


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves tests/fixtures, plus two endpoints that misbehave on purpose."""

    def do_GET(self):  # noqa: N802 - stdlib naming
        # Match the route EXACTLY, query stripped. A `startswith("/hang")` prefix
        # test also matches "/hang_fixture.html", which served the fixture page
        # itself from the hanging endpoint and stalled the load for 30s. Same
        # anchoring lesson as the role patterns and the cart-target segments:
        # a prefix is not a name.
        route = self.path.split("?", 1)[0]
        if route == "/slow-add":
            time.sleep(_SLOW_ADD_SECONDS)  # the add lands only after this
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if route == "/hang":
            time.sleep(30)  # never answers within any sane ceiling
            return
        super().do_GET()

    def log_message(self, *args):
        pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, *args):
        pass  # the browser closing a connection is noise, not a failure


@pytest.fixture(scope="module")
def base_url():
    """Serve the fixtures over HTTP — `fetch()` is blocked on file:// origins,
    and a real request is the whole point of the AJAX fixture."""
    handler = functools.partial(_Handler, directory=str(FIXTURES))
    httpd = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def _run(coro):
    return asyncio.run(coro)


async def _old_settle(page, inflight):
    """The shipped logic, verbatim: a lifecycle event that already fired."""
    await page.wait_for_load_state("networkidle")
    return True


def _crawl(url: str, evidence_dir: Path, max_states: int = 4) -> list[dict]:
    return _run(Crawler(evidence_dir=evidence_dir).crawl(url, max_states=max_states))


def _cart_state(states: list[dict], evidence_dir: Path) -> dict | None:
    """The state the ADD produced, identified by its recorded provenance."""
    for state in states:
        prov = json.loads(
            (evidence_dir / f"{state['state_hash']}_provenance.json").read_text(
                encoding="utf-8"
            )
        )
        if "add to cart" in ((prov.get("via") or {}).get("text") or "").lower():
            return state
    return None


def _is_populated(state: dict) -> bool:
    """Did the added item actually reach the cart page's evidence?"""
    elements = json.loads(Path(state["elements"]).read_text(encoding="utf-8"))
    return any(e.get("text") == "Aero Daily Fitness Tee" for e in elements)


# --------------------------------------------------------------------------- #
# The race: reproduced, then killed
# --------------------------------------------------------------------------- #
def test_the_fixture_really_races_under_the_old_settle(base_url, tmp_path, monkeypatch):
    """THE SHIPPED BUG, pinned as a test.

    With the old logic the crawler captures the cart page the add has NOT yet
    changed — and still records `via: "Add to cart"` on it. If this test ever
    stops failing-without-the-fix, the fixture has stopped reproducing reality.
    """
    monkeypatch.setattr(crawler_mod, "_settle_after_click", _old_settle)

    states = _crawl(f"{base_url}/ajax_storefront.html", tmp_path)
    cart = _cart_state(states, tmp_path)

    assert cart is not None, "expected the add->cart path to capture something"
    assert not _is_populated(cart), (
        "the fixture did not race: the old settle captured a populated cart, so "
        "this fixture cannot prove the fix does anything"
    )


def test_settle_predicate_captures_the_populated_cart(base_url, tmp_path):
    """The fix: the add's effect reaches evidence."""
    states = _crawl(f"{base_url}/ajax_storefront.html", tmp_path)
    cart = _cart_state(states, tmp_path)

    assert cart is not None
    assert _is_populated(cart), "the add's effect never landed in the captured state"


def test_old_and_new_settle_capture_different_states(base_url, tmp_path, monkeypatch):
    """The two runs disagree, and the disagreement IS the bug's footprint."""
    monkeypatch.setattr(crawler_mod, "_settle_after_click", _old_settle)
    old = _cart_state(
        _crawl(f"{base_url}/ajax_storefront.html", tmp_path / "old"), tmp_path / "old"
    )
    monkeypatch.undo()
    new = _cart_state(
        _crawl(f"{base_url}/ajax_storefront.html", tmp_path / "new"), tmp_path / "new"
    )

    assert old is not None and new is not None
    assert old["state_hash"] != new["state_hash"]
    assert not _is_populated(old)
    assert _is_populated(new)


def test_provenance_is_now_true_for_both_carts(base_url, tmp_path):
    """The empty cart and the populated cart are BOTH captured, each attributed
    to the affordance that really produced it — the add no longer takes credit
    for a state it did not change."""
    states = _crawl(f"{base_url}/ajax_storefront.html", tmp_path)

    by_via = {}
    for state in states:
        prov = json.loads(
            (tmp_path / f"{state['state_hash']}_provenance.json").read_text(
                encoding="utf-8"
            )
        )
        via = (prov.get("via") or {}).get("text")
        if via:
            by_via[via] = state

    assert "Add to cart" in by_via and "Cart" in by_via
    assert _is_populated(by_via["Add to cart"])
    assert not _is_populated(by_via["Cart"])
    assert by_via["Add to cart"]["state_hash"] != by_via["Cart"]["state_hash"]


def test_two_crawls_agree_on_the_populated_cart(base_url, tmp_path):
    """Determinism: the settled state is a state that stopped moving."""
    first = _cart_state(
        _crawl(f"{base_url}/ajax_storefront.html", tmp_path / "a"), tmp_path / "a"
    )
    second = _cart_state(
        _crawl(f"{base_url}/ajax_storefront.html", tmp_path / "b"), tmp_path / "b"
    )
    assert first is not None and second is not None
    assert first["state_hash"] == second["state_hash"]


# --------------------------------------------------------------------------- #
# Navigation must be unaffected
# --------------------------------------------------------------------------- #
def test_navigation_clicks_still_capture_the_same_states(tmp_path):
    """The predicate handles a plain navigation as well as `networkidle` did.

    HN is all link-follows and has no action to lose, so the fix must be inert
    here: same states, same order.
    """
    states = _crawl(HN_FIXTURE.resolve().as_uri(), tmp_path, max_states=2)
    assert len(states) == 2
    assert states[0]["clicks"] == []
    assert len(states[1]["clicks"]) == 1
    assert len({s["state_hash"] for s in states}) == 2


# --------------------------------------------------------------------------- #
# The ceiling
# --------------------------------------------------------------------------- #
async def _click_and_settle(base_url: str, page_name: str, selector: str):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(f"{base_url}/{page_name}", wait_until="networkidle")
        inflight = _Inflight(page)
        await page.click(selector)
        started = time.monotonic()
        settled = await _settle_after_click(page, inflight)
        elapsed = time.monotonic() - started
        await browser.close()
        return settled, elapsed


def test_a_never_returning_request_does_not_stall_the_crawl(base_url):
    """A request that never comes back must NOT hold the crawl hostage.

    Real pages fire these constantly — scrapingcourse alone leaves a Google
    Analytics beacon and ~20 same-origin prefetches permanently outstanding. An
    earlier draft waited for zero in-flight requests and could therefore never
    settle on any such page: it rode the ceiling every time and captured
    whatever had rendered by then, differently on each run.
    """
    settled, elapsed = _run(_click_and_settle(base_url, "hang_fixture.html", "#hang"))

    assert settled is True, "a stale request must stop being waited for, not stall"
    assert elapsed < 6.0, f"settled only via the ceiling ({elapsed:.1f}s)"


def test_settle_gives_up_when_the_page_never_stops_changing(base_url, caplog):
    """THIS is what the ceiling is for: a DOM that will never be stable to hash."""
    with caplog.at_level("INFO"):
        settled, elapsed = _run(
            _click_and_settle(base_url, "churn_fixture.html", "#churn")
        )

    assert settled is False, "endless structural churn must report an unsettled page"
    assert 10.0 <= elapsed <= 20.0, elapsed  # proceeds at the ceiling, doesn't hang
    assert any("ceiling" in r.message for r in caplog.records), (
        "proceeding on an unsettled page must be logged, never silent"
    )
