"""Capture lifecycle against PostgreSQL: begin, load batches, reconcile, publish.

Connection contract: these functions own their transaction boundaries, so they
need a connection that has none of its own -- **autocommit and idle**, which is
what ``db.connect`` returns. Each atomic unit is an explicit
``with conn.transaction()`` block, so it is a real ``BEGIN``/``COMMIT`` visible to
other connections the moment it returns; single reads run outside a transaction.
Nothing here commits or rolls back a transaction the caller opened.

Invariants:

* A capture's identity is persisted before any notice row is written, and a retry
  of the same artifact reuses it -- including from a durable ``loaded`` state or
  after a failed publish. An intentional re-acquisition (``force_recapture``) is a
  new capture even when the bytes are identical, so A -> B -> A keeps three.
* A retry resumes the *current* attempt for the package -- the most recently
  acquired capture -- never an older one a later capture has already overtaken.
* A batch commits its notice rows and its ``capture_batch`` record together.
* Readers keep seeing the previously published capture until ``publish`` swaps the
  pointer in a single transaction.
* Concurrent captures of one source package are serialized by a session advisory
  lock held until the publish transaction commits; a failed publish changes
  nothing.
"""

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import psycopg
from psycopg import pq

from ..projection import CONTRACT_VERSION, ProjectedNotice

_NOTICE_COLUMNS = (
    "capture_id", "publication_year", "publication_number", "batch_ordinal",
    "source_format", "schema_version", "source_filename", "notice_uuid",
    "notice_version", "publication_date", "publication_date_raw", "dispatch_date",
    "dispatch_date_raw", "buyer_country", "buyer_country_iso", "buyer_country_status",
    "primary_cpv", "primary_cpv_status", "additional_cpv",
)

_RESUMABLE_STATUSES = ("acquiring", "loading", "loaded")


class CaptureError(RuntimeError):
    """A capture cannot proceed as requested."""


class ConcurrentCaptureError(CaptureError):
    def __init__(self, source_package_id: str):
        super().__init__(f"another capture of {source_package_id!r} is in progress")
        self.source_package_id = source_package_id


class CaptureNotReady(CaptureError):
    """publish() was called on a capture that has not reconciled."""


class ConnectionStateError(CaptureError):
    """The connection cannot own the transaction boundaries this module needs."""


@dataclass(frozen=True)
class Capture:
    capture_id: int
    source_package_id: str
    acquisition_ordinal: int
    artifact_sha256: str
    contract_version: str
    status: str
    batch_size: int | None


@dataclass(frozen=True)
class BeginResult:
    capture: Capture
    resumed: bool
    already_complete: bool = False


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    loaded_row_count: int
    member_count: int
    distinct_notice_count: int


def require_transaction_owner(conn: psycopg.Connection) -> None:
    """Refuse a connection whose transaction boundaries are not ours to own.

    Autocommit alone is not enough: inside ``with conn.transaction()`` the flag
    stays True while every block below it degrades to a savepoint. Work would
    then be reported as published while another session still sees nothing, the
    advisory lock would be released before the real commit, and the caller's
    rollback would erase all of it. Reject before writing or locking anything,
    and never commit or roll back a transaction the caller opened.
    """
    if not conn.autocommit:
        raise ConnectionStateError(
            "the capture repository requires an autocommit connection"
            " (db.connect() returns one by default)"
        )
    status = conn.info.transaction_status
    if status != pq.TransactionStatus.IDLE:
        raise ConnectionStateError(
            f"the connection is already inside a transaction ({status.name});"
            " the caller must commit or roll it back first"
        )


def _lock_key_sql(placeholder: str) -> str:
    return f"hashtext({placeholder})::int8"


def _acquire_lock(conn: psycopg.Connection, source_package_id: str, wait: bool) -> bool:
    if wait:
        conn.execute("set lock_timeout = '30s'")
        conn.execute(
            f"select pg_advisory_lock({_lock_key_sql('%s')})", (source_package_id,)
        )
        return True
    got = conn.execute(
        f"select pg_try_advisory_lock({_lock_key_sql('%s')})", (source_package_id,)
    ).fetchone()[0]
    return bool(got)


def acquire_lock(
    conn: psycopg.Connection, source_package_id: str, *, wait: bool = False
) -> None:
    """Take the package's session advisory lock or refuse. Everything that
    replaces or inspects a package's published capture -- loading, re-acquiring,
    verifying -- shares this one key space, so they serialize against each
    other rather than only against their own kind."""
    if not _acquire_lock(conn, source_package_id, wait):
        raise ConcurrentCaptureError(source_package_id)


def release_lock(conn: psycopg.Connection, source_package_id: str) -> None:
    conn.execute(
        f"select pg_advisory_unlock({_lock_key_sql('%s')})", (source_package_id,)
    )


def release_lock_quietly(conn: psycopg.Connection, source_package_id: str) -> None:
    """Release the package lock while handling a failure. If the connection is
    the thing that broke, the lock died with the session anyway and the error
    being handled matters more than this bookkeeping."""
    with contextlib.suppress(psycopg.Error):
        release_lock(conn, source_package_id)


