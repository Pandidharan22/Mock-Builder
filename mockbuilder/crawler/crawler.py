"""Async Playwright crawler that captures evidence for a source app.

Phase 1 performs a breadth-first crawl: starting from a URL, it captures the
landing state and then follows a couple of clickable elements per state to
discover new ones. Each state is keyed by the hash of its *normalized* DOM, so
structurally-identical renders collapse to one node and the crawl terminates
instead of looping forever.

Some states are reachable only through an ACTION, not a link: no href leads to a
populated cart. So besides following links, the crawler synthesizes a click-path
from a real captured affordance (see :func:`synthesize_cart_path`) and replays it
to capture the resulting state.

For every newly-seen state we persist:
  * ``evidence/{state_hash}.png``             — full-page screenshot
  * ``evidence/{state_hash}_elements.json``   — discovered actionable elements
  * ``evidence/{state_hash}_records.json``    — extracted repeating-unit records
  * ``evidence/{state_hash}_provenance.json`` — the EDGE that produced this state
  * ``evidence/design_tokens.json``           — harvested computed styles
  * ``evidence/fixtures/*.json``              — captured JSON API responses

Provenance is what keeps a captured state from being an orphan: evidence would
otherwise be a set of states with no recorded relation between them, and anything
downstream wanting to know "which affordance produced this cart" would have to
guess. The crawler already computes the path; it just used to drop it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from ..recorder.design_tokens import harvest_tokens, save_tokens
from ..recorder.network import attach_network_listener
from .dom import discover_elements, normalize_dom
from .records import extract_records_async

logger = logging.getLogger(__name__)

# Project root is two levels above this file: <root>/mockbuilder/crawler/crawler.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = PROJECT_ROOT / "evidence"

# How many clickable elements we branch on from each newly-discovered state.
_BRANCH_FACTOR = 2

# Decides whether a candidate is usable, evaluated in-page on the settled DOM.
# Both halves are DISQUALIFICATIONS ("can't be clicked", "goes nowhere new"), not
# preferences: this removes non-candidates from the input, it never ranks what's
# left — the selection heuristic (first N in DOM order) is unchanged.
#
# The two halves are independently selectable via `requireNewTarget`, because they
# answer different questions and only one is universal:
#   * clickable        — can this element receive a click? Always required.
#   * goesSomewhereNew — does following it reach a different page? Required when
#                        BRANCHING (a self-link wastes crawl budget), but WRONG for
#                        an ACTION: an add-to-cart is a mutation, not a navigation,
#                        and legitimately carries `href="#"` or no href at all.
# They stay in one definition rather than two so "clickable" can never drift into
# meaning different things in different callers.
#
# Filters by PROPERTY, never by name — enumerating known offenders
# ("skip-to-content", "sr-only") breaks on the next site that names them
# differently; a property generalizes.
#
# The case that motivated this: accessibility skip-links. They are the universal
# visually-hidden pattern — `position:absolute` at **1x1 px** with
# `clip-path: inset(50%)`. They are NOT display:none, NOT visibility:hidden, they
# DO have an offsetParent, and their size is nonzero — so the obvious checks pass
# them. Only a *meaningful box* separates them from a real target. Playwright's
# click times out on them (they can't receive the event), which killed the crawl
# at one state on every accessible site.
#
# Note: deliberately NOT using `offsetParent` — it is null for `position:fixed`
# elements, which would wrongly exclude legitimate sticky/fixed nav links.
_CLICKABLE_JS = r"""
({ selectors, requireNewTarget }) => {
  // An element clipped away by an ANCESTOR is not clickable either — checking
  // only the element's own box misses collapsed containers (e.g. a menu with
  // overflow:hidden + max-height:0, whose children keep a real box but can never
  // receive a click). Playwright times out on these exactly like a skip-link.
  const clippedByAncestor = (el, r) => {
    let p = el.parentElement;
    while (p) {
      const cs = getComputedStyle(p);
      const clips = cs.overflow === 'hidden' || cs.overflowX === 'hidden'
                 || cs.overflowY === 'hidden' || cs.clipPath !== 'none';
      if (clips) {
        const pr = p.getBoundingClientRect();
        if (pr.width <= 1 || pr.height <= 1) return true;   // ancestor collapsed
        const outside = r.right <= pr.left || r.left >= pr.right
                     || r.bottom <= pr.top || r.top >= pr.bottom;
        if (outside) return true;                            // clipped out of view
      }
      p = p.parentElement;
    }
    return false;
  };

  const clickable = (el) => {
    if (!el) return false;
    if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true') return false;
    if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) {
      return false;
    }
    const r = el.getBoundingClientRect();
    // > 1 (not > 0): the 1x1 clipped visually-hidden pattern must not pass.
    if (!(r.width > 1 && r.height > 1)) return false;
    return !clippedByAncestor(el, r);
  };

  // A link whose resolved target is the page we're already on is not a branch —
  // it's the same state by definition (dedup proves it after the fact by
  // rejecting the visit). Dropping it beforehand removes a non-candidate from the
  // input; it does NOT rank candidates. Fragment-only differences are the same
  // document, so they count as self-links too.
  const goesSomewhereNew = (el) => {
    const href = el.getAttribute('href');
    if (href === null) return true;  // no href (e.g. a button) — not a self-link
    const raw = href.trim();
    if (raw === '' || raw === '#') return false;
    let target;
    try {
      target = new URL(raw, document.baseURI);
    } catch (e) {
      return false;  // unresolvable target is not a usable branch
    }
    const here = new URL(document.location.href);
    return !(
      target.origin === here.origin
      && target.pathname === here.pathname
      && target.search === here.search
    );
  };

  return selectors.map((sel) => {
    let el = null;
    try {
      el = document.querySelector(sel);
    } catch (e) {
      return false;  // an unparseable selector is not a usable branch
    }
    if (!clickable(el)) return false;
    return requireNewTarget ? goesSomewhereNew(el) : true;
  });
}
"""


# --------------------------------------------------------------------------- #
# Settle predicate (Step 10-pre-fix2)
# --------------------------------------------------------------------------- #
# `page.click()` returns as soon as the event is dispatched, so something must
# wait for the page to actually REACT before we read its state.
#
# `wait_for_load_state("networkidle")` was that something, and it is wrong here.
# Load states are per-DOCUMENT LIFECYCLE events, not a live view of the network:
# once `networkidle` has fired for the current document (which `goto(wait_until=
# "networkidle")` guarantees on arrival), asking for it again returns IMMEDIATELY
# no matter how many requests are in flight right now. It waits for navigations
# and is blind to everything after them.
#
# That blindness shipped a real defect. A WooCommerce add is `ajax_add_to_cart`:
# it preventDefaults its own href (the href is a no-JS fallback, NOT a
# navigation), fires an XHR, and mutates the DOM when the response lands. The
# crawler clicked it, "waited" for a lifecycle event that had already fired, read
# the page ~0ms later, captured the state the add had not yet changed, and stamped
# `via: "Add to cart"` on it — a false record of what happened.
#
# So wait for QUIESCENCE, measured live, not for a lifecycle event:
#   * no requests in flight (this is what catches the AJAX add), AND
#   * no DOM mutations for a quiet window (this is what catches the effect
#     landing — the response arriving is not the same moment as the DOM settling),
#   * with a ceiling, because some pages never go quiet (long-poll, analytics,
#     a hung endpoint) and a crawl must not hang on them.
#
# Both signals are required and neither is sufficient. Network-only would return
# while the response is still being rendered; DOM-only would declare victory in
# the silent gap between the click and the response — the very window this bug
# lives in.
#
# `networkidle` is still used, for the one job it is right for: waiting out a
# NAVIGATION's post-load requests. It was never broken at that — it is broken as a
# general "has the page reacted" question. The two stages compose: networkidle
# covers the new-document case (and no-ops for in-place clicks, where it was the
# bug), quiescence covers the in-place case. Neither alone spans both.
#
# Quiescence is measured on the NORMALIZED DOM — the exact artifact the state hash
# is taken from — and not on raw MutationObserver events. That is deliberate and
# was also learned by measuring: scrapingcourse mutates its DOM continuously
# (analytics and text churn), so a raw-mutation quiet window NEVER closed and
# every settle ran to the ceiling, capturing whatever had rendered by then —
# different on each run. But `normalize_dom` strips text and every attribute but
# class/role/data-testid, so none of that churn can move the hash. Waiting for the
# thing we actually measure to stop changing is both the honest question and the
# only one that can be answered stably: settle is defined as "what we are about to
# hash has stopped changing".
_SETTLE_QUIET_MS = 500       # normalized DOM must be unchanged this long
_SETTLE_CEILING_MS = 12000   # hard cap; past this we proceed and log
_SETTLE_POLL_MS = 100
# A request outstanding longer than this is presumed fire-and-forget and stops
# being waited for. NOT a guess — measured on scrapingcourse's cart page:
#   * a Google Analytics beacon (`google.com/g/collect`) that never completes, and
#   * ~20 SAME-ORIGIN `other` requests fired at once (prefetch/speculation) that
#     also never complete.
# So "zero requests in flight" is simply unreachable there, and it is unreachable
# on most of the real web. Requiring it made the predicate degenerate into exactly
# the fixed sleep this step exists to avoid: every settle rode the ceiling and
# captured whatever had rendered by then — one run caught the cart mid-render (17
# elements, no rows), the next caught it complete (25 elements). Non-deterministic,
# which is worse than the bug being fixed. Note origin filtering would NOT have
# helped: the prefetches are same-origin.
#
# THE TRADE-OFF, stated plainly: this must exceed the slowest request whose
# response we genuinely need, and stay under the time a never-completing request
# is allowed to block us. There is no value that is right for every site. 1500ms
# clears a normal add (scrapingcourse's is ~300ms; the AJAX fixture's is 800ms) and
# drops the dead prefetches quickly. A legitimately slower-than-1.5s action would
# be under-waited — the failure mode to watch for, and the reason a settle that
# hits the ceiling is logged rather than silent.
_SETTLE_STALE_MS = 1500

class _Inflight:
    """Tracks requests the page has outstanding, and how long they've been so.

    Playwright exposes no "is the network busy right now" API — only the
    per-document lifecycle states that caused this bug — so we track events. Must
    be attached BEFORE the click: a click handler fires its XHR synchronously, so
    a counter attached afterwards would miss the very request it exists to see.
    """

    def __init__(self, page: Any) -> None:
        self._pending: dict[Any, float] = {}
        page.on("request", self._start)
        page.on("requestfinished", self._end)
        page.on("requestfailed", self._end)

    def _start(self, request: Any) -> None:
        self._pending[request] = time.monotonic()

    def _end(self, request: Any) -> None:
        self._pending.pop(request, None)

    def busy(self) -> int:
        """Requests young enough that their response could still change the page.

        Anything older than ``_SETTLE_STALE_MS`` is presumed fire-and-forget (an
        analytics beacon that will never complete) and excluded, so one tracker
        cannot make the page look permanently busy.
        """
        cutoff = time.monotonic() - _SETTLE_STALE_MS / 1000
        return sum(1 for started in self._pending.values() if started > cutoff)


async def _settle_after_click(page: Any, inflight: _Inflight) -> bool:
    """Wait until the page has actually reacted to a click. True if it settled.

    Handles both kinds of click with one predicate: a NAVIGATION (wait for the new
    document, then for it to go quiet) and an IN-PLACE MUTATION (no navigation at
    all — quiescence is the only signal there is).

    Deterministic by construction: we return only from a page with no requests in
    flight that has not changed for ``_SETTLE_QUIET_MS``, so the state we then hash
    is a state that has stopped moving. Returns False if the ceiling was hit, which
    is logged — a crawl that proceeds on an unsettled page is a recorded compromise,
    never a silent one.
    """
    deadline = time.monotonic() + _SETTLE_CEILING_MS / 1000

    def _remaining_ms() -> float:
        return max(0.0, (deadline - time.monotonic()) * 1000)

    # STAGE 1 — the navigation, if there was one. `networkidle` is not useless; it
    # is the RIGHT tool for exactly this and blind to everything else. For a NEW
    # document its lifecycle has not fired yet, so it genuinely waits for the
    # post-load XHRs — which is what a client-rendered page needs (WooCommerce's
    # cart block fetches its contents from the Store API *after* load, so the page
    # loads, goes quiet, and only then wakes up to render the cart). For an
    # in-place click nothing navigated, the state already fired, and both calls
    # return instantly — costing nothing precisely where they were never any use.
    try:
        await page.wait_for_load_state("load", timeout=_remaining_ms())
        await page.wait_for_load_state("networkidle", timeout=_remaining_ms())
    except Exception:
        pass  # a slow/absent navigation is the ceiling's problem, not an error

    # STAGE 2 — quiescence of the normalized DOM (the artifact we hash).
    fingerprint: str | None = None
    stable_since = time.monotonic()

    while True:
        try:
            current = hashlib.sha256(
                (await normalize_dom(page)).encode("utf-8")
            ).hexdigest()
        except Exception:
            # A navigation mid-poll destroys the execution context; treat it as a
            # change and re-read against the new document on the next pass.
            current = None

        now = time.monotonic()
        if current != fingerprint:
            fingerprint = current
            stable_since = now
        stable_ms = (now - stable_since) * 1000

        if inflight.busy() == 0 and stable_ms >= _SETTLE_QUIET_MS:
            return True

        if now >= deadline:
            logger.info(
                "settle: ceiling %dms reached (busy=%d, stable=%.0fms); "
                "proceeding on an unsettled page",
                _SETTLE_CEILING_MS,
                inflight.busy(),
                stable_ms,
            )
            return False

        await page.wait_for_timeout(_SETTLE_POLL_MS)


# --------------------------------------------------------------------------- #
# Affordance synthesis (Step 10-pre)
# --------------------------------------------------------------------------- #
# BFS can only reach states via link-follows, so a state that exists only AFTER a
# state-changing action is unreachable by traversal alone: no link leads to a
# populated cart. Synthesis closes that gap — it derives a click-path from a REAL
# captured affordance and hands it to the existing replay.
#
# Detection reads an element's MEANING — its label and its target — never its
# selector or class: "Add to cart" means the same thing on every storefront,
# whereas `.ajax_add_to_cart` is one site's private vocabulary. Matching meaning
# generalizes; matching a class is per-app code, which is what this project
# refuses to write.
#
# An ACTION is identified by its label alone: a mutation announces itself in words
# ("Add to cart") and its href, where one exists, is site-private query vocabulary
# (`?add-to-cart=2765`) that must not be matched.
#
# A NAVIGATION is identified by label OR target, because a link's intent lives in
# either — and real storefront headers routinely express it only in the target.
# The one clickable cart link on the site this was built against is labelled
# "$0.00 0 items": a price, containing no word for what it is. Its meaning is in
# `/cart/`. Label-only detection cannot see that link, and the ones it CAN see
# (a nav "Cart" collapsed inside a hamburger, a 0x0 footer "Cart 0") are not
# clickable — so label-only detection finds nothing here and would report a cart
# site as cartless.
#
# Both signals are ANCHORED, exactly like role inference in records.py:
#   * labels FULLMATCH the whitespace-collapsed, lowercased text. A substring
#     match would eat a product named "...Add to Cart Guide", and would misread
#     the "cart"-containing labels above as cart links.
#   * targets match a whole PATH SEGMENT, and the query string is deliberately
#     ignored. `/ecommerce/cart/` -> segment "cart" -> a cart link;
#     `?add-to-cart=2765` -> path `/ecommerce/` -> NOT a cart link, though the raw
#     URL plainly contains the substring "cart". Segment equality is what keeps
#     the add button from being mistaken for the cart it feeds.
# The deliberate trade is records.py's: an unanticipated phrasing falls through and
# NOTHING is synthesized (honest absence) rather than the wrong element being
# clicked (silent corruption). If a real site is missed, WIDEN THE ANCHORED
# PATTERN — never loosen the anchor to a substring.
_ADD_TO_CART_RE = re.compile(r"add to (?:cart|basket|bag)")
_CART_LINK_RE = re.compile(r"(?:(?:view|open|my|shopping)\s+)*(?:cart|basket|bag)")
_CART_PATH_SEGMENTS = frozenset({"cart", "basket", "bag"})


def _normalize_label(text: str | None) -> str:
    """Collapse a raw label to a comparable form: whitespace-folded, lowercased.

    Real labels carry markup whitespace ("Cart\\t\\t\\t\\t0" -> "cart 0"), so
    folding is what makes an anchored match usable at all.
    """
    return " ".join((text or "").split()).lower()


def _targets_cart(href: str | None) -> bool:
    """True if ``href``'s PATH names a cart, ignoring any query string."""
    if not href:
        return False
    path = urlparse(href).path
    return any(seg.lower() in _CART_PATH_SEGMENTS for seg in path.split("/") if seg)


