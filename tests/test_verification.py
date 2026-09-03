"""Coverage verification against a real PostgreSQL, with a scripted source.

The API is never contacted: every case drives the verifier through a controlled
transport. What needs a real database is everything else -- the attempt history,
the coverage flag, the advisory lock shared with the loader, transaction
boundaries, and the reader role's access.
"""

import tempfile
import unittest
from pathlib import Path

import psycopg

from ted_fixtures import (
    DEFAULT_OJS,
    TEST_DB,
    FakeTransport,
    api_page,
    ensure_test_database,
    legacy_member,
    truncate_all,
    write_package,
)
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.db import repository as repo
from tender_ledger.loader import digest_archive, load_package
from tender_ledger.source_api import (
    Budgets,
    Clock,
    SourceUnavailable,
    TransientSourceError,
    UnsupportedPackage,
    keys_digest,
)
from tender_ledger.verification import VerificationError, verify_capture

PACKAGE = "daily/202300220"
OTHER_PACKAGE = "daily/202300221"
OTHER_OJS = "221/2023"


def setUpModule():
    ensure_test_database()


def source(numbers, *, ojs=DEFAULT_OJS, total=None):
    """Pages covering ``numbers`` in one page, then the terminal empty page TED
    sends -- which still carries a token."""
    total = len(numbers) if total is None else total
    pages = [api_page(numbers, total=total, ojs=ojs, token="page-2")]
    if numbers:
        pages.append(api_page([], total=total, ojs=ojs, token="still-here"))
    return pages


class VerificationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.conn = self.new_conn()
        truncate_all(self.conn)

    def new_conn(self, **kw) -> psycopg.Connection:
        conn = db.connect(load_config(dbname=TEST_DB), **kw)
        self.addCleanup(conn.close)
        return conn

    def publish(self, package_id, numbers, *, conn=None):
        path = write_package(
            self.dir / package_id.replace("/", "_"), [legacy_member(n) for n in numbers]
        )
        result = load_package(conn or self.new_conn(), path, package_id)
        self.assertEqual(result.status, "published")
        return result.capture_id

    def verify(self, capture_id, script, *, conn=None, before_request=None, **kwargs):
        transport = FakeTransport(script, before_request=before_request)
        self.transport = transport
        return verify_capture(
            conn or self.new_conn(), capture_id, transport=transport, **kwargs
        )

    def attempts(self, capture_id=None):
        sql = (
            "select attempt_id, state, reason, announced_total, api_record_count,"
            " api_distinct_count, api_duplicate_count, local_distinct_count,"
            " only_local_count, only_api_count, only_local_sample, only_api_sample,"
            " pages_fetched, http_attempts, api_keys_sha256, finished_at"
            " from tl_work.verification_attempt"
        )
        args: tuple = ()
        if capture_id is not None:
            sql += " where capture_id = %s"
            args = (capture_id,)
        sql += " order by attempt_id"
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            names = [d.name for d in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def coverage(self, capture_id):
        return self.conn.execute(
            "select coverage_verified from tl_work.capture where capture_id = %s",
            (capture_id,),
        ).fetchone()[0]

    def lock_is_free(self, package=PACKAGE):
        got = self.conn.execute(
            "select pg_try_advisory_lock(hashtext(%s)::int8)", (package,)
        ).fetchone()[0]
        if got:
            self.conn.execute("select pg_advisory_unlock(hashtext(%s)::int8)", (package,))
        return got


class VerifiedTests(VerificationTestCase):
    def test_an_exact_match_verifies_and_records_its_evidence(self):
        capture = self.publish(PACKAGE, [1, 2, 3])
        result = self.verify(capture, source([1, 2, 3]))

        self.assertEqual(result.state, "verified")
        self.assertIsNone(result.reason)
        self.assertTrue(result.coverage_verified)
        self.assertEqual(result.query, "OJ = 220/2023")
        self.assertEqual(result.scope, "ALL")
        self.assertEqual(
            (result.announced_total, result.api_record_count, result.api_distinct_count),
            (3, 3, 3),
        )
        self.assertEqual((result.only_local_count, result.only_api_count), (0, 0))
        self.assertEqual((result.only_local_sample, result.only_api_sample), ([], []))
        self.assertEqual((result.pages_fetched, result.http_attempts), (2, 2))
        self.assertEqual(
            result.api_keys_sha256, keys_digest({(2023, 1), (2023, 2), (2023, 3)})
        )
        self.assertTrue(self.coverage(capture))

    def test_padding_and_order_do_not_affect_identity(self):
        capture = self.publish(PACKAGE, [7, 42])
        pages = [
            api_page(["0000042", "007"], total=2, token="page-2"),
            api_page([], total=2, token="done"),
        ]
        self.assertEqual(self.verify(capture, pages).state, "verified")

    def test_the_views_and_the_reader_role_report_the_verified_state(self):
        capture = self.publish(PACKAGE, [1])
        self.verify(capture, source([1]))

        reader = self.new_conn()
        reader.execute("set role tender_ledger_reader")
        row = reader.execute(
            "select source_coverage_verified, verification_state, verification_attempt_id,"
            " verification_finished_at is not null"
            " from tl_read.capture_status where capture_id = %s",
            (capture,),
        ).fetchone()
        self.assertEqual(row[:2], (True, "verified"))
        self.assertIsNotNone(row[2])
        self.assertTrue(row[3])
        self.assertEqual(
            reader.execute(
                "select state, source_package_id from tl_read.verification_attempt"
            ).fetchall(),
            [("verified", PACKAGE)],
        )
        self.assertTrue(
            reader.execute(
                "select source_coverage_verified from tl_read.notice limit 1"
            ).fetchone()[0]
        )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            reader.execute("select * from tl_work.verification_attempt")


class MismatchTests(VerificationTestCase):
    def test_equal_counts_with_one_missing_and_one_extra_is_a_mismatch(self):
        capture = self.publish(PACKAGE, [1, 2, 3])
        result = self.verify(capture, source([1, 2, 4]))

        self.assertEqual(result.state, "mismatch")
        self.assertEqual((result.only_local_count, result.only_api_count), (1, 1))
        self.assertEqual(result.only_local_sample, ["3-2023"])
        self.assertEqual(result.only_api_sample, ["4-2023"])
        self.assertEqual(result.announced_total, 3)
        self.assertFalse(result.coverage_verified)
        # The walk finished, so the source key set is known and worth recording.
        self.assertIsNotNone(result.api_keys_sha256)

    def test_difference_samples_are_bounded_but_the_counts_are_not(self):
        local = list(range(100, 130))
        capture = self.publish(PACKAGE, local)
        result = self.verify(capture, source(list(range(200, 230))))
        self.assertEqual((result.only_local_count, result.only_api_count), (30, 30))
        self.assertEqual(len(result.only_local_sample), 20)
        self.assertEqual(result.only_local_sample[0], "100-2023")

    def test_an_overlapping_package_cannot_cover_for_the_capture_being_verified(self):
        capture = self.publish(PACKAGE, [1, 2])
        self.publish(OTHER_PACKAGE, [1, 2, 3])
        # 3-2023 is published by the other package, so the global distinct view
        # holds it. The verified capture does not, and that is the mismatch.
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_read.distinct_notice"
            ).fetchone()[0],
            3,
        )
        result = self.verify(capture, source([1, 2, 3]))
        self.assertEqual(result.state, "mismatch")
        self.assertEqual((result.only_local_count, result.only_api_count), (0, 1))
        self.assertEqual(result.only_api_sample, ["3-2023"])

    def test_an_empty_source_against_a_loaded_capture_is_a_mismatch(self):
        capture = self.publish(PACKAGE, [1])
        result = self.verify(capture, source([], total=0))
        self.assertEqual(result.state, "mismatch")
        self.assertEqual((result.only_local_count, result.only_api_count), (1, 0))


