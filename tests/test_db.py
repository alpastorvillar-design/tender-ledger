"""Integration tests for the transactional loader against a real PostgreSQL.

These need the local Compose database running. They use a dedicated
``tender_ledger_test`` database, never the development one, and never touch its
volume. If the server is unreachable the module skips with a clear message
rather than passing silently.
"""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import psycopg
from psycopg import sql

from ted_fixtures import (
    TEST_DB,
    eforms_member,
    ensure_test_database,
    legacy_member,
    truncate_all,
    write_package,
)
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.db import repository as repo
from tender_ledger.loader import _is_transient, digest_archive, load_package
from tender_ledger.packages import stream_notices
from tender_ledger.projection import ChangeReference, project_member


def setUpModule():
    ensure_test_database()


def _digest(path):
    return digest_archive(path)


def projected_rows(package):
    return [project_member(m) for m in stream_notices(package)]


class LoaderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._conns: list[psycopg.Connection] = []
        self.conn = self.new_conn()
        truncate_all(self.conn)

    def new_conn(self, **kw) -> psycopg.Connection:
        conn = db.connect(load_config(dbname=TEST_DB), **kw)
        self._conns.append(conn)
        self.addCleanup(conn.close)
        return conn

    def package(self, name, members):
        return write_package(self.dir / name, members)

    def notices(self, conn=None, *, package=None, view="tl_read.notice"):
        conn = conn or self.conn
        sql = f"select publication_ref from {view}"
        args: tuple = ()
        if package is not None:
            sql += " where source_package_id = %s"
            args = (package,)
        return sorted(r[0] for r in conn.execute(sql, args))

    def capture_status(self, capture_id, conn=None):
        conn = conn or self.conn
        return conn.execute(
            "select status from tl_work.capture where capture_id = %s", (capture_id,)
        ).fetchone()[0]

    def persisted(self, capture_id, conn=None):
        """(status, committed batches, notice rows) as another connection sees it."""
        conn = conn or self.conn
        return conn.execute(
            "select status,"
            " (select count(*) from tl_work.capture_batch where capture_id = %s),"
            " (select count(*) from tl_work.notice_capture where capture_id = %s)"
            " from tl_work.capture where capture_id = %s",
            (capture_id,) * 3,
        ).fetchone()

    def captures(self, package, conn=None):
        conn = conn or self.conn
        return conn.execute(
            "select count(*) from tl_work.capture where source_package_id = %s", (package,)
        ).fetchone()[0]

    def lock_is_free(self, package, conn=None):
        """Take and release the capture lock from outside, spelling out the key."""
        conn = conn or self.conn
        got = conn.execute(
            "select pg_try_advisory_lock(hashtext(%s)::int8)", (package,)
        ).fetchone()[0]
        if got:
            conn.execute("select pg_advisory_unlock(hashtext(%s)::int8)", (package,))
        return got


class HappyPathTests(LoaderTestCase):
    def test_loads_a_mixed_package_and_publishes_it(self):
        pkg = self.package("daily", [
            legacy_member(1001), legacy_member(1002, namespace="R2.0.8", version_attr=""),
            eforms_member(1003), eforms_member(1004),
        ])
        result = load_package(self.conn, pkg, "daily/2023220", batch_size=2)
        self.assertEqual(result.status, "published")
        self.assertEqual(result.member_count, 4)
        self.assertEqual(result.distinct_notice_count, 4)
        self.assertEqual(result.loaded_row_count, 4)
        self.assertFalse(result.resumed)
        self.assertEqual(self.notices(), ["1001-2023", "1002-2023", "1003-2023", "1004-2023"])
        row = self.conn.execute(
            "select source_coverage_verified from tl_read.notice limit 1"
        ).fetchone()
        self.assertFalse(row[0])

    def test_replaying_the_same_capture_does_not_duplicate_or_advance(self):
        members = [legacy_member(2001), eforms_member(2002)]
        pkg = self.package("daily", members)
        first = load_package(self.conn, pkg, "daily/x")
        second = load_package(self.new_conn(), pkg, "daily/x")
        self.assertEqual(first.capture_id, second.capture_id)
        self.assertTrue(second.resumed)
        self.assertEqual(self.notices(), ["2001-2023", "2002-2023"])
        count = self.conn.execute("select count(*) from tl_work.capture").fetchone()[0]
        self.assertEqual(count, 1)


class VisibilityTests(LoaderTestCase):
    def test_reader_keeps_previous_capture_until_replacement_publishes(self):
        pkg_v1 = self.package("v1", [legacy_member(10), legacy_member(11)])
        load_package(self.conn, pkg_v1, "daily/z")

        pkg_v2 = self.package("v2", [legacy_member(10), legacy_member(11), legacy_member(12)])
        sha, size = _digest(pkg_v2)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "daily/z", sha, size, batch_size=100)
        # load its batch but do not reconcile or publish
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg_v2))

        reader = self.new_conn()
        reader.execute("set role tender_ledger_reader")
        self.assertEqual(self.notices(reader, package="daily/z"), ["10-2023", "11-2023"])

    def test_reader_role_cannot_touch_working_tables(self):
        pkg = self.package("p", [legacy_member(1)])
        load_package(self.conn, pkg, "daily/p")
        reader = self.new_conn()
        reader.execute("set role tender_ledger_reader")
        self.assertEqual(len(self.notices(reader)), 1)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            reader.execute("select * from tl_work.notice_capture")

    def test_failed_publish_transaction_changes_nothing(self):
        pkg_v1 = self.package("v1", [legacy_member(5)])
        r1 = load_package(self.conn, pkg_v1, "daily/q")

        pkg_v2 = self.package("v2", [legacy_member(5), legacy_member(6)])

        def boom(_conn):
            raise RuntimeError("simulated failure before commit")

        result = load_package(self.new_conn(), pkg_v2, "daily/q", before_publish=boom)
        self.assertEqual(result.status, "loaded")
        self.assertIn("RuntimeError", result.publish_error or "")

        # previous capture still visible, both captures in their expected state
        self.assertEqual(self.notices(package="daily/q"), ["5-2023"])
        published = self.conn.execute(
            "select capture_id from tl_work.published_capture where source_package_id = %s",
            ("daily/q",),
        ).fetchone()[0]
        self.assertEqual(published, r1.capture_id)
        self.assertEqual(self.capture_status(published), "published")
        self.assertEqual(self.capture_status(result.capture_id), "loaded")

    def test_publish_failure_is_retriable_with_the_same_identity(self):
        pkg_v1 = self.package("v1", [legacy_member(5)])
        load_package(self.conn, pkg_v1, "daily/qr")
        pkg_v2 = self.package("v2", [legacy_member(5), legacy_member(6)])

        failed = load_package(
            self.new_conn(), pkg_v2, "daily/qr",
            before_publish=lambda c: c.execute("select 1 / 0"),
        )
        self.assertEqual(failed.status, "loaded")

        retry = load_package(self.new_conn(), pkg_v2, "daily/qr")
        self.assertEqual(retry.status, "published")
        self.assertTrue(retry.resumed)
        self.assertEqual(retry.capture_id, failed.capture_id)
        self.assertEqual(self.notices(package="daily/qr"), ["5-2023", "6-2023"])


