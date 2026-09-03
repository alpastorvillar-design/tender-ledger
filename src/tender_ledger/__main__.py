"""Command-line entry point: inspect an archive, load one, or verify its coverage."""

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .packages import Limits, inspect_package


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tender-ledger")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser(
        "inspect", help="validate a local .tar.gz and print a summary"
    )
    inspect.add_argument("archive", type=Path)
    inspect.add_argument("--max-compressed-mib", type=int, default=64)
    inspect.add_argument("--max-expanded-mib", type=int, default=512)
    inspect.add_argument("--max-member-mib", type=int, default=8)
    inspect.add_argument("--max-notices", type=int, default=10_000)

    db_cmd = commands.add_parser("db", help="database maintenance")
    db_sub = db_cmd.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("upgrade", help="apply pending schema migrations")

    load = commands.add_parser("load", help="load a package archive into PostgreSQL")
    load.add_argument("archive", type=Path)
    load.add_argument(
        "--package-id", required=True,
        help="source package identity, e.g. daily/202300220",
    )
    load.add_argument("--batch-size", type=int, default=500)
    load.add_argument(
        "--lock-wait", action="store_true",
        help="wait for a concurrent capture instead of failing fast",
    )
    load.add_argument(
        "--force-recapture", action="store_true",
        help="acquire the package again as a new capture, even for identical bytes",
    )

    verify = commands.add_parser(
        "verify", help="check a published capture against the TED Search API"
    )
    verify.add_argument("--capture-id", type=int, required=True)
    verify.add_argument(
        "--lock-wait", action="store_true",
        help="wait for concurrent work on the package instead of failing fast",
    )

    status = commands.add_parser("status", help="show capture status")
    status.add_argument("--package-id", help="restrict to one source package")

    return parser


def _cmd_inspect(args: argparse.Namespace) -> int:
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


def _cmd_db_upgrade(_args: argparse.Namespace) -> int:
    from . import db

    with db.connect() as conn:
        applied = db.migrate(conn)
    print(json.dumps({"applied": applied}, indent=2))
    return 0


def _cmd_load(args: argparse.Namespace) -> int:
    from . import db
    from .loader import load_package

    with db.connect() as conn:
        result = load_package(
            conn, args.archive, args.package_id,
            batch_size=args.batch_size, lock_wait=args.lock_wait,
            force_recapture=args.force_recapture,
        )
    print(json.dumps(dataclasses.asdict(result), indent=2))
    if result.status != "published":
        reason = result.failure_reason or result.load_error or result.publish_error
        print(
            f"capture {result.capture_id} is {result.status!r}, not published: {reason}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from . import db
    from .verification import VERIFIED, verify_capture

    with db.connect() as conn:
        result = verify_capture(conn, args.capture_id, lock_wait=args.lock_wait)
    print(json.dumps(dataclasses.asdict(result), indent=2, default=str))
    # Success means a committed 'verified' row, not a hopeful in-memory outcome.
    if not (result.state == VERIFIED and result.coverage_verified):
        print(
            f"capture {result.capture_id} coverage is {result.state!r}: {result.reason}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from . import db

    sql = (
        "select source_package_id, capture_id, acquisition_ordinal, status,"
        " is_published, member_count, distinct_notice_count, loaded_row_count,"
        " source_coverage_verified, verification_state, verification_attempt_id,"
        " verification_finished_at, failure_reason"
        " from tl_read.capture_status"
    )
    params: tuple = ()
    if args.package_id:
        sql += " where source_package_id = %s"
        params = (args.package_id,)
    sql += " order by acquisition_ordinal"
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d.name for d in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    print(json.dumps(rows, indent=2, default=str))
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "inspect":
            return _cmd_inspect(args)
        if args.command == "db":
            return _cmd_db_upgrade(args)
        if args.command == "load":
            return _cmd_load(args)
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "status":
            return _cmd_status(args)
    except Exception as exc:  # surface a clean message, not a traceback
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
