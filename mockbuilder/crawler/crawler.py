"""Async Playwright crawler that captures evidence for a source app.

Phase 1 scope: visit a single URL, capture a structurally-normalized DOM, hash
it into a ``state_hash``, and persist the evidence (screenshot + discovered
elements) under the project's ``evidence/`` directory. The ``max_states``
parameter is the seam for multi-state crawling in a later phase; for now only the
landing state is captured.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from .dom import discover_elements, normalize_dom

# Project root is two levels above this file: <root>/mockbuilder/crawler/crawler.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = PROJECT_ROOT / "evidence"


class Crawler:
    """Drives a headless browser to capture app state as evidence."""

    def __init__(self, evidence_dir: Path | str = EVIDENCE_DIR) -> None:
        self.evidence_dir = Path(evidence_dir)

    async def crawl(self, url: str, max_states: int = 1) -> list[dict[str, Any]]:
        """Crawl ``url`` and persist evidence for the captured state(s).

        Returns a list of per-state records (``state_hash`` plus evidence paths).
        ``max_states`` bounds how many distinct states to capture; Phase 1 only
        captures the landing state.
        """
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        captured: list[dict[str, Any]] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle")

                # Capture structural DOM + actionable elements.
                normalized_dom = await normalize_dom(page)
                elements = await discover_elements(page)

                # The state hash collapses structurally-identical renders.
                state_hash = hashlib.sha256(
                    normalized_dom.encode("utf-8")
                ).hexdigest()

                screenshot_path = self.evidence_dir / f"{state_hash}.png"
                elements_path = self.evidence_dir / f"{state_hash}_elements.json"

                await page.screenshot(path=str(screenshot_path), full_page=True)
                elements_path.write_text(
                    json.dumps(elements, indent=2), encoding="utf-8"
                )

                record = {
                    "url": url,
                    "state_hash": state_hash,
                    "screenshot": str(screenshot_path),
                    "elements": str(elements_path),
                    "element_count": len(elements),
                }
                captured.append(record)
                print(
                    f"Captured state {state_hash} "
                    f"({len(elements)} elements) -> {screenshot_path}"
                )
            finally:
                await browser.close()

        return captured
