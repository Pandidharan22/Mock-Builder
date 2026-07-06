"""Automated property verifier: P8 (buildable), P1 (offline), P4 (navigable).

Turns the PLAN's properties into a mechanical scorecard. P8 is proven by actually
installing and building the generated project; P1 and P4 are proven at runtime by
the Playwright agent walk (:mod:`.agent_walk`). Nothing about this stage is
heuristic — a red property means the generated harness genuinely fails it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .agent_walk import walk_flows


def _run_npm(args: str, cwd: Path) -> bool:
    """Run ``npm <args>`` with output streamed to the console; True if exit 0.

    Resolves npm's absolute path (a ``.cmd`` shim on Windows) and prepends its
    directory to PATH so the shim can find ``node`` — robust to PATH quirks in
    non-interactive shells. Returns False (not an exception) if npm is missing.
    """
    npm = shutil.which("npm")
    if npm is None:
        print("  [verify] npm not found on PATH — cannot verify P8/P1/P4")
        return False
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(npm) + os.pathsep + env.get("PATH", "")
    completed = subprocess.run(
        f'"{npm}" {args}', cwd=str(cwd), shell=True, env=env
    )
    return completed.returncode == 0


async def run_verification(out_dir: Path, app_model: dict) -> dict[str, Any]:
    """Verify the generated harness at ``out_dir`` and return a scorecard dict.

    Scorecard keys: ``P8``/``P1``/``P4`` (bools) and ``details`` (per-property
    strings). P1/P4 are skipped if the build (P8) fails, since there is nothing
    to run.
    """
    out_dir = Path(out_dir)
    scorecard: dict[str, Any] = {
        "P8": False,
        "P1": False,
        "P4": False,
        "details": {},
    }

    # --- P8: the project must install and build cleanly -------------------
    print("  [verify] npm install ...")
    if not _run_npm("install", out_dir):
        scorecard["details"]["P8"] = "npm install failed (see output above)"
        scorecard["details"]["P1"] = "skipped (install failed)"
        scorecard["details"]["P4"] = "skipped (install failed)"
        return scorecard

    print("  [verify] npm run build ...")
    build_ok = _run_npm("run build", out_dir)
    scorecard["P8"] = build_ok
    scorecard["details"]["P8"] = (
        "npm run build exited 0" if build_ok else "npm run build failed (see output above)"
    )
    if not build_ok:
        scorecard["details"]["P1"] = "skipped (build failed)"
        scorecard["details"]["P4"] = "skipped (build failed)"
        return scorecard

    # --- P1 + P4: runtime agent walk against the built dist ----------------
    print("  [verify] launching Playwright agent walk ...")
    try:
        walk = await walk_flows(out_dir, app_model)
    except Exception as exc:  # never let a verifier crash kill the build
        scorecard["details"]["P1"] = scorecard["details"]["P4"] = f"agent walk errored: {exc}"
        return scorecard

    scorecard["P1"] = walk["P1"]
    scorecard["P4"] = walk["P4"]
    scorecard["details"]["P1"] = walk["P1_detail"]
    scorecard["details"]["P4"] = walk["P4_detail"]
    return scorecard
