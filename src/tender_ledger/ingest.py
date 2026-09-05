"""One package, end to end: acquire, load, verify, checkpoint.

``load`` publishes a local capture. ``verify`` decides whether the source agrees
that capture is the whole issue. Neither of them means a workflow may treat the
package as processed, and this is what may: a run that got an artifact it
validated, a capture it published from those exact bytes, and a verification
that confirmed their coverage, sealed as one checkpoint.

The filesystem and PostgreSQL have no shared transaction, so the order of
operations is the design:

    persist a run  ->  download to an exclusive temporary file
                   ->  validate it, fsync, rename into place
                   ->  commit the artifact reference
                   ->  load (the capture link commits with the capture)
                   ->  verify (no SQL transaction is open while HTTP runs)
                   ->  seal the checkpoint in one short transaction
                   ->  read it back before reporting success

Each arrow is a place a crash can land, and each one is recoverable from what is
already committed plus the file on disk. A run never trusts a database row about
an artifact without re-reading the bytes, and never reports success from a row it
has not read back after the commit.

Everything for one package -- including the download -- runs under the package's
session advisory lock, the same key space the loader and the verifier use. Those
two are called while it is held: PostgreSQL counts advisory locks per session, so
their own balanced acquire/release nests inside this one and a concurrent load,
re-acquisition or verification of the same package is still refused. No SQL
transaction is held across HTTP.
"""

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .config import default_data_root
from .db import repository as repo
from .download import (
    Artifact,
    DownloadBudgets,
    DownloadError,
    artifact_destination,
    download_package,
    package_url,
    validate_artifact,
)
from .loader import load_package
from .package_contract import policy_for
from .packages import Limits, limits_from
from .projection import CONTRACT_VERSION
from .source_api import Budgets, Clock, Transport
from .source_api import budgets_from as api_budgets_from
from .verification import VERIFIED, verify_capture

DEFAULT_BATCH_SIZE = 500

#: How far a run has got, in order. 'failed' is outside it: terminal, not a step.
_PHASE_ORDER = (
    "starting", "artifact_ready", "capture_published", "source_verified", "completed"
)
_OPEN_PHASES = _PHASE_ORDER[:-1]
_RUN_COLUMNS = (
    "run_id, source_package_id, attempt_ordinal, phase, artifact_path,"
    " artifact_sha256, artifact_bytes, capture_id, verification_attempt_id"
)


class IngestError(RuntimeError):
    """The package cannot be ingested as requested."""


@dataclass(frozen=True)
class IngestResult:
    """What the run achieved, as the database records it."""

    run_id: int
    source_package_id: str
    phase: str
    #: 'processed' (this run sealed the checkpoint), 'replayed' (a current
    #: checkpoint and its artifact were re-validated), or 'incomplete'.
    outcome: str
    resumed: bool
    #: How the bytes were obtained: 'downloaded', 'adopted' (found at the
    #: destination after an interrupted run), 'reused' (already referenced by
    #: this run), 'replayed', or 'none'.
    artifact_action: str
    artifact_path: str | None
    artifact_sha256: str | None
    artifact_bytes: int | None
    notice_count: int | None
    capture_id: int | None
    verification_attempt_id: int | None
    checkpoint_is_current: bool
    http_attempts: int
    downloaded_bytes: int
    error: str | None = None


@dataclass(frozen=True)
class _Run:
    run_id: int
    source_package_id: str
    attempt_ordinal: int
    phase: str
    artifact_path: str | None
    artifact_sha256: str | None
    artifact_bytes: int | None
    capture_id: int | None
    verification_attempt_id: int | None


@dataclass
class _Progress:
    """What this invocation did, for the result. Never a source of truth."""

    artifact_action: str = "none"
    notice_count: int | None = None
    http_attempts: int = 0
    downloaded_bytes: int = 0


class _Incomplete(Exception):
    """The flow stopped somewhere consistent. ``terminal`` closes the run."""

    def __init__(self, message: str, *, terminal: bool = False):
        super().__init__(message)
        self.terminal = terminal


