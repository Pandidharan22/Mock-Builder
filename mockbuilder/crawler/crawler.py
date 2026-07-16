"""Async Playwright crawler that captures evidence for a source app.

Phase 1 performs a breadth-first crawl: starting from a URL, it captures the
landing state and then follows a couple of clickable elements per state to
discover new ones. Each state is keyed by the hash of its *normalized* DOM, so
structurally-identical renders collapse to one node and the crawl terminates
instead of looping forever.

For every newly-seen state we persist:
  * ``evidence/{state_hash}.png``            — full-page screenshot
  * ``evidence/{state_hash}_elements.json``  — discovered actionable elements
  * ``evidence/{state_hash}_records.json``   — extracted repeating-unit records
  * ``evidence/design_tokens.json``          — harvested computed styles
  * ``evidence/fixtures/*.json``             — captured JSON API responses
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

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

# Decides whether a branch candidate is usable, evaluated in-page on the settled
# DOM. A candidate must be BOTH a real click target AND lead somewhere new. Both
# are DISQUALIFICATIONS ("can't be clicked", "goes nowhere new"), not preferences:
# this removes non-candidates from the input, it never ranks what's left — the
# selection heuristic (first N in DOM order) is unchanged.
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
(selectors) => {
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
    return clickable(el) && goesSomewhereNew(el);
  });
}
"""


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
        """
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

        captured: list[dict[str, Any]] = []
        visited_states: set[str] = set()

        # Each queue item: {"url": base_url, "clicks": [selector, ...]}.
        queue: list[dict[str, Any]] = [{"url": url, "clicks": []}]

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

            await page.goto(base_url, wait_until="networkidle")

            # Replay the click path that defines this state.
            for selector in clicks:
                try:
                    await page.click(selector, timeout=5000)
                    await page.wait_for_load_state("networkidle")
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

            # Queue follow-up states: a few clickable elements to branch on.
            for selector in await self._pick_branch_selectors(page, elements):
                queue.append({"url": base_url, "clicks": clicks + [selector]})

            record = {
                "url": base_url,
                "clicks": clicks,
                "state_hash": state_hash,
                "screenshot": str(screenshot_path),
                "elements": str(elements_path),
                "records": str(records_path),
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
    async def _pick_branch_selectors(
        page: Any, elements: list[dict[str, Any]]
    ) -> list[str]:
        """Pick up to ``_BRANCH_FACTOR`` selectors to branch on, preferring links
        and buttons (the elements most likely to lead to new screens).

        Candidates are first filtered to elements that are genuinely CLICKABLE
        (see ``_CLICKABLE_JS``) — otherwise the first elements in DOM order on any
        accessible site are hidden skip-links, whose clicks time out and abandon
        the path, capping the crawl at a single state. The selection heuristic is
        unchanged (first ``_BRANCH_FACTOR`` in DOM order); only its input is now
        clickable. Deterministic: same DOM -> same eligible set -> same picks.
        """
        preferred = [e for e in elements if e.get("tag") in ("a", "button")]
        pool = preferred or elements

        candidates: list[str] = []
        for e in pool:
            sel = e.get("selector")
            if sel and sel not in candidates:
                candidates.append(sel)
        if not candidates:
            return []

        eligible = await page.evaluate(_CLICKABLE_JS, candidates)
        return [sel for sel, ok in zip(candidates, eligible) if ok][:_BRANCH_FACTOR]