_CAPTURE_COLUMNS = (
    "capture_id, source_package_id, acquisition_ordinal, artifact_sha256,"
    " contract_version, status, batch_size"
)


def _load_capture(conn: psycopg.Connection, capture_id: int) -> Capture:
    row = conn.execute(
        f"select {_CAPTURE_COLUMNS} from tl_work.capture where capture_id = %s",
        (capture_id,),
    ).fetchone()
    if row is None:
        raise CaptureError(f"capture {capture_id} does not exist")
    return Capture(*row)


def begin_capture(
    conn: psycopg.Connection,
    source_package_id: str,
    artifact_sha256: str,
    artifact_bytes: int,
    *,
    batch_size: int,
    contract_version: str = CONTRACT_VERSION,
    lock_wait: bool = False,
    force_recapture: bool = False,
    on_capture: Callable[[psycopg.Connection, "Capture"], None] | None = None,
) -> BeginResult:
    """Persist (or resume) a capture identity for one source package.

    Without ``force_recapture``: when the most recently acquired capture of this
    package is still open (``acquiring``/``loading``/``loaded``) for the same
    artifact and contract it is resumed; otherwise a published capture of the
    same artifact is returned as a no-op. An older open attempt that a later
    capture has overtaken is left alone, and a superseded or failed capture is
    never reused -- re-acquiring it is a new capture. ``force_recapture`` always
    creates a new capture.

    ``on_capture`` runs inside the transaction that decides the capture, so an
    external record of "this run is loading that capture" is committed with the
    capture itself. Without it a caller could only write the link afterwards and
    a crash in between would leave a capture nothing points at, recoverable only
    by guessing. Raising from the hook aborts the whole decision.
    """
    require_transaction_owner(conn)
    if batch_size < 1:
        raise CaptureError("batch_size must be a positive integer")
    acquire_lock(conn, source_package_id, wait=lock_wait)

    with conn.transaction():
        result = _decide_capture(
            conn, source_package_id, artifact_sha256, artifact_bytes,
            batch_size=batch_size, contract_version=contract_version,
            force_recapture=force_recapture,
        )
        if on_capture is not None:
            on_capture(conn, result.capture)
    return result


def _decide_capture(
    conn: psycopg.Connection,
    source_package_id: str,
    artifact_sha256: str,
    artifact_bytes: int,
    *,
    batch_size: int,
    contract_version: str,
    force_recapture: bool,
) -> BeginResult:
    if not force_recapture:
        # The current attempt decides. Resuming it before looking for a replay is
        # what lets an intentional recapture of identical bytes finish after an
        # interrupted publish instead of replaying the capture it supersedes;
        # ordering by acquisition also keeps an attempt that a later capture has
        # already overtaken from being resurrected.
        latest = conn.execute(
            f"select {_CAPTURE_COLUMNS} from tl_work.capture"
            " where source_package_id = %s order by acquisition_ordinal desc limit 1",
            (source_package_id,),
        ).fetchone()
        if latest is not None:
            attempt = Capture(*latest)
            if (
                attempt.status in _RESUMABLE_STATUSES
                and attempt.artifact_sha256 == artifact_sha256
                and attempt.contract_version == contract_version
            ):
                return BeginResult(capture=attempt, resumed=True)

        current = conn.execute(
            "select c.capture_id from tl_work.capture c"
            " join tl_work.published_capture p on p.capture_id = c.capture_id"
            " where c.source_package_id = %s and c.artifact_sha256 = %s"
            "   and c.contract_version = %s and c.status = 'published'",
            (source_package_id, artifact_sha256, contract_version),
        ).fetchone()
        if current is not None:
            return BeginResult(
                capture=_load_capture(conn, current[0]), resumed=True, already_complete=True
            )

    capture_id = conn.execute(
        "insert into tl_work.capture"
        " (source_package_id, artifact_sha256, artifact_bytes, contract_version, batch_size)"
        " values (%s, %s, %s, %s, %s) returning capture_id",
        (source_package_id, artifact_sha256, artifact_bytes, contract_version, batch_size),
    ).fetchone()[0]
    return BeginResult(capture=_load_capture(conn, capture_id), resumed=False)


def committed_batches(conn: psycopg.Connection, capture_id: int) -> set[int]:
    return {
        r[0]
        for r in conn.execute(
            "select batch_ordinal from tl_work.capture_batch where capture_id = %s",
            (capture_id,),
        )
    }


def clear_uncommitted_rows(conn: psycopg.Connection, capture_id: int) -> int:
    """Drop notice rows whose batch never committed. With real per-batch
    transactions this should find nothing; it guards the resume invariant."""
    require_transaction_owner(conn)
    with conn.transaction():
        deleted = conn.execute(
            "delete from tl_work.notice_capture n where n.capture_id = %s"
            " and not exists (select 1 from tl_work.capture_batch b"
            "   where b.capture_id = n.capture_id and b.batch_ordinal = n.batch_ordinal)",
            (capture_id,),
        ).rowcount
    return deleted


