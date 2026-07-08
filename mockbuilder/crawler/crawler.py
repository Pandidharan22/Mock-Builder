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
            # empty file (distinguished by signature=null + the ERROR log). A
            # page that legitimately has no repeating collection returns count=0
            # without raising — same empty shape, but no ERROR and signature="".
            records_path = self.evidence_dir / f"{state_hash}_records.json"
            try:
                extraction = await extract_records_async(page)
                records_payload = extraction.to_dict()
                logger.info(
                    "records: state=%s count=%d signature=%s",
                    state_hash,
                    extraction.count,
                    extraction.signature,
                )
            except Exception:
                logger.error(
                    "record extraction failed for state=%s; writing empty result",
                    state_hash,
                    exc_info=True,
                )
                records_payload = {
                    "count": 0,
                    "field_count": 0,
                    "records": [],
                    "signature": None,
                }
            records_path.write_text(
                json.dumps(records_payload, indent=2), encoding="utf-8"
            )

            # Queue follow-up states: a few clickable elements to branch on.
            for selector in self._pick_branch_selectors(elements):
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
    def _pick_branch_selectors(elements: list[dict[str, Any]]) -> list[str]:
        """Pick up to ``_BRANCH_FACTOR`` selectors to branch on, preferring
        links and buttons (the elements most likely to lead to new screens)."""
        preferred = [e for e in elements if e.get("tag") in ("a", "button")]
        pool = preferred or elements
        selectors: list[str] = []
        for e in pool:
            sel = e.get("selector")
            if sel and sel not in selectors:
                selectors.append(sel)
            if len(selectors) >= _BRANCH_FACTOR:
                break
        return selectors
