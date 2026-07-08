"""Generic repeating-structure record extractor (the "DATA" track).

MockBuilder splits every screen's evidence into two tracks with a hard
boundary between them:

  * **DATA** — the actual records on the page (real titles, prices, domains,
    ages). This is transcription, which LLMs do badly, so it is produced here,
    *deterministically*, straight from the live DOM.
  * **STRUCTURE** — what kind of app this is, what the repeating unit is, which
    fields it has and how they map to UI. That is judgment, which LLMs do well,
    so it is left to the reasoning stage.

This module owns the DATA track. It never talks to a model, never invents a
row, and contains **no app-specific selectors** — it finds the page's primary
repeating collection purely from structure, so the identical code extracts
Hacker News stories and grocery products alike.

Algorithm (productionized from ``repeating_extractor_poc.py``):

1. Give every element a data-independent structural *signature* (its subtree
   tag-shape, capped in depth). Sibling elements that share a signature are
   repetition candidates.
2. The largest group of structurally-identical, text-rich siblings is the
   page's primary collection — the repeating unit.
3. Merge each unit with an adjacent non-group sibling, to handle split rows
   (e.g. a title row followed by a separate metadata row).
4. Type each leaf with deterministic Python role inference
   (title / price / age / domain / ...). The result is clean typed records —
   ready-made seed data for the generator.

Public API:
    extract_records(page) -> ExtractionResult
    infer_role(field)     -> str

``ExtractionResult`` is the crawler's in-memory shape; call ``.to_dict()`` to
get the JSON wire format written to ``records.json`` (recurses through
``Record``/``Field`` and omits absent optional keys). Nothing downstream reads
the dataclass — only this dict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, TypedDict

__all__ = ["Field", "Record", "ExtractionResult", "extract_records", "infer_role"]


# --------------------------------------------------------------------------- #
# Return shapes
# --------------------------------------------------------------------------- #
class RawField(TypedDict, total=False):
    """A leaf as emitted by the in-page detector, before role inference."""

    tag: str
    text: str
    href: str
    src: str


@dataclass
class Field:
    """One typed leaf of a record."""

    tag: str
    text: str
    role: str
    href: str | None = None
    src: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form. Absent optional keys (``href``/``src``) are dropped
        rather than emitted as ``null``, keeping the wire format tight."""
        out: dict[str, Any] = {"tag": self.tag, "text": self.text, "role": self.role}
        if self.href is not None:
            out["href"] = self.href
        if self.src is not None:
            out["src"] = self.src
        return out


@dataclass
class Record:
    """One instance of the page's repeating unit."""

    index: int
    fields: list[Field] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "fields": [f.to_dict() for f in self.fields]}


