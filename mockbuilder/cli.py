"""Command-line interface for MockBuilder.

Exposes a single ``build`` command. As of Phase 1 it runs the crawler to capture
evidence; the reason -> generate -> verify stages are wired in later phases.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .crawler.crawler import Crawler


def run_build(url: str, out_dir: str) -> None:
    """Run the build pipeline. Phase 1: crawl the URL and capture evidence."""
    asyncio.run(Crawler().crawl(url))


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
        run_build(args.url, args.out_dir)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
