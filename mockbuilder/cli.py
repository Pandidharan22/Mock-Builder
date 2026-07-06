"""Command-line interface for MockBuilder.

Exposes a single ``build`` command that crawls a URL to capture evidence and then
reasons the primary state into a validated AppModel; the generate -> verify stages
are wired in later phases.
"""

from __future__ import annotations

# Load local `.env` variables (e.g. GROQ_API_KEY) before anything else, so the
# SDK clients can resolve credentials from the environment. Secrets live in
# `.env` (gitignored), never in the code.
from dotenv import load_dotenv

load_dotenv()

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .crawler.crawler import EVIDENCE_DIR, Crawler
from .generator.generate import ReactGenerator
from .reasoning.reason import synthesize_model
from .verifier.verify import run_verification


def _print_scorecard(scorecard: dict[str, Any]) -> None:
    """Render the verification scorecard to the console."""
    details = scorecard.get("details", {})

    def mark(passed: bool) -> str:
        return "[PASS]" if passed else "[FAIL]"

    print("\n=== Verification Scorecard ===")
    print(
        f"  {mark(scorecard['P8'])} P8 Build      (readable/buildable) "
        f"- {details.get('P8', '')}"
    )
    print(
        f"  {mark(scorecard['P1'])} P1 Offline    (self-contained)    "
        f"- {details.get('P1', '')}"
    )
    print(
        f"  {mark(scorecard['P4'])} P4 Navigable  (flows connected)   "
        f"- {details.get('P4', '')}"
    )
    overall = scorecard["P8"] and scorecard["P1"] and scorecard["P4"]
    print(f"  Overall: {'GREEN (all properties pass)' if overall else 'RED'}")


def _check_credentials() -> bool:
    """Return True if a Groq credential is available in the environment.

    The reasoning stage constructs ``AsyncGroq()`` with no explicit key, so it
    needs ``GROQ_API_KEY`` set — via a local `.env` loaded above, or exported in
    the shell. We check up front so we fail fast with a clear message instead of
    crawling first and crashing on the API call.
    """
    return bool(os.getenv("GROQ_API_KEY"))


async def _build_pipeline(url: str, out_dir: str) -> None:
    """Crawl ``url``, synthesize an AppModel, then generate the React harness."""
    records = await Crawler().crawl(url)
    if not records:
        print("No states captured; nothing to synthesize.")
        return

    # The primary state is the landing state (first recorded, depth 0).
    primary = records[0]
    state_hash = primary["state_hash"]

    print(f"Reasoning: synthesizing AppModel for primary state {state_hash} ...")
    model: dict[str, Any] = await synthesize_model(EVIDENCE_DIR, state_hash)

    meta = model.get("meta", {})
    print(
        "Generated AppModel: "
        f"appName={meta.get('appName')!r}, "
        f"appType={meta.get('appType')!r}, "
        f"entities={len(model.get('entities', []))}, "
        f"components={len(model.get('components', []))}, "
        f"screens={len(model.get('screens', []))}, "
        f"flows={len(model.get('flows', []))} "
        f"-> {EVIDENCE_DIR / f'{state_hash}_model.json'}"
    )

    # Deterministic generation: compile the validated AppModel into a React
    # harness under the output directory (no AI, pure Jinja2 templates).
    print(f"Generating React harness -> {out_dir} ...")
    ReactGenerator(model, out_dir).generate()
    print(
        f"Wrote {len(model.get('components', []))} component(s) and "
        f"{len(model.get('screens', []))} screen(s) under {Path(out_dir) / 'src'}"
    )

    # Phase 4: mechanically verify the generated harness (P8 build, P1 offline,
    # P4 navigable) and print a scorecard.
    print("Verifying generated harness ...")
    scorecard = await run_verification(out_dir, model)
    _print_scorecard(scorecard)
    (Path(out_dir) / "scorecard.json").write_text(
        json.dumps(scorecard, indent=2), encoding="utf-8"
    )


def run_build(url: str, out_dir: str) -> None:
    """Run the build pipeline: crawl, reason, then generate the harness."""
    asyncio.run(_build_pipeline(url, out_dir))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mockbuilder",
        description="MockBuilder — generate a deterministic mock harness from a live app.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_cmd = subparsers.add_parser(
        "build", help="Build a mock harness from a source URL."
    )
    build_cmd.add_argument("url", help="The URL of the app to crawl and mock.")
    build_cmd.add_argument(
        "-o",
        "--out-dir",
        required=True,
        help="Output directory for the generated harness.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "build":
        if not _check_credentials():
            print(
                "Error: no Groq credentials found in the environment.\n"
                "The reasoning stage needs GROQ_API_KEY. Set it either by:\n"
                "  1. Creating a local .env file (copy .env.example to .env and\n"
                "     fill in your key):   copy .env.example .env\n"
                "  2. Or exporting it in your shell:\n"
                '       PowerShell:  $env:GROQ_API_KEY = \"gsk_...\"\n'
                "       bash:        export GROQ_API_KEY=gsk_...",
                file=sys.stderr,
            )
            return 2
        run_build(args.url, args.out_dir)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
