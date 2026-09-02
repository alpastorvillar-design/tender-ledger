"""Load one TED package file into PostgreSQL as a recoverable, publishable capture.

Data flow, in one breath: hash the archive; persist a capture identity; stream
its XML members and project each into the allow-listed shape; write them in
fixed-size deterministic batches (COPY) whose rows and status commit together;
reconcile member count / distinct keys / loaded rows; verify the archive is
unchanged; then publish the capture with a single transactional pointer swap.
A crash at any point leaves the previous published capture visible and lets a
retry resume from the last committed batch.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .db import repository as repo
from .packages import Limits, PackageError, stream_notices
from .projection import CONTRACT_VERSION, project_member

_DEFAULT_BATCH_SIZE = 500

# Daily packages hold a few thousand notices; monthly packages hold far more.
# Large-scale uniqueness is the database primary key's job, so the stream's own
# in-memory guard can be generous here.
_LOADER_LIMITS = Limits(
    compressed_bytes=1024 * 1024 * 1024,
    expanded_bytes=8 * 1024 * 1024 * 1024,
    member_bytes=32 * 1024 * 1024,
    notices=2_000_000,
)


@dataclass(frozen=True)
class LoadResult:
    capture_id: int
    source_package_id: str
    acquisition_ordinal: int
    status: str  # 'published' or 'failed'
    resumed: bool
    member_count: int
    distinct_notice_count: int
    loaded_row_count: int
    reconciled: bool
    failure_reason: str | None = None


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
    before_publish=None,
) -> LoadResult:
    path = Path(path)
    limits = limits or _LOADER_LIMITS
    sha256, size = digest_archive(path)

    begin = repo.begin_capture(
        conn, source_package_id, sha256, size,
        contract_version=CONTRACT_VERSION, lock_wait=lock_wait,
    )
    capture = begin.capture
    if begin.already_complete:
        repo.release_lock(conn, source_package_id)
        return _load_result(conn, capture.capture_id, resumed=True)
    done = repo.committed_batches(conn, capture.capture_id) if begin.resumed else set()
    if begin.resumed:
        repo.clear_uncommitted_rows(conn, capture.capture_id)

    member_count = 0
    distinct_keys: set[tuple[int, int]] = set()
    batch: list = []
    ordinal = 0
    try:
        for member in stream_notices(path, limits):
            member_count += 1
            projected = project_member(member)
            distinct_keys.add((projected.key.year, projected.key.number))
            batch.append(projected)
            if len(batch) >= batch_size:
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
            conn, capture.capture_id, member_count, len(distinct_keys)
        )
        if not result.ok:
            raise PackageError(
                f"reconciliation failed: members={member_count}"
                f" distinct={len(distinct_keys)} loaded={result.loaded_row_count}"
            )
        repo.publish(conn, capture.capture_id, before_commit=before_publish)
    except (PackageError, psycopg.Error) as exc:
        repo.fail_capture(conn, capture.capture_id, f"{type(exc).__name__}: {exc}")
        final = _load_result(conn, capture.capture_id, begin.resumed)
        return final

    return _load_result(conn, capture.capture_id, begin.resumed)


def _load_result(conn: psycopg.Connection, capture_id: int, resumed: bool) -> LoadResult:
    row = conn.execute(
        "select source_package_id, acquisition_ordinal, status, member_count,"
        " distinct_notice_count, loaded_row_count, failure_reason"
        " from tl_work.capture where capture_id = %s",
        (capture_id,),
    ).fetchone()
    package, ordinal, status, members, distinct, loaded, reason = row
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
        failure_reason=reason,
    )
