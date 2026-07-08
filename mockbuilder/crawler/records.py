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
row, and contains **no app-specific selectors** — it finds every repeating
collection purely from structure, so the identical code extracts Hacker News
stories and grocery products alike.

It emits *all* qualifying collections ranked by score, and deliberately does not
pick which one is the main entity: that is semantic judgment reserved for the
reasoning stage.

Algorithm (productionized from ``repeating_extractor_poc.py``):

1. Give every element a data-independent structural *signature* (its subtree
   tag-shape, capped in depth). Sibling elements that share a signature are
   repetition candidates.
2. Every group of >= 3 structurally-identical, text-rich siblings becomes a
   candidate collection, scored by ``size * min(leaf_richness, 8)``.
3. Merge each unit with an adjacent non-group sibling, to handle split rows
   (e.g. a title row followed by a separate metadata row).
4. Rank collections by score descending and drop ones nested inside another
   (same region at a different depth). Type each leaf with deterministic Python
   role inference (title / price / age / domain / ...). The result is clean
   typed records per collection — ready-made seed data for the generator.

Public API:
    extract_records(page)       -> ExtractionResult   # sync Playwright Page
    extract_records_async(page) -> ExtractionResult   # async Playwright Page
    infer_role(field)           -> str

Both entrypoints do the same two things — evaluate ``_DETECTOR_JS`` in the page,
then hand the returned dict to :func:`build_result_from_raw`. That transform is
the *single*, pure (no Playwright, no I/O, no ``await``) definition of how raw
detector output becomes typed dataclasses, so the sync and async paths can never
drift. The crawler is async and uses :func:`extract_records_async`; the sync
path exists for isolated/fixture use.

``ExtractionResult`` is the crawler's in-memory shape; call ``.to_dict()`` to
get the JSON wire format written to ``records.json`` (recurses through
``Record``/``Field`` and omits absent optional keys). Nothing downstream reads
the dataclass — only this dict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, TypedDict

__all__ = [
    "Field",
    "Record",
    "Collection",
    "ExtractionResult",
    "build_result_from_raw",
    "extract_records",
    "extract_records_async",
    "infer_role",
]


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
    """One instance of a collection's repeating unit."""

    index: int
    fields: list[Field] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "fields": [f.to_dict() for f in self.fields]}


@dataclass
class Collection:
    """One repeating collection detected on the page.

    ``rank`` is a *position*, not a verdict: collections are sorted by ``score``
    descending and ``rank`` is the 0-based index after that sort. The extractor
    deliberately does NOT decide which collection is the main entity — that is
    semantic judgment reserved for the reasoning stage. ``score`` is the raw
    group score (``member_count * min(leaf_richness, 8)``); ``signature`` is the
    group's structural key (parent-tag + subtree tag-shape).
    """

    rank: int
    score: float
    signature: str
    count: int
    field_count: int
    records: list[Record] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "score": self.score,
            "signature": self.signature,
            "count": self.count,
            "field_count": self.field_count,
            "records": [r.to_dict() for r in self.records],
        }