class FailureTests(LoaderTestCase):
    def test_duplicate_identity_within_a_package_fails_the_capture(self):
        pkg = self.package("dup", [legacy_member(7), eforms_member(7)])
        result = load_package(self.conn, pkg, "daily/dup", batch_size=10)
        self.assertEqual(result.status, "failed")
        self.assertIn("UniqueViolation", result.failure_reason or "")
        self.assertIsNone(result.load_error)
        self.assertEqual(self.notices(), [])
        self.assertEqual(
            self.conn.execute("select count(*) from tl_work.capture_batch").fetchone()[0], 0
        )

    def test_failure_after_a_committed_batch_keeps_it_internal_and_resumable(self):
        pkg = self.package("big", [legacy_member(100 + i) for i in range(5)])
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "daily/big", sha, size, batch_size=2)
        rows = projected_rows(pkg)
        repo.load_batch(writer, begin.capture.capture_id, 0, rows[:2])
        # crash: real disconnect before further batches / publish
        writer.close()

        # a second connection still sees the committed batch and its rows
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.capture_batch where capture_id = %s",
                (begin.capture.capture_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.notice_capture where capture_id = %s",
                (begin.capture.capture_id,),
            ).fetchone()[0],
            2,
        )
        # but the restricted reader sees nothing for this (unpublished) package
        reader = self.new_conn()
        reader.execute("set role tender_ledger_reader")
        self.assertEqual(self.notices(reader, package="daily/big"), [])

        resumed = load_package(self.new_conn(), pkg, "daily/big", batch_size=2)
        self.assertEqual(resumed.status, "published")
        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.capture_id, begin.capture.capture_id)
        self.assertEqual(
            self.notices(self._conns[-1], package="daily/big"),
            [f"{100 + i}-2023" for i in range(5)],
        )

    def test_corrupt_archive_fails_and_keeps_previous_capture(self):
        good = self.package("good", [legacy_member(1), legacy_member(2)])
        load_package(self.conn, good, "daily/c")

        corrupt = self.dir / "corrupt"
        corrupt.write_bytes(self.package("tmp", [legacy_member(1), legacy_member(2), legacy_member(3)]).read_bytes()[:-6])
        result = load_package(self.new_conn(), corrupt, "daily/c")
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.notices(package="daily/c"), ["1-2023", "2-2023"])

    def test_empty_local_package_publishes_as_complete_but_unverified(self):
        pkg = self.package("empty", [])
        result = load_package(self.conn, pkg, "daily/empty")
        self.assertEqual(result.status, "published")
        self.assertEqual(result.member_count, 0)
        self.assertEqual(self.notices(package="daily/empty"), [])
        row = self.conn.execute(
            "select is_published, member_count, source_coverage_verified"
            " from tl_read.capture_status where source_package_id = %s",
            ("daily/empty",),
        ).fetchone()
        self.assertEqual(row, (True, 0, False))


class RecaptureTests(LoaderTestCase):
    def test_a_b_a_recapture_keeps_three_distinct_captures(self):
        pkg_a = self.package("a", [legacy_member(1), legacy_member(2)])
        pkg_b = self.package("b", [legacy_member(1), legacy_member(2), legacy_member(3)])

        r1 = load_package(self.new_conn(), pkg_a, "daily/aba")
        r2 = load_package(self.new_conn(), pkg_b, "daily/aba")
        r3 = load_package(self.new_conn(), pkg_a, "daily/aba")

        ids = {r1.capture_id, r2.capture_id, r3.capture_id}
        self.assertEqual(len(ids), 3)
        self.assertEqual([r1.acquisition_ordinal, r2.acquisition_ordinal, r3.acquisition_ordinal], [1, 2, 3])
        total = self.conn.execute(
            "select count(*) from tl_work.capture where source_package_id = %s", ("daily/aba",)
        ).fetchone()[0]
        self.assertEqual(total, 3)
        self.assertEqual(self.notices(package="daily/aba"), ["1-2023", "2-2023"])

    def test_recapture_with_a_retired_member_replaces_it_cleanly(self):
        full = self.package("full", [legacy_member(1), legacy_member(2), legacy_member(3)])
        load_package(self.new_conn(), full, "daily/retire")
        self.assertEqual(self.notices(package="daily/retire"), ["1-2023", "2-2023", "3-2023"])

        shrunk = self.package("shrunk", [legacy_member(1), legacy_member(2)])
        load_package(self.new_conn(), shrunk, "daily/retire")
        self.assertEqual(self.notices(package="daily/retire"), ["1-2023", "2-2023"])


class OverlapTests(LoaderTestCase):
    def test_two_packages_with_overlapping_publications_do_not_inflate_distinct(self):
        daily = self.package("daily", [legacy_member(1), legacy_member(2), legacy_member(3)])
        monthly = self.package("monthly", [legacy_member(2), legacy_member(3), legacy_member(4)])
        load_package(self.new_conn(), daily, "daily/2023220")
        load_package(self.new_conn(), monthly, "monthly/202311")

        self.assertEqual(len(self.notices(view="tl_read.notice")), 6)
        self.assertEqual(
            self.notices(view="tl_read.distinct_notice"),
            ["1-2023", "2-2023", "3-2023", "4-2023"],
        )

    def test_distinct_notice_resolves_to_most_recent_acquisition(self):
        first = self.package("first", [eforms_member(9, buyer_country="DEU")])
        second = self.package("second", [eforms_member(9, buyer_country="FRA")])
        load_package(self.new_conn(), first, "daily/one")
        load_package(self.new_conn(), second, "daily/two")
        row = self.conn.execute(
            "select buyer_country from tl_read.distinct_notice where publication_number = 9"
        ).fetchone()
        self.assertEqual(row[0], "FRA")