def ingest_package(
    conn: psycopg.Connection,
    source_package_id: str,
    *,
    data_root: Path | None = None,
    url: str | None = None,
    budgets: DownloadBudgets | None = None,
    clock: Clock | None = None,
    limits: Limits | None = None,
    transport: Transport | None = None,
    api_budgets: Budgets | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lock_wait: bool = False,
) -> IngestResult:
    """Process one package and return what the database says about it.

    ``url`` overrides the derived package URL (tests point it at a local server);
    ``transport`` is the Search API client the verification step uses.
    """
    repo.require_transaction_owner(conn)
    package_url(source_package_id)  # an unsupported identity stops here, before any work
    root = Path(data_root) if data_root is not None else default_data_root()
    destination = artifact_destination(root, source_package_id)
    # One policy for the whole flow: the archive limits, the acquisition budgets
    # and the enumeration budgets all come from this package's identity, so no
    # step can accept work another step would refuse.
    policy = policy_for(source_package_id)
    limits = limits or limits_from(policy)

    repo.acquire_lock(conn, source_package_id, wait=lock_wait)
    try:
        replayed = _replay(conn, source_package_id, destination, limits)
        if replayed is not None:
            return replayed
        run, resumed = _open_run(conn, source_package_id)
        return _advance(
            conn, run, resumed, destination, root,
            url=url, budgets=budgets, clock=clock, limits=limits,
            transport=transport, api_budgets=api_budgets or api_budgets_from(policy),
            batch_size=batch_size,
        )
    finally:
        # Releases this function's own acquisition. The loader and the verifier
        # balance theirs internally, so the package is free again after success,
        # after a failure, and after a cancellation.
        repo.release_lock_quietly(conn, source_package_id)


def _advance(
    conn: psycopg.Connection,
    run: _Run,
    resumed: bool,
    destination: Path,
    root: Path,
    *,
    url: str | None,
    budgets: DownloadBudgets | None,
    clock: Clock | None,
    limits: Limits,
    transport: Transport | None,
    api_budgets: Budgets,
    batch_size: int,
) -> IngestResult:
    """Take the run as far as it can go, one committed phase at a time."""
    progress = _Progress()
    try:
        run, artifact = _ensure_artifact(
            conn, run, destination, root, progress,
            url=url, budgets=budgets, clock=clock, limits=limits,
        )
        run = _load(conn, run, artifact, batch_size=batch_size, limits=limits)
        run = _ensure_coverage(
            conn, run, transport=transport, budgets=api_budgets, clock=clock
        )
        return _seal(conn, run, resumed, progress)
    except _Incomplete as exc:
        run = _record_error(conn, run, str(exc), terminal=exc.terminal)
        return _result(conn, run, resumed, progress, outcome="incomplete", error=str(exc))


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


def _replay(
    conn: psycopg.Connection, source_package_id: str, destination: Path, limits: Limits
) -> IngestResult | None:
    """Certify a current checkpoint without touching the network, or decline.

    Declining is how recovery starts: a checkpoint that no longer describes the
    package, or whose artifact is gone or no longer hashes to what was recorded,
    falls through to a fresh run. A row on its own is never enough to report a
    package processed.

    A checkpoint sealed under an older projection contract is declined the same
    way, even when it is otherwise internally consistent (its capture, artifact
    checksum and attempt all still agree with each other): the database has no
    way to know today's ``CONTRACT_VERSION``, so this comparison is made here,
    once, against the constant this running code actually uses -- not
    duplicated as a literal in SQL, where it could drift out of sync with
    Python. Declining falls through to a fresh run, which acquires a new
    capture under the current contract and reprojects it; it does not resume
    or overwrite the old, still-valid v1 evidence.
    """
    status = _status(conn, source_package_id)
    if status is None or not status["checkpoint_is_current"]:
        return None
    if (
        status["checkpoint_contract_version"] != CONTRACT_VERSION
        or status["published_contract_version"] != CONTRACT_VERSION
    ):
        return None
    artifact = validate_artifact(
        destination, expected_sha256=status["checkpoint_artifact_sha256"], limits=limits
    )
    if artifact is None:
        return None
    run = _read_run(conn, status["checkpoint_run_id"])
    return IngestResult(
        run_id=run.run_id,
        source_package_id=source_package_id,
        phase=run.phase,
        outcome="replayed",
        resumed=True,
        artifact_action="replayed",
        artifact_path=run.artifact_path,
        artifact_sha256=artifact.sha256,
        artifact_bytes=artifact.size_bytes,
        notice_count=status["checkpoint_notice_count"],
        capture_id=status["checkpoint_capture_id"],
        verification_attempt_id=status["checkpoint_attempt_id"],
        checkpoint_is_current=True,
        http_attempts=0,
        downloaded_bytes=0,
    )


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


