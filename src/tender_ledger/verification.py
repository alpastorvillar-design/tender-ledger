"""Check one published capture against the TED Search API and persist the attempt.

The capture is the subject, not the database as a whole: a daily and a monthly
package overlap, so an identifier missing from the daily capture could be hidden
by the monthly one in ``tl_read.distinct_notice``. The comparison therefore runs
against the rows of the selected capture only.

Sequence, and why it is in this order:

1. Validate the capture and derive its query before locking or writing anything,
   so a bad request costs nothing.
2. Take the package's advisory lock -- the same key space the loader uses -- and
   validate again under it, so a replacement cannot publish underneath the run.
3. Open the attempt and clear ``coverage_verified`` in one committed transaction.
   From that moment the capture claims nothing, and an interruption leaves a
   visible ``in_progress`` row rather than a silent gap.
4. Enumerate the source with no SQL transaction open. HTTP never runs inside one.
5. Close the attempt and set ``coverage_verified`` in one committed transaction,
   then release the lock. The result is read back from the database, so a
   ``verified`` return means a committed ``verified`` row.

``coverage_verified`` is the standing of the most recent attempt for that
capture. A later failure lowers it to false and keeps the earlier evidence in
the history; it does not erase what an earlier attempt found.
"""

from dataclasses import dataclass

import psycopg

from .db import repository as repo
from .source_api import (
    KEY_DIGEST_RECIPE,
    VERIFIER_VERSION,
    Budgets,
    Clock,
    Enumeration,
    PackageQuery,
    SourceUnavailable,
    Transport,
    UrllibTransport,
    enumerate_publication_keys,
    keys_digest,
    package_query,
)

#: Differences are reported as counts plus a bounded, ordered illustration.
SAMPLE_LIMIT = 20

VERIFIED = "verified"
MISMATCH = "mismatch"
UNAVAILABLE = "unavailable"
EMPTY_UNCONFIRMED = "empty_unconfirmed"
IN_PROGRESS = "in_progress"


class VerificationError(RuntimeError):
    """The capture cannot be verified as requested."""


@dataclass(frozen=True)
class VerificationResult:
    """The persisted attempt, read back from the database."""

    attempt_id: int
    capture_id: int
    source_package_id: str
    state: str
    reason: str | None
    query: str
    scope: str
    announced_total: int | None
    api_record_count: int | None
    api_distinct_count: int | None
    api_duplicate_count: int | None
    local_distinct_count: int | None
    only_local_count: int | None
    only_api_count: int | None
    only_local_sample: list[str]
    only_api_sample: list[str]
    pages_fetched: int | None
    http_attempts: int | None
    api_keys_sha256: str | None
    coverage_verified: bool


@dataclass(frozen=True)
class _Capture:
    capture_id: int
    source_package_id: str
    artifact_sha256: str
    contract_version: str


@dataclass(frozen=True)
class _Outcome:
    state: str
    reason: str | None
    observed: Enumeration | None
    only_local: list[tuple[int, int]] | None = None
    only_api: list[tuple[int, int]] | None = None


def verify_capture(
    conn: psycopg.Connection,
    capture_id: int,
    *,
    transport: Transport | None = None,
    budgets: Budgets = Budgets(),
    clock: Clock | None = None,
    lock_wait: bool = False,
) -> VerificationResult:
    """Verify one published capture and return the attempt as it was persisted."""
    repo.require_transaction_owner(conn)
    capture = _verifiable_capture(conn, capture_id)
    query = package_query(capture.source_package_id)

    repo.acquire_lock(conn, capture.source_package_id, wait=lock_wait)
    try:
        # The capture may have been superseded between the first read and the
        # lock; re-reading under it is what makes the check meaningful.
        capture = _verifiable_capture(conn, capture_id)
        local_keys = _local_keys(conn, capture_id)
        attempt_id = _open_attempt(conn, capture, query, len(local_keys))
    except BaseException:
        repo.release_lock_quietly(conn, capture.source_package_id)
        raise

    try:
        outcome = _run(transport, query, budgets, clock, local_keys)
        _close_attempt(conn, attempt_id, capture_id, outcome)
        return _read_attempt(conn, attempt_id)
    finally:
        repo.release_lock_quietly(conn, capture.source_package_id)


def _run(
    transport: Transport | None,
    query: PackageQuery,
    budgets: Budgets,
    clock: Clock | None,
    local_keys: set[tuple[int, int]],
) -> _Outcome:
    try:
        enumeration = enumerate_publication_keys(
            transport or UrllibTransport(), query, budgets=budgets, clock=clock
        )
    except SourceUnavailable as exc:
        # Whatever the walk saw is evidence about the attempt. It is never a set
        # the source can be held to, so the differences stay unknown.
        return _Outcome(UNAVAILABLE, str(exc), exc.observed)
    return _compare(enumeration, local_keys)


def _compare(enumeration: Enumeration, local_keys: set[tuple[int, int]]) -> _Outcome:
    only_local = sorted(local_keys - enumeration.keys)
    only_api = sorted(enumeration.keys - local_keys)
    if only_local or only_api:
        reason = (
            f"{len(only_local)} identifiers only in the capture,"
            f" {len(only_api)} only in the source"
        )
        return _Outcome(MISMATCH, reason, enumeration, only_local, only_api)
    if enumeration.announced_total == 0:
        return _Outcome(
            EMPTY_UNCONFIRMED,
            "the source and the capture are both empty; zero alone does not"
            " establish that this issue was published",
            enumeration,
            only_local,
            only_api,
        )
    return _Outcome(VERIFIED, None, enumeration, only_local, only_api)


