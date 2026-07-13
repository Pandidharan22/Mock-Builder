"""Seed injection — the DATA track meets the STRUCTURE track.

This is the transform that makes the pipeline end-to-end and kills the fabrication
defect for good. Reasoning produces an AppModel whose entities define SHAPE only
(field names + types + ``sourceRole`` linkage, no data). This module fills
``entity.seed`` from the REAL extracted records, zipping each entity's fields onto
the records of its ``sourceCollection`` via the ``sourceRole`` key:

    for each record in collections[entity.sourceCollection]:
        for each field in entity.fields:
            leaf  = first record leaf whose role == field.sourceRole
            row[field.name] = coerce(leaf.text, field.type)
        row["id"] = <synthetic 0-based row index>

Every record becomes a seed row — ALL of them, never a sample. The model never
touches seed; it comes only from ``records_data``. Fabrication is structurally
impossible: a value that isn't in a real record can't appear.

Two guards make malformed linkage loud rather than silent (see :func:`inject_seed`):
uniqueness (two fields can't share a ``sourceRole``) and resolution (a
``sourceRole`` matching no record at all is an invented role, vs. matching some
records but not this one, which is legitimate graceful shortening).

Note on the schema: Step 4 removed ``seed`` from the entity ``$def`` (so the model
can never emit it, and ``additionalProperties: false`` forbids it). Injection adds
``seed`` back to the entity object *after* validation — this is safe because
nothing downstream re-validates: the generator reads ``entity.seed`` directly and
the verifier runs the built app. ``seed`` is generator-owned, populated here.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["SeedInjectionError", "inject_seed"]

# Pull the first (optionally signed/decimal) number out of a leaf's text, e.g.
# "100 points" -> 100, "3.5 stars" -> 3.5, "234 comments" -> 234.
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


class SeedInjectionError(Exception):
    """Raised when the entity<->record linkage is malformed in a way that would
    silently corrupt seed data (see the two guards in :func:`inject_seed`)."""


def _coerce(leaf: dict[str, Any], field_type: str) -> Any:
    """Coerce a record leaf to the field's declared type. Deterministic, never
    raises. On an unparseable number, keep the raw string (a visibly-wrong value
    beats a silent hole) and log at DEBUG. Never invents a value."""
    if field_type == "imageUrl":
        # The extractor emits an image leaf as {src, text:"[img]"}; prefer src.
        return leaf.get("src") or leaf.get("text") or ""

    text = leaf.get("text", "")

    if field_type == "number":
        match = _NUMBER_RE.search(text)
        if match is None:
            logger.debug("number coercion fell back to raw text: %r", text)
            return text
        raw = match.group(0)
        return float(raw) if "." in raw else int(raw)

    if field_type == "boolean":
        return text.strip().lower() in ("true", "yes", "1", "on", "in stock")

    # string / currency / id / enum / ref / array (and anything else): keep the
    # real text as-is. currency stays as its displayed form ("₹52") so the mock
    # reads faithfully; the model chose these types.
    return text


def _seed_rows_for_entity(entity: dict[str, Any], collection: dict[str, Any]) -> list[dict[str, Any]]:
    """Build every seed row for one entity from its source collection's records."""
    name = entity.get("name")
    fields = entity.get("fields", [])
    records = collection.get("records", [])

    # GUARD (a) UNIQUENESS: two fields sharing a sourceRole is unresolvable under
    # first-occurrence — both would take the same leaf, silently dropping the
    # other's real data. Legal at 6a, caught here at use-time. Fail loudly.
    by_role: dict[str, str] = {}
    for field in fields:
        role = field.get("sourceRole")
        if role in by_role:
            raise SeedInjectionError(
                f"Entity '{name}': fields '{by_role[role]}' and '{field.get('name')}' "
                f"both have sourceRole '{role}'. Cannot deterministically resolve "
                f"which record leaf each takes (first-occurrence would give both the "
                f"same value). Give them distinct sourceRoles or refine the extractor."
            )
        by_role[role] = field.get("name")

    # GUARD (b) RESOLUTION, collection level: a sourceRole present in NO record of
    # the collection is an invented role (6a should have caught it). Fail loudly.
    # (A role present in SOME records but missing from a given record is handled
    # per-row below as legitimate graceful shortening.)
    roles_in_collection = {
        leaf.get("role")
        for record in records
        for leaf in record.get("fields", [])
        if leaf.get("role")
    }
    for field in fields:
        role = field.get("sourceRole")
        if role not in roles_in_collection:
            raise SeedInjectionError(
                f"Entity '{name}' field '{field.get('name')}' has sourceRole "
                f"'{role}' which appears in NO record of collection "
                f"{entity.get('sourceCollection')} (an invented role 6a should have "
                f"rejected). Roles present in this collection: "
                f"{sorted(r for r in roles_in_collection)}."
            )

    seed: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        leaves = record.get("fields", [])
        row: dict[str, Any] = {}
        for field in fields:
            role = field.get("sourceRole")
            # First-occurrence match (the one load-bearing rule on a duplicate,
            # different-valued role like HN's meta: author is the first meta).
            leaf = next((leaf for leaf in leaves if leaf.get("role") == role), None)
            if leaf is None:
                # Graceful shortening: this record lacks the role (e.g. a jobs
                # row with no score). Omit the key; the templates render a missing
                # prop as '' via `{props.x || ''}`. Do NOT fabricate a value.
                continue
            row[field.get("name")] = _coerce(leaf, field.get("type", "string"))
        # Synthetic 0-based row id for React keys / reducer remove-toggle. NOTE:
        # HN's href-borne story id (item?id=<n>) is a recoverable refinement IF
        # cross-crawl identity ever matters — but recovering it needs its own
        # uniqueness guard (user?id=<author> collides with item?id=<story>), so it
        # is not free; synthetic index is deterministic and unique within a build.
        row["id"] = index
        seed.append(row)

    return seed


def inject_seed(app_model: dict[str, Any], records_data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``app_model`` with ``entity.seed`` populated from real
    records via the ``sourceCollection`` + ``sourceRole`` linkage.

    Pure and deterministic: no I/O, no network, no mutation of the input (a deep
    copy is returned so the caller decides what to hand the generator). An
    AppModel with no data-bearing entity (the zero-collection case) passes through
    unchanged. Raises :class:`SeedInjectionError` on malformed linkage (a missing
    source collection, duplicate ``sourceRole``s, or an invented role).
    """
    result = copy.deepcopy(app_model)
    collections_by_rank = {
        col.get("rank"): col for col in records_data.get("collections", [])
    }

    for entity in result.get("entities", []):
        source_collection = entity.get("sourceCollection")
        collection = collections_by_rank.get(source_collection)
        if collection is None:
            raise SeedInjectionError(
                f"Entity '{entity.get('name')}' has sourceCollection "
                f"{source_collection}, which is not present in the extracted records "
                f"(available collection indices: "
                f"{sorted(r for r in collections_by_rank)})."
            )
        entity["seed"] = _seed_rows_for_entity(entity, collection)

    return result
