"""Load one TED package file into PostgreSQL as a recoverable, publishable capture.

Data flow, in one breath: hash the archive; persist a capture identity (with the
batch-partition size); stream its XML members and project each into the
allow-listed shape; write them in fixed-size deterministic batches (COPY) whose
rows and status commit together; reconcile member count / distinct keys / loaded
rows; verify the archive is unchanged; then publish the capture with a single
transactional pointer swap. A crash at any point leaves the previous published
capture visible and lets a retry resume from the last committed batch -- or, if
the crash was between reconcile and publish, from the durable ``loaded`` state.

Failures are classified rather than lumped together. A transient database error
(a cancelled statement, a deadlock, a lost connection) leaves the capture in its
recoverable state and is reported to the caller; a failure that condemns the
artifact itself (duplicate identity, unreadable XML, a reconciliation mismatch)
marks the capture ``failed``, which no retry resumes. Either way the load reports
an error and the CLI exits non-zero.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .db import repository as repo
from .package_contract import policy_for
from .packages import Limits, PackageError, limits_from, stream_notices
from .projection import CONTRACT_VERSION, project_member

_DEFAULT_BATCH_SIZE = 500


# A transient error leaves nothing about the capture wrong: the server dropped
# or cancelled the work in flight, so the committed batches and the identity stay
# valid and a retry resumes them. Anything saying the data itself is unacceptable
# -- a duplicate identity, a constraint violation, unreadable XML -- is terminal.
_TRANSIENT_SQLSTATE_CLASSES = ("08", "53", "57")  # connection, resources, intervention
_TRANSIENT_SQLSTATES = frozenset({
    "40001",  # serialization_failure
    "40003",  # statement_completion_unknown
    "40P01",  # deadlock_detected
    "55006",  # object_in_use
    "55P03",  # lock_not_available
})


def _is_transient(exc: psycopg.Error) -> bool:
    if exc.sqlstate is None:
        # No server diagnostic at all: the connection broke or was already gone.
        return isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError)
    return (
        exc.sqlstate[:2] in _TRANSIENT_SQLSTATE_CLASSES
        or exc.sqlstate in _TRANSIENT_SQLSTATES
    )


@dataclass(frozen=True)
class LoadResult:
    capture_id: int
    source_package_id: str
    acquisition_ordinal: int
    status: str  # 'published', 'loaded'/'loading' (retriable), or 'failed'
    resumed: bool
    member_count: int
    distinct_notice_count: int
    loaded_row_count: int
    reconciled: bool
    source_coverage_verified: bool
    batch_size: int | None
    failure_reason: str | None = None   # persisted: the capture is terminally failed
    publish_error: str | None = None    # publish did not commit; the capture is retriable
    load_error: str | None = None       # transient failure mid-load; the capture is retriable


def digest_archive(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def load_package(
    conn: psycopg.Connection,
    path: Path,
    source_package_id: str,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    limits: Limits | None = None,
    lock_wait: bool = False,
    force_recapture: bool = False,
    before_publish=None,
    on_capture=None,
) -> LoadResult:
    """``on_capture`` is passed to :func:`repository.begin_capture`: it commits a
    caller's record of the chosen capture together with the capture itself."""
    if batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    repo.require_transaction_owner(conn)

    path = Path(path)
    # The archive is validated against the ceilings its own package identity is
    # allowed to cost. A daily package therefore cannot be loaded through wider
    # limits than the ones that acquired it, and an identity this contract does
    # not recognize gets the narrowest policy rather than a permissive one.
    limits = limits or limits_from(policy_for(source_package_id))
    sha256, size = digest_archive(path)

    begin = repo.begin_capture(
        conn, source_package_id, sha256, size,
        batch_size=batch_size, contract_version=CONTRACT_VERSION,
        lock_wait=lock_wait, force_recapture=force_recapture,
        on_capture=on_capture,
    )
    try:
        result = _load_capture(
            conn,
            path,
            begin,
            sha256,
            limits,
            before_publish,
        )
    except BaseException:
        repo.release_lock_quietly(conn, source_package_id)
        raise
    repo.release_lock(conn, source_package_id)
    return result