def _verifiable_capture(conn: psycopg.Connection, capture_id: int) -> _Capture:
    """Load a capture that may be verified, or explain why it may not."""
    row = conn.execute(
        "select c.source_package_id, c.artifact_sha256, c.contract_version, c.status,"
        " c.member_count, c.distinct_notice_count, c.loaded_row_count,"
        " (p.capture_id is not null) as is_published"
        " from tl_work.capture c"
        " left join tl_work.published_capture p on p.capture_id = c.capture_id"
        " where c.capture_id = %s",
        (capture_id,),
    ).fetchone()
    if row is None:
        raise VerificationError(f"capture {capture_id} does not exist")
    package, sha256, contract, status, members, distinct, loaded, is_published = row
    if status != "published" or not is_published:
        raise VerificationError(
            f"capture {capture_id} is {status!r} and is not the published capture of"
            f" {package!r}; only a published capture can be verified"
        )
    if None in (members, distinct, loaded) or not (members == distinct == loaded):
        raise VerificationError(
            f"capture {capture_id} has not reconciled: members={members}"
            f" distinct={distinct} loaded={loaded}"
        )
    return _Capture(capture_id, package, sha256, contract)


def _local_keys(conn: psycopg.Connection, capture_id: int) -> set[tuple[int, int]]:
    return {
        (year, number)
        for year, number in conn.execute(
            "select publication_year, publication_number from tl_work.notice_capture"
            " where capture_id = %s",
            (capture_id,),
        )
    }


def _open_attempt(
    conn: psycopg.Connection, capture: _Capture, query: PackageQuery, local_count: int
) -> int:
    with conn.transaction():
        attempt_id = conn.execute(
            "insert into tl_work.verification_attempt"
            " (capture_id, artifact_sha256, contract_version, verifier_version,"
            "  query_text, query_scope, state, local_distinct_count)"
            " values (%s, %s, %s, %s, %s, %s, 'in_progress', %s) returning attempt_id",
            (
                capture.capture_id,
                capture.artifact_sha256,
                capture.contract_version,
                VERIFIER_VERSION,
                query.expression,
                query.scope,
                local_count,
            ),
        ).fetchone()[0]
        conn.execute(
            "update tl_work.capture set coverage_verified = false where capture_id = %s",
            (capture.capture_id,),
        )
    return attempt_id


def _sample(keys: list[tuple[int, int]] | None) -> list[str] | None:
    if keys is None:
        return None
    return [f"{number}-{year}" for year, number in keys[:SAMPLE_LIMIT]]


def _close_attempt(
    conn: psycopg.Connection, attempt_id: int, capture_id: int, outcome: _Outcome
) -> None:
    """Record the terminal state and the capture's standing in one transaction."""
    observed = outcome.observed
    complete = observed is not None and observed.complete
    with conn.transaction():
        updated = conn.execute(
            "update tl_work.verification_attempt set"
            " state = %s, reason = %s, finished_at = now(),"
            " announced_total = %s, api_record_count = %s, api_distinct_count = %s,"
            " api_duplicate_count = %s, only_local_count = %s, only_api_count = %s,"
            " only_local_sample = %s, only_api_sample = %s,"
            " pages_fetched = %s, http_attempts = %s,"
            " api_keys_sha256 = %s, key_digest_recipe = %s"
            " where attempt_id = %s and state = 'in_progress'",
            (
                outcome.state,
                outcome.reason,
                None if observed is None else observed.announced_total,
                None if observed is None else observed.record_count,
                None if observed is None else len(observed.keys),
                None if observed is None else observed.duplicate_count,
                None if outcome.only_local is None else len(outcome.only_local),
                None if outcome.only_api is None else len(outcome.only_api),
                _sample(outcome.only_local),
                _sample(outcome.only_api),
                None if observed is None else observed.pages_fetched,
                None if observed is None else observed.http_attempts,
                keys_digest(observed.keys) if complete else None,
                KEY_DIGEST_RECIPE if complete else None,
                attempt_id,
            ),
        ).rowcount
        if updated != 1:
            raise VerificationError(
                f"verification attempt {attempt_id} was no longer in progress"
            )
        conn.execute(
            "update tl_work.capture set coverage_verified = %s where capture_id = %s",
            (outcome.state == VERIFIED, capture_id),
        )


def _read_attempt(conn: psycopg.Connection, attempt_id: int) -> VerificationResult:
    row = conn.execute(
        "select a.attempt_id, a.capture_id, c.source_package_id, a.state, a.reason,"
        " a.query_text, a.query_scope, a.announced_total, a.api_record_count,"
        " a.api_distinct_count, a.api_duplicate_count, a.local_distinct_count,"
        " a.only_local_count, a.only_api_count, a.only_local_sample, a.only_api_sample,"
        " a.pages_fetched, a.http_attempts, a.api_keys_sha256, c.coverage_verified"
        " from tl_work.verification_attempt a"
        " join tl_work.capture c on c.capture_id = a.capture_id"
        " where a.attempt_id = %s",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise VerificationError(f"verification attempt {attempt_id} was not persisted")
    values = list(row)
    values[14] = list(values[14] or ())
    values[15] = list(values[15] or ())
    return VerificationResult(*values)
