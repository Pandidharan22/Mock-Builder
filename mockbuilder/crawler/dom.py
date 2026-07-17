"""Browser-side state capture.

These functions run JavaScript inside the live page via Playwright's
``page.evaluate()`` and return Python-native data. There are two captures:

* :func:`normalize_dom` — a structural, denoised snapshot of the DOM used as the
  basis for the state hash. It strips everything volatile (text, styling, scripts,
  embedded media) so that two renders that are *structurally* the same collapse to
  the same string.
* :func:`discover_elements` — the actionable surface of the page (links, buttons,
  inputs) with a reliable CSS selector for each, so the crawler can later drive
  interactions.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from playwright.async_api import Page


# --------------------------------------------------------------------------- #
# DOM normalization
# --------------------------------------------------------------------------- #
# Clone <body>, drop text nodes and noise tags (script/style/iframe/svg), and
# strip every attribute except the structurally meaningful ones (class, role,
# data-testid). Returns the cleaned outerHTML string.
_NORMALIZE_JS = r"""
() => {
  const KEEP_ATTRS = new Set(['class', 'role', 'data-testid']);
  const DROP_TAGS = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'SVG']);

  const root = document.body.cloneNode(true);

  // Walk the cloned tree, collecting element/text nodes to remove. We collect
  // first and mutate after so we don't disturb the live walker.
  const walker = document.createTreeWalker(
    root,
    NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
    null
  );

  const toRemove = [];
  let node = walker.nextNode();
  while (node) {
    if (node.nodeType === Node.TEXT_NODE) {
      toRemove.push(node);
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if (DROP_TAGS.has(node.tagName)) {
        toRemove.push(node);
      } else {
        // Strip all attributes except the kept set.
        for (const attr of Array.from(node.attributes)) {
          if (!KEEP_ATTRS.has(attr.name)) {
            node.removeAttribute(attr.name);
          }
        }
      }
    }
    node = walker.nextNode();
  }

  for (const n of toRemove) {
    if (n.parentNode) {
      n.parentNode.removeChild(n);
    }
  }

  return root.outerHTML;
}
"""


# --------------------------------------------------------------------------- #
# Element discovery
# --------------------------------------------------------------------------- #
# Find every clickable/input element and return
# {tag, text, selector, testid, href}. The selector prefers id / data-testid,
# otherwise builds an nth-of-type path so it is unique and stable enough to
# re-address the element.
#
# `text` is the element's LABEL and is load-bearing downstream, not decoration: a
# selector like `li:nth-of-type(6) > a:nth-of-type(2)` cannot say what an element
# DOES, whereas "Add to cart" can. Affordance synthesis and edge provenance both
# key off the label, so it must stay attached to the element it came from.
#
# `href` is the other half of that meaning: a link's intent lives in its label OR
# in where it GOES, and real headers routinely express the second without the
# first — a cart widget labelled "$0.00 0 items" pointing at /cart/ says "cart"
# with its target and nothing else.
_DISCOVER_JS = r"""
() => {
  const SELECTOR = 'a, button, input, select, [role="button"], [role="tab"]';

  // Build a reasonably reliable, unique CSS selector for an element.
  const cssSelector = (el) => {
    if (el.id) {
      return '#' + CSS.escape(el.id);
    }
    const testId = el.getAttribute('data-testid');
    if (testId) {
      return '[data-testid="' + testId.replace(/"/g, '\\"') + '"]';
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.documentElement) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(
          (c) => c.tagName === node.tagName
        );
        if (siblings.length > 1) {
          const index = siblings.indexOf(node) + 1;
          part += ':nth-of-type(' + index + ')';
        }
      }
      parts.unshift(part);
      if (node.id) {
        parts[0] = '#' + CSS.escape(node.id);
        break;
      }
      node = node.parentElement;
    }
    return parts.join(' > ');
  };

  // Resolved against baseURI so callers get an absolute target and never have to
  // re-resolve a relative href without knowing the page it came from.
  const absoluteHref = (el) => {
    const raw = el.getAttribute('href');
    if (raw === null) return null;
    try {
      return new URL(raw, document.baseURI).href;
    } catch (e) {
      return null;
    }
  };

  const elements = Array.from(document.querySelectorAll(SELECTOR));
  return elements.map((el) => {
    const text =
      (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    return {
      tag: el.tagName.toLowerCase(),
      text: text,
      selector: cssSelector(el),
      testid: el.getAttribute('data-testid'),
      href: absoluteHref(el),
    };
  });
}
"""


async def normalize_dom(page: "Page") -> str:
    """Return the structurally-normalized ``outerHTML`` of the page body."""
    return await page.evaluate(_NORMALIZE_JS)


async def discover_elements(page: "Page") -> list[dict[str, Any]]:
    """Return ``{tag, text, selector, testid, href}`` for every actionable element.

    ``testid`` is ``None`` when the element carries no ``data-testid``; ``href`` is
    ``None`` for non-links (and for unresolvable targets), and absolute otherwise.
    """
    return await page.evaluate(_DISCOVER_JS)