def _row_tuple(capture_id: int, batch_ordinal: int, n: ProjectedNotice) -> tuple:
    return (
        capture_id,
        n.key.year,
        n.key.number,
        batch_ordinal,
        n.source_format,
        n.schema_version,
        n.source_filename,
        n.notice_uuid,
        n.notice_version,
        n.publication_date,
        n.publication_date_raw,
        n.dispatch_date,
        n.dispatch_date_raw,
        n.buyer_country,
        n.buyer_country_iso,
        n.buyer_country_status,
        n.primary_cpv,
        n.primary_cpv_status,
        list(n.additional_cpv),
    )


def load_batch(
    conn: psycopg.Connection,
    capture_id: int,
    batch_ordinal: int,
    notices: Sequence[ProjectedNotice],
) -> None:
    """Load one deterministic batch: COPY the rows and record the batch in one
    transaction. A duplicate canonical identity violates the notice primary key,
    the transaction rolls back, and the caller fails the capture."""
    require_transaction_owner(conn)
    columns = ", ".join(_NOTICE_COLUMNS)
    lo = notices[0].source_filename if notices else ""
    hi = notices[-1].source_filename if notices else ""
    with conn.transaction(), conn.cursor() as cur:
        with cur.copy(f"copy tl_work.notice_capture ({columns}) from stdin") as copy:
            for notice in notices:
                copy.write_row(_row_tuple(capture_id, batch_ordinal, notice))
        cur.execute(
            "insert into tl_work.capture_batch"
            " (capture_id, batch_ordinal, member_lo, member_hi, row_count)"
            " values (%s, %s, %s, %s, %s)",
            (capture_id, batch_ordinal, lo, hi, len(notices)),
        )
        cur.execute(
            "update tl_work.capture set status = 'loading'"
            " where capture_id = %s and status = 'acquiring'",
            (capture_id,),
        )


def reconcile(
    conn: psycopg.Connection,
    capture_id: int,
    member_count: int,
    distinct_notice_count: int,
) -> ReconcileResult:
    require_transaction_owner(conn)
    with conn.transaction():
        loaded = conn.execute(
            "select count(*) from tl_work.notice_capture where capture_id = %s",
            (capture_id,),
        ).fetchone()[0]
        ok = loaded == distinct_notice_count == member_count
        conn.execute(
            "update tl_work.capture"
            " set member_count = %s, distinct_notice_count = %s, loaded_row_count = %s,"
            "     status = case when %s and status in ('acquiring', 'loading')"
            "                   then 'loaded' else status end"
            " where capture_id = %s",
            (member_count, distinct_notice_count, loaded, ok, capture_id),
        )
    return ReconcileResult(ok, loaded, member_count, distinct_notice_count)


def publish(
    conn: psycopg.Connection,
    capture_id: int,
    *,
    before_commit: Callable[[psycopg.Connection], None] | None = None,
) -> None:
    """Make a reconciled capture the visible one for its source package.

    The pointer swap, the supersede of the prior capture, and the status change
    happen in one transaction. ``before_commit`` runs inside it: raising from it
    (a future coverage gate, or a simulated failure in tests) aborts the publish
    and leaves visibility and completion status untouched. The advisory lock is
    released only after the transaction commits.
    """
    require_transaction_owner(conn)
    with conn.transaction():
        row = conn.execute(
            "select source_package_id, status, member_count, distinct_notice_count,"
            " loaded_row_count from tl_work.capture where capture_id = %s for update",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise CaptureError(f"capture {capture_id} does not exist")
        package, status, members, distinct, loaded = row
        if status != "loaded":
            raise CaptureNotReady(f"capture {capture_id} is {status!r}, not 'loaded'")
        if not (loaded == distinct == members):
            raise CaptureNotReady(f"capture {capture_id} has not reconciled")

        prior = conn.execute(
            "select capture_id from tl_work.published_capture where source_package_id = %s",
            (package,),
        ).fetchone()
        if prior is not None and prior[0] != capture_id:
            conn.execute(
                "update tl_work.capture set status = 'superseded' where capture_id = %s",
                (prior[0],),
            )
        conn.execute(
            "insert into tl_work.published_capture (source_package_id, capture_id, published_at)"
            " values (%s, %s, now())"
            " on conflict (source_package_id)"
            " do update set capture_id = excluded.capture_id, published_at = now()",
            (package, capture_id),
        )
        conn.execute(
            "update tl_work.capture set status = 'published', published_at = now()"
            " where capture_id = %s",
            (capture_id,),
        )
        if before_commit is not None:
            before_commit(conn)
    release_lock(conn, package)


def fail_capture(conn: psycopg.Connection, capture_id: int, reason: str) -> None:
    require_transaction_owner(conn)
    with conn.transaction():
        row = conn.execute(
            "select source_package_id, status from tl_work.capture"
            " where capture_id = %s for update",
            (capture_id,),
        ).fetchone()
        if row is not None and row[1] != "published":
            conn.execute(
                "update tl_work.capture set status = 'failed', failure_reason = %s"
                " where capture_id = %s",
                (reason[:2000], capture_id),
            )
    if row is not None:
        release_lock(conn, row[0])