class EmptyPeriodTests(VerificationTestCase):
    def test_two_empty_sides_stay_unconfirmed_rather_than_verified(self):
        capture = self.publish(PACKAGE, [])
        result = self.verify(capture, source([], total=0))
        self.assertEqual(result.state, "empty_unconfirmed")
        self.assertIn("zero alone does not", result.reason)
        self.assertEqual((result.announced_total, result.local_distinct_count), (0, 0))
        self.assertFalse(result.coverage_verified)

    def test_an_empty_capture_against_a_populated_source_is_a_mismatch(self):
        capture = self.publish(PACKAGE, [])
        result = self.verify(capture, source([1, 2]))
        self.assertEqual(result.state, "mismatch")
        self.assertEqual((result.only_local_count, result.only_api_count), (0, 2))


class UnavailableTests(VerificationTestCase):
    def test_a_late_complete_response_cannot_persist_verified(self):
        capture = self.publish(PACKAGE, [1])
        clock = Clock()
        elapsed = [0.0]
        clock.monotonic = lambda: elapsed[0]

        def late(_number):
            elapsed[0] = 2.0

        result = self.verify(
            capture, [api_page([1], total=1, token=None)], clock=clock,
            budgets=Budgets(total_seconds=1.0), before_request=late,
        )
        self.assertEqual(result.state, "unavailable")
        self.assertFalse(self.coverage(capture))
        self.assertEqual([a["state"] for a in self.attempts(capture)], ["unavailable"])

    def test_same_page_duplicates_are_persisted_accurately(self):
        capture = self.publish(PACKAGE, [1])
        result = self.verify(capture, [
            api_page([1, "000001"], total=2, token="end"), api_page([], total=2),
        ])
        self.assertEqual(result.state, "unavailable")
        self.assertEqual((result.api_record_count, result.api_distinct_count,
                          result.api_duplicate_count), (2, 1, 1))

    def test_a_transport_failure_records_the_attempt_without_a_difference(self):
        capture = self.publish(PACKAGE, [1, 2])
        result = self.verify(capture, [TransientSourceError("connection reset")] * 3)

        self.assertEqual(result.state, "unavailable")
        self.assertIn("connection reset", result.reason)
        self.assertEqual(result.http_attempts, 3)
        self.assertEqual(result.pages_fetched, 0)
        # Nothing was learned about the source, so the differences are unknown.
        self.assertIsNone(result.only_local_count)
        self.assertIsNone(result.only_api_count)
        self.assertIsNone(result.announced_total)
        self.assertIsNone(result.api_keys_sha256)
        self.assertEqual(result.local_distinct_count, 2)
        self.assertFalse(result.coverage_verified)

    def test_a_partial_walk_records_what_it_saw_and_still_certifies_nothing(self):
        capture = self.publish(PACKAGE, [1, 2, 3])
        script = [api_page([1, 2], total=3, token="page-2")]
        script += [TransientSourceError("connection reset")] * 3
        result = self.verify(capture, script)

        self.assertEqual(result.state, "unavailable")
        self.assertEqual((result.pages_fetched, result.http_attempts), (1, 4))
        self.assertEqual(
            (result.announced_total, result.api_record_count, result.api_distinct_count),
            (3, 2, 2),
        )
        self.assertIsNone(result.only_local_count)
        self.assertIsNone(result.api_keys_sha256)

    def test_a_duplicate_from_the_source_cannot_approve_the_cardinality(self):
        capture = self.publish(PACKAGE, [1, 2, 3])
        result = self.verify(capture, [
            api_page([1, 2], total=3, token="a"),
            api_page([2, 3], total=3, token="b"),
            api_page([], total=3, token="c"),
        ])
        self.assertEqual(result.state, "unavailable")
        self.assertEqual(result.api_duplicate_count, 1)
        self.assertEqual((result.api_record_count, result.api_distinct_count), (4, 3))
        self.assertFalse(result.coverage_verified)

    def test_an_exhausted_budget_is_unavailable_and_never_a_result(self):
        capture = self.publish(PACKAGE, [1])
        result = self.verify(
            capture,
            [api_page([1], total=9, token=f"t{n}") for n in range(4)],
            budgets=Budgets(page_size=1, max_pages=2),
        )
        self.assertEqual(result.state, "unavailable")
        self.assertIn("exceeded 2 pages", result.reason)


