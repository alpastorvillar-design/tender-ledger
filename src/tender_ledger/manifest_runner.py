"""Run a :class:`~tender_ledger.manifest.Manifest` sequentially, one package
at a time, composing the already-accepted ``ingest_package`` use case.

This module adds no state of its own: no manifest-level table, no global
transaction, no second recovery mechanism. Every package's durability comes
entirely from ``ingest_package`` -- its run, capture, verification attempt and
checkpoint. What this module owns is the sequencing: one connection open at a
time, closed after every attempt; a hard stop at the first entry that is not a
completed package or that raises; and a bounded JSON-shaped report of what was
attempted, in order.

Because a manifest entry's planning metadata (observed counts, bytes, purpose)
is never read here, a second run over the same manifest is exactly a second
``ingest_package`` call per identity: an already-current, contract-matching
checkpoint replays with no request of any kind, and a run left mid-flight by an
earlier interruption resumes from the state ``ingest_package`` itself already
persisted. There is nothing about "the manifest" for either of those to
remember.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from .ingest import IngestResult, ingest_package
from .manifest import Manifest, ManifestEntry

#: The shape of a report this module writes. Bumped only if a field is added,
#: renamed or removed -- not for a change in what values a field can hold.
REPORT_VERSION = 1

#: Kept well under any reasonable log or terminal limit, and short enough that
#: a report file stays bounded regardless of how many entries it holds.
_MAX_ERROR_CHARS = 2000

_DEFAULT_INGEST_OPTIONS: Callable[[ManifestEntry], dict] = lambda entry: {}  # noqa: E731


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class ManifestSummary:
    """An identifying echo of the manifest that was run, not its content."""

    manifest_version: int
    path: str | None
    package_count: int


@dataclass(frozen=True)
class EntryReport:
    """What happened to one manifest entry. ``outcome`` mirrors
    :class:`IngestResult.outcome` ("processed", "replayed", "incomplete") for
    an entry ``ingest_package`` returned from, or is "error" for one where the
    call raised instead of returning."""

    order: int
    source_package_id: str
    outcome: str
    run_id: int | None
    capture_id: int | None
    verification_attempt_id: int | None
    checkpoint_is_current: bool | None
    phase: str | None
    http_attempts: int | None
    downloaded_bytes: int | None
    error: str | None


@dataclass(frozen=True)
class ManifestReport:
    report_version: int
    manifest: ManifestSummary
    started_at: dt.datetime
    finished_at: dt.datetime
    duration_seconds: float
    #: "completed" once every entry reported a current, processed or replayed
    #: checkpoint; "failed" from the first entry that did not.
    status: str
    failed_entry: str | None
    entries: tuple[EntryReport, ...]


def run_manifest(
    manifest: Manifest,
    *,
    connect_factory: Callable[[], psycopg.Connection],
    ingest_one: Callable[..., IngestResult] = ingest_package,
    ingest_options: Callable[[ManifestEntry], dict] = _DEFAULT_INGEST_OPTIONS,
    manifest_path: str | None = None,
    now: Callable[[], dt.datetime] = _utcnow,
) -> ManifestReport:
    """Process every entry in order, stopping at the first one that is not a
    completed package.

    At most one connection is open at a time: ``connect_factory`` is called
    once per attempted entry, and that connection is always closed before the
    next one opens, in a package's own ``finally`` -- an exception included.
    An entry after a stopping point is never attempted: ``connect_factory`` is
    never even called for it, so it never appears in the report.

    ``ingest_one`` defaults to the real ``ingest_package``; tests substitute it
    to force a specific entry to raise without reaching into that module's
    internals. ``ingest_options(entry)`` supplies the per-call keyword
    arguments (``data_root``, ``batch_size``, ``lock_wait``, and in tests
    ``url``/``transport``/``budgets``) -- the manifest's own planning fields on
    ``entry`` are never read to build them.
    """
    started_at = now()
    entries: list[EntryReport] = []
    status = "completed"
    failed_entry: str | None = None

    for entry in manifest.entries:
        try:
            conn = connect_factory()
        except Exception as exc:  # connecting itself failed: nothing to close
            entries.append(_error_entry(entry, exc))
            status, failed_entry = "failed", entry.source_package_id
            break
        try:
            try:
                result = ingest_one(conn, entry.source_package_id, **ingest_options(entry))
            except Exception as exc:
                entries.append(_error_entry(entry, exc))
                status, failed_entry = "failed", entry.source_package_id
                break
            entries.append(_result_entry(entry, result))
            if not (result.checkpoint_is_current and result.outcome in ("processed", "replayed")):
                status, failed_entry = "failed", entry.source_package_id
                break
        finally:
            conn.close()

    finished_at = now()
    return ManifestReport(
        report_version=REPORT_VERSION,
        manifest=ManifestSummary(
            manifest_version=manifest.manifest_version,
            path=manifest_path,
            package_count=len(manifest.entries),
        ),
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=(finished_at - started_at).total_seconds(),
        status=status,
        failed_entry=failed_entry,
        entries=tuple(entries),
    )


def _result_entry(entry: ManifestEntry, result: IngestResult) -> EntryReport:
    return EntryReport(
        order=entry.order,
        source_package_id=entry.source_package_id,
        outcome=result.outcome,
        run_id=result.run_id,
        capture_id=result.capture_id,
        verification_attempt_id=result.verification_attempt_id,
        checkpoint_is_current=result.checkpoint_is_current,
        phase=result.phase,
        http_attempts=result.http_attempts,
        downloaded_bytes=result.downloaded_bytes,
        error=None if result.error is None else result.error[:_MAX_ERROR_CHARS],
    )


def _error_entry(entry: ManifestEntry, exc: Exception) -> EntryReport:
    return EntryReport(
        order=entry.order,
        source_package_id=entry.source_package_id,
        outcome="error",
        run_id=None,
        capture_id=None,
        verification_attempt_id=None,
        checkpoint_is_current=None,
        phase=None,
        http_attempts=None,
        downloaded_bytes=None,
        error=f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS],
    )


def report_to_dict(report: ManifestReport) -> dict:
    """A JSON-safe rendering: timestamps as ISO 8601, everything else as-is."""
    return {
        "report_version": report.report_version,
        "manifest": {
            "manifest_version": report.manifest.manifest_version,
            "path": report.manifest.path,
            "package_count": report.manifest.package_count,
        },
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "duration_seconds": report.duration_seconds,
        "status": report.status,
        "failed_entry": report.failed_entry,
        "entries": [
            {
                "order": e.order,
                "source_package_id": e.source_package_id,
                "outcome": e.outcome,
                "run_id": e.run_id,
                "capture_id": e.capture_id,
                "verification_attempt_id": e.verification_attempt_id,
                "checkpoint_is_current": e.checkpoint_is_current,
                "phase": e.phase,
                "http_attempts": e.http_attempts,
                "downloaded_bytes": e.downloaded_bytes,
                "error": e.error,
            }
            for e in report.entries
        ],
    }