def _load_capture(
    conn: psycopg.Connection,
    path: Path,
    begin: repo.BeginResult,
    sha256: str,
    limits: Limits,
    before_publish,
) -> LoadResult:
    """Run one capture while ``load_package`` owns its single lock acquisition."""
    capture = begin.capture

    if begin.already_complete:
        return _load_result(conn, capture.capture_id, resumed=True)

    # A capture that reconciled but never published only needs the publish step.
    if capture.status == "loaded":
        return _publish(conn, capture.capture_id, before_publish, resumed=True)

    if capture.batch_size is None:
        # Captures written before 0002 never recorded how the archive was split,
        # so a resume cannot line its ordinals up with the committed batches.
        # Refuse before writing rather than silently repartitioning.
        raise repo.CaptureError(
            f"capture {capture.capture_id} predates the recorded batch size and"
            " cannot be resumed safely; re-acquire the package with force_recapture"
        )
    effective_batch_size = capture.batch_size
    done = repo.committed_batches(conn, capture.capture_id) if begin.resumed else set()
    if begin.resumed:
        repo.clear_uncommitted_rows(conn, capture.capture_id)

    member_count = 0
    distinct_keys: set[tuple[int, int]] = set()
    change_reference_count = 0
    batch: list = []
    ordinal = 0
    try:
        for member in stream_notices(path, limits):
            member_count += 1
            projected = project_member(member)
            distinct_keys.add((projected.key.year, projected.key.number))
            change_reference_count += len(projected.change_references)
            batch.append(projected)
            if len(batch) >= effective_batch_size:
                if ordinal not in done:
                    repo.load_batch(conn, capture.capture_id, ordinal, batch)
                ordinal += 1
                batch = []
        if batch and ordinal not in done:
            repo.load_batch(conn, capture.capture_id, ordinal, batch)

        # The archive must be the same bytes we recorded; a mid-run swap or
        # truncation cannot be published as a complete capture.
        recheck, _ = digest_archive(path)
        if recheck != sha256:
            raise PackageError("archive changed during load")

        result = repo.reconcile(
            conn, capture.capture_id, member_count, len(distinct_keys), change_reference_count
        )
        if not result.ok:
            raise PackageError(
                f"reconciliation failed: members={member_count}"
                f" distinct={len(distinct_keys)} loaded={result.loaded_row_count}"
                f" references={change_reference_count}"
                f" persisted_references={result.persisted_reference_count}"
            )
    except (PackageError, psycopg.Error) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, psycopg.Error) and _is_transient(exc):
            try:
                return _load_result(
                    conn, capture.capture_id, begin.resumed, load_error=detail
                )
            except psycopg.Error:
                raise exc from None  # the connection is gone; report what broke
        try:
            repo.fail_capture(
                conn, capture.capture_id, detail, release_capture_lock=False
            )
        except (psycopg.Error, repo.CaptureError):
            # Marking the failure needs the same connection. If that write cannot
            # happen, the original error is what the operator has to see.
            raise exc from None
        return _load_result(conn, capture.capture_id, begin.resumed)

    return _publish(conn, capture.capture_id, before_publish, begin.resumed)


def _publish(conn, capture_id, before_publish, resumed):
    """Publish is a separate, recoverable step: a failure here keeps the capture
    in ``loaded`` (retriable) and does not touch the previously visible one."""
    try:
        repo.publish(
            conn,
            capture_id,
            before_commit=before_publish,
            release_capture_lock=False,
        )
    except Exception as exc:  # any publish failure keeps the capture retriable
        try:
            return _load_result(
                conn, capture_id, resumed, publish_error=f"{type(exc).__name__}: {exc}"
            )
        except psycopg.Error:
            raise exc from None  # the connection is gone; report what broke
    return _load_result(conn, capture_id, resumed)


def _load_result(
    conn: psycopg.Connection,
    capture_id: int,
    resumed: bool,
    *,
    publish_error: str | None = None,
    load_error: str | None = None,
) -> LoadResult:
    row = conn.execute(
        "select source_package_id, acquisition_ordinal, status, member_count,"
        " distinct_notice_count, loaded_row_count, failure_reason, coverage_verified,"
        " batch_size from tl_work.capture where capture_id = %s",
        (capture_id,),
    ).fetchone()
    package, ordinal, status, members, distinct, loaded, reason, coverage, batch_size = row
    return LoadResult(
        capture_id=capture_id,
        source_package_id=package,
        acquisition_ordinal=ordinal,
        status=status,
        resumed=resumed,
        member_count=members or 0,
        distinct_notice_count=distinct or 0,
        loaded_row_count=loaded or 0,
        reconciled=status in ("loaded", "published"),
        source_coverage_verified=coverage,
        batch_size=batch_size,
        failure_reason=reason,
        publish_error=publish_error,
        load_error=load_error,
    )
