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

import contextlib
import datetime as dt
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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
    file_name: str | None
    sha256: str
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
    started_clock = time.perf_counter()
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
            file_name=_manifest_file_name(manifest_path),
            sha256=_manifest_sha256(manifest),
            package_count=len(manifest.entries),
        ),
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=time.perf_counter() - started_clock,
        status=status,
        failed_entry=failed_entry,
        entries=tuple(entries),
    )


def _manifest_file_name(path: str | None) -> str | None:
    """Keep report provenance useful without exposing a private absolute path."""
    if path is None:
        return None
    normalized = path.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] or None


def _manifest_sha256(manifest: Manifest) -> str:
    """Fingerprint the complete validated manifest without echoing its metadata."""
    document = {
        "manifest_version": manifest.manifest_version,
        "packages": [
            {
                "order": entry.order,
                "source_package_id": entry.source_package_id,
                "notice_count_observed": entry.notice_count_observed,
                "compressed_bytes_observed": entry.compressed_bytes_observed,
                "observed_at": entry.observed_at.isoformat(),
                "purpose": entry.purpose,
            }
            for entry in manifest.entries
        ],
    }
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
            "file_name": report.manifest.file_name,
            "sha256": report.manifest.sha256,
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


def write_report_file(path: Path, report: ManifestReport) -> None:
    """Write the report as JSON to ``path``, replacing it atomically.

    ``path``'s parent directory must already exist: nothing here creates one
    the caller did not, so a typo in ``--report`` fails loudly instead of
    quietly making a new directory. The temporary file is exclusive to this
    call and lives beside the destination, so the final rename stays on one
    filesystem; it is removed if anything goes wrong before the rename.
    """
    path = Path(path)
    payload = json.dumps(report_to_dict(report), indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".manifest-report-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise
