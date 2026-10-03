"""Command-line entry point for ibkr-ato."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr-ato",
        description="Interactive Brokers Flex Query exports to ATO individual tax-return figures.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="compute figures and write the per-FY report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"{args.command}: not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
