"""Playwright agent walk: verifies P1 (offline) and P4 (navigable).

Serves the built ``dist/`` locally, drives a headless Chromium through every
declared flow, and asserts two runtime properties:

  * **P1 (self-contained)** — a request listener flags any request whose host is
    not localhost/127.0.0.1. A faithful mock is entirely offline after build.
  * **P4 (navigable)** — for each flow step, click the element (prefix-matching
    the ``testId`` so interpolated ``{id}`` instances match) and assert the
    expected screen's ``data-testid`` (``{expectScreen}-screen``) appears.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import async_playwright

HOST = "127.0.0.1"
PORT = 4173
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Block until ``host:port`` accepts a TCP connection, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


async def walk_flows(out_dir: Path, app_model: dict) -> dict[str, Any]:
    """Boot a local preview of ``out_dir/dist`` and walk the AppModel's flows.

    Returns ``{P1, P4, P1_detail, P4_detail}``.
    """
    out_dir = Path(out_dir)
    dist_dir = out_dir / "dist"
    result: dict[str, Any] = {
        "P1": False,
        "P4": False,
        "P1_detail": "",
        "P4_detail": "",
    }

    if not dist_dir.is_dir():
        result["P1_detail"] = result["P4_detail"] = "dist/ missing (build did not run)"
        return result

    # Host the built SPA locally. Python's http.server is simple and trivial to
    # tear down; client-side routing means we never deep-link, so no SPA
    # fallback is needed — we start at "/" and click within the app.
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(PORT),
            "--bind",
            HOST,
            "--directory",
            str(dist_dir),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://{HOST}:{PORT}"

    try:
        if not _wait_for_port(HOST, PORT):
            result["P1_detail"] = result["P4_detail"] = "preview server did not start"
            return result

        external_requests: list[str] = []
        p4_ok = True
        p4_notes: list[str] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()

            def _on_request(request: Any) -> None:
                parsed = urlparse(request.url)
                if parsed.scheme in ("http", "https") and parsed.hostname not in _LOCAL_HOSTS:
                    external_requests.append(request.url)

            page.on("request", _on_request)

            flows = app_model.get("flows", [])
            if not flows:
                p4_notes.append("no flows declared to walk")

            for flow in flows:
                flow_id = flow.get("id", "<flow>")
                # Reset to the app root before each flow.
                await page.goto(base_url, wait_until="networkidle")

                for i, step in enumerate(flow.get("steps", [])):
                    test_id = step.get("testId", "")
                    # Prefix match so `story-link-{id}` -> `story-link-` matches a
                    # rendered `story-link-5`.
                    prefix = test_id.replace("{id}", "")
                    selector = f"[data-testid^='{prefix}']"
                    try:
                        await page.click(selector, timeout=5000)
                        await page.wait_for_load_state("networkidle")
                    except Exception:
                        p4_ok = False
                        p4_notes.append(
                            f"{flow_id} step {i}: could not click '{test_id}' ({selector})"
                        )
                        break

                    expect = step.get("expectScreen")
                    if expect:
                        try:
                            await page.wait_for_selector(
                                f"[data-testid='{expect}-screen']", timeout=5000
                            )
                        except Exception:
                            p4_ok = False
                            p4_notes.append(
                                f"{flow_id} step {i}: expected screen "
                                f"'{expect}-screen' not reached after '{test_id}'"
                            )
                            break

            await browser.close()

        result["P1"] = len(external_requests) == 0
        result["P1_detail"] = (
            "no external network requests"
            if not external_requests
            else f"{len(external_requests)} external request(s): {external_requests[:3]}"
        )
        result["P4"] = p4_ok
        result["P4_detail"] = (
            "; ".join(p4_notes) if p4_notes else "all flow steps navigable"
        )
        return result
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except Exception:
            server.kill()