def _ensure_artifact(
    conn: psycopg.Connection,
    run: _Run,
    destination: Path,
    root: Path,
    progress: _Progress,
    *,
    url: str | None,
    budgets: DownloadBudgets | None,
    clock: Clock | None,
    limits: Limits,
) -> tuple[_Run, Artifact]:
    """Leave a validated package file at ``destination`` and referenced by the run.

    Three ways in, in order of cost: the run already references bytes that still
    hash correctly; a file is sitting at the destination from a run that renamed
    it but crashed before committing the reference; or a fresh download. The
    first two re-read and re-validate the file -- a filename and a database row
    say nothing about content.
    """
    if run.artifact_sha256 is not None:
        artifact = validate_artifact(
            root / run.artifact_path, expected_sha256=run.artifact_sha256, limits=limits
        )
        if artifact is not None:
            progress.artifact_action = "reused"
            progress.notice_count = artifact.notice_count
            return run, artifact

    adopted = validate_artifact(destination, limits=limits)
    if adopted is not None:
        progress.artifact_action = "adopted"
        progress.notice_count = adopted.notice_count
        return _record_artifact(conn, run, adopted, root), adopted

    try:
        artifact = download_package(
            run.source_package_id, destination,
            url=url, budgets=budgets, clock=clock, limits=limits,
        )
    except DownloadError as exc:
        # A failed acquisition still cost requests and bytes. Reporting them is
        # what keeps a body that transferred and was then refused distinguishable
        # from one that never reached the network.
        progress.http_attempts = exc.http_attempts
        progress.downloaded_bytes = exc.downloaded_bytes
        raise _Incomplete(f"{type(exc).__name__}: {exc}") from exc
    progress.artifact_action = "downloaded"
    progress.notice_count = artifact.notice_count
    progress.http_attempts = artifact.http_attempts
    progress.downloaded_bytes = artifact.downloaded_bytes
    return _record_artifact(conn, run, artifact, root), artifact


def _record_artifact(
    conn: psycopg.Connection, run: _Run, artifact: Artifact, root: Path
) -> _Run:
    """Commit the artifact reference. Until this returns, the file is the only
    evidence, which is why the next run re-validates the destination."""
    return _set_phase(
        conn, run, "artifact_ready",
        artifact_path=artifact.path.relative_to(root).as_posix(),
        artifact_sha256=artifact.sha256,
        artifact_bytes=artifact.size_bytes,
    )


def _load(
    conn: psycopg.Connection,
    run: _Run,
    artifact: Artifact,
    *,
    batch_size: int,
    limits: Limits,
) -> _Run:
    """Publish a capture of these bytes, reusing the loader's own recovery.

    The capture link is written by ``on_capture`` inside the transaction that
    creates or resumes the capture, so an interruption before the first batch
    still leaves the run pointing at the right capture.

    A resumed run can find a *different* capture here: while it waited, the
    package may have been re-acquired and its own capture superseded. The link
    therefore also drops a verification attempt that belonged to the capture it
    is leaving. Keeping it would claim confirmed coverage for a capture nobody
    checked -- and the composite foreign key would refuse the row anyway.
    """
    def link(inner: psycopg.Connection, capture: repo.Capture) -> None:
        inner.execute(
            "update tl_work.ingest_run set capture_id = %s,"
            " verification_attempt_id = case when capture_id is distinct from %s"
            "     then null else verification_attempt_id end,"
            " phase = case when capture_id is distinct from %s and phase = 'source_verified'"
            "     then 'capture_published' else phase end,"
            " updated_at = now()"
            " where run_id = %s",
            (capture.capture_id,) * 3 + (run.run_id,),
        )

    result = load_package(
        conn, artifact.path, run.source_package_id,
        batch_size=batch_size, limits=limits, on_capture=link,
    )
    if result.status != "published":
        reason = result.failure_reason or result.load_error or result.publish_error
        raise _Incomplete(
            f"capture {result.capture_id} is {result.status!r}, not published: {reason}",
            terminal=result.status == "failed",
        )
    # Re-read: the link may have changed the capture and cleared the attempt.
    # A run that had already verified this same capture keeps that phase rather
    # than stepping backwards over work it does not have to redo.
    run = _read_run(conn, run.run_id)
    if run.capture_id != result.capture_id or _rank(run.phase) < _rank("capture_published"):
        run = _set_phase(conn, run, "capture_published", capture_id=result.capture_id)
    return run


