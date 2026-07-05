"""Deterministic React code generator (Phase 3).

Purely template-driven: renders Jinja2 templates from a *validated* AppModel into
a React harness. There are **no LLM calls here** — reasoning already produced the
contract; generation is a mechanical, reproducible transform, so the same
AppModel always yields byte-identical output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

# Templates live alongside this module; the Jinja environment reads ONLY from
# here — no network, no AI, no external I/O beyond the local templates dir.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


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

    def generate_context(self) -> Path:
        """Render the global state manager to ``output_dir/src/GlobalContext.jsx``."""
        template = self.env.get_template("GlobalContext.jsx.jinja")
        rendered = template.render(entities=self.app_model["entities"])

        out_path = self.output_dir / "src" / "GlobalContext.jsx"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        return out_path