class ConcurrencyTests(LoaderTestCase):
    def test_an_aborted_capture_link_rolls_back_and_releases_its_lock(self):
        pkg = self.package("hook", [legacy_member(1)])
        sha, size = _digest(pkg)
        writer = self.new_conn()

        def abort(_conn, _capture):
            raise RuntimeError("link failed")

        with self.assertRaisesRegex(RuntimeError, "link failed"):
            repo.begin_capture(
                writer,
                "daily/hook",
                sha,
                size,
                batch_size=100,
                on_capture=abort,
            )

        self.assertEqual(self.captures("daily/hook"), 0)
        self.assertTrue(self.lock_is_free("daily/hook"))

    def test_second_capture_of_the_same_package_is_refused_while_one_is_open(self):
        pkg = self.package("p", [legacy_member(1)])
        sha, size = _digest(pkg)
        holder = self.new_conn()
        repo.begin_capture(holder, "daily/lock", sha, size, batch_size=100)

        with self.assertRaises(repo.ConcurrentCaptureError):
            load_package(self.new_conn(), pkg, "daily/lock")

        opened = self.conn.execute(
            "select count(*) from tl_work.capture where source_package_id = %s", ("daily/lock",)
        ).fetchone()[0]
        self.assertEqual(opened, 1)

    def test_different_packages_capture_concurrently(self):
        pkg_a = self.package("a", [legacy_member(1)])
        pkg_b = self.package("b", [legacy_member(2)])
        sha_a, size_a = _digest(pkg_a)
        holder = self.new_conn()
        repo.begin_capture(holder, "daily/a", sha_a, size_a, batch_size=100)
        result = load_package(self.new_conn(), pkg_b, "daily/b")
        self.assertEqual(result.status, "published")


class DurabilityTests(LoaderTestCase):
    def test_committed_batch_is_visible_to_another_connection_and_survives_disconnect(self):
        pkg = self.package("d", [legacy_member(100 + i) for i in range(5)])
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "review/durability", sha, size, batch_size=2)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg)[:2])

        observer = self.new_conn()
        before = observer.execute(
            "select status,"
            " (select count(*) from tl_work.capture_batch where capture_id = c.capture_id),"
            " (select count(*) from tl_work.notice_capture where capture_id = c.capture_id)"
            " from tl_work.capture c where capture_id = %s",
            (begin.capture.capture_id,),
        ).fetchone()
        self.assertEqual(before, ("loading", 1, 2))

        writer.close()  # real disconnect, not a mocked transaction
        after = observer.execute(
            "select status,"
            " (select count(*) from tl_work.capture_batch where capture_id = c.capture_id),"
            " (select count(*) from tl_work.notice_capture where capture_id = c.capture_id)"
            " from tl_work.capture c where capture_id = %s",
            (begin.capture.capture_id,),
        ).fetchone()
        self.assertEqual(after, ("loading", 1, 2))

    def test_failed_batch_leaves_neither_rows_nor_a_batch_record(self):
        pkg = self.package("f", [legacy_member(7), eforms_member(7)])  # duplicate identity
        writer = self.new_conn()
        sha, size = _digest(pkg)
        begin = repo.begin_capture(writer, "review/failbatch", sha, size, batch_size=10)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg))

        seen = self.new_conn()
        self.assertEqual(
            seen.execute(
                "select"
                " (select count(*) from tl_work.capture_batch where capture_id = %s),"
                " (select count(*) from tl_work.notice_capture where capture_id = %s)",
                (begin.capture.capture_id, begin.capture.capture_id),
            ).fetchone(),
            (0, 0),
        )

    def test_retry_does_not_rewrite_a_committed_batch(self):
        pkg = self.package("r", [legacy_member(100 + i) for i in range(5)])
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "review/norewrite", sha, size, batch_size=2)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg)[:2])
        writer.close()

        called: list[int] = []
        original = repo.load_batch

        def spy(conn, capture_id, ordinal, notices):
            called.append(ordinal)
            return original(conn, capture_id, ordinal, notices)

        with mock.patch.object(repo, "load_batch", side_effect=spy):
            resumed = load_package(self.new_conn(), pkg, "review/norewrite", batch_size=2)

        self.assertEqual(resumed.status, "published")
        self.assertEqual(called, [1, 2])


class RecoverableRetryTests(LoaderTestCase):
    def test_crash_between_reconcile_and_publish_keeps_the_identity(self):
        pkg = self.package("g", [legacy_member(100 + i) for i in range(5)])
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "review/loaded-gap", sha, size, batch_size=5)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg))
        repo.reconcile(writer, begin.capture.capture_id, 5, 5)
        writer.close()  # crash after reconcile, before publish
        self.assertEqual(self.capture_status(begin.capture.capture_id), "loaded")

        retry = load_package(self.new_conn(), pkg, "review/loaded-gap", batch_size=5)
        self.assertEqual(retry.status, "published")
        self.assertTrue(retry.resumed)
        self.assertEqual(retry.capture_id, begin.capture.capture_id)

    def test_intentional_recapture_with_identical_bytes_gets_a_new_identity(self):
        pkg = self.package("h", [legacy_member(1), legacy_member(2)])
        first = load_package(self.new_conn(), pkg, "daily/force")
        replay = load_package(self.new_conn(), pkg, "daily/force")
        self.assertEqual(replay.capture_id, first.capture_id)  # plain replay is a no-op

        forced = load_package(self.new_conn(), pkg, "daily/force", force_recapture=True)
        self.assertNotEqual(forced.capture_id, first.capture_id)
        self.assertEqual(forced.acquisition_ordinal, first.acquisition_ordinal + 1)
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.capture where source_package_id = %s",
                ("daily/force",),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(self.notices(package="daily/force"), ["1-2023", "2-2023"])


