"""Tests for synthesis-based stateful flow capture + edge provenance (Step 10-pre).

Two gaps this step closes:

1. **States had no recorded edges.** The crawler computed the click-path that
   reached each state and then dropped it at the CLI boundary, so evidence was a
   set of states with no relation between them. Anything downstream asking "which
   affordance produced this cart?" would have to guess — the reconstruct-by-
   inference trap this project refuses at every layer.

2. **A populated cart was unreachable.** BFS only follows links, and no href leads
   to a cart with something in it. Synthesis derives a click-path from a REAL
   captured affordance ([add, cart]) and hands it to the existing replay.

The faithfulness line runs through both: act only on an affordance the page really
has. No add-to-cart -> no path -> no cart state (honest absence). The alternative —
inferring "this looks like a shop, so give it a cart" — is the `Story 4..8`
fabrication one layer up.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mockbuilder.crawler.crawler import Crawler, synthesize_cart_path

FIXTURES = Path(__file__).parent / "fixtures"
STOREFRONT_FIXTURE = FIXTURES / "storefront_fixture.html"
HN_FIXTURE = FIXTURES / "hn_fixture.html"


def _run(coro):
    """Run an async coroutine from a sync test (no loop is running here)."""
    return asyncio.run(coro)


def _crawl(fixture: Path, evidence_dir: Path, max_states: int = 4) -> list[dict]:
    """Run a real crawl of a fixture into an isolated evidence dir."""
    return _run(
        Crawler(evidence_dir=evidence_dir).crawl(
            fixture.resolve().as_uri(), max_states=max_states
        )
    )


def _provenance(evidence_dir: Path, state_hash: str) -> dict:
    path = evidence_dir / f"{state_hash}_provenance.json"
    assert path.exists(), f"no provenance persisted for state {state_hash}"
    return json.loads(path.read_text(encoding="utf-8"))


def _cart_state(states: list[dict], evidence_dir: Path) -> dict | None:
    """The state reached by a synthesized multi-click path, if any.

    Identified by its PROVENANCE (an edge whose `via` is an add-to-cart), never by
    poking at page content — the provenance is the thing under test.
    """
    for state in states:
        prov = _provenance(evidence_dir, state["state_hash"])
        via = prov.get("via") or {}
        if "add to cart" in (via.get("text") or "").lower():
            return state
    return None


# --------------------------------------------------------------------------- #
# Affordance detection (pure — no browser needed)
# --------------------------------------------------------------------------- #
def test_detection_is_anchored_not_substring():
    """Labels fullmatch. A substring match would eat real, unrelated elements.

    These are not hypothetical: `$0.00 0 items` and `Cart\\t\\t\\t\\t0` are the
    actual labels of two other cart-ish links on the live site this was built
    against, and a product genuinely named "...Add to Cart Guide" must not become
    a click target.
    """
    elements = [
        {"tag": "a", "text": "How to Add to Cart Guide", "selector": "#guide"},
        {"tag": "a", "text": "$0.00 0 items", "selector": "#contents"},
        {"tag": "a", "text": "Cart\t\t\t\t0", "selector": "#footer-cart"},
        {"tag": "a", "text": "Checkout", "selector": "#checkout"},
    ]
    assert synthesize_cart_path(elements) is None


def test_detection_folds_whitespace_and_case():
    """Real labels carry markup whitespace and arbitrary casing."""
    elements = [
        {"tag": "a", "text": "  ADD TO\n  Cart ", "selector": "#add"},
        {"tag": "a", "text": "My  Cart", "selector": "#cart"},
    ]
    synthesized = synthesize_cart_path(elements)
    assert synthesized is not None
    add, cart = synthesized
    assert add["selector"] == "#add"
    assert cart["selector"] == "#cart"


def test_detection_picks_first_add_in_document_order():
    """The action choice is what makes the whole crawl reproducible."""
    elements = [
        {"tag": "a", "text": "Cart", "selector": "#cart"},
        {"tag": "a", "text": "Add to cart", "selector": "#add-first"},
        {"tag": "a", "text": "Add to cart", "selector": "#add-second"},
    ]
    add, _ = synthesize_cart_path(elements)
    assert add["selector"] == "#add-first"


def test_no_cart_link_synthesizes_nothing():
    """An add with nowhere to observe its effect is not a usable flow."""
    elements = [{"tag": "a", "text": "Add to cart", "selector": "#add"}]
    assert synthesize_cart_path(elements) is None


def test_cart_link_is_found_by_target_when_its_label_is_a_price():
    """The real header cart widget says "$0.00 0 items" — a price, not a word.

    Its only statement of what it is, is where it goes. Label-only detection is
    blind to it and would call a storefront cartless.
    """
    elements = [
        {"tag": "a", "text": "Add to cart", "selector": "#add"},
        {
            "tag": "a",
            "text": "$0.00 0 items",
            "selector": '[data-testid="cart-contents"]',
            "href": "https://shop.example/ecommerce/cart/",
        },
    ]
    add, cart = synthesize_cart_path(elements)
    assert add["selector"] == "#add"
    assert cart["selector"] == '[data-testid="cart-contents"]'


def test_add_to_carts_own_href_is_not_mistaken_for_the_cart():
    """`?add-to-cart=2765` contains "cart" but is the ADD, not the cart.

    Matching the raw URL as a substring would resolve the cart link back to the
    add button and synthesize the nonsense path [add, add]. Only a whole PATH
    SEGMENT counts, and the query string is ignored.
    """
    elements = [
        {
            "tag": "a",
            "text": "Add to cart",
            "selector": "#add",
            "href": "https://shop.example/ecommerce/?add-to-cart=2765",
        },
    ]
    # the add is present, but nothing here is a cart link
    assert synthesize_cart_path(elements) is None


def test_checkout_is_not_a_cart():
    """A neighbouring path segment must not be swept in."""
    elements = [
        {"tag": "a", "text": "Add to cart", "selector": "#add"},
        {
            "tag": "a",
            "text": "Checkout",
            "selector": "#checkout",
            "href": "https://shop.example/ecommerce/checkout/",
        },
    ]
    assert synthesize_cart_path(elements) is None


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
def test_every_captured_state_has_provenance(tmp_path):
    states = _crawl(STOREFRONT_FIXTURE, tmp_path)
    assert states
    for state in states:
        prov = _provenance(tmp_path, state["state_hash"])
        assert prov.keys() == {"url", "from_state", "clicks", "via"}


def test_landing_state_has_no_incoming_edge(tmp_path):
    """The landing state was reached by navigation, not by an affordance."""
    states = _crawl(STOREFRONT_FIXTURE, tmp_path)
    landing = states[0]
    prov = _provenance(tmp_path, landing["state_hash"])

    assert prov["from_state"] is None
    assert prov["via"] is None
    assert prov["clicks"] == []
    assert prov["url"] == STOREFRONT_FIXTURE.resolve().as_uri()


def test_link_reached_state_records_its_edge(tmp_path):
    """A link-follow records the parent, the path, and the REAL affordance label."""
    states = _crawl(STOREFRONT_FIXTURE, tmp_path)
    landing_hash = states[0]["state_hash"]

    edges = [_provenance(tmp_path, s["state_hash"]) for s in states[1:]]
    link_edges = [e for e in edges if len(e["clicks"]) == 1]
    assert link_edges, "expected at least one link-reached state"

    for edge in link_edges:
        assert edge["from_state"] == landing_hash
        assert edge["via"] is not None
        # the label is the point: a selector cannot say what the edge DOES
        assert edge["via"]["text"]
        assert edge["via"]["selector"] == edge["clicks"][0]

    # the cart link is the only link-reachable branch (Home is a self-link), and
    # the edge records it by its real label
    assert {e["via"]["text"] for e in link_edges} == {"Cart"}


# --------------------------------------------------------------------------- #
# Stateful capture — the headline
# --------------------------------------------------------------------------- #
def test_synthesized_path_captures_the_populated_cart(tmp_path):
    """A state that link-following alone can NEVER reach now exists in evidence."""
    states = _crawl(STOREFRONT_FIXTURE, tmp_path)
    cart = _cart_state(states, tmp_path)
    assert cart is not None, "the synthesized add->cart flow captured no state"

    prov = _provenance(tmp_path, cart["state_hash"])
    # provenance names the ADD as the cause — the cart link only navigates to
    # where the effect is observable.
    assert prov["via"]["text"] == "Add to cart"
    assert prov["via"]["selector"] == prov["clicks"][0]
    assert prov["from_state"] == states[0]["state_hash"]
    # the full ordered path, not just the last hop
    assert len(prov["clicks"]) == 2

    elements = json.loads(Path(cart["elements"]).read_text(encoding="utf-8"))
    assert any(e["selector"] == "#shop-link" for e in elements), "not the cart page"


def test_synthesis_skips_the_unclickable_cart_link(tmp_path):
    """The collapsed menu's "Cart" is first in document order — and a trap.

    It has a real box and is visible to the DOM, but no click can reach it. Acting
    on it times out, abandons the path, and reports a storefront as cartless. This
    is the live failure that label-only, unfiltered detection actually produced.
    """
    states = _crawl(STOREFRONT_FIXTURE, tmp_path)
    cart = _cart_state(states, tmp_path)
    assert cart is not None, "synthesis picked the unclickable cart link"

    prov = _provenance(tmp_path, cart["state_hash"])
    assert prov["clicks"][1] == "#cart-link"
    assert prov["clicks"][1] != "#hidden-cart"


def test_captured_cart_really_is_populated(tmp_path):
    """The add had an EFFECT — the populated cart is not the empty cart page.

    Both carts are captured here: BFS follows the Cart link to the EMPTY one,
    synthesis reaches the POPULATED one. Distinct hashes prove the action produced
    a structural change, which is the whole reason a cart needs synthesizing
    rather than link-following.
    """
    states = _crawl(STOREFRONT_FIXTURE, tmp_path)

    populated = _cart_state(states, tmp_path)
    assert populated is not None

    empty = next(
        (
            s
            for s in states
            if (_provenance(tmp_path, s["state_hash"]).get("via") or {}).get("text")
            == "Cart"
        ),
        None,
    )
    assert empty is not None, "expected BFS to reach the empty cart via its link"

    assert populated["state_hash"] != empty["state_hash"]


def test_the_first_add_in_document_order_is_the_one_acted_on(tmp_path):
    """Determinism, observed end-to-end: the cart holds the FIRST product's item.

    The fixture offers two add-to-cart affordances. If the action choice were not
    first-in-document-order, the cart could hold the other item and the state hash
    would differ run to run.
    """
    states = _crawl(STOREFRONT_FIXTURE, tmp_path)
    cart = _cart_state(states, tmp_path)
    assert cart is not None

    elements = json.loads(Path(cart["elements"]).read_text(encoding="utf-8"))
    labels = [e.get("text") for e in elements]
    assert "Aero Daily Fitness Tee" in labels, labels
    assert "Affirm Water Bottle" not in labels, labels


def test_cart_state_is_new_evidence_not_a_relabelled_landing(tmp_path):
    """The cart is a distinct state, not the landing page under another name."""
    states = _crawl(STOREFRONT_FIXTURE, tmp_path)
    cart = _cart_state(states, tmp_path)
    assert cart is not None
    assert cart["state_hash"] != states[0]["state_hash"]
    assert len({s["state_hash"] for s in states}) == len(states)


# --------------------------------------------------------------------------- #
# Honest absence — the faithfulness proof
# --------------------------------------------------------------------------- #
def test_hn_affordances_synthesize_no_path(tmp_path):
    """Against HN's REAL discovered elements, detection declines to fire.

    Checked directly on the affordances the crawler actually found, so this fails
    if the patterns ever loosen enough to match ordinary link text.
    """
    states = _crawl(HN_FIXTURE, tmp_path, max_states=1)
    elements = json.loads(Path(states[0]["elements"]).read_text(encoding="utf-8"))
    assert elements, "expected HN's landing state to discover elements"

    assert synthesize_cart_path(elements) is None


def test_page_without_add_to_cart_captures_no_cart(tmp_path):
    """HN has no add-to-cart. No phantom cart may appear in its evidence.

    If detection ever fires here it is matching something it shouldn't — and a
    fabricated affordance is exactly the defect this architecture exists to kill.
    """
    states = _crawl(HN_FIXTURE, tmp_path, max_states=2)
    assert states
    assert _cart_state(states, tmp_path) is None


def test_hn_captured_states_are_unchanged(tmp_path):
    """No regression: synthesis is additive, it does not perturb link-following.

    The state SET for a page with no add-to-cart must be exactly what BFS alone
    produces — same hashes, same order.
    """
    with_synthesis = [
        s["state_hash"] for s in _crawl(HN_FIXTURE, tmp_path / "a", max_states=2)
    ]

    # BFS-only baseline: the same crawl with the synthesis seam stubbed out.
    import mockbuilder.crawler.crawler as crawler_mod

    original = crawler_mod.synthesize_cart_path
    crawler_mod.synthesize_cart_path = lambda elements: None
    try:
        baseline = [
            s["state_hash"] for s in _crawl(HN_FIXTURE, tmp_path / "b", max_states=2)
        ]
    finally:
        crawler_mod.synthesize_cart_path = original

    assert with_synthesis == baseline


# --------------------------------------------------------------------------- #
# Determinism — the contract check
# --------------------------------------------------------------------------- #
def test_two_crawls_produce_identical_cart_hash(tmp_path):
    """Two full crawls agree on the cart's hash.

    normalize_dom already strips the volatile parts of a mutated page, so the only
    determinism requirement is that both crawls choose the SAME action — which
    first-in-document-order guarantees.
    """
    first = _crawl(STOREFRONT_FIXTURE, tmp_path / "first")
    second = _crawl(STOREFRONT_FIXTURE, tmp_path / "second")

    cart_first = _cart_state(first, tmp_path / "first")
    cart_second = _cart_state(second, tmp_path / "second")
    assert cart_first is not None and cart_second is not None
    assert cart_first["state_hash"] == cart_second["state_hash"]

    # and the whole crawl, not just the cart
    assert [s["state_hash"] for s in first] == [s["state_hash"] for s in second]
