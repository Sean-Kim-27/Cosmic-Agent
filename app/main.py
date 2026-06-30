"""Unified application entrypoint for API servers and CLI mode.

Examples:
    uvicorn app.main:app --reload
    python -m app.main --mode cli --provider google --model gemma-4-31b-it
"""

from __future__ import annotations

import argparse

from app.api.application import app
from app.interfaces.cli import main as cli_main

__all__ = ["app", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Build the compatibility entrypoint parser requested by Phase 5.5."""

    parser = argparse.ArgumentParser(description="Run Cosmic Agent.")
    parser.add_argument(
        "--mode",
        choices=("cli",),
        default="cli",
        help="Runtime mode. API mode is exposed as app.main:app for uvicorn.",
    )
    parser.add_argument("--provider", help="Override LLM provider for CLI mode.")
    parser.add_argument("--model", help="Override model or configured model alias for CLI mode.")
    parser.add_argument(
        "--no-cgi-parse",
        action="store_true",
        help="Disable post-stream CGI memory parsing in CLI mode.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Dispatch to the selected interface."""

    args = build_parser().parse_args(argv)
    if args.mode == "cli":
        cli_args: list[str] = []
        if args.provider:
            cli_args.extend(["--provider", args.provider])
        if args.model:
            cli_args.extend(["--model", args.model])
        if args.no_cgi_parse:
            cli_args.append("--no-cgi-parse")
        cli_main(cli_args)


if __name__ == "__main__":
    main()