class BatchSizeContractTests(LoaderTestCase):
    def test_resume_reuses_the_persisted_batch_size(self):
        pkg = self.package("b", [legacy_member(100 + i) for i in range(5)])
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "review/batch-size", sha, size, batch_size=2)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg)[:2])
        writer.close()

        # retry asks for a different --batch-size; the persisted 2 wins
        retry = load_package(self.new_conn(), pkg, "review/batch-size", batch_size=3)
        self.assertEqual(retry.status, "published")
        self.assertEqual(retry.batch_size, 2)
        self.assertEqual(retry.loaded_row_count, 5)
        self.assertEqual(
            self.notices(package="review/batch-size"),
            [f"{100 + i}-2023" for i in range(5)],
        )

    def test_retry_with_the_same_batch_size_also_completes(self):
        pkg = self.package("b2", [legacy_member(100 + i) for i in range(5)])
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "review/same-size", sha, size, batch_size=2)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg)[:2])
        writer.close()
        retry = load_package(self.new_conn(), pkg, "review/same-size", batch_size=2)
        self.assertEqual(retry.status, "published")
        self.assertEqual(retry.loaded_row_count, 5)

    def test_a_capture_from_before_the_recorded_batch_size_is_not_resumed(self):
        pkg = self.package("b4", [legacy_member(100 + i) for i in range(5)])
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "daily/pre0002", sha, size, batch_size=2)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg)[:2])
        writer.close()
        # as a capture written before migration 0002: no recorded partition
        self.conn.execute(
            "update tl_work.capture set batch_size = null where capture_id = %s",
            (begin.capture.capture_id,),
        )

        with self.assertRaises(repo.CaptureError):
            load_package(self.new_conn(), pkg, "daily/pre0002", batch_size=2)

        self.assertEqual(self.persisted(begin.capture.capture_id), ("loading", 1, 2))
        self.assertTrue(self.lock_is_free("daily/pre0002"))

    def test_a_loaded_capture_without_a_recorded_batch_size_still_publishes(self):
        pkg = self.package("b5", [legacy_member(1), legacy_member(2)])
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "daily/pre0002-loaded", sha, size, batch_size=2)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg))
        repo.reconcile(writer, begin.capture.capture_id, 2, 2)
        writer.close()
        self.conn.execute(
            "update tl_work.capture set batch_size = null where capture_id = %s",
            (begin.capture.capture_id,),
        )

        retry = load_package(self.new_conn(), pkg, "daily/pre0002-loaded", batch_size=2)
        self.assertEqual(retry.capture_id, begin.capture.capture_id)
        self.assertEqual(retry.status, "published")

    def test_non_positive_batch_size_is_rejected(self):
        pkg = self.package("b3", [legacy_member(1)])
        with self.assertRaises(ValueError):
            load_package(self.new_conn(), pkg, "daily/b3", batch_size=0)


class CoverageOutputTests(LoaderTestCase):
    def test_load_replay_and_failure_all_report_coverage(self):
        pkg = self.package("c", [legacy_member(1), legacy_member(2)])
        loaded = load_package(self.conn, pkg, "daily/cov")
        self.assertIs(loaded.source_coverage_verified, False)

        replay = load_package(self.new_conn(), pkg, "daily/cov")
        self.assertIs(replay.source_coverage_verified, False)

        bad = self.package("bad", [legacy_member(3), eforms_member(3)])
        failed = load_package(self.new_conn(), bad, "daily/covfail")
        self.assertEqual(failed.status, "failed")
        self.assertIs(failed.source_coverage_verified, False)

        # the view agrees
        view = self.conn.execute(
            "select bool_or(source_coverage_verified) from tl_read.capture_status"
        ).fetchone()[0]
        self.assertFalse(view)


class RecoveryEquivalenceTests(LoaderTestCase):
    def test_recovered_state_matches_a_clean_run_over_the_same_artifact(self):
        members = [legacy_member(10 + i) for i in range(4)] + [eforms_member(20 + i) for i in range(3)]
        pkg = self.package("pkg", members)

        clean = load_package(self.new_conn(), pkg, "daily/clean", batch_size=3)
        clean_rows = _notice_rows(self.conn, clean.capture_id)

        # interrupted run of the same artifact into a different package id
        sha, size = _digest(pkg)
        c1 = self.new_conn()
        begin = repo.begin_capture(c1, "daily/interrupted", sha, size, batch_size=3)
        repo.load_batch(c1, begin.capture.capture_id, 0, projected_rows(pkg)[:3])
        c1.close()

        resumed = load_package(self.new_conn(), pkg, "daily/interrupted", batch_size=3)
        self.assertTrue(resumed.resumed)
        resumed_rows = _notice_rows(self.conn, resumed.capture_id)
        self.assertEqual(clean_rows, resumed_rows)


class ChangeReferenceProjectionLoadTests(LoaderTestCase):
    """Contract v2: references are projected, persisted, and readable per notice."""

    def reference_rows(self, capture_id, conn=None):
        return _reference_rows(conn or self.conn, capture_id)

    def test_a_notice_with_references_persists_them_in_document_order(self):
        pkg = self.package("refs", [
            eforms_member(1, change_refs=[("00099-2020", "notice-id-ref"), ("uid/02", None)]),
            eforms_member(2),  # no references: absent
            legacy_member(3),  # legacy: not_applicable
        ])
        result = load_package(self.conn, pkg, "daily/refs")
        self.assertEqual(result.status, "published")

        statuses = dict(self.conn.execute(
            "select publication_number, change_reference_status"
            " from tl_work.notice_capture where capture_id = %s",
            (result.capture_id,),
        ))
        self.assertEqual(statuses, {1: "present", 2: "absent", 3: "not_applicable"})
        rows = self.reference_rows(result.capture_id)
        self.assertEqual(
            [(r[2], r[3], r[4]) for r in rows if r[1] == 1],
            [(0, "00099-2020", "notice-id-ref"), (1, "uid/02", None)],
        )

    def test_the_history_view_exposes_change_reference_status(self):
        pkg = self.package("refs", [eforms_member(1, change_refs=[("00099-2020", None)])])
        result = load_package(self.conn, pkg, "daily/refhist")
        row = self.conn.execute(
            "select change_reference_status from tl_read.notice_history"
            " where capture_id = %s and publication_number = 1",
            (result.capture_id,),
        ).fetchone()
        self.assertEqual(row[0], "present")

    def test_the_reference_history_view_has_one_row_per_reference(self):
        pkg = self.package("refs", [
            eforms_member(1, change_refs=[("00099-2020", "notice-id-ref"), ("uid/02", None)]),
        ])
        result = load_package(self.conn, pkg, "daily/refhist2")
        rows = self.conn.execute(
            "select ordinal, value, scheme_name from tl_read.notice_change_reference_history"
            " where capture_id = %s order by ordinal",
            (result.capture_id,),
        ).fetchall()
        self.assertEqual(rows, [(0, "00099-2020", "notice-id-ref"), (1, "uid/02", None)])

    def test_the_reader_cannot_select_the_change_reference_table_directly(self):
        pkg = self.package("refs", [eforms_member(1, change_refs=[("00099-2020", None)])])
        load_package(self.conn, pkg, "daily/refperm")
        reader = self.new_conn()
        reader.execute("set role tender_ledger_reader")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            reader.execute("select * from tl_work.notice_change_reference")