class AttemptHistoryTests(VerificationTestCase):
    def test_a_later_failure_lowers_coverage_and_keeps_the_earlier_evidence(self):
        capture = self.publish(PACKAGE, [1, 2])
        self.assertEqual(self.verify(capture, source([1, 2])).state, "verified")
        second = self.verify(capture, [TransientSourceError("gone")] * 3)

        self.assertEqual(second.state, "unavailable")
        self.assertFalse(self.coverage(capture))
        history = self.attempts(capture)
        self.assertEqual([a["state"] for a in history], ["verified", "unavailable"])
        self.assertIsNotNone(history[0]["api_keys_sha256"])
        self.assertEqual(history[0]["only_local_count"], 0)

        latest = self.conn.execute(
            "select verification_state, verification_attempt_id"
            " from tl_read.capture_status where capture_id = %s", (capture,)
        ).fetchone()
        self.assertEqual(latest, ("unavailable", history[1]["attempt_id"]))

    def test_a_new_attempt_lowers_coverage_before_reaching_the_network(self):
        capture = self.publish(PACKAGE, [1])
        self.verify(capture, source([1]))
        self.assertTrue(self.coverage(capture))

        seen = []
        self.verify(
            capture,
            [TransientSourceError("gone")] * 3,
            before_request=lambda n: seen.append(self.coverage(capture)),
        )
        self.assertEqual(seen[0], False)

    def test_an_interrupted_attempt_stays_visible_and_frees_the_lock(self):
        capture = self.publish(PACKAGE, [1])

        def die(_n):
            raise KeyboardInterrupt("operator stopped the run")

        with self.assertRaises(KeyboardInterrupt):
            self.verify(capture, source([1]), before_request=die)

        history = self.attempts(capture)
        self.assertEqual([a["state"] for a in history], ["in_progress"])
        self.assertIsNone(history[0]["finished_at"])
        self.assertFalse(self.coverage(capture))
        self.assertTrue(self.lock_is_free())

    def test_a_retry_opens_a_new_attempt_rather_than_continuing_the_old_one(self):
        capture = self.publish(PACKAGE, [1])
        self.verify(capture, [TransientSourceError("gone")] * 3)
        self.verify(capture, source([1]))
        history = self.attempts(capture)
        self.assertEqual([a["state"] for a in history], ["unavailable", "verified"])
        self.assertTrue(self.coverage(capture))


class RejectionTests(VerificationTestCase):
    """Nothing is locked or written before the request is known to be valid."""

    def assert_untouched(self, package=PACKAGE):
        self.assertEqual(self.attempts(), [])
        self.assertTrue(self.lock_is_free(package))

    def test_an_unknown_capture_is_refused(self):
        with self.assertRaises(VerificationError) as caught:
            self.verify(9999, [])
        self.assertIn("does not exist", str(caught.exception))
        self.assert_untouched()

    def test_a_superseded_capture_is_refused(self):
        first = self.publish(PACKAGE, [1])
        path = write_package(self.dir / "replacement", [legacy_member(1), legacy_member(2)])
        second = load_package(self.new_conn(), path, PACKAGE, force_recapture=True)
        self.assertEqual(second.status, "published")

        with self.assertRaises(VerificationError) as caught:
            self.verify(first, [])
        self.assertIn("'superseded'", str(caught.exception))
        self.assert_untouched()

    def test_a_capture_that_never_published_is_refused(self):
        path = write_package(self.dir / "unfinished", [legacy_member(1)])
        writer = self.new_conn()
        sha, size = digest_archive(path)
        begin = repo.begin_capture(writer, PACKAGE, sha, size, batch_size=10)
        repo.release_lock(writer, PACKAGE)

        with self.assertRaises(VerificationError) as caught:
            self.verify(begin.capture.capture_id, [])
        self.assertIn("'acquiring'", str(caught.exception))
        self.assert_untouched()

    def test_a_package_with_no_supported_query_is_refused(self):
        capture = self.publish("daily/notacanonicalid", [1])
        with self.assertRaises(UnsupportedPackage):
            self.verify(capture, [])
        self.assertEqual(self.attempts(), [])
        self.assertTrue(self.lock_is_free("daily/notacanonicalid"))

    def test_a_caller_transaction_is_refused_before_locking_or_writing(self):
        capture = self.publish(PACKAGE, [1])
        caller = self.new_conn()
        with caller.transaction():
            caller.execute("select 1")
            with self.assertRaises(repo.ConnectionStateError):
                verify_capture(caller, capture, transport=FakeTransport([]))
        self.assert_untouched()

    def test_a_non_autocommit_connection_is_refused(self):
        capture = self.publish(PACKAGE, [1])
        caller = self.new_conn(autocommit=False)
        with self.assertRaises(repo.ConnectionStateError):
            verify_capture(caller, capture, transport=FakeTransport([]))
        caller.rollback()
        self.assert_untouched()