def _ensure_coverage(
    conn: psycopg.Connection,
    run: _Run,
    *,
    transport: Transport | None,
    budgets: Budgets,
    clock: Clock | None,
) -> _Run:
    """Make the capture's coverage confirmed, checking the database first.

    A confirmed, still-current verification is reused rather than repeated: a run
    that crashed between verifying and sealing must not have to ask the source
    again. That is a statement about stored evidence, not about the source being
    unchanged since; freshness is an orchestration decision.
    """
    attempt_id = _confirmed_attempt(conn, run.capture_id)
    if attempt_id is None:
        result = verify_capture(
            conn, run.capture_id, transport=transport, budgets=budgets, clock=clock
        )
        if not (result.state == VERIFIED and result.coverage_verified):
            raise _Incomplete(
                f"capture {result.capture_id} coverage is {result.state!r}: {result.reason}"
            )
        attempt_id = _confirmed_attempt(conn, run.capture_id)
        if attempt_id != result.attempt_id:
            raise _Incomplete(
                f"verification attempt {result.attempt_id} is no longer the capture's"
                " confirmed coverage"
            )
    return _set_phase(conn, run, "source_verified", verification_attempt_id=attempt_id)


def _confirmed_attempt(conn: psycopg.Connection, capture_id: int) -> int | None:
    """The capture's latest attempt, if it is a verified one it still stands on."""
    row = conn.execute(
        "select v.attempt_id from tl_work.verification_attempt v"
        " join tl_work.capture c on c.capture_id = v.capture_id"
        " where v.capture_id = %s and (v.state = 'verified') is true"
        "   and (c.coverage_verified) is true"
        "   and v.attempt_id = (select max(a.attempt_id)"
        "                       from tl_work.verification_attempt a"
        "                       where a.capture_id = v.capture_id)",
        (capture_id,),
    ).fetchone()
    return None if row is None else row[0]


def _seal(
    conn: psycopg.Connection, run: _Run, resumed: bool, progress: _Progress
) -> IngestResult:
    """Write the checkpoint and close the run in one short transaction.

    Every condition is re-checked here, in SQL, under the package lock: the
    capture is still the published one, it reconciled, its coverage stands, its
    latest attempt is the one being cited, and the artifact and contract still
    match. ``is true`` throughout, so an unknown value satisfies nothing. If the
    statement writes no row, none of that held and no checkpoint exists.
    """
    with conn.transaction():
        sealed = conn.execute(
            "insert into tl_work.package_checkpoint (source_package_id, run_id,"
            " capture_id, verification_attempt_id, artifact_sha256, contract_version,"
            " notice_count)"
            " select c.source_package_id, %s, c.capture_id, v.attempt_id,"
            "        c.artifact_sha256, c.contract_version, c.distinct_notice_count"
            " from tl_work.capture c"
            " join tl_work.published_capture p"
            "   on p.source_package_id = c.source_package_id and p.capture_id = c.capture_id"
            " join tl_work.verification_attempt v on v.capture_id = c.capture_id"
            " where c.capture_id = %s and v.attempt_id = %s"
            "   and (c.status = 'published') is true"
            "   and (c.coverage_verified) is true"
            "   and (v.state = 'verified') is true"
            "   and (v.artifact_sha256 = c.artifact_sha256) is true"
            "   and (v.contract_version = c.contract_version) is true"
            "   and (c.member_count = c.distinct_notice_count) is true"
            "   and (c.distinct_notice_count = c.loaded_row_count) is true"
            "   and v.attempt_id = (select max(a.attempt_id)"
            "                       from tl_work.verification_attempt a"
            "                       where a.capture_id = c.capture_id)"
            " on conflict (source_package_id) do update set"
            "   run_id = excluded.run_id, capture_id = excluded.capture_id,"
            "   verification_attempt_id = excluded.verification_attempt_id,"
            "   artifact_sha256 = excluded.artifact_sha256,"
            "   contract_version = excluded.contract_version,"
            "   notice_count = excluded.notice_count, sealed_at = now()",
            (run.run_id, run.capture_id, run.verification_attempt_id),
        ).rowcount
        if sealed != 1:
            raise _Incomplete(
                f"capture {run.capture_id} no longer satisfies the checkpoint"
                " conditions; nothing was sealed"
            )
        closed = conn.execute(
            "update tl_work.ingest_run set phase = 'completed', completed_at = now(),"
            " updated_at = now(), last_error = null"
            " where run_id = %s and phase = 'source_verified'",
            (run.run_id,),
        ).rowcount
        if closed != 1:
            raise _Incomplete(f"ingest run {run.run_id} was no longer in progress")

    # Read the committed row back before calling the package processed.
    run = _read_run(conn, run.run_id)
    result = _result(conn, run, resumed, progress, outcome="processed")
    if not (result.checkpoint_is_current and result.phase == "completed"):
        raise IngestError(
            f"the checkpoint for {run.source_package_id!r} did not read back as current"
        )
    return result