class ChangeReferenceAtomicityTests(LoaderTestCase):
    """A batch's notice rows and its reference rows commit or roll back together."""

    def test_an_invalid_reference_rolls_back_the_whole_batch(self):
        pkg = self.package("bad", [eforms_member(1, change_refs=[("00099-2020", None)])])
        rows = projected_rows(pkg)
        invalid = dataclasses.replace(
            rows[0],
            change_reference_status="present",
            change_references=(ChangeReference(0, "", None),),
        )
        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(writer, "daily/badref", sha, size, batch_size=10)
        with self.assertRaises(psycopg.errors.CheckViolation):
            repo.load_batch(writer, begin.capture.capture_id, 0, [invalid])

        seen = self.new_conn()
        self.assertEqual(
            seen.execute(
                "select"
                " (select count(*) from tl_work.capture_batch where capture_id = %s),"
                " (select count(*) from tl_work.notice_capture where capture_id = %s),"
                " (select count(*) from tl_work.notice_change_reference where capture_id = %s)",
                (begin.capture.capture_id,) * 3,
            ).fetchone(),
            (0, 0, 0),
        )

    def test_deleting_an_uncommitted_notice_takes_its_references_with_it(self):
        # clear_uncommitted_rows only targets tl_work.notice_capture; the
        # foreign key's ON DELETE CASCADE is what removes the orphaned
        # reference rather than a second, easily-forgotten DELETE.
        pkg = self.package("refs", [eforms_member(1, change_refs=[("00099-2020", None)])])
        writer = self.new_conn()
        sha, size = _digest(pkg)
        begin = repo.begin_capture(writer, "daily/orphan", sha, size, batch_size=10)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg))
        self.assertEqual(len(self.reference_rows(begin.capture.capture_id, writer)), 1)

        deleted = repo.clear_uncommitted_rows(writer, begin.capture.capture_id)
        self.assertEqual(deleted, 0)  # the batch above is already committed

        # Force an uncommitted row directly to exercise the cascade in isolation.
        writer.execute(
            "insert into tl_work.notice_capture"
            " (capture_id, publication_year, publication_number, batch_ordinal,"
            "  source_format, schema_version, source_filename, publication_date,"
            "  publication_date_raw, buyer_country_status, primary_cpv_status,"
            "  change_reference_status)"
            " values (%s, 2099, 999, 5, 'legacy', 'R2.0.9', 'x.xml', '2020-01-01',"
            " '20200101', 'absent', 'absent', 'not_applicable')",
            (begin.capture.capture_id,),
        )
        writer.execute(
            "insert into tl_work.notice_change_reference"
            " (capture_id, publication_year, publication_number, ordinal, value)"
            " values (%s, 2099, 999, 0, 'orphan-ref')",
            (begin.capture.capture_id,),
        )
        deleted = repo.clear_uncommitted_rows(writer, begin.capture.capture_id)
        self.assertEqual(deleted, 1)
        self.assertEqual(
            writer.execute(
                "select count(*) from tl_work.notice_change_reference"
                " where capture_id = %s and publication_number = 999",
                (begin.capture.capture_id,),
            ).fetchone()[0],
            0,
        )
        # the earlier, genuinely committed reference is untouched
        self.assertEqual(len(self.reference_rows(begin.capture.capture_id, writer)), 1)

    def reference_rows(self, capture_id, conn=None):
        return _reference_rows(conn or self.conn, capture_id)


class ReconciliationReferenceCountTests(LoaderTestCase):
    """reconcile() checks an independent persisted count, not just that COPY ran."""

    def test_a_reference_count_mismatch_fails_reconciliation(self):
        pkg = self.package("refs", [eforms_member(1, change_refs=[("00099-2020", None)])])
        writer = self.new_conn()
        sha, size = _digest(pkg)
        begin = repo.begin_capture(writer, "daily/mismatch", sha, size, batch_size=10)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg))

        result = repo.reconcile(
            writer, begin.capture.capture_id, 1, 1, change_reference_count=2
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.persisted_reference_count, 1)
        self.assertEqual(
            self.capture_status(begin.capture.capture_id, writer), "loading"
        )  # never promoted to 'loaded' on a mismatch

    def test_a_matching_reference_count_reconciles(self):
        pkg = self.package("refs", [eforms_member(1, change_refs=[("00099-2020", None)])])
        writer = self.new_conn()
        sha, size = _digest(pkg)
        begin = repo.begin_capture(writer, "daily/matched", sha, size, batch_size=10)
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg))

        result = repo.reconcile(
            writer, begin.capture.capture_id, 1, 1, change_reference_count=1
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.persisted_reference_count, 1)


class ChangeReferenceRecoveryTests(LoaderTestCase):
    """Recovery reproduces the same reference rows as a clean run, not just the
    same notice rows."""

    def _cancel_before(self, ordinal):
        original = repo.load_batch

        def load_batch(conn, capture_id, batch_ordinal, notices):
            if batch_ordinal == ordinal:
                conn.execute("set statement_timeout = '10ms'")
                try:
                    conn.execute("select pg_sleep(0.5)")
                finally:
                    conn.execute("set statement_timeout = 0")
            return original(conn, capture_id, batch_ordinal, notices)

        return mock.patch.object(repo, "load_batch", side_effect=load_batch)

    def test_a_cancelled_batch_leaves_no_references_without_a_committed_batch(self):
        members = [eforms_member(100 + i, change_refs=[(f"ref-{i}-2020", None)])
                   for i in range(5)]
        pkg = self.package("t", members)
        with self._cancel_before(1):
            interrupted = load_package(self.new_conn(), pkg, "daily/refcancel", batch_size=2)
        self.assertEqual(interrupted.status, "loading")
        # exactly one committed batch's worth of references survive the cancel
        self.assertEqual(len(_reference_rows(self.conn, interrupted.capture_id)), 2)

        retry = load_package(self.new_conn(), pkg, "daily/refcancel", batch_size=2)
        self.assertEqual(retry.status, "published")

        clean = load_package(self.new_conn(), pkg, "daily/refclean", batch_size=2)
        self.assertEqual(
            _reference_rows(self.conn, retry.capture_id),
            _reference_rows(self.conn, clean.capture_id),
        )
        self.assertEqual(
            _notice_rows(self.conn, retry.capture_id), _notice_rows(self.conn, clean.capture_id)
        )