def _is_add_to_cart(el: dict[str, Any]) -> bool:
    """An add-to-cart action, by its label only (see the note above on targets)."""
    return bool(_ADD_TO_CART_RE.fullmatch(_normalize_label(el.get("text"))))


def _is_cart_link(el: dict[str, Any]) -> bool:
    """A link to the cart, by what it SAYS or by where it GOES."""
    return bool(
        _CART_LINK_RE.fullmatch(_normalize_label(el.get("text")))
    ) or _targets_cart(el.get("href"))


def _first_where(
    elements: list[dict[str, Any]], predicate: Any
) -> dict[str, Any] | None:
    """First element in DOCUMENT ORDER satisfying ``predicate``.

    First-in-document-order is the whole determinism requirement for synthesis:
    ``normalize_dom`` already strips the volatile parts of a mutated page (text
    and every attribute but class/role/data-testid), so two crawls agree on the
    resulting hash as long as they agree on WHICH element to act on.
    """
    for el in elements:
        if el.get("selector") and predicate(el):
            return el
    return None


def synthesize_cart_path(
    elements: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Derive the (add-to-cart, cart-link) affordance pair for a stateful path.

    ``elements`` must already be filtered to CLICKABLE elements (see
    :meth:`Crawler._filter_clickable`); this function is pure so the detection
    rules stay testable without a browser. Passing unfiltered elements is what
    makes it choose a cart link buried in a collapsed hamburger menu — visible to
    the DOM, unclickable in practice, and a 5s replay timeout that abandons the
    path and reports a false absence.

    Returns ``None`` — synthesizing NOTHING — unless the page really offers both
    affordances. This is the faithfulness line: the action must come from an
    affordance the page actually has, never from inferring "it looks like a shop,
    so give it a cart". A page with no add-to-cart (Hacker News) yields no path
    and therefore no captured cart, which is the honest answer.
    """
    add = _first_where(elements, _is_add_to_cart)
    if add is None:
        return None
    cart = _first_where(elements, _is_cart_link)
    if cart is None:
        return None
    return add, cart


class Crawler:
    """Drives a headless browser to capture app state as evidence via BFS."""

    def __init__(self, evidence_dir: Path | str = EVIDENCE_DIR) -> None:
        self.evidence_dir = Path(evidence_dir)

    async def crawl(self, url: str, max_states: int = 3) -> list[dict[str, Any]]:
        """Breadth-first crawl from ``url``, capturing up to ``max_states`` states.

        The queue holds *actions*: each item is the base URL plus an ordered list
        of selectors to click to reach a state (replayed from the base URL each
        time — simple and drift-free). A state is only recorded the first time its
        normalized-DOM hash is seen. Returns one record per captured state.

        Each item also carries the EDGE that produced it — ``from_state`` (the
        parent's hash) and ``via`` (the affordance clicked) — which ``_visit``
        persists as that state's provenance. The landing state has no incoming
        edge, so both are ``None``.
        """
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

        captured: list[dict[str, Any]] = []
        visited_states: set[str] = set()

        # Each queue item:
        #   {"url": base_url, "clicks": [selector, ...],
        #    "from_state": parent_hash | None, "via": element_dict | None}
        queue: list[dict[str, Any]] = [
            {"url": url, "clicks": [], "from_state": None, "via": None}
        ]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                while queue and len(captured) < max_states:
                    item = queue.pop(0)
                    record = await self._visit(
                        browser, item, visited_states, queue
                    )
                    if record is not None:
                        captured.append(record)
            finally:
                await browser.close()

        print(f"BFS complete: captured {len(captured)} unique state(s).")
        return captured

    async def _visit(
        self,
        browser: Any,
        item: dict[str, Any],
        visited_states: set[str],
        queue: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Reach one state (navigate + replay clicks), record it if new, and
        enqueue follow-up actions. Returns the record, or ``None`` if the state
        was already visited or the path could not be reached."""
        base_url = item["url"]
        clicks: list[str] = item["clicks"]

        # Fresh page per state so replayed click-paths start from a clean slate.
        page = await browser.new_page()
        try:
            # Attach the network recorder BEFORE navigation so no early API
            # responses are missed.
            await attach_network_listener(page, self.evidence_dir)

            # Attach BEFORE anything navigates or clicks: a click handler fires
            # its XHR synchronously, so a counter attached later misses it.
            inflight = _Inflight(page)

            await page.goto(base_url, wait_until="networkidle")

            # Replay the click path that defines this state.
            for selector in clicks:
                try:
                    await page.click(selector, timeout=5000)
                    # Wait for the page to actually REACT (see _settle_after_click).
                    # `wait_for_load_state("networkidle")` used to live here and is
                    # a no-op after the first document: it silently discarded every
                    # AJAX action's effect.
                    await _settle_after_click(page, inflight)
                except Exception:
                    # A step in the path could not be reached; abandon it.
                    return None

            normalized_dom = await normalize_dom(page)
            state_hash = hashlib.sha256(normalized_dom.encode("utf-8")).hexdigest()

            if state_hash in visited_states:
                # Structurally-identical to a state we've already recorded.
                return None
            visited_states.add(state_hash)

            elements = await discover_elements(page)

            # Persist evidence for this new state.
            screenshot_path = self.evidence_dir / f"{state_hash}.png"
            elements_path = self.evidence_dir / f"{state_hash}_elements.json"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            elements_path.write_text(json.dumps(elements, indent=2), encoding="utf-8")

            # Harvest design tokens from the new state.
            tokens = await harvest_tokens(page)
            tokens_path = save_tokens(tokens, self.evidence_dir)

            # Extract the page's real records (the DATA track). This must NEVER
            # abort a crawl: on any failure we log ERROR and still write a valid
            # sentinel. The two empty paths stay distinguishable by value:
            #   * legitimate empty  -> {"collections": []}               , INFO only
            #   * extraction crashed -> {"collections": [], "error": true}, ERROR logged
            records_path = self.evidence_dir / f"{state_hash}_records.json"
            try:
                extraction = await extract_records_async(page)
                records_payload = extraction.to_dict()
                summary = "; ".join(
                    f"[{c.rank}] count={c.count} score={c.score:g} sig={c.signature}"
                    for c in extraction.collections
                )
                logger.info(
                    "records: state=%s collections=%d %s",
                    state_hash,
                    len(extraction.collections),
                    summary,
                )
            except Exception:
                logger.error(
                    "record extraction failed for state=%s; writing empty result",
                    state_hash,
                    exc_info=True,
                )
                records_payload = {"collections": [], "error": True}
            records_path.write_text(
                json.dumps(records_payload, indent=2), encoding="utf-8"
            )

            # Persist the EDGE that produced this state, alongside the state's own
            # evidence. `clicks` is the full path from `url` (not just the last
            # hop), which is exactly what a synthesized multi-click action needs to
            # be reproducible. `via` is the affordance dict — its label is the part
            # that carries meaning.
            provenance_path = self.evidence_dir / f"{state_hash}_provenance.json"
            provenance = {
                "url": base_url,
                "from_state": item.get("from_state"),
                "clicks": clicks,
                "via": item.get("via"),
            }
            provenance_path.write_text(
                json.dumps(provenance, indent=2), encoding="utf-8"
            )

            # Queue follow-up states: a few clickable elements to branch on.
            for element in await self._pick_branch_elements(page, elements):
                queue.append(
                    {
                        "url": base_url,
                        "clicks": clicks + [element["selector"]],
                        "from_state": state_hash,
                        "via": element,
                    }
                )

            # Synthesize a stateful path from the landing state's real affordances.
            # Landing only (`not clicks`): scope is ONE action, ONE linear path.
            # Inserted at the FRONT of the queue because it is the high-value state
            # — appending would let cheap link-follows exhaust `max_states` first
            # and starve the cart out of the crawl entirely.
            #
            # Synthesis OFFERS a path; it does not assert the path leads anywhere
            # new. If the action has no structural effect — a storefront whose cart
            # page never renders line items, so adding changes only a text badge
            # that normalize_dom strips — the replay lands on a state already seen
            # and dedup rejects it. The crawl is then exactly what BFS alone would
            # produce. That is the honest outcome, not a failure to fix here: the
            # action really did happen, and the page really did not change.
            if not clicks:
                actionable = await self._filter_clickable(page, elements)
                synthesized = synthesize_cart_path(actionable)
                if synthesized is not None:
                    add_el, cart_el = synthesized
                    queue.insert(
                        0,
                        {
                            "url": base_url,
                            "clicks": [add_el["selector"], cart_el["selector"]],
                            "from_state": state_hash,
                            # The ADD is what produces the populated cart; the cart
                            # link merely navigates to where the effect is visible.
                            "via": add_el,
                        },
                    )
                    logger.info(
                        "synthesized stateful path from state=%s: %r -> %r",
                        state_hash,
                        add_el.get("text"),
                        cart_el.get("text"),
                    )

            record = {
                "url": base_url,
                "clicks": clicks,
                "state_hash": state_hash,
                "screenshot": str(screenshot_path),
                "elements": str(elements_path),
                "records": str(records_path),
                "provenance": str(provenance_path),
                "design_tokens": str(tokens_path),
                "element_count": len(elements),
            }
            print(
                f"Captured state {state_hash} "
                f"(depth {len(clicks)}, {len(elements)} elements) -> {screenshot_path}"
            )
            return record
        finally:
            await page.close()

    @staticmethod
    async def _pick_branch_elements(
        page: Any, elements: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Pick up to ``_BRANCH_FACTOR`` ELEMENTS to branch on, preferring links
        and buttons (the elements most likely to lead to new screens).

        Returns the whole element dict, not a bare selector, so the element's
        LABEL survives to the caller: a selector cannot say that an edge is "Add
        to cart" but its text can, and edge provenance records that label. The
        association between selector and label already exists here — narrowing to
        a string would discard it and force a lookup-by-selector to guess it back,
        which mis-attributes whenever two elements share a selector.

        Candidates are first filtered to elements that are genuinely CLICKABLE
        (see ``_CLICKABLE_JS``) — otherwise the first elements in DOM order on any
        accessible site are hidden skip-links, whose clicks time out and abandon
        the path, capping the crawl at a single state. The selection heuristic is
        unchanged (first ``_BRANCH_FACTOR`` in DOM order); only its input is now
        clickable. Deterministic: same DOM -> same eligible set -> same picks.
        """
        preferred = [e for e in elements if e.get("tag") in ("a", "button")]
        pool = preferred or elements

        # Dedup by selector (two discovered elements can address the same node),
        # but keep the element dict — the selector is the identity, not the value.
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for e in pool:
            sel = e.get("selector")
            if sel and sel not in seen:
                seen.add(sel)
                candidates.append(e)
        if not candidates:
            return []

        eligible = await page.evaluate(
            _CLICKABLE_JS,
            {
                "selectors": [c["selector"] for c in candidates],
                # branching must not spend budget on links back to this page
                "requireNewTarget": True,
            },
        )
        return [c for c, ok in zip(candidates, eligible) if ok][:_BRANCH_FACTOR]

    @staticmethod
    async def _filter_clickable(
        page: Any, elements: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Elements that can actually receive a click, in document order.

        The clickability half only: an ACTION is not a navigation, so the
        self-link rule must not apply (an add-to-cart with ``href="#"`` is still
        a perfectly good add-to-cart).
        """
        candidates = [e for e in elements if e.get("selector")]
        if not candidates:
            return []
        verdicts = await page.evaluate(
            _CLICKABLE_JS,
            {
                "selectors": [c["selector"] for c in candidates],
                "requireNewTarget": False,
            },
        )
        return [c for c, ok in zip(candidates, verdicts) if ok]