@dataclass
class ExtractionResult:
    """The outcome of extracting the primary repeating collection from a page.

    ``field_count`` is the total number of typed fields across every record.
    ``signature`` is the winning group's key (parent-tag + subtree tag-shape);
    it is empty when no repeating group was found.
    """

    count: int
    field_count: int
    records: list[Record] = dataclass_field(default_factory=list)
    signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The wire format written to ``records.json``.

        The dataclass is the crawler's in-memory API; this dict is what every
        downstream stage consumes. Recurses through ``Record``/``Field`` and
        drops ``None`` optional keys.
        """
        return {
            "count": self.count,
            "field_count": self.field_count,
            "records": [r.to_dict() for r in self.records],
            "signature": self.signature,
        }


# --------------------------------------------------------------------------- #
# In-page detector (steps 1-3). Data-independent; no app-specific selectors.
# --------------------------------------------------------------------------- #
_DETECTOR_JS = r"""
() => {
  const clean = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();

  // (1) structural signature: a subtree's tag-shape, independent of its data.
  function signature(el, d = 0) {
    if (d > 4) return '';
    const kids = [...el.children];
    if (!kids.length) return el.tagName;
    return el.tagName + '(' + kids.map(c => signature(c, d + 1)).join(',') + ')';
  }

  // Group sibling elements that share a (parent, signature) key.
  const groups = {};
  for (const el of document.querySelectorAll('body *')) {
    const p = el.parentElement;
    if (!p) continue;
    const sig = signature(el);
    if (sig.length < 4) continue;
    const key = p.tagName + '>' + sig;
    (groups[key] = groups[key] || []).push(el);
  }

  // (2) largest text-rich group wins. Score = size * (capped) leaf richness.
  let best = null, bestKey = '', bestScore = 0;
  for (const key in groups) {
    const members = groups[key];
    if (members.length < 3) continue;
    const richness = members[0].querySelectorAll('a,span,td,p,h1,h2,h3,h4,img').length;
    const score = members.length * Math.min(richness, 8);
    if (score > bestScore) { bestScore = score; best = members; bestKey = key; }
  }
  if (!best) return { count: 0, records: [], signature: '' };

  const groupSet = new Set(best);

  // Leaf fields of an element (text-bearing leaves plus images).
  function leavesOf(el) {
    const out = [];
    for (const lf of el.querySelectorAll(
        'a,span:not(:has(*)),td:not(:has(*)),h1,h2,h3,h4,img')) {
      const t = clean(lf.textContent);
      if (!t && lf.tagName !== 'IMG') continue;
      const f = { tag: lf.tagName.toLowerCase(), text: t };
      const href = lf.getAttribute && lf.getAttribute('href');
      if (href) f.href = href;
      if (lf.tagName === 'IMG') { f.src = lf.getAttribute('src'); f.text = '[img]'; }
      out.push(f);
    }
    return out;
  }

  // (3) absorb a trailing non-group sibling (a split "metadata" row).
  const records = best.map(m => {
    let fields = leavesOf(m);
    const sib = m.nextElementSibling;
    if (sib && !groupSet.has(sib) && sib.querySelector('a,span')) {
      fields = fields.concat(leavesOf(sib));
    }
    return fields;
  }).filter(r => r.length);

  return { count: records.length, records, signature: bestKey };
}
"""


# --------------------------------------------------------------------------- #
# (4) Deterministic role inference. Pure Python, no browser, unit-testable.
# --------------------------------------------------------------------------- #
_RANK_RE = re.compile(r"\d+\.")
_VOTES = ("▲", "△", "↑", "▴")  # ▲ △ ↑ ▴
_PRICE_RE = re.compile(r"[₹$€£]\s?\d")  # ₹ $ € £ followed by a digit
_NUMBER_RE = re.compile(r"\d+(\.\d+)?")
_COMMENT_RE = re.compile(r"\bcomments?\b")
_SCORE_RE = re.compile(r"\b(points?|votes?)\b")
_AGE_RE = re.compile(r"\b(seconds?|minutes?|hours?|days?|weeks?|months?|years?|ago)\b")
_DOMAIN_RE = re.compile(r"[a-z0-9.-]+\.[a-z]{2,}(/\S*)?")


def infer_role(field: dict[str, Any]) -> str:
    """Classify a single leaf into a role, deterministically.

    Pure function: takes one field dict (``{tag, text, href?, src?}``) and
    returns exactly one of: image, rank, vote, price, number, comment_count,
    score, age, domain, title, meta. No Playwright, no state, no randomness.

    Order matters — the more specific patterns are tried first so that, e.g.,
    "100 points" is a ``score`` rather than a bare ``number``.
    """
    tag = field.get("tag", "")
    text = field.get("text", "")

    if tag == "img":
        return "image"
    if _RANK_RE.fullmatch(text):
        return "rank"
    if text in _VOTES:
        return "vote"
    if _PRICE_RE.search(text):
        return "price"
    if _COMMENT_RE.search(text):
        return "comment_count"
    if _SCORE_RE.search(text):
        return "score"
    if _AGE_RE.search(text):
        return "age"
    if _DOMAIN_RE.fullmatch(text):
        return "domain"
    if _NUMBER_RE.fullmatch(text):
        return "number"
    if field.get("href") and len(text) > 15:
        return "title"
    if len(text) > 20:
        return "title"
    return "meta"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def extract_records(page: Any) -> ExtractionResult:
    """Extract the primary repeating collection from a settled Playwright page.

    ``page`` is a live *sync* Playwright ``Page`` that has already been
    navigated and allowed to settle. Runs the in-page detector, applies role
    inference to every leaf, and returns typed records.

    Returns an ``ExtractionResult`` with ``count == 0`` and ``records == []``
    when the page has no detectable repeating structure (never raises for that
    case).
    """
    raw: dict[str, Any] = page.evaluate(_DETECTOR_JS)

    records: list[Record] = []
    field_count = 0
    for i, raw_fields in enumerate(raw.get("records", [])):
        fields: list[Field] = []
        for rf in raw_fields:
            fields.append(
                Field(
                    tag=rf.get("tag", ""),
                    text=rf.get("text", ""),
                    role=infer_role(rf),
                    href=rf.get("href"),
                    src=rf.get("src"),
                )
            )
        field_count += len(fields)
        records.append(Record(index=i, fields=fields))

    return ExtractionResult(
        count=len(records),
        field_count=field_count,
        records=records,
        signature=raw.get("signature", ""),
    )