class MigrationUpgradeTests(unittest.TestCase):
    """Clean install applies every migration; upgrading from 0001 keeps captures."""

    UPGRADE_DB = TEST_DB.replace("_test", "_upgrade_test")

    def setUp(self):
        assert self.UPGRADE_DB.endswith("_test")
        try:
            self.admin = db.connect(load_config(dbname="postgres"), autocommit=True)
        except psycopg.OperationalError as exc:
            self.skipTest(str(exc))
        self.admin.execute(
            sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(self.UPGRADE_DB))
        )
        self.admin.execute(sql.SQL("create database {}").format(sql.Identifier(self.UPGRADE_DB)))
        self.addCleanup(self._drop)
        self.conn = db.connect(load_config(dbname=self.UPGRADE_DB))
        self.addCleanup(self.conn.close)

    def _drop(self):
        self.admin.execute(
            sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(self.UPGRADE_DB))
        )
        self.admin.close()

    def test_upgrade_from_0001_preserves_an_existing_capture(self):
        applied = db.migrate(self.conn, up_to="0001_core")
        self.assertEqual(applied, ["0001_core"])

        # a capture as written by the 0001-era code (no batch_size column yet)
        capture_id = self.conn.execute(
            "insert into tl_work.capture (source_package_id, artifact_sha256, artifact_bytes,"
            " contract_version, status, member_count, distinct_notice_count, loaded_row_count)"
            " values ('daily/old', 'abc123', 100, '1', 'published', 1, 1, 1) returning capture_id"
        ).fetchone()[0]
        self.conn.execute(
            "insert into tl_work.notice_capture (capture_id, publication_year, publication_number,"
            " batch_ordinal, source_format, schema_version, source_filename, publication_date,"
            " publication_date_raw, buyer_country_status, primary_cpv_status)"
            " values (%s, 2020, 995, 0, 'legacy', 'R2.0.9', '995_2020.xml', '2020-01-03',"
            " '20200103', 'absent', 'absent')",
            (capture_id,),
        )
        self.conn.execute(
            "insert into tl_work.published_capture (source_package_id, capture_id)"
            " values ('daily/old', %s)",
            (capture_id,),
        )

        self.assertEqual(
            db.migrate(self.conn),
            [
                "0002_capture_batch_size", "0003_source_verification",
                "0004_package_ingest", "0005_projection_contract_v2",
            ],
        )

        row = self.conn.execute(
            "select status, is_published, member_count, batch_size, verification_state"
            " from tl_read.capture_status where capture_id = %s", (capture_id,)
        ).fetchone()
        self.assertEqual(row, ("published", True, 1, None, None))
        self.assertEqual(
            self.conn.execute(
                "select publication_ref, change_reference_status from tl_read.notice"
                " where source_package_id = 'daily/old'"
            ).fetchone(),
            ("995-2020", None),
        )

    def test_upgrade_from_0002_keeps_the_capture_and_adds_verification(self):
        self.assertEqual(
            db.migrate(self.conn, up_to="0002_capture_batch_size"),
            ["0001_core", "0002_capture_batch_size"],
        )
        capture_id = self.conn.execute(
            "insert into tl_work.capture (source_package_id, artifact_sha256, artifact_bytes,"
            " contract_version, status, member_count, distinct_notice_count, loaded_row_count,"
            " batch_size) values ('daily/202300220', 'abc123', 100, '1', 'published', 1, 1, 1, 500)"
            " returning capture_id"
        ).fetchone()[0]
        self.conn.execute(
            "insert into tl_work.notice_capture (capture_id, publication_year, publication_number,"
            " batch_ordinal, source_format, schema_version, source_filename, publication_date,"
            " publication_date_raw, buyer_country_status, primary_cpv_status)"
            " values (%s, 2023, 694329, 0, 'legacy', 'R2.0.9', '694329_2023.xml', '2023-11-15',"
            " '20231115', 'absent', 'absent')",
            (capture_id,),
        )
        self.conn.execute(
            "insert into tl_work.published_capture (source_package_id, capture_id)"
            " values ('daily/202300220', %s)",
            (capture_id,),
        )

        self.assertEqual(
            db.migrate(self.conn),
            ["0003_source_verification", "0004_package_ingest", "0005_projection_contract_v2"],
        )

        self.assertEqual(
            self.conn.execute(
                "select status, is_published, batch_size, source_coverage_verified,"
                " verification_state, verification_attempt_id"
                " from tl_read.capture_status where capture_id = %s", (capture_id,)
            ).fetchone(),
            ("published", True, 500, False, None, None),
        )
        self.assertEqual(
            self.conn.execute(
                "select publication_ref from tl_read.notice"
                " where source_package_id = 'daily/202300220'"
            ).fetchone()[0],
            "694329-2023",
        )
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_read.verification_attempt"
            ).fetchone()[0],
            0,
        )

    def test_upgrade_from_0004_leaves_a_v1_capture_with_no_reference_status(self):
        self.assertEqual(
            db.migrate(self.conn, up_to="0004_package_ingest"),
            [
                "0001_core", "0002_capture_batch_size",
                "0003_source_verification", "0004_package_ingest",
            ],
        )
        capture_id = self.conn.execute(
            "insert into tl_work.capture (source_package_id, artifact_sha256, artifact_bytes,"
            " contract_version, status, member_count, distinct_notice_count, loaded_row_count,"
            " batch_size) values ('daily/202300220', 'abc123', 100, '1', 'published', 1, 1, 1, 500)"
            " returning capture_id"
        ).fetchone()[0]
        self.conn.execute(
            "insert into tl_work.notice_capture (capture_id, publication_year, publication_number,"
            " batch_ordinal, source_format, schema_version, source_filename, publication_date,"
            " publication_date_raw, buyer_country_status, primary_cpv_status)"
            " values (%s, 2023, 694329, 0, 'eforms', 'eforms-sdk-1.9', '694329_2023.xml',"
            " '2023-11-15', '2023-11-15Z', 'absent', 'absent')",
            (capture_id,),
        )
        self.conn.execute(
            "insert into tl_work.published_capture (source_package_id, capture_id)"
            " values ('daily/202300220', %s)",
            (capture_id,),
        )

        self.assertEqual(db.migrate(self.conn), ["0005_projection_contract_v2"])

        # NULL, not backfilled to 'absent': the v1 row was never projected under
        # contract v2, so nothing here should claim it published no references.
        row = self.conn.execute(
            "select change_reference_status from tl_work.notice_capture"
            " where capture_id = %s", (capture_id,)
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(
            self.conn.execute(
                "select publication_ref, change_reference_status from tl_read.notice"
                " where source_package_id = 'daily/202300220'"
            ).fetchone(),
            ("694329-2023", None),
        )
        # complete-history surfaces exist and agree
        self.assertEqual(
            self.conn.execute(
                "select change_reference_status from tl_read.notice_history"
                " where capture_id = %s", (capture_id,)
            ).fetchone(),
            (None,),
        )
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_read.notice_change_reference_history"
                " where capture_id = %s", (capture_id,)
            ).fetchone()[0],
            0,
        )
        reader = db.connect(load_config(dbname=self.UPGRADE_DB))
        self.addCleanup(reader.close)
        reader.execute("set role tender_ledger_reader")
        self.assertEqual(
            reader.execute("select count(*) from tl_read.notice_history").fetchone()[0], 1
        )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            reader.execute("select * from tl_work.notice_change_reference")

    def test_clean_install_applies_every_migration(self):
        applied = db.migrate(self.conn)
        self.assertEqual(
            applied,
            [
                "0001_core", "0002_capture_batch_size",
                "0003_source_verification", "0004_package_ingest",
                "0005_projection_contract_v2",
            ],
        )
        self.assertIn(
            "batch_size",
            {
                r[0]
                for r in self.conn.execute(
                    "select column_name from information_schema.columns"
                    " where table_schema = 'tl_work' and table_name = 'capture'"
                )
            },
        )
        self.assertEqual(
            self.conn.execute(
                "select to_regclass('tl_work.verification_attempt') is not null"
            ).fetchone()[0],
            True,
        )
        self.assertEqual(
            self.conn.execute(
                "select to_regclass('tl_work.notice_change_reference') is not null"
            ).fetchone()[0],
            True,
        )
        self.assertEqual(
            self.conn.execute(
                "select to_regclass('tl_read.notice_history') is not null,"
                " to_regclass('tl_read.notice_change_reference_history') is not null"
            ).fetchone(),
            (True, True),
        )

    def test_reapplying_0005_after_a_clean_install_is_a_no_op(self):
        db.migrate(self.conn)
        self.assertEqual(db.migrate(self.conn), [])


