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
import os
import sys
from typing import Any

from .crawler.crawler import EVIDENCE_DIR, Crawler
from .reasoning.reason import synthesize_model


def _check_credentials() -> bool:
    """Return True if a Groq credential is available in the environment.

    The reasoning stage constructs ``AsyncGroq()`` with no explicit key, so it
    needs ``GROQ_API_KEY`` set — via a local `.env` loaded above, or exported in
    the shell. We check up front so we fail fast with a clear message instead of
    crawling first and crashing on the API call.
    """
    return bool(os.getenv("GROQ_API_KEY"))


async def _build_pipeline(url: str) -> None:
    """Crawl ``url``, then synthesize an AppModel for the primary state."""
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


def run_build(url: str, out_dir: str) -> None:
    """Run the build pipeline: crawl the URL, then reason the primary state."""
    asyncio.run(_build_pipeline(url))


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
