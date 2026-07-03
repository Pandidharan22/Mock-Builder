"""Design-token harvester.

Extracts (never invents) a small set of styles from the live page so the
generator can emit a theme that resembles the source app. Phase 1 keeps this
deliberately coarse: the body font stack plus background colors observed on the
major structural regions — sampling both modern layout landmarks and legacy
presentation markup (``table``/``td`` with ``bgcolor``) so older sites like
Hacker News still yield their brand colors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from playwright.async_api import Page


# Reads the computed font-family of <body> plus structural background colors.
# Samples modern layout landmarks (header/nav/main/footer) AND legacy layout
# markup (table/td), and for each element prefers the presentational `bgcolor`
# attribute (e.g. Hacker News's `<td bgcolor="#ff6600">`) over the computed
# background-color. Colors are normalized to canonical #rrggbb. Returns a plain,
# JSON-serializable dict.
_HARVEST_JS = r"""
() => {
  const bodyStyle = getComputedStyle(document.body);

  const isVisibleColor = (c) =>
    c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent';

  // rgb()/rgba() -> #rrggbb (alpha dropped; we only sample visible colors).
  const rgbToHex = (rgb) => {
    const m = String(rgb).match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (!m) return null;
    const toHex = (n) => Number(n).toString(16).padStart(2, '0');
    return '#' + toHex(m[1]) + toHex(m[2]) + toHex(m[3]);
  };

  // Normalize a legacy `bgcolor` attribute — hex with or without '#', or a
  // named color like 'orange' — to a canonical #rrggbb, using the browser's own
  // color parser to validate and resolve it. Returns null if it isn't a color.
  const normalizeAttrColor = (value) => {
    if (!value) return null;
    let v = String(value).trim();
    if (/^[0-9a-fA-F]{3}$|^[0-9a-fA-F]{6}$/.test(v)) v = '#' + v;  // legacy 'ff6600'
    const probe = document.createElement('span');
    probe.style.color = '';
    probe.style.color = v;              // invalid values are ignored -> stays ''
    if (probe.style.color === '') return null;
    document.body.appendChild(probe);
    const resolved = getComputedStyle(probe).color;  // canonical rgb(...)
    probe.remove();
    return rgbToHex(resolved);
  };

  // Background of an element: legacy `bgcolor` attribute first, then the
  // computed background-color; null for transparent/default.
  const bgOf = (el) => {
    const attr = normalizeAttrColor(el.getAttribute('bgcolor'));
    if (attr) return attr;
    const computed = getComputedStyle(el).backgroundColor;
    return isVisibleColor(computed) ? (rgbToHex(computed) || computed) : null;
  };

  const structural = {};
  const structuralColors = [];
  const addColor = (c) => {
    if (c && !structuralColors.includes(c)) structuralColors.push(c);
  };

  // Modern layout landmarks.
  for (const sel of ['header', 'nav', 'main', 'footer']) {
    const el = document.querySelector(sel);
    if (el) {
      const bg = bgOf(el);
      if (bg) { structural[sel] = bg; addColor(bg); }
    }
  }

  // Legacy presentation markup: <table>/<td> commonly carry bgcolor on older or
  // email-style layouts (this is where Hacker News hides its #ff6600 bar).
  const cells = Array.from(document.querySelectorAll('table, td')).slice(0, 60);
  for (const el of cells) {
    const bg = bgOf(el);
    if (bg) {
      const key = el.tagName.toLowerCase();
      if (!(key in structural)) structural[key] = bg;  // first per tag
      addColor(bg);
    }
    if (structuralColors.length >= 8) break;
  }

  // Fallback: sample background colors from the first few generic divs so we
  // still get a palette on apps without semantic landmarks.
  const divBackgrounds = [];
  const divs = Array.from(document.querySelectorAll('div')).slice(0, 20);
  for (const d of divs) {
    const bg = bgOf(d);
    if (bg && !divBackgrounds.includes(bg)) divBackgrounds.push(bg);
    if (divBackgrounds.length >= 5) break;
  }

  // Normalize the body colors to hex so we never feed raw rgb()/rgba() strings
  // into the schema's hex-only color fields. A transparent body background has
  // no meaningful color, so fall back to white.
  const bodyBg = isVisibleColor(bodyStyle.backgroundColor)
    ? (rgbToHex(bodyStyle.backgroundColor) || '#ffffff')
    : '#ffffff';
  const bodyFg = rgbToHex(bodyStyle.color) || '#000000';

  return {
    fontFamily: bodyStyle.fontFamily,
    baseSize: bodyStyle.fontSize,
    bodyBackground: bodyBg,
    bodyColor: bodyFg,
    structuralBackgrounds: structural,
    structuralColors: structuralColors,
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