class ConnectionContractTests(LoaderTestCase):
    """The loader owns its transactions, so it refuses a connection that has one.

    An autocommit connection inside ``with conn.transaction()`` keeps
    ``autocommit`` True while every block below becomes a savepoint: the loader
    would report a publish that no other session can see and that the caller's
    rollback would erase.
    """

    def _own_work(self, conn):
        conn.execute(
            "insert into tl_work.capture (source_package_id, artifact_sha256,"
            " artifact_bytes, contract_version, status)"
            " values ('caller/own-work', 'aa', 1, 'caller', 'failed')"
        )

    def test_load_inside_a_caller_transaction_is_refused_and_keeps_that_work(self):
        pkg = self.package("t", [legacy_member(1), legacy_member(2)])
        writer = self.new_conn()
        observer = self.new_conn()

        with writer.transaction():
            self._own_work(writer)
            with self.assertRaises(repo.ConnectionStateError):
                load_package(writer, pkg, "daily/outer")
            # refused before writing or locking: nothing exists outside, and the
            # advisory lock is still available to another session
            self.assertEqual(observer.execute(
                "select count(*) from tl_work.capture where source_package_id = %s",
                ("daily/outer",),
            ).fetchone()[0], 0)
            self.assertTrue(self.lock_is_free("daily/outer", observer))

        # the caller's own transaction was neither committed nor rolled back for it
        self.assertEqual(self.captures("caller/own-work", observer), 1)
        self.assertEqual(self.captures("daily/outer", observer), 0)

    def test_repository_mutators_refuse_a_caller_transaction(self):
        pkg = self.package("t", [legacy_member(1), legacy_member(2)])
        sha, size = _digest(pkg)
        setup = self.new_conn()
        capture_id = repo.begin_capture(
            setup, "daily/mutators", sha, size, batch_size=2
        ).capture.capture_id
        rows = projected_rows(pkg)

        writer = self.new_conn()
        with writer.transaction():
            self._own_work(writer)
            calls = {
                "begin_capture": lambda: repo.begin_capture(
                    writer, "daily/other", sha, size, batch_size=2
                ),
                "load_batch": lambda: repo.load_batch(writer, capture_id, 0, rows),
                "clear_uncommitted_rows": lambda: repo.clear_uncommitted_rows(
                    writer, capture_id
                ),
                "reconcile": lambda: repo.reconcile(writer, capture_id, 2, 2),
                "publish": lambda: repo.publish(writer, capture_id),
                "fail_capture": lambda: repo.fail_capture(writer, capture_id, "x"),
                "migrate": lambda: db.migrate(writer),
            }
            for name, call in calls.items():
                with self.subTest(operation=name), self.assertRaises(
                    repo.ConnectionStateError
                ):
                    call()

        # every refusal happened before touching the database, so the caller's
        # transaction stayed usable and committed
        self.assertEqual(self.captures("caller/own-work"), 1)
        self.assertEqual(self.captures("daily/other"), 0)
        self.assertEqual(self.persisted(capture_id), ("acquiring", 0, 0))

    def test_a_connection_without_autocommit_is_refused(self):
        pkg = self.package("t", [legacy_member(1)])
        manual = self.new_conn(autocommit=False)
        with self.assertRaises(repo.ConnectionStateError):
            load_package(manual, pkg, "daily/manual")
        self.assertEqual(self.captures("daily/manual"), 0)


