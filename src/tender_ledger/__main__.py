"""Command-line inspection of a local TED notice archive."""

import argparse
import json
from pathlib import Path
import sys

from .packages import Limits, inspect_package


def main() -> int:
    parser = argparse.ArgumentParser(prog="tender-ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="validate a local .tar.gz and print a summary")
    inspect.add_argument("archive", type=Path)
    inspect.add_argument("--max-compressed-mib", type=int, default=64)
    inspect.add_argument("--max-expanded-mib", type=int, default=512)
    inspect.add_argument("--max-member-mib", type=int, default=8)
    inspect.add_argument("--max-notices", type=int, default=10_000)
    args = parser.parse_args()
    try:
        limits = Limits(
            compressed_bytes=args.max_compressed_mib * 1024**2,
            expanded_bytes=args.max_expanded_mib * 1024**2,
            member_bytes=args.max_member_mib * 1024**2,
            notices=args.max_notices,
        )
        result = inspect_package(args.archive, limits)
    except ValueError as exc:
        print(f"Inspection failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