class ConcurrencyTests(VerificationTestCase):
    def test_a_second_verifier_is_refused_while_the_first_holds_the_package(self):
        capture = self.publish(PACKAGE, [1])
        blocked = []

        def during(n):
            if n == 1:
                try:
                    verify_capture(
                        self.new_conn(), capture, transport=FakeTransport([])
                    )
                except repo.CaptureError as exc:
                    blocked.append(exc)

        self.assertEqual(self.verify(capture, source([1]), before_request=during).state,
                         "verified")
        self.assertIsInstance(blocked[0], repo.ConcurrentCaptureError)
        self.assertEqual([a["state"] for a in self.attempts(capture)], ["verified"])

    def test_a_verification_blocks_a_recapture_of_the_same_package(self):
        capture = self.publish(PACKAGE, [1])
        path = write_package(self.dir / "again", [legacy_member(1), legacy_member(2)])
        blocked = []

        def during(n):
            if n == 1:
                try:
                    load_package(self.new_conn(), path, PACKAGE, force_recapture=True)
                except repo.CaptureError as exc:
                    blocked.append(exc)

        self.verify(capture, source([1]), before_request=during)
        self.assertIsInstance(blocked[0], repo.ConcurrentCaptureError)
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.capture where source_package_id = %s",
                (PACKAGE,),
            ).fetchone()[0],
            1,
        )

    def test_another_package_verifies_in_parallel(self):
        first = self.publish(PACKAGE, [1])
        second = self.publish(OTHER_PACKAGE, [5])
        done = []

        def during(n):
            if n == 1:
                done.append(
                    self.verify(
                        second, source([5], ojs=OTHER_OJS), conn=self.new_conn()
                    ).state
                )

        self.assertEqual(self.verify(first, source([1]), before_request=during).state,
                         "verified")
        self.assertEqual(done, ["verified"])

    def test_the_lock_is_released_after_a_failed_verification(self):
        capture = self.publish(PACKAGE, [1])
        self.verify(capture, [TransientSourceError("gone")] * 3)
        self.assertTrue(self.lock_is_free())


class DurabilityTests(VerificationTestCase):
    def test_the_result_is_committed_before_the_call_returns(self):
        capture = self.publish(PACKAGE, [1, 2])
        observer = self.new_conn()
        result = self.verify(capture, source([1, 2]))
        row = observer.execute(
            "select state, coverage_verified from tl_read.verification_attempt a"
            " join tl_work.capture c on c.capture_id = a.capture_id"
            " where a.attempt_id = %s",
            (result.attempt_id,),
        ).fetchone()
        self.assertEqual(row, ("verified", True))

    def test_an_attempt_closed_by_someone_else_is_not_reported_as_success(self):
        capture = self.publish(PACKAGE, [1])

        def steal(n):
            if n == 1:
                self.conn.execute(
                    "update tl_work.verification_attempt set state = 'unavailable',"
                    " reason = 'closed elsewhere', finished_at = now()"
                    " where capture_id = %s and state = 'in_progress'",
                    (capture,),
                )

        with self.assertRaises(VerificationError) as caught:
            self.verify(capture, source([1]), before_request=steal)
        self.assertIn("no longer in progress", str(caught.exception))
        self.assertFalse(self.coverage(capture))
        self.assertEqual([a["state"] for a in self.attempts(capture)], ["unavailable"])

    def test_a_lost_connection_at_the_close_does_not_report_success(self):
        capture = self.publish(PACKAGE, [1, 2])
        victim = self.new_conn()
        pid = victim.execute("select pg_backend_pid()").fetchone()[0]

        def kill(n):
            if n == 2:
                self.conn.execute("select pg_terminate_backend(%s)", (pid,))

        with self.assertRaises(psycopg.Error):
            self.verify(capture, source([1, 2]), conn=victim, before_request=kill)

        history = self.attempts(capture)
        self.assertEqual([a["state"] for a in history], ["in_progress"])
        self.assertFalse(self.coverage(capture))
        self.assertTrue(self.lock_is_free())


