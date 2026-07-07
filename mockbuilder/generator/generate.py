"""Deterministic React code generator (Phase 3).

Purely template-driven: renders Jinja2 templates from a *validated* AppModel into
a React harness. There are **no LLM calls here** — reasoning already produced the
contract; generation is a mechanical, reproducible transform, so the same
AppModel always yields byte-identical output.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

# Templates live alongside this module; the Jinja environment reads ONLY from
# here — no network, no AI, no external I/O beyond the local templates dir.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Matches a `{fieldName}` placeholder inside a schema label/testId/route.
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Characters that aren't safe in an npm package name.
_UNSAFE_NAME = re.compile(r"[^a-z0-9]+")


def label_expr(label: str) -> str:
    """Render a schema label as a JS expression body.

    Labels may embed field placeholders (e.g. ``"{title}"``). Those become a
    template literal referencing the bound entity via props
    (``` `${props.title}` ```); a plain label becomes a quoted JS string. Mirrors
    the ``{id}`` interpolation used for ``data-testid``.
    """
    text = str(label)
    if _PLACEHOLDER.search(text):
        # Safe fallback: `${props.title || ''}` never renders "undefined".
        return "`" + _PLACEHOLDER.sub(r"${props.\1 || ''}", text) + "`"
    return json.dumps(text)


def route_path(route: str) -> str:
    """Convert a schema route to React Router syntax: ``{id}`` -> ``:id``."""
    return _PLACEHOLDER.sub(r":\1", str(route))


def nav_target(route: str) -> str:
    """Convert a React-Router route into a JS ``navigate()`` target expression.

    Path params become per-instance interpolations so a click lands on the
    concrete row: ``/story/:id`` -> ``` `/story/${props.id}` ```. A param-free
    route becomes a plain quoted string.
    """
    text = str(route)
    if ":" in text:
        return "`" + re.sub(r":(\w+)", r"${props.\1}", text) + "`"
    return json.dumps(text)


def package_slug(name: str) -> str:
    """Slugify an app name into a valid (lowercase, url-safe) npm package name."""
    slug = _UNSAFE_NAME.sub("-", str(name).lower()).strip("-")
    return slug or "mock-app"


class ReactGenerator:
    """Renders a validated AppModel into a React harness under ``output_dir``."""

    def __init__(self, app_model: dict[str, Any], output_dir: Path | str) -> None:
        self.app_model = app_model
        self.output_dir = Path(output_dir)
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            # Output is JS/JSX, not HTML — HTML autoescaping would corrupt it.
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self.env.filters["label_expr"] = label_expr
        self.env.filters["route_path"] = route_path
        self.env.filters["nav_target"] = nav_target

    def _write(self, template_name: str, relpath: str, **context: Any) -> Path:
        """Render ``template_name`` and write it to ``output_dir/relpath``."""
        rendered = self.env.get_template(template_name).render(**context)
        out_path = self.output_dir / relpath
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        return out_path

    def generate(self) -> None:
        """Run the full generation pipeline for this AppModel, in order."""
        self.generate_context()
        self.generate_components()
        self.generate_screens()
        self.generate_scaffold()

    def generate_context(self) -> Path:
        """Render the global state manager to ``output_dir/src/GlobalContext.jsx``."""
        template = self.env.get_template("GlobalContext.jsx.jinja")
        rendered = template.render(entities=self.app_model["entities"])

        out_path = self.output_dir / "src" / "GlobalContext.jsx"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        return out_path

    def _flow_action_lookup(self) -> dict[str, str]:
        """Map a flow-step ``testId`` -> its ``expectScreen`` target.

        This is the deterministic interactivity source: the flows graph already
        encodes which element leads where, independent of whether the LLM
        remembered to put an ``action`` on the element itself.
        """
        lookup: dict[str, str] = {}
        for flow in self.app_model.get("flows", []):
            for step in flow.get("steps", []):
                test_id = step.get("testId")
                target = step.get("expectScreen")
                if test_id and target:
                    lookup.setdefault(test_id, target)
        return lookup

    @staticmethod
    def _testids_equivalent(a: str, b: str) -> bool:
        """True if two testIds match, treating ``{id}`` as a per-instance slot
        (so ``story-link-{id}`` matches a concrete ``story-link-5``)."""
        if a == b:
            return True
        for pattern, other in ((a, b), (b, a)):
            if "{id}" in pattern:
                regex = (
                    "^"
                    + re.escape(pattern).replace(re.escape("{id}"), r"[A-Za-z0-9_-]+")
                    + "$"
                )
                if re.fullmatch(regex, other):
                    return True
        return False

    def _inject_flow_actions(
        self, component: dict[str, Any], flow_lookup: dict[str, str], screen_ids: set
    ) -> dict[str, Any]:
        """Return a copy of ``component`` where elements lacking a real action
        get a synthetic ``navigate`` action derived from the flows graph."""
        enriched = copy.deepcopy(component)
        for el in enriched.get("interactiveElements", []):
            action = el.get("action") or {}
            if action.get("type") and action.get("type") != "none":
                continue  # keep a real model-provided action

            test_id = el.get("testId", "")
            target = flow_lookup.get(test_id)
            if target is None:
                for flow_tid, tgt in flow_lookup.items():
                    if self._testids_equivalent(test_id, flow_tid):
                        target = tgt
                        break
            if target and target in screen_ids:
                el["action"] = {"type": "navigate", "targetScreen": target}
        return enriched

    def generate_components(self) -> list[Path]:
        """Render one ``src/components/{Name}.jsx`` per component in the model."""
        template = self.env.get_template("Component.jsx.jinja")
        out_dir = self.output_dir / "src" / "components"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Map screen id -> React-Router route so navigate actions can resolve
        # `action.targetScreen` (a kebab id) to a concrete path.
        screen_routes = {
            s["id"]: route_path(s.get("route", "/"))
            for s in self.app_model.get("screens", [])
        }
        screen_ids = set(screen_routes)
        flow_lookup = self._flow_action_lookup()

        written: list[Path] = []
        for component in self.app_model.get("components", []):
            # Deterministically wire navigation from the flows graph for any
            # element the model left without an action.
            enriched = self._inject_flow_actions(component, flow_lookup, screen_ids)
            rendered = template.render(
                component=enriched, screen_routes=screen_routes
            )
            out_path = out_dir / f"{component['name']}.jsx"
            out_path.write_text(rendered, encoding="utf-8")
            written.append(out_path)
        return written

    def generate_screens(self) -> list[Path]:
        """Render one ``src/screens/{screenId}.jsx`` per screen in the model."""
        template = self.env.get_template("Screen.jsx.jinja")
        out_dir = self.output_dir / "src" / "screens"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Only components that actually exist may be imported/rendered, so a
        # screen that references an undefined component can't break the build.
        component_names = {c["name"] for c in self.app_model.get("components", [])}

        written: list[Path] = []
        for screen in self.app_model["screens"]:
            rendered = template.render(screen=screen, component_names=component_names)
            out_path = out_dir / f"{screen['id']}.jsx"
            out_path.write_text(rendered, encoding="utf-8")
            written.append(out_path)
        return written

    def generate_scaffold(self) -> list[Path]:
        """Render the Vite/Tailwind app shell + entry points to ``output_dir``.

        Emits the project config at the root (package.json, vite/tailwind/postcss
        config, index.html) and the entry points under src/ (main.jsx, index.css,
        App.jsx). App.jsx maps each screen's route to its component; tailwind's
        theme is seeded from the harvested design-token colors.
        """
        colors = self.app_model.get("designTokens", {}).get("colors", {})
        app_name = self.app_model.get("meta", {}).get("appName", "Mock App")

        return [
            self._write(
                "package.json.jinja",
                "package.json",
                package_name=package_slug(app_name),
            ),
            self._write("vite.config.js.jinja", "vite.config.js"),
            self._write("tailwind.config.js.jinja", "tailwind.config.js", colors=colors),
            self._write("postcss.config.js.jinja", "postcss.config.js"),
            self._write("index.html.jinja", "index.html", app_name=app_name),
            self._write("main.jsx.jinja", "src/main.jsx"),
            self._write("index.css.jinja", "src/index.css"),
            self._write("App.jsx.jinja", "src/App.jsx", screens=self.app_model["screens"]),
        ]