# --------------------------------------------------------------------------- #
# Run state
# --------------------------------------------------------------------------- #


def _open_run(conn: psycopg.Connection, source_package_id: str) -> tuple[_Run, bool]:
    """Resume this package's unfinished run, or start the next one.

    A completed run is never reopened. When its checkpoint has been retired by a
    re-acquisition or a failed check, the recovery is a new run against the
    capture that is published *now* -- reviving the old one would put a
    superseded capture back in front of the one that replaced it.
    """
    placeholders = ", ".join(["%s"] * len(_OPEN_PHASES))
    row = conn.execute(
        f"select {_RUN_COLUMNS} from tl_work.ingest_run"
        f" where source_package_id = %s and phase in ({placeholders})"
        " order by run_id desc limit 1",
        (source_package_id, *_OPEN_PHASES),
    ).fetchone()
    if row is not None:
        return _Run(*row), True
    with conn.transaction():
        run_id = conn.execute(
            "insert into tl_work.ingest_run (source_package_id, attempt_ordinal)"
            " select %s, coalesce(max(attempt_ordinal), 0) + 1 from tl_work.ingest_run"
            " where source_package_id = %s returning run_id",
            (source_package_id, source_package_id),
        ).fetchone()[0]
    return _read_run(conn, run_id), False


def _rank(phase: str) -> int:
    return _PHASE_ORDER.index(phase)


def _read_run(conn: psycopg.Connection, run_id: int) -> _Run:
    row = conn.execute(
        f"select {_RUN_COLUMNS} from tl_work.ingest_run where run_id = %s", (run_id,)
    ).fetchone()
    if row is None:
        raise IngestError(f"ingest run {run_id} does not exist")
    return _Run(*row)


def _set_phase(conn: psycopg.Connection, run: _Run, phase: str, **columns) -> _Run:
    assignments = "".join(f", {name} = %s" for name in columns)
    with conn.transaction():
        conn.execute(
            f"update tl_work.ingest_run set phase = %s, updated_at = now(){assignments}"
            " where run_id = %s",
            (phase, *columns.values(), run.run_id),
        )
    return dataclasses.replace(run, phase=phase, **columns)


def _record_error(
    conn: psycopg.Connection, run: _Run, message: str, *, terminal: bool
) -> _Run:
    """Keep the bounded reason. A terminal failure also closes the run, so the
    next invocation starts a fresh one instead of resuming condemned work."""
    phase = "failed" if terminal else run.phase
    with conn.transaction():
        conn.execute(
            "update tl_work.ingest_run set phase = %s, last_error = %s, updated_at = now()"
            " where run_id = %s",
            (phase, message[:2000], run.run_id),
        )
    return dataclasses.replace(run, phase=phase)


def _status(conn: psycopg.Connection, source_package_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "select * from tl_read.package_ingest_status where source_package_id = %s",
            (source_package_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d.name for d in cur.description], row, strict=True))


def _result(
    conn: psycopg.Connection,
    run: _Run,
    resumed: bool,
    progress: _Progress,
    *,
    outcome: str,
    error: str | None = None,
) -> IngestResult:
    status = _status(conn, run.source_package_id) or {}
    current = bool(status.get("checkpoint_is_current")) and (
        status.get("checkpoint_run_id") == run.run_id
    )
    return IngestResult(
        run_id=run.run_id,
        source_package_id=run.source_package_id,
        phase=run.phase,
        outcome=outcome,
        resumed=resumed,
        artifact_action=progress.artifact_action,
        artifact_path=run.artifact_path,
        artifact_sha256=run.artifact_sha256,
        artifact_bytes=run.artifact_bytes,
        notice_count=progress.notice_count,
        capture_id=run.capture_id,
        verification_attempt_id=run.verification_attempt_id,
        checkpoint_is_current=current,
        http_attempts=progress.http_attempts,
        downloaded_bytes=progress.downloaded_bytes,
        error=error,
    )