class ConstraintTests(VerificationTestCase):
    """The schema refuses a 'verified' row that its own numbers do not support."""

    def open_attempt(self, capture_id):
        return self.conn.execute(
            "insert into tl_work.verification_attempt (capture_id, artifact_sha256,"
            " contract_version, verifier_version, query_text, query_scope, state,"
            " local_distinct_count) values (%s, 'x', '1', '1', 'OJ = 220/2023', 'ALL',"
            " 'in_progress', 2) returning attempt_id",
            (capture_id,),
        ).fetchone()[0]

    def test_a_verified_row_without_matching_counts_is_rejected(self):
        capture = self.publish(PACKAGE, [1, 2])
        attempt = self.open_attempt(capture)
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "update tl_work.verification_attempt set state = 'verified',"
                " finished_at = now(), announced_total = 2, api_record_count = 2,"
                " api_distinct_count = 2, api_duplicate_count = 0,"
                " only_local_count = 1, only_api_count = 0, api_keys_sha256 = 'd'"
                " where attempt_id = %s",
                (attempt,),
            )

    def test_a_terminal_row_must_carry_a_finish_time(self):
        capture = self.publish(PACKAGE, [1, 2])
        attempt = self.open_attempt(capture)
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "update tl_work.verification_attempt set state = 'unavailable'"
                " where attempt_id = %s",
                (attempt,),
            )

    def test_verified_evidence_rejects_each_unknown_count(self):
        capture = self.publish(PACKAGE, [1, 2])
        result = self.verify(capture, source([1, 2]))
        for column in (
            "announced_total", "api_record_count", "api_distinct_count",
            "api_duplicate_count", "local_distinct_count", "only_local_count", "only_api_count",
        ):
            with self.subTest(column=column), self.assertRaises(psycopg.errors.CheckViolation):
                self.conn.execute(
                    f"update tl_work.verification_attempt set {column} = null where attempt_id = %s",
                    (result.attempt_id,),
                )

    def test_empty_evidence_requires_known_zero_counts(self):
        capture = self.publish(PACKAGE, [])
        result = self.verify(capture, source([], total=0))
        for column in ("announced_total", "api_record_count", "api_distinct_count", "local_distinct_count"):
            with self.subTest(column=column), self.assertRaises(psycopg.errors.CheckViolation):
                self.conn.execute(
                    f"update tl_work.verification_attempt set {column} = null where attempt_id = %s",
                    (result.attempt_id,),
                )


class BudgetPlumbingTests(VerificationTestCase):
    def test_budgets_reach_the_transport(self):
        capture = self.publish(PACKAGE, [1])
        budgets = Budgets(page_size=7, max_response_bytes=1234, operation_timeout=3.0)
        self.verify(capture, source([1]), budgets=budgets)
        request = self.transport.requests[0]
        self.assertEqual(request["payload"]["limit"], 7)
        self.assertEqual(request["max_bytes"], 1234)
        self.assertEqual(request["timeout"], 3.0)

    def test_the_verifier_reports_a_source_failure_rather_than_raising_it(self):
        capture = self.publish(PACKAGE, [1])
        result = self.verify(capture, [SourceUnavailable("HTTP 404")])
        self.assertEqual(result.state, "unavailable")
        self.assertEqual(result.reason, "HTTP 404")


if __name__ == "__main__":
    unittest.main()
