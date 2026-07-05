"""Deterministic React code generator (Phase 3).

Purely template-driven: renders Jinja2 templates from a *validated* AppModel into
a React harness. There are **no LLM calls here** — reasoning already produced the
contract; generation is a mechanical, reproducible transform, so the same
AppModel always yields byte-identical output.
"""

from __future__ import annotations

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
        return "`" + _PLACEHOLDER.sub(r"${props.\1}", text) + "`"
    return json.dumps(text)


def route_path(route: str) -> str:
    """Convert a schema route to React Router syntax: ``{id}`` -> ``:id``."""
    return _PLACEHOLDER.sub(r":\1", str(route))


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

    def generate_components(self) -> list[Path]:
        """Render one ``src/components/{Name}.jsx`` per component in the model."""
        template = self.env.get_template("Component.jsx.jinja")
        out_dir = self.output_dir / "src" / "components"
        out_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for component in self.app_model.get("components", []):
            rendered = template.render(component=component)
            out_path = out_dir / f"{component['name']}.jsx"
            out_path.write_text(rendered, encoding="utf-8")
            written.append(out_path)
        return written

    def generate_screens(self) -> list[Path]:
        """Render one ``src/screens/{screenId}.jsx`` per screen in the model."""
        template = self.env.get_template("Screen.jsx.jinja")
        out_dir = self.output_dir / "src" / "screens"
        out_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for screen in self.app_model["screens"]:
            rendered = template.render(screen=screen)
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
