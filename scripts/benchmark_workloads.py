"""Benchmark harness for the SQL workloads in ``queries/``.

Reusable and portable: it needs only ``psycopg`` and a reachable PostgreSQL
database, nothing specific to any one host, dataset size or candidate index.
For each workload it keeps three things separate:

* a single ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` plan, which itself
  executes the query once;
* one uncounted warmup execution;
* a fixed number of separately timed repetitions, reduced to a median and a
  95th percentile in milliseconds.

Every repetition's result is folded into a row count and a SHA-256 checksum of
the ordered rows -- never the rows themselves -- so two variants of the same
workload (for instance, before and after a candidate index) can be compared
for identical output before their timings are compared at all. A workload
whose own query has no ``ORDER BY`` would make that checksum meaningless; all
six shipped workloads order their result deterministically by design (see each
query file's own header), and this harness trusts that rather than imposing
its own ordering.

This module intentionally has no opinion about *which* indexes exist. Applying
or dropping ``queries/benchmark_candidate_indexes.sql`` is a separate, explicit
step against a disposable database -- never the migrated schema.
"""

import argparse
import hashlib
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql

QUERIES_DIR = Path(__file__).resolve().parents[1] / "queries"


@dataclass(frozen=True)
class Workload:
    name: str
    query_file: str
    params: dict = field(default_factory=dict)


#: The six workloads the M3 contract names, with one representative set of
#: parameters each. A caller benchmarking a different dataset overrides the
#: parameterized ones (cutoff, first_month/last_month, package_a/package_b)
#: rather than editing this table.
DEFAULT_WORKLOADS = (
    Workload("monthly_notice_counts", "monthly_notice_counts.sql"),
    Workload("latest_capture_per_publication", "latest_capture_per_publication.sql"),
    Workload("official_change_references", "official_change_references.sql"),
    Workload(
        "acquisition_cutoff_state", "acquisition_cutoff_state.sql", {"cutoff": "5"}
    ),
    Workload(
        "monthly_coverage_calendar",
        "monthly_coverage_calendar.sql",
        {"first_month": "2020-01-01", "last_month": "2025-12-01"},
    ),
    Workload(
        "cross_package_overlap_audit",
        "cross_package_overlap_audit.sql",
        {"package_a": "daily/202300220", "package_b": "monthly/2023-11"},
    ),
)


def render_query(text: str, **params: str) -> str:
    """Substitute this repo's ``:'name'`` psql-style tokens with quoted
    literals -- the same convention ``psql -v name=value`` performs, and the
    one ``tests/test_queries.py`` already uses for the same query files."""
    for name, value in params.items():
        # psycopg owns literal quoting here. Values come from CLI arguments,
        # so surrounding them with quotes by hand would allow an apostrophe to
        # terminate the literal and change the benchmark query.
        literal = sql.Literal(value).as_string()
        text = text.replace(f":'{name}'", literal)
    return text


def load_query(query_file: str, params: dict, *, queries_dir: Path = QUERIES_DIR) -> str:
    text = (queries_dir / query_file).read_text(encoding="utf-8")
    return render_query(text, **params)