@dataclass
class ExtractionResult:
    """All repeating collections found on a page, ranked by score descending.

    ``collections`` may be empty when the page has no repeating structure. There
    is intentionally no main-entity flag and no top-level record list: choosing
    the main entity from these ranked candidates is the reasoning stage's job.
    """

    collections: list[Collection] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The wire format written to ``records.json``.

        The dataclass is the crawler's in-memory API; this dict is what every
        downstream stage consumes. Recurses through
        ``Collection``/``Record``/``Field`` and drops ``None`` optional keys.
        ``{"collections": []}`` is the legitimate "no collection" sentinel; the
        crawler adds ``"error": true`` only on the extraction-crash path.
        """
        return {"collections": [c.to_dict() for c in self.collections]}


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

  // (2) EVERY qualifying group becomes a candidate collection (>= 3 siblings).
  // Score is the SAME formula as before — size * (capped) leaf richness — and is
  // NOT retuned. Records are built per-collection, each with the (3) adjacent-
  // sibling merge that absorbs a trailing split "metadata" row.
  const candidates = [];
  for (const key in groups) {
    const members = groups[key];
    if (members.length < 3) continue;
    const richness = members[0].querySelectorAll('a,span,td,p,h1,h2,h3,h4,img').length;
    const score = members.length * Math.min(richness, 8);
    const groupSet = new Set(members);
    const absorbed = new Set();  // siblings merged in (they belong to no group)
    const records = members.map(m => {
      let fields = leavesOf(m);
      const sib = m.nextElementSibling;
      if (sib && !groupSet.has(sib) && sib.querySelector('a,span')) {
        fields = fields.concat(leavesOf(sib));
        absorbed.add(sib);
      }
      return fields;
    }).filter(r => r.length);
    if (!records.length) continue;
    candidates.push({ signature: key, score, members, absorbed, records });
  }

  // Rank by score descending. V8's sort is stable, so on a score tie the group
  // encountered first in document order stays first — this makes collection[0]
  // byte-identical to the previous single-winner detector.
  candidates.sort((a, b) => b.score - a.score);

  // Dedup collections that describe a region another collection already covers:
  //   (1) NESTING — every member of A sits inside a member of B (e.g. a grid and
  //       a redundant per-item wrapper, or a grid inside a page-wrapper);
  //   (2) MERGE-ABSORPTION — every member of A was pulled into B's records by the
  //       adjacent-sibling merge (e.g. a split metadata row already folded into
  //       its title row). Emitting them again would double-count the same region.
  // Keep the higher-scored collection; on a tie keep the outer/covering one.
  const subsumedBy = (A, B) =>
    A.members.every(ma => B.members.some(mb => mb !== ma && mb.contains(ma)));
  // B absorbed A when every member of A is (or sits inside) a sibling B merged.
  // The containment check also sweeps up the sub-structure groups of an absorbed
  // sibling — e.g. the cells/spans inside an already-folded-in metadata row.
  const absorbedBy = (A, B) => {
    if (!B.absorbed.size) return false;
    const abs = [...B.absorbed];
    return A.members.every(ma => abs.some(s => s === ma || s.contains(ma)));
  };
  const coveredBy = (A, B) => subsumedBy(A, B) || absorbedBy(A, B);
  const dropped = new Set();
  for (let i = 0; i < candidates.length; i++) {
    if (dropped.has(i)) continue;
    for (let j = 0; j < candidates.length; j++) {
      if (i === j || dropped.has(j)) continue;
      if (coveredBy(candidates[i], candidates[j])) {
        // i is covered by j. Drop the lower score; on a tie drop the covered (i).
        dropped.add(candidates[i].score > candidates[j].score ? j : i);
        if (dropped.has(i)) break;
      }
    }
  }

  const collections = candidates
    .filter((_, i) => !dropped.has(i))
    .map(c => ({ signature: c.signature, score: c.score, records: c.records }));

  return { collections };
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
# The one pure transform (shared by both entrypoints)
# --------------------------------------------------------------------------- #
def build_result_from_raw(raw: dict[str, Any]) -> ExtractionResult:
    """Turn the ``_DETECTOR_JS`` output into typed dataclasses. **Pure.**

    Takes the dict returned by ``page.evaluate(_DETECTOR_JS)`` — shaped like
    ``{"collections": [{"signature", "score", "records": [[rawfield, ...], ...]},
    ...]}``, already ranked by score descending — applies role inference to every
    leaf, and assembles the ``ExtractionResult``. ``rank`` is assigned here as the
    position in that already-sorted list (a position, not a verdict). No
    Playwright, no I/O, no ``await``: this is the single definition of the
    transform, so the sync and async entrypoints cannot diverge, and it is the
    algorithm's browser-free regression anchor.

    Returns ``collections == []`` when ``raw`` has none (never raises).
    """
    collections: list[Collection] = []
    for rank, raw_col in enumerate(raw.get("collections", [])):
        records: list[Record] = []
        field_count = 0
        for i, raw_fields in enumerate(raw_col.get("records", [])):
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

        collections.append(
            Collection(
                rank=rank,
                score=float(raw_col.get("score", 0)),
                signature=raw_col.get("signature", ""),
                count=len(records),
                field_count=field_count,
                records=records,
            )
        )

    return ExtractionResult(collections=collections)


# --------------------------------------------------------------------------- #
# Public entry points — thin seams over the detector + the pure transform.
# --------------------------------------------------------------------------- #
def extract_records(page: Any) -> ExtractionResult:
    """Extract all repeating collections from a settled *sync* page.

    ``page`` is a live *sync* Playwright ``Page`` that has already been
    navigated and allowed to settle. Evaluates the detector, then delegates to
    :func:`build_result_from_raw`. Returns an empty ``collections`` list when the
    page has no detectable repeating structure (never raises for that case).
    """
    raw: dict[str, Any] = page.evaluate(_DETECTOR_JS)
    return build_result_from_raw(raw)


async def extract_records_async(page: Any) -> ExtractionResult:
    """Async counterpart of :func:`extract_records` for an *async* Playwright
    ``Page``. Awaits the detector evaluation, then delegates to the identical
    pure :func:`build_result_from_raw` — same collections, records, roles, and
    signatures as the sync path for the same page."""
    raw: dict[str, Any] = await page.evaluate(_DETECTOR_JS)
    return build_result_from_raw(raw)
