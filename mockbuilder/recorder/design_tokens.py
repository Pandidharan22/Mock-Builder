"""Design-token harvester.

Extracts (never invents) a small set of computed styles from the live page so the
generator can emit a theme that resembles the source app. Phase 1 keeps this
deliberately coarse: the body font stack plus background colors observed on the
major structural regions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from playwright.async_api import Page


# Reads the computed font-family of <body> and the background-color of the main
# structural regions (falling back to the first few generic divs when semantic
# landmarks are absent). Returns a plain, JSON-serializable dict.
_HARVEST_JS = r"""
() => {
  const bodyStyle = getComputedStyle(document.body);

  const isVisibleColor = (c) =>
    c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent';

  const structural = {};
  const selectors = ['header', 'nav', 'main', 'footer'];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) {
      const bg = getComputedStyle(el).backgroundColor;
      if (isVisibleColor(bg)) {
        structural[sel] = bg;
      }
    }
  }

  // Fallback: sample background colors from the first few generic divs so we
  // still get a palette on apps without semantic landmarks.
  const divBackgrounds = [];
  const divs = Array.from(document.querySelectorAll('div')).slice(0, 20);
  for (const d of divs) {
    const bg = getComputedStyle(d).backgroundColor;
    if (isVisibleColor(bg) && !divBackgrounds.includes(bg)) {
      divBackgrounds.push(bg);
    }
    if (divBackgrounds.length >= 5) break;
  }

  return {
    fontFamily: bodyStyle.fontFamily,
    baseSize: bodyStyle.fontSize,
    bodyBackground: bodyStyle.backgroundColor,
    bodyColor: bodyStyle.color,
    structuralBackgrounds: structural,
    divBackgrounds: divBackgrounds,
  };
}
"""


async def harvest_tokens(page: "Page") -> dict[str, Any]:
    """Return a coarse dict of computed design tokens observed on the page."""
    return await page.evaluate(_HARVEST_JS)


def save_tokens(tokens: dict[str, Any], evidence_dir: Path) -> Path:
    """Persist harvested tokens to ``evidence_dir/design_tokens.json``."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_path = evidence_dir / "design_tokens.json"
    out_path.write_text(
        json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return out_path