def _checksum(rows: list) -> str:
    payload = json.dumps([list(row) for row in rows], default=str, ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def explain_plan(conn: psycopg.Connection, sql: str) -> dict:
    """One ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``. Executes the query once."""
    with conn.cursor() as cur:
        cur.execute(f"explain (analyze, buffers, format json) {sql}")
        return cur.fetchone()[0][0]


def timed_repetitions(
    conn: psycopg.Connection, sql: str, *, warmup: int, repetitions: int
) -> dict:
    """One uncounted warmup, then ``repetitions`` separately timed executions.

    Every repetition's row count and checksum must agree with the first, or
    this raises -- a workload is not "benchmarked" if its own repetitions
    disagree on the answer.
    """
    if warmup < 0:
        raise ValueError("warmup must not be negative")
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    for _ in range(warmup):
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.fetchall()

    durations_ms: list[float] = []
    row_count: int | None = None
    checksum: str | None = None
    for i in range(repetitions):
        start = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        durations_ms.append((time.perf_counter() - start) * 1000.0)
        this_checksum = _checksum(rows)
        if checksum is None:
            checksum = this_checksum
            row_count = len(rows)
        elif this_checksum != checksum:
            raise RuntimeError(
                f"repetition {i} returned a different result"
                f" ({len(rows)} rows, checksum {this_checksum}) than repetition 0"
                f" ({row_count} rows, checksum {checksum})"
            )

    sorted_ms = sorted(durations_ms)
    return {
        "warmup": warmup,
        "repetitions": repetitions,
        "row_count": row_count,
        "checksum_sha256": checksum,
        "median_ms": statistics.median(sorted_ms),
        "p95_ms": sorted_ms[max(0, int(round(0.95 * (len(sorted_ms) - 1))))],
        "min_ms": sorted_ms[0],
        "max_ms": sorted_ms[-1],
        "durations_ms": durations_ms,
    }


def run_workload(
    conn: psycopg.Connection,
    workload: Workload,
    *,
    warmup: int,
    repetitions: int,
    queries_dir: Path = QUERIES_DIR,
) -> dict:
    sql = load_query(workload.query_file, workload.params, queries_dir=queries_dir)
    plan = explain_plan(conn, sql)
    stats = timed_repetitions(conn, sql, warmup=warmup, repetitions=repetitions)
    return {
        "name": workload.name,
        "query_file": workload.query_file,
        "params": workload.params,
        **stats,
        "plan": plan,
    }


def session_settings(conn: psycopg.Connection) -> dict:
    names = (
        "server_version", "effective_cache_size", "shared_buffers", "work_mem",
        "jit", "max_parallel_workers_per_gather", "max_wal_size", "wal_level",
    )
    with conn.cursor() as cur:
        cur.execute(
            "select name, setting, unit from pg_settings where name = any(%s)", (list(names),)
        )
        return {row[0]: {"setting": row[1], "unit": row[2]} for row in cur.fetchall()}


def analyze(conn: psycopg.Connection, *, vacuum: bool = False) -> None:
    """ANALYZE (optionally VACUUM first) outside any transaction block."""
    was_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute("vacuum (analyze)" if vacuum else "analyze")
    finally:
        conn.autocommit = was_autocommit


def run_benchmark(
    conn: psycopg.Connection,
    *,
    workloads: tuple[Workload, ...] = DEFAULT_WORKLOADS,
    warmup: int = 1,
    repetitions: int = 20,
    effective_cache_size: str = "1536MB",
    label: str | None = None,
    queries_dir: Path = QUERIES_DIR,
) -> dict:
    """Run every workload under one fixed session setting.

    Only ``effective_cache_size`` is set here, deliberately: the protocol
    fixes it identically across variants and records the rest of the
    configuration (:func:`session_settings`) rather than changing it.
    """
    with conn.cursor() as cur:
        # SET's value position does not accept a bind parameter; sql.Literal
        # quotes it safely without going through string interpolation.
        statement = sql.SQL("set effective_cache_size = {}")
        cur.execute(statement.format(sql.Literal(effective_cache_size)))
    results = [
        run_workload(
            conn, workload, warmup=warmup, repetitions=repetitions, queries_dir=queries_dir
        )
        for workload in workloads
    ]
    return {
        "label": label,
        "effective_cache_size": effective_cache_size,
        "session_settings": session_settings(conn),
        "workloads": results,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbname", required=True)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--effective-cache-size", default="1536MB")
    parser.add_argument("--label", default=None)
    parser.add_argument("--cutoff", default=None, help="override workload 4's :'cutoff'")
    parser.add_argument("--first-month", default=None, help="override workload 5's :'first_month'")
    parser.add_argument("--last-month", default=None, help="override workload 5's :'last_month'")
    parser.add_argument("--package-a", default=None, help="override workload 6's :'package_a'")
    parser.add_argument("--package-b", default=None, help="override workload 6's :'package_b'")
    parser.add_argument(
        "--output", type=Path, default=None, help="write JSON here instead of stdout"
    )
    return parser


def _workloads_from_args(args: argparse.Namespace) -> tuple[Workload, ...]:
    overrides = {
        "acquisition_cutoff_state": {"cutoff": args.cutoff},
        "monthly_coverage_calendar": {
            "first_month": args.first_month, "last_month": args.last_month
        },
        "cross_package_overlap_audit": {"package_a": args.package_a, "package_b": args.package_b},
    }
    workloads = []
    for workload in DEFAULT_WORKLOADS:
        extra = {k: v for k, v in overrides.get(workload.name, {}).items() if v is not None}
        params = {**workload.params, **extra}
        workloads.append(Workload(workload.name, workload.query_file, params))
    return tuple(workloads)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    conninfo = {
        k: v
        for k, v in {
            "dbname": args.dbname, "host": args.host, "port": args.port,
            "user": args.user, "password": args.password,
        }.items()
        if v is not None
    }
    with psycopg.connect(**conninfo, autocommit=True) as conn:
        report = run_benchmark(
            conn,
            workloads=_workloads_from_args(args),
            warmup=args.warmup,
            repetitions=args.repetitions,
            effective_cache_size=args.effective_cache_size,
            label=args.label,
        )
    payload = json.dumps(report, indent=2, default=str)
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
