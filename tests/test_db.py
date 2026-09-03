"""Integration tests for the transactional loader against a real PostgreSQL.

These need the local Compose database running. They use a dedicated
``tender_ledger_test`` database, never the development one, and never touch its
volume. If the server is unreachable the module skips with a clear message
rather than passing silently.
"""

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
from tender_ledger.loader import digest_archive, load_package
from tender_ledger.packages import stream_notices
from tender_ledger.projection import project_member


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

        self.assertEqual(db.migrate(self.conn), ["0002_capture_batch_size"])

        row = self.conn.execute(
            "select status, is_published, member_count, batch_size"
            " from tl_read.capture_status where capture_id = %s", (capture_id,)
        ).fetchone()
        self.assertEqual(row, ("published", True, 1, None))
        self.assertEqual(
            self.conn.execute(
                "select publication_ref from tl_read.notice where source_package_id = 'daily/old'"
            ).fetchone()[0],
            "995-2020",
        )

    def test_clean_install_applies_every_migration(self):
        applied = db.migrate(self.conn)
        self.assertEqual(applied, ["0001_core", "0002_capture_batch_size"])
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


def _notice_rows(conn, capture_id):
    return conn.execute(
        "select publication_year, publication_number, source_format, schema_version,"
        " source_filename, publication_date, dispatch_date, buyer_country,"
        " buyer_country_iso, buyer_country_status, primary_cpv, primary_cpv_status,"
        " additional_cpv from tl_work.notice_capture where capture_id = %s"
        " order by publication_year, publication_number",
        (capture_id,),
    ).fetchall()


if __name__ == "__main__":
    unittest.main()
