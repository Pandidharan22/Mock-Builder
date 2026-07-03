"""Network response recorder.

Attaches a listener to the page that captures JSON API responses to disk. These
fixtures become the local database seed for the generated harness: the real app's
payloads are what make the mock's data look authentic without needing a backend.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from playwright.async_api import Page, Response

# Methods whose response bodies we care about capturing.
_CAPTURED_METHODS = {"GET", "POST"}

# Characters that are unsafe / noisy in a filename get collapsed to underscores.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(method: str, url: str) -> str:
    """Build a filesystem-safe ``METHOD_URL.json`` key for a response."""
    raw = f"{method}_{url}"
    safe = _UNSAFE.sub("_", raw).strip("_")
    # Keep filenames bounded — very long query strings blow past path limits.
    if len(safe) > 180:
        safe = safe[:180]
    return f"{safe}.json"


def _is_json(response: "Response") -> bool:
    """True if the response advertises a JSON content-type.

    Matches ``application/json`` as well as ``+json`` suffixes such as
    ``application/vnd.api+json``.
    """
    content_type = response.headers.get("content-type", "").lower()
    return "json" in content_type


async def attach_network_listener(page: "Page", output_dir: Path) -> None:
    """Save JSON GET/POST response bodies under ``output_dir/fixtures/``.

    Must be attached *before* navigation so responses aren't missed. Body reads
    are wrapped defensively: a stream may already be consumed, blocked, or the
    request may have been served from cache, and none of those should abort the
    crawl.
    """
    fixtures_dir = Path(output_dir) / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    async def _on_response(response: "Response") -> None:
        try:
            method = response.request.method.upper()
            if method not in _CAPTURED_METHODS:
                return
            if not _is_json(response):
                return

            # Reading the body can fail if the stream is gone/blocked — degrade
            # gracefully rather than tearing down the crawl.
            body = await response.json()

            filename = _sanitize(method, response.url)
            (fixtures_dir / filename).write_text(
                json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            # Consumed stream, non-decodable body, navigation teardown, etc.
            return

    page.on("response", _on_response)
