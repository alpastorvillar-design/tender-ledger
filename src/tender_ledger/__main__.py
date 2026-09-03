"""Command-line entry point: inspect an archive, load one, verify its coverage,
or run the whole flow for one package and checkpoint it."""

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .packages import Limits, inspect_package, limits_for, survey_package


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tender-ledger")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser(
        "inspect", help="validate a local .tar.gz and print a summary"
    )
    inspect.add_argument("archive", type=Path)
    inspect.add_argument(
        "--package-id",
        help="source package identity, e.g. daily/202300220 or monthly/2020-01;"
             " it selects the resource policy the archive is checked against",
    )
    inspect.add_argument(
        "--max-compressed-mib", type=int,
        help="override the compressed ceiling (default 64, or the package policy)",
    )
    inspect.add_argument(
        "--max-expanded-mib", type=int,
        help="override the expanded ceiling (default 512, or the package policy)",
    )
    inspect.add_argument(
        "--max-member-mib", type=int,
        help="override the per-member ceiling (default 8, or the package policy)",
    )
    inspect.add_argument(
        "--max-notices", type=int,
        help="override the notice ceiling (default 10000, or the package policy)",
    )
    inspect.add_argument(
        "--survey", action="store_true",
        help="inventory the archive's compatibility instead of stopping at the"
             " first member that cannot be loaded",
    )

    db_cmd = commands.add_parser("db", help="database maintenance")
    db_sub = db_cmd.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("upgrade", help="apply pending schema migrations")

    load = commands.add_parser("load", help="load a package archive into PostgreSQL")
    load.add_argument("archive", type=Path)
    load.add_argument(
        "--package-id", required=True,
        help="source package identity, e.g. daily/202300220 or monthly/2020-01;"
             " it selects the resource policy the archive is checked against",
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

    ingest = commands.add_parser(
        "ingest",
        help="download, load, verify and checkpoint one package",
    )
    ingest.add_argument(
        "--package-id", required=True,
        help="source package identity, e.g. daily/202300220 or monthly/2020-01;"
             " it derives the URL, the destination and the resource policy",
    )
    ingest.add_argument(
        "--data-dir", type=Path,
        help="directory holding downloaded archives (default: ./data, ignored by Git)",
    )
    ingest.add_argument("--batch-size", type=int, default=500)
    ingest.add_argument(
        "--lock-wait", action="store_true",
        help="wait for concurrent work on the package instead of failing fast",
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


def _inspection_limits(args: argparse.Namespace) -> Limits:
    """Resolve the ceilings this inspection runs under.

    With a package identity the policy is the maximum allowed: an explicit flag
    may narrow it for an ad-hoc look at an archive, but widening it here would
    let a hand-typed number approve bytes the loader of that same package would
    refuse.
    """
    policy = limits_for(args.package_id) if args.package_id else Limits()
    requested = {
        "compressed_bytes": _mib(args.max_compressed_mib),
        "expanded_bytes": _mib(args.max_expanded_mib),
        "member_bytes": _mib(args.max_member_mib),
        "notices": args.max_notices,
    }
    chosen = {}
    for name, value in requested.items():
        ceiling = getattr(policy, name)
        if value is None:
            chosen[name] = ceiling
            continue
        if args.package_id and value > ceiling:
            raise ValueError(
                f"{name} {value} is above the policy of {args.package_id!r} ({ceiling})"
            )
        chosen[name] = value
    return Limits(**chosen)


def _mib(value: int | None) -> int | None:
    return None if value is None else value * 1024**2


def _cmd_inspect(args: argparse.Namespace) -> int:
    if args.survey:
        return _cmd_survey(args)
    try:
        result = inspect_package(args.archive, _inspection_limits(args))
    except ValueError as exc:
        print(f"Inspection failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_survey(args: argparse.Namespace) -> int:
    """Print the compatibility inventory, and exit non-zero unless it is loadable.

    An archive-level fault prints no inventory at all: there is nothing this
    command can honestly say about an archive it could not finish reading.
    """
    try:
        result = survey_package(
            args.archive, _inspection_limits(args), source_package_id=args.package_id
        )
    except ValueError as exc:
        print(f"Survey failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if not result["compatible_for_load"]:
        print(
            f"{result['incompatible_member_count']} members and"
            f" {result['duplicate_identity_count']} repeated identities make this"
            " archive not loadable; a load publishes nothing from it",
            file=sys.stderr,
        )
        return 1
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


def _cmd_ingest(args: argparse.Namespace) -> int:
    from . import db
    from .ingest import ingest_package

    with db.connect() as conn:
        result = ingest_package(
            conn, args.package_id, data_root=args.data_dir,
            batch_size=args.batch_size, lock_wait=args.lock_wait,
        )
    print(json.dumps(dataclasses.asdict(result), indent=2, default=str))
    # Success means a checkpoint this run's evidence still satisfies, read back
    # from the database -- not that every step happened to return without error.
    if not (result.checkpoint_is_current and result.outcome in ("processed", "replayed")):
        print(
            f"{result.source_package_id} is not processed"
            f" (run {result.run_id} is {result.phase!r}): {result.error}",
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
        if args.command == "ingest":
            return _cmd_ingest(args)
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