class TransientFailureTests(LoaderTestCase):
    """A database error that cancels work in flight is not the same as one that
    condemns the artifact."""

    def _cancel_before(self, ordinal):
        """Make PostgreSQL cancel a real statement just before that batch."""
        original = repo.load_batch

        def load_batch(conn, capture_id, batch_ordinal, notices):
            if batch_ordinal == ordinal:
                conn.execute("set statement_timeout = '10ms'")
                try:
                    conn.execute("select pg_sleep(0.5)")
                finally:
                    conn.execute("set statement_timeout = 0")
            return original(conn, capture_id, batch_ordinal, notices)

        return mock.patch.object(repo, "load_batch", side_effect=load_batch)

    def test_a_cancelled_statement_keeps_the_capture_and_its_committed_batch(self):
        pkg = self.package("t", [legacy_member(100 + i) for i in range(5)])
        with self._cancel_before(1):
            interrupted = load_package(self.new_conn(), pkg, "daily/cancel", batch_size=2)

        self.assertEqual(interrupted.status, "loading")
        self.assertIn("QueryCanceled", interrupted.load_error or "")
        self.assertIsNone(interrupted.failure_reason)
        self.assertEqual(self.persisted(interrupted.capture_id), ("loading", 1, 2))

        written = []
        original = repo.load_batch

        def spy(conn, capture_id, ordinal, notices):
            written.append(ordinal)
            return original(conn, capture_id, ordinal, notices)

        with mock.patch.object(repo, "load_batch", side_effect=spy):
            retry = load_package(self.new_conn(), pkg, "daily/cancel", batch_size=2)

        self.assertEqual(retry.capture_id, interrupted.capture_id)
        self.assertEqual(retry.acquisition_ordinal, interrupted.acquisition_ordinal)
        self.assertTrue(retry.resumed)
        self.assertEqual(retry.status, "published")
        self.assertEqual(written, [1, 2])  # batch 0 was not rewritten
        self.assertEqual(self.captures("daily/cancel"), 1)

        clean = load_package(self.new_conn(), pkg, "daily/clean", batch_size=2)
        self.assertEqual(
            _notice_rows(self.conn, retry.capture_id),
            _notice_rows(self.conn, clean.capture_id),
        )

    def test_a_duplicate_identity_stays_terminal_and_is_never_resumed(self):
        pkg = self.package("dup", [legacy_member(7), eforms_member(7)])
        first = load_package(self.new_conn(), pkg, "daily/terminal", batch_size=10)
        self.assertEqual(first.status, "failed")
        self.assertIsNone(first.load_error)

        retry = load_package(self.new_conn(), pkg, "daily/terminal", batch_size=10)
        self.assertEqual(retry.status, "failed")
        self.assertNotEqual(retry.capture_id, first.capture_id)
        self.assertFalse(retry.resumed)

    def test_classification_of_the_errors_that_decide_recoverability(self):
        from psycopg import errors

        transient = (
            errors.QueryCanceled("cancelled"),          # statement_timeout
            errors.DeadlockDetected("deadlock"),
            errors.SerializationFailure("conflict"),
            errors.AdminShutdown("server restarting"),
            errors.ConnectionFailure("gone"),
            psycopg.OperationalError("connection closed"),  # no sqlstate at all
        )
        terminal = (
            errors.UniqueViolation("duplicate key"),
            errors.CheckViolation("violates check"),
            errors.NotNullViolation("null value"),
            errors.UndefinedTable("no such relation"),
        )
        for exc in transient:
            with self.subTest(error=type(exc).__name__):
                self.assertTrue(_is_transient(exc))
        for exc in terminal:
            with self.subTest(error=type(exc).__name__):
                self.assertFalse(_is_transient(exc))


class InterruptedRecaptureTests(LoaderTestCase):
    """A retry resumes the current attempt, not the capture it was replacing."""

    def test_recapture_of_identical_bytes_resumes_after_a_failed_publish(self):
        pkg = self.package("a", [legacy_member(1), legacy_member(2)])
        first = load_package(self.new_conn(), pkg, "daily/again")

        forced = load_package(
            self.new_conn(), pkg, "daily/again", force_recapture=True,
            before_publish=lambda c: c.execute("select 1 / 0"),
        )
        self.assertEqual(forced.status, "loaded")
        self.assertNotEqual(forced.capture_id, first.capture_id)

        retry = load_package(self.new_conn(), pkg, "daily/again")
        self.assertEqual(retry.capture_id, forced.capture_id)
        self.assertEqual(retry.status, "published")
        self.assertTrue(retry.resumed)
        self.assertEqual(self.capture_status(first.capture_id), "superseded")
        self.assertEqual(self.captures("daily/again"), 2)
        self.assertEqual(self.notices(package="daily/again"), ["1-2023", "2-2023"])

    def test_recapture_of_identical_bytes_resumes_from_its_committed_batches(self):
        pkg = self.package("a", [legacy_member(100 + i) for i in range(5)])
        first = load_package(self.new_conn(), pkg, "daily/partial", batch_size=2)

        sha, size = _digest(pkg)
        writer = self.new_conn()
        begin = repo.begin_capture(
            writer, "daily/partial", sha, size, batch_size=2, force_recapture=True
        )
        repo.load_batch(writer, begin.capture.capture_id, 0, projected_rows(pkg)[:2])
        writer.close()  # crash during the intentional recapture

        retry = load_package(self.new_conn(), pkg, "daily/partial", batch_size=2)
        self.assertEqual(retry.capture_id, begin.capture.capture_id)
        self.assertEqual(retry.status, "published")
        self.assertEqual(self.capture_status(first.capture_id), "superseded")
        self.assertEqual(self.captures("daily/partial"), 2)

    def test_an_attempt_a_later_capture_overtook_is_not_resurrected(self):
        pkg_a = self.package("a", [legacy_member(100 + i) for i in range(4)])
        pkg_b = self.package("b", [legacy_member(200 + i) for i in range(4)])
        sha_a, size_a = _digest(pkg_a)

        abandoned = self.new_conn()
        begin = repo.begin_capture(abandoned, "daily/overtaken", sha_a, size_a, batch_size=2)
        repo.load_batch(abandoned, begin.capture.capture_id, 0, projected_rows(pkg_a)[:2])
        abandoned.close()

        later = load_package(self.new_conn(), pkg_b, "daily/overtaken", batch_size=2)
        self.assertEqual(later.status, "published")

        again = load_package(self.new_conn(), pkg_a, "daily/overtaken", batch_size=2)
        self.assertEqual(again.status, "published")
        self.assertNotEqual(again.capture_id, begin.capture.capture_id)
        self.assertGreater(again.acquisition_ordinal, later.acquisition_ordinal)
        # the overtaken attempt is left exactly as it was, and stays invisible
        self.assertEqual(self.persisted(begin.capture.capture_id), ("loading", 1, 2))
        self.assertEqual(
            self.notices(package="daily/overtaken"), [f"{100 + i}-2023" for i in range(4)]
        )


def _notice_rows(conn, capture_id):
    return conn.execute(
        "select publication_year, publication_number, source_format, schema_version,"
        " source_filename, publication_date, dispatch_date, buyer_country,"
        " buyer_country_iso, buyer_country_status, primary_cpv, primary_cpv_status,"
        " additional_cpv, change_reference_status"
        " from tl_work.notice_capture where capture_id = %s"
        " order by publication_year, publication_number",
        (capture_id,),
    ).fetchall()


def _reference_rows(conn, capture_id):
    return conn.execute(
        "select publication_year, publication_number, ordinal, value, scheme_name"
        " from tl_work.notice_change_reference where capture_id = %s"
        " order by publication_year, publication_number, ordinal",
        (capture_id,),
    ).fetchall()


if __name__ == "__main__":
    unittest.main()
