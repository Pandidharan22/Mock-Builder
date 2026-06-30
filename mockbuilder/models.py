"""Dataclass models that mirror app_model.schema.json.

The JSON Schema (``app_model.schema.json`` in the repo root) is the single
source of truth for the AppModel contract between the reasoning (LLM) stage and
the deterministic generator stage. These dataclasses are a typed, ergonomic
mirror of that schema for use inside the Python pipeline — they are NOT a second
source of truth. Anything that crosses the contract boundary should be validated
with :func:`validate_app_model`, which checks against the JSON Schema itself.

Plain stdlib dataclasses are used deliberately (no Pydantic) to keep the
dependency surface small and to keep the JSON file authoritative for validation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

# The schema lives at the repository root, one level above this package.
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "app_model.schema.json"


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
@dataclass
class Meta:
    """Provenance and identity of the mock (schema: #/properties/meta)."""

    sourceUrl: str
    appName: str
    appType: str  # one of the appType enum values
    generatedAt: str  # ISO-8601 date-time
    modelVersion: str
    crawlEvidenceHash: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Meta":
        return cls(
            sourceUrl=data["sourceUrl"],
            appName=data["appName"],
            appType=data["appType"],
            generatedAt=data["generatedAt"],
            modelVersion=data["modelVersion"],
            crawlEvidenceHash=data.get("crawlEvidenceHash"),
        )


# --------------------------------------------------------------------------- #
# DesignTokens
# --------------------------------------------------------------------------- #
@dataclass
class Typography:
    """Schema: #/properties/designTokens/properties/typography."""

    fontFamily: str
    headingFontFamily: str | None = None
    baseSize: str | None = None
    scale: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Typography":
        return cls(
            fontFamily=data["fontFamily"],
            headingFontFamily=data.get("headingFontFamily"),
            baseSize=data.get("baseSize"),
            scale=list(data.get("scale", [])),
        )


@dataclass
class DesignTokens:
    """Extracted (not invented) theme tokens (schema: #/properties/designTokens).

    ``colors`` carries named semantic roles plus arbitrary extra hex colors, so
    it is kept as a plain dict to honour the schema's ``additionalProperties``.
    ``spacing``/``radii``/``shadows`` are likewise open string maps.
    """

    colors: dict[str, str]
    typography: Typography
    spacing: dict[str, str] = field(default_factory=dict)
    radii: dict[str, str] = field(default_factory=dict)
    shadows: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DesignTokens":
        return cls(
            colors=dict(data["colors"]),
            typography=Typography.from_dict(data["typography"]),
            spacing=dict(data.get("spacing", {})),
            radii=dict(data.get("radii", {})),
            shadows=dict(data.get("shadows", {})),
        )


# --------------------------------------------------------------------------- #
# Entity
# --------------------------------------------------------------------------- #
@dataclass
class EntityField:
    """A field on an entity (schema: #/$defs/entity/properties/fields/items)."""

    name: str
    type: str  # one of the field-type enum values
    enumValues: list[str] | None = None  # required when type == "enum"
    refEntity: str | None = None  # required when type == "ref"/"array" of refs
    optional: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityField":
        return cls(
            name=data["name"],
            type=data["type"],
            enumValues=data.get("enumValues"),
            refEntity=data.get("refEntity"),
            optional=data.get("optional", False),
        )


@dataclass
class Entity:
    """A data-model entity (schema: #/$defs/entity)."""

    name: str
    fields: list[EntityField]
    seed: list[dict[str, Any]]
    description: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entity":
        return cls(
            name=data["name"],
            fields=[EntityField.from_dict(f) for f in data["fields"]],
            seed=list(data["seed"]),
            description=data.get("description"),
        )


# --------------------------------------------------------------------------- #
# Component
# --------------------------------------------------------------------------- #
@dataclass
class Prop:
    """A React prop (schema: #/$defs/component/properties/props/items)."""

    name: str
    type: str
    optional: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Prop":
        return cls(
            name=data["name"],
            type=data["type"],
            optional=data.get("optional", False),
        )


@dataclass
class Mutation:
    """A declarative local-state mutation (schema: #/$defs/interactiveElement
    .../action/properties/mutation)."""

    store: str | None = None
    op: str | None = None  # one of the mutation-op enum values
    payloadFrom: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Mutation":
        return cls(
            store=data.get("store"),
            op=data.get("op"),
            payloadFrom=data.get("payloadFrom"),
        )


@dataclass
class Action:
    """What happens on interaction (schema: #/$defs/interactiveElement/properties/action)."""

    type: str | None = None  # navigate | mutateState | navigateAndMutate | none
    targetScreen: str | None = None
    mutation: Mutation | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Action":
        return cls(
            type=data.get("type"),
            targetScreen=data.get("targetScreen"),
            mutation=Mutation.from_dict(data["mutation"]) if data.get("mutation") else None,
        )


@dataclass
class InteractiveElement:
    """An addressable, actionable element (schema: #/$defs/interactiveElement)."""

    testId: str
    kind: str  # button | link | input | select | toggle | tab
    label: str
    action: Action | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InteractiveElement":
        return cls(
            testId=data["testId"],
            kind=data["kind"],
            label=data["label"],
            action=Action.from_dict(data["action"]) if data.get("action") else None,
        )


@dataclass
class Component:
    """A reusable UI component (schema: #/$defs/component)."""

    name: str  # PascalCase
    role: str
    boundToEntity: str | None = None
    props: list[Prop] = field(default_factory=list)
    interactiveElements: list[InteractiveElement] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Component":
        return cls(
            name=data["name"],
            role=data["role"],
            boundToEntity=data.get("boundToEntity"),
            props=[Prop.from_dict(p) for p in data.get("props", [])],
            interactiveElements=[
                InteractiveElement.from_dict(e)
                for e in data.get("interactiveElements", [])
            ],
        )


# --------------------------------------------------------------------------- #
# Screen
# --------------------------------------------------------------------------- #
@dataclass
class Region:
    """A coarse layout region (schema: #/$defs/screen .../layout/regions/items)."""

    name: str  # header | nav | sidebar | main | footer | modal
    components: list[str]
    repeats: bool | None = None
    repeatsOver: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Region":
        return cls(
            name=data["name"],
            components=list(data["components"]),
            repeats=data.get("repeats"),
            repeatsOver=data.get("repeatsOver"),
        )


@dataclass
class Variant:
    """A screen state (schema: #/$defs/screen/properties/variants/items)."""

    id: str
    kind: str  # default | empty | error | loading | unavailable | success
    description: str | None = None
    trigger: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Variant":
        return cls(
            id=data["id"],
            kind=data["kind"],
            description=data.get("description"),
            trigger=data.get("trigger"),
        )


@dataclass
class Screen:
    """A distinct screen / route (schema: #/$defs/screen)."""

    id: str
    name: str
    route: str
    variants: list[Variant]
    purpose: str | None = None
    layout_regions: list[Region] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Screen":
        layout = data.get("layout") or {}
        return cls(
            id=data["id"],
            name=data["name"],
            route=data["route"],
            variants=[Variant.from_dict(v) for v in data["variants"]],
            purpose=data.get("purpose"),
            layout_regions=[Region.from_dict(r) for r in layout.get("regions", [])],
        )


# --------------------------------------------------------------------------- #
# Flow
# --------------------------------------------------------------------------- #
@dataclass
class FlowStep:
    """A single ordered step in a flow (schema: #/$defs/flow/properties/steps/items)."""

    screen: str
    testId: str
    variant: str | None = None
    expectScreen: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlowStep":
        return cls(
            screen=data["screen"],
            testId=data["testId"],
            variant=data.get("variant"),
            expectScreen=data.get("expectScreen"),
        )


@dataclass
class Flow:
    """A named user journey (schema: #/$defs/flow)."""

    id: str
    name: str
    steps: list[FlowStep]
    description: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Flow":
        return cls(
            id=data["id"],
            name=data["name"],
            steps=[FlowStep.from_dict(s) for s in data["steps"]],
            description=data.get("description"),
        )


# --------------------------------------------------------------------------- #
# AppModel (root)
# --------------------------------------------------------------------------- #
@dataclass
class AppModel:
    """The root contract object (schema: AppModel / top-level object)."""

    meta: Meta
    designTokens: DesignTokens
    entities: list[Entity]
    screens: list[Screen]
    flows: list[Flow]
    components: list[Component] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppModel":
        """Build an :class:`AppModel` from a (preferably validated) dict.

        Call :func:`validate_app_model` first to guarantee the dict conforms to
        the contract; this constructor assumes well-formed input.
        """
        return cls(
            meta=Meta.from_dict(data["meta"]),
            designTokens=DesignTokens.from_dict(data["designTokens"]),
            entities=[Entity.from_dict(e) for e in data["entities"]],
            screens=[Screen.from_dict(s) for s in data["screens"]],
            flows=[Flow.from_dict(f) for f in data["flows"]],
            components=[Component.from_dict(c) for c in data.get("components", [])],
        )


# --------------------------------------------------------------------------- #
# Validation gate
# --------------------------------------------------------------------------- #
def _load_schema() -> dict[str, Any]:
    """Read and parse the AppModel JSON Schema from disk."""
    with SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_app_model(data: dict) -> None:
    """Validate ``data`` against app_model.schema.json.

    Loads the JSON Schema from disk (the single source of truth) and validates
    the supplied dictionary against it using ``jsonschema``. Returns ``None`` on
    success; raises ``jsonschema.ValidationError`` on the first violation.
    """
    schema = _load_schema()
    jsonschema.validate(instance=data, schema=schema)
