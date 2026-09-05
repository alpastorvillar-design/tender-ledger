"""The daily ingest flow against a real PostgreSQL and a local HTTP server.

TED is never contacted: the package comes from a local socket and the Search API
from a scripted transport. What needs the real database is everything the flow
claims -- the run history, the artifact/capture/attempt links, the advisory lock
shared with the loader and the verifier, the checkpoint transaction, and what a
second connection sees while a run is interrupted.
"""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import psycopg
from psycopg import sql

from ted_fixtures import (
    DEFAULT_OJS,
    TEST_DB,
    FakeTransport,
    api_page,
    eforms_member,
    ensure_test_database,
    legacy_member,
    package_bytes,
    truncate_all,
    write_package,
)
from tender_ledger import db, ingest
from tender_ledger.config import load_config
from tender_ledger.db import repository as repo
from tender_ledger.download import DownloadBudgets, artifact_destination
from tender_ledger.ingest import ingest_package
from tender_ledger.loader import load_package
from tender_ledger.package_contract import UnsupportedPackage
from tender_ledger.verification import verify_capture

PACKAGE = "daily/202300220"
OTHER_PACKAGE = "daily/202300221"
OTHER_OJS = "221/2023"


def setUpModule():
    ensure_test_database()


def source(numbers, *, ojs=DEFAULT_OJS):
    """One page of records plus the terminal empty page TED still tokenises."""
    total = len(numbers)
    pages = [api_page(numbers, total=total, ojs=ojs, token="page-2")]
    if numbers:
        pages.append(api_page([], total=total, ojs=ojs, token="still-here"))
    return pages


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # BaseHTTPRequestHandler's required naming
        self.server.received.append(self.path)
        self.server.reply(self)

    def log_message(self, *args):
        pass


def serve(payload, *, status=200):
    def reply(handler):
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    return reply


class IngestTestCase(unittest.TestCase):
    NUMBERS = (1001, 1002, 1003, 1004, 1005)

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = self.new_conn()
        truncate_all(self.conn)

        self.archive = package_bytes([legacy_member(n) for n in self.NUMBERS])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, address: None
        self.server.received = []
        self.server.reply = serve(self.archive)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/packages/{PACKAGE}"
        self.transport = None

    def new_conn(self, **kw) -> psycopg.Connection:
        conn = db.connect(load_config(dbname=TEST_DB), **kw)
        self.addCleanup(conn.close)
        return conn

    def ingest(self, conn=None, *, package=PACKAGE, numbers=None, script=None, **kwargs):
        numbers = self.NUMBERS if numbers is None else numbers
        self.transport = FakeTransport(
            source(list(numbers)) if script is None else script
        )
        kwargs.setdefault("url", self.url)
        kwargs.setdefault("budgets", DownloadBudgets(backoff_base_seconds=0.01))
        return ingest_package(
            conn or self.new_conn(), package,
            data_root=self.root, transport=self.transport, **kwargs
        )

    def destination(self, package=PACKAGE):
        return artifact_destination(self.root, package)

    def run_rows(self, package=PACKAGE, conn=None):
        conn = conn or self.conn
        with conn.cursor() as cur:
            cur.execute(
                "select run_id, attempt_ordinal, phase, artifact_sha256, capture_id,"
                " verification_attempt_id, completed_at, last_error"
                " from tl_work.ingest_run where source_package_id = %s order by run_id",
                (package,),
            )
            names = [d.name for d in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def checkpoint(self, package=PACKAGE, conn=None):
        conn = conn or self.conn
        with conn.cursor() as cur:
            cur.execute(
                "select run_id, capture_id, verification_attempt_id, artifact_sha256,"
                " contract_version, notice_count from tl_work.package_checkpoint"
                " where source_package_id = %s",
                (package,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip([d.name for d in cur.description], row, strict=True))

    def status(self, package=PACKAGE, conn=None):
        conn = conn or self.conn
        with conn.cursor() as cur:
            cur.execute(
                "select * from tl_read.package_ingest_status where source_package_id = %s",
                (package,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip([d.name for d in cur.description], row, strict=True))

    def notices(self, package=PACKAGE):
        return sorted(
            r[0] for r in self.conn.execute(
                "select publication_ref from tl_read.notice where source_package_id = %s",
                (package,),
            )
        )

    def batches(self, capture_id):
        return self.conn.execute(
            "select count(*) from tl_work.capture_batch where capture_id = %s", (capture_id,)
        ).fetchone()[0]

    def attempts(self, capture_id=None):
        sql_text = "select attempt_id, capture_id, state from tl_work.verification_attempt"
        args: tuple = ()
        if capture_id is not None:
            sql_text += " where capture_id = %s"
            args = (capture_id,)
        return self.conn.execute(sql_text + " order by attempt_id", args).fetchall()

    def lock_is_free(self, package=PACKAGE, conn=None):
        conn = conn or self.new_conn()
        got = conn.execute(
            "select pg_try_advisory_lock(hashtext(%s)::int8)", (package,)
        ).fetchone()[0]
        if got:
            conn.execute("select pg_advisory_unlock(hashtext(%s)::int8)", (package,))
        return got

    def held_locks(self, conn):
        return conn.execute(
            "select count(*) from pg_locks where locktype = 'advisory'"
            " and pid = pg_backend_pid()"
        ).fetchone()[0]

    def part_files(self):
        return sorted(p.name for p in self.destination().parent.glob("*.part-*"))


class HappyPathTests(IngestTestCase):
    def test_one_command_downloads_loads_verifies_and_checkpoints(self):
        result = self.ingest()

        self.assertEqual(result.outcome, "processed")
        self.assertEqual(result.phase, "completed")
        self.assertEqual(result.artifact_action, "downloaded")
        self.assertTrue(result.checkpoint_is_current)
        self.assertEqual(result.notice_count, 5)
        self.assertEqual(self.notices(), [f"{n}-2023" for n in self.NUMBERS])
        self.assertEqual(self.part_files(), [])

        # read back through a different connection: a returned success means a
        # committed one
        seen = self.checkpoint(conn=self.new_conn())
        self.assertEqual(seen["capture_id"], result.capture_id)
        self.assertEqual(seen["verification_attempt_id"], result.verification_attempt_id)
        self.assertEqual(seen["artifact_sha256"], result.artifact_sha256)
        self.assertEqual(seen["notice_count"], 5)

        status = self.status(conn=self.new_conn())
        self.assertTrue(status["checkpoint_is_current"])
        self.assertTrue(status["capture_still_published"])
        self.assertIsNone(status["open_run_id"])

    def test_a_replay_certifies_the_stored_evidence_without_touching_the_network(self):
        first = self.ingest()
        requests_before = len(self.server.received)

        replay = self.ingest(script=[])  # any API call would raise from the script
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(replay.artifact_action, "replayed")
        self.assertEqual(replay.run_id, first.run_id)
        self.assertEqual(replay.capture_id, first.capture_id)
        self.assertTrue(replay.checkpoint_is_current)

        self.assertEqual(len(self.server.received), requests_before)
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(len(self.run_rows()), 1)
        self.assertEqual(self.batches(first.capture_id), 1)
        self.assertEqual(self.notices(), [f"{n}-2023" for n in self.NUMBERS])
        self.assertEqual(self.attempts(), self.attempts(first.capture_id))
        # a current v2 checkpoint replays with no new capture and no new batches
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.capture where source_package_id = %s",
                (PACKAGE,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.capture_batch"
            ).fetchone()[0],
            1,
        )

    def test_a_replay_does_not_move_the_completion_timestamp(self):
        self.ingest()
        completed = self.run_rows()[0]["completed_at"]
        sealed = self.conn.execute(
            "select sealed_at from tl_work.package_checkpoint where source_package_id = %s",
            (PACKAGE,),
        ).fetchone()[0]

        self.ingest(script=[])
        self.assertEqual(self.run_rows()[0]["completed_at"], completed)
        self.assertEqual(
            self.conn.execute(
                "select sealed_at from tl_work.package_checkpoint"
                " where source_package_id = %s", (PACKAGE,)
            ).fetchone()[0],
            sealed,
        )

    def test_the_reader_role_sees_the_status_but_not_the_working_tables(self):
        self.ingest()
        reader = self.new_conn()
        reader.execute("set role tender_ledger_reader")
        row = reader.execute(
            "select checkpoint_is_current, checkpoint_notice_count"
            " from tl_read.package_ingest_status where source_package_id = %s",
            (PACKAGE,),
        ).fetchone()
        self.assertEqual(row, (True, 5))
        self.assertEqual(
            reader.execute(
                "select count(*) from tl_read.ingest_run where source_package_id = %s",
                (PACKAGE,),
            ).fetchone()[0],
            1,
        )
        for table in ("tl_work.ingest_run", "tl_work.package_checkpoint"):
            with self.subTest(table=table), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                reader.execute(f"select count(*) from {table}")
        reader.execute("reset role")


class SourceFailureTests(IngestTestCase):
    def assert_nothing_processed(self, result):
        self.assertEqual(result.outcome, "incomplete")
        self.assertFalse(result.checkpoint_is_current)
        self.assertIsNone(self.checkpoint())
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.part_files(), [])

    def test_a_404_is_not_an_empty_window(self):
        self.server.reply = serve(b"not found", status=404)
        self.assert_nothing_processed(self.ingest())
        self.assertIn("404", self.run_rows()[0]["last_error"])

    def test_html_with_a_200_never_becomes_a_processed_package(self):
        self.server.reply = serve(b"<html>maintenance</html>")
        self.assert_nothing_processed(self.ingest())

    def test_a_corrupt_archive_never_becomes_a_processed_package(self):
        broken = bytearray(self.archive)
        broken[-3] ^= 0xFF
        self.server.reply = serve(bytes(broken))
        self.assert_nothing_processed(self.ingest())

    def test_a_body_that_transferred_and_was_refused_reports_what_it_cost(self):
        # The failure a manifest report shows must not read like a package that
        # never reached the network: the whole body arrived and was rejected.
        self.server.reply = serve(b"<html>maintenance</html>")
        result = self.ingest()
        self.assert_nothing_processed(result)
        self.assertEqual(result.http_attempts, 1)
        self.assertEqual(result.downloaded_bytes, len(b"<html>maintenance</html>"))

    def test_an_unreachable_source_reports_its_attempts_and_no_bytes(self):
        self.server.reply = serve(b"busy", status=503)
        result = self.ingest(budgets=DownloadBudgets(
            max_attempts=2, backoff_base_seconds=0.01, total_seconds=30
        ))
        self.assert_nothing_processed(result)
        self.assertEqual(result.http_attempts, 2)
        self.assertEqual(result.downloaded_bytes, 0)

    def test_an_exhausted_source_leaves_a_resumable_run(self):
        self.server.reply = serve(b"busy", status=503)
        result = self.ingest(budgets=DownloadBudgets(
            max_attempts=2, backoff_base_seconds=0.01, total_seconds=30
        ))
        self.assert_nothing_processed(result)
        self.assertEqual(self.run_rows()[0]["phase"], "starting")
        self.assertEqual(len(self.server.received), 2)

        self.server.reply = serve(self.archive)
        retry = self.ingest()
        self.assertEqual(retry.outcome, "processed")
        self.assertEqual(retry.run_id, result.run_id)  # the same run, resumed
        self.assertTrue(retry.resumed)


class ArtifactRecoveryTests(IngestTestCase):
    def test_a_crash_after_the_rename_adopts_the_file_instead_of_downloading_again(self):
        with mock.patch.object(
            ingest, "_record_artifact", side_effect=RuntimeError("crash after rename")
        ), self.assertRaises(RuntimeError):
            self.ingest()

        self.assertTrue(self.destination().is_file())
        self.assertEqual(self.run_rows()[0]["artifact_sha256"], None)
        self.assertEqual(len(self.server.received), 1)

        recovered = self.ingest()
        self.assertEqual(recovered.outcome, "processed")
        self.assertEqual(recovered.artifact_action, "adopted")
        self.assertEqual(len(self.server.received), 1)  # no second GET

    def test_a_crash_before_the_rename_leaves_no_artifact_and_restarts_cleanly(self):
        def hang_up(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(self.archive) + 1000))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(self.archive[:100])
            handler.close_connection = True

        self.server.reply = hang_up
        result = self.ingest(budgets=DownloadBudgets(max_attempts=1))
        self.assertEqual(result.outcome, "incomplete")
        self.assertFalse(self.destination().exists())
        self.assertEqual(self.part_files(), [])

        self.server.reply = serve(self.archive)
        self.assertEqual(self.ingest().outcome, "processed")

    def test_a_recorded_artifact_is_reused_rather_than_downloaded_again(self):
        with mock.patch.object(
            ingest, "_load", side_effect=RuntimeError("crash after the artifact")
        ), self.assertRaises(RuntimeError):
            self.ingest()
        self.assertEqual(self.run_rows()[0]["phase"], "artifact_ready")

        resumed = self.ingest()
        self.assertEqual(resumed.artifact_action, "reused")
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(resumed.outcome, "processed")

    def test_a_corrupt_cached_artifact_is_re_acquired_rather_than_trusted(self):
        first = self.ingest()
        self.assertEqual(first.outcome, "processed")
        self.destination().write_bytes(b"not an archive any more")

        again = self.ingest(script=[])
        self.assertEqual(again.artifact_action, "downloaded")
        self.assertEqual(len(self.server.received), 2)
        self.assertEqual(again.outcome, "processed")
        self.assertEqual(again.capture_id, first.capture_id)  # identical bytes, same capture
        self.assertEqual(self.destination().read_bytes(), self.archive)

    def test_a_missing_cached_artifact_is_re_acquired_rather_than_replayed_from_the_database(self):
        first = self.ingest()
        self.destination().unlink()

        again = self.ingest(script=[])
        self.assertEqual(again.outcome, "processed")
        self.assertEqual(again.artifact_action, "downloaded")
        self.assertNotEqual(again.run_id, first.run_id)  # a new run did the recovery
        self.assertEqual(self.notices(), [f"{n}-2023" for n in self.NUMBERS])


class LoadRecoveryTests(IngestTestCase):
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

    def test_the_run_is_linked_to_its_capture_before_the_first_batch_commits(self):
        with self._cancel_before(0):
            result = self.ingest(batch_size=2)

        self.assertEqual(result.outcome, "incomplete")
        run = self.run_rows()[0]
        self.assertEqual(run["phase"], "artifact_ready")
        self.assertIsNotNone(run["capture_id"])  # durable link, not guessed later
        self.assertEqual(self.batches(run["capture_id"]), 0)
        self.assertIsNone(self.checkpoint())

        retry = self.ingest(batch_size=2)
        self.assertEqual(retry.outcome, "processed")
        self.assertEqual(retry.capture_id, run["capture_id"])
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.capture where source_package_id = %s",
                (PACKAGE,),
            ).fetchone()[0],
            1,
        )

    def test_a_durable_batch_survives_and_is_not_rewritten(self):
        with self._cancel_before(1):
            interrupted = self.ingest(batch_size=2)
        self.assertEqual(interrupted.outcome, "incomplete")
        capture_id = self.run_rows()[0]["capture_id"]
        self.assertEqual(self.batches(capture_id), 1)

        written = []
        original = repo.load_batch

        def spy(conn, cid, ordinal, notices):
            written.append(ordinal)
            return original(conn, cid, ordinal, notices)

        with mock.patch.object(repo, "load_batch", side_effect=spy):
            retry = self.ingest(batch_size=2)

        self.assertEqual(retry.outcome, "processed")
        self.assertEqual(written, [1, 2])
        self.assertEqual(retry.capture_id, capture_id)
        self.assertEqual(self.notices(), [f"{n}-2023" for n in self.NUMBERS])

    def test_a_repeated_publication_is_rejected_before_anything_is_loaded(self):
        # Validation walks the archive, so a package repeating one canonical
        # identity never reaches the loader or the database.
        self.archive = package_bytes([legacy_member(7), eforms_member(7)])
        self.server.reply = serve(self.archive)
        result = self.ingest(numbers=[7])
        self.assertEqual(result.outcome, "incomplete")
        self.assertIn("Duplicate publication", result.error)
        self.assertFalse(self.destination().exists())
        self.assertEqual(
            self.conn.execute("select count(*) from tl_work.capture").fetchone()[0], 0
        )
        self.assertIsNone(self.checkpoint())

    def test_a_terminal_load_failure_closes_the_run_instead_of_resuming_it(self):
        with mock.patch.object(
            repo, "load_batch",
            side_effect=psycopg.errors.UniqueViolation("duplicate key"),
        ):
            result = self.ingest(batch_size=2)

        self.assertEqual(result.outcome, "incomplete")
        first = self.run_rows()[0]
        self.assertEqual(first["phase"], "failed")
        self.assertIn("UniqueViolation", first["last_error"])
        self.assertIsNone(self.checkpoint())
        self.assertEqual(
            self.conn.execute(
                "select status from tl_work.capture where capture_id = %s",
                (first["capture_id"],),
            ).fetchone()[0],
            "failed",
        )

        retry = self.ingest(batch_size=2)
        self.assertEqual(retry.outcome, "processed")
        self.assertNotEqual(retry.run_id, first["run_id"])  # a failed run is never resumed
        self.assertNotEqual(retry.capture_id, first["capture_id"])
        self.assertEqual(self.run_rows()[1]["attempt_ordinal"], 2)

    def test_a_crash_between_publish_and_verification_resumes_without_reloading(self):
        with mock.patch.object(
            ingest, "_ensure_coverage",
            side_effect=psycopg.OperationalError("connection lost"),
        ), self.assertRaises(psycopg.OperationalError):
            self.ingest()

        run = self.run_rows()[0]
        self.assertEqual(run["phase"], "capture_published")
        self.assertIsNone(self.checkpoint())
        self.assertEqual(self.notices(), [f"{n}-2023" for n in self.NUMBERS])

        resumed = self.ingest()
        self.assertEqual(resumed.outcome, "processed")
        self.assertEqual(resumed.capture_id, run["capture_id"])
        self.assertEqual(resumed.artifact_action, "reused")
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(self.batches(run["capture_id"]), 1)

    def test_recovery_produces_the_same_rows_as_a_clean_run(self):
        with self._cancel_before(1):
            self.ingest(batch_size=2)
        recovered = self.ingest(batch_size=2)

        clean_dir = TemporaryDirectory()
        self.addCleanup(clean_dir.cleanup)
        clean = ingest_package(
            self.new_conn(), OTHER_PACKAGE, data_root=Path(clean_dir.name),
            url=self.url, transport=FakeTransport(source(list(self.NUMBERS), ojs=OTHER_OJS)),
        )
        self.assertEqual(clean.outcome, "processed")
        self.assertEqual(
            _rows(self.conn, recovered.capture_id), _rows(self.conn, clean.capture_id)
        )


class CoverageGateTests(IngestTestCase):
    def assert_no_checkpoint(self, result):
        self.assertEqual(result.outcome, "incomplete")
        self.assertIsNone(self.checkpoint())
        self.assertFalse(result.checkpoint_is_current)

    def test_a_mismatch_publishes_the_capture_but_never_checkpoints_it(self):
        result = self.ingest(script=source([1001, 1002]))
        self.assert_no_checkpoint(result)
        self.assertEqual(self.notices(), [f"{n}-2023" for n in self.NUMBERS])
        self.assertEqual(self.attempts()[0][2], "mismatch")
        self.assertEqual(self.run_rows()[0]["phase"], "capture_published")

    def test_an_unavailable_source_never_checkpoints(self):
        self.assert_no_checkpoint(self.ingest(script=[api_page([], total=9, token=None)]))
        self.assertEqual(self.attempts()[0][2], "unavailable")

    def test_an_empty_unconfirmed_window_never_checkpoints(self):
        self.archive = package_bytes([])
        self.server.reply = serve(self.archive)
        self.assert_no_checkpoint(self.ingest(script=[api_page([], total=0, token=None)]))
        self.assertEqual(self.attempts()[0][2], "empty_unconfirmed")

    def test_a_retry_after_a_failed_check_opens_a_new_attempt(self):
        self.ingest(script=[api_page([], total=9, token=None)])
        first = self.attempts()
        self.assertEqual(len(first), 1)

        retry = self.ingest()
        self.assertEqual(retry.outcome, "processed")
        all_attempts = self.attempts()
        self.assertEqual(len(all_attempts), 2)
        self.assertEqual([a[2] for a in all_attempts], ["unavailable", "verified"])
        self.assertEqual(self.checkpoint()["verification_attempt_id"], all_attempts[1][0])

    def test_a_crash_during_the_check_leaves_the_attempt_visible(self):
        boom = FakeTransport([RuntimeError("dropped mid-walk")])
        with self.assertRaises(RuntimeError):
            ingest_package(
                self.new_conn(), PACKAGE, data_root=self.root, url=self.url,
                transport=boom,
            )
        attempts = self.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][2], "in_progress")
        self.assertIsNone(self.checkpoint())
        self.assertTrue(self.lock_is_free())

    def test_a_confirmed_check_is_reused_to_seal_a_pending_checkpoint(self):
        with mock.patch.object(
            ingest, "_seal", side_effect=psycopg.OperationalError("connection lost")
        ), self.assertRaises(psycopg.OperationalError):
            self.ingest()

        run = self.run_rows()[0]
        self.assertEqual(run["phase"], "source_verified")
        self.assertIsNone(self.checkpoint())

        resumed = self.ingest(script=[])  # no API call is allowed by the script
        self.assertEqual(resumed.outcome, "processed")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(
            self.checkpoint()["verification_attempt_id"], run["verification_attempt_id"]
        )

    def test_a_pending_run_whose_capture_was_replaced_drops_the_stale_attempt(self):
        with mock.patch.object(
            ingest, "_seal", side_effect=psycopg.OperationalError("connection lost")
        ), self.assertRaises(psycopg.OperationalError):
            self.ingest()
        pending = self.run_rows()[0]
        self.assertEqual(pending["phase"], "source_verified")

        # someone re-acquires the package from different bytes while the run waits
        other = write_package(self.root / "other.tar.gz", [legacy_member(2001)])
        replacement = load_package(
            self.new_conn(), other, PACKAGE, force_recapture=True
        )
        self.assertEqual(replacement.status, "published")

        resumed = self.ingest()
        self.assertEqual(resumed.outcome, "processed")
        self.assertEqual(resumed.run_id, pending["run_id"])
        self.assertNotEqual(resumed.capture_id, pending["capture_id"])
        self.assertNotEqual(
            resumed.verification_attempt_id, pending["verification_attempt_id"]
        )
        self.assertEqual(self.checkpoint()["capture_id"], resumed.capture_id)
        self.assertEqual(self.notices(), [f"{n}-2023" for n in self.NUMBERS])

    def test_a_lost_session_at_the_seal_reports_no_success_and_writes_no_checkpoint(self):
        victim = self.new_conn()
        killer = self.new_conn()
        original = ingest._seal

        def terminate_then_seal(conn, *args, **kwargs):
            killer.execute(
                "select pg_terminate_backend(%s)", (conn.info.backend_pid,)
            )
            return original(conn, *args, **kwargs)

        with mock.patch.object(ingest, "_seal", side_effect=terminate_then_seal), \
                self.assertRaises(psycopg.Error):
            self.ingest(conn=victim)

        observer = self.new_conn()
        self.assertIsNone(self.checkpoint(conn=observer))
        self.assertEqual(self.run_rows(conn=observer)[0]["phase"], "source_verified")
        self.assertTrue(self.lock_is_free(conn=observer))

        resumed = self.ingest(script=[])
        self.assertEqual(resumed.outcome, "processed")
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(len(self.server.received), 1)


class CheckpointCurrencyTests(IngestTestCase):
    def test_a_recapture_retires_the_checkpoint_and_keeps_its_evidence(self):
        first = self.ingest()
        load_package(
            self.new_conn(), self.destination(), PACKAGE, force_recapture=True
        )

        status = self.status()
        self.assertFalse(status["checkpoint_is_current"])
        self.assertFalse(status["capture_still_published"])
        self.assertEqual(status["checkpoint_capture_id"], first.capture_id)  # evidence kept

        replay = self.ingest()
        self.assertEqual(replay.outcome, "processed")
        self.assertNotEqual(replay.capture_id, first.capture_id)
        self.assertNotEqual(replay.run_id, first.run_id)
        self.assertEqual(self.checkpoint()["capture_id"], replay.capture_id)

    def test_a_later_failed_check_retires_the_checkpoint(self):
        result = self.ingest()
        verify_capture(
            self.new_conn(), result.capture_id,
            transport=FakeTransport([api_page([], total=9, token=None)]),
        )
        status = self.status()
        self.assertFalse(status["checkpoint_is_current"])
        self.assertFalse(status["coverage_verified"])
        self.assertEqual(status["latest_verification_state"], "unavailable")
        self.assertEqual(status["checkpoint_capture_id"], result.capture_id)

    def test_a_check_still_running_retires_the_checkpoint(self):
        result = self.ingest()
        blocked = FakeTransport([RuntimeError("interrupted")])
        with self.assertRaises(RuntimeError):
            verify_capture(self.new_conn(), result.capture_id, transport=blocked)

        status = self.status()
        self.assertFalse(status["checkpoint_is_current"])
        self.assertEqual(status["latest_verification_state"], "in_progress")

        # and ingest refuses to replay it as processed
        replay = self.ingest()
        self.assertEqual(replay.outcome, "processed")
        self.assertNotEqual(replay.verification_attempt_id, result.verification_attempt_id)

    def test_an_interrupted_new_run_is_visible_next_to_the_current_checkpoint(self):
        self.ingest()
        with mock.patch.object(
            ingest, "_replay", return_value=None
        ), mock.patch.object(
            ingest, "_load", side_effect=RuntimeError("crash")
        ), self.assertRaises(RuntimeError):
            self.ingest()

        status = self.status()
        self.assertTrue(status["checkpoint_is_current"])
        self.assertIsNotNone(status["open_run_id"])
        self.assertEqual(status["open_run_phase"], "artifact_ready")

    def test_an_overlapping_package_does_not_cover_a_missing_checkpoint(self):
        self.ingest()
        status = self.status(OTHER_PACKAGE)
        self.assertIsNone(status)
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_read.package_ingest_status"
                " where checkpoint_is_current"
            ).fetchone()[0],
            1,
        )

    def test_a_package_loaded_and_verified_by_hand_has_no_ingest_checkpoint(self):
        path = write_package(self.root / "manual.tar.gz", [legacy_member(n) for n in (1, 2)])
        loaded = load_package(self.new_conn(), path, OTHER_PACKAGE)
        verify_capture(
            self.new_conn(), loaded.capture_id,
            transport=FakeTransport(source([1, 2], ojs=OTHER_OJS)),
        )
        status = self.status(OTHER_PACKAGE)
        self.assertFalse(status["has_checkpoint"])
        self.assertFalse(status["checkpoint_is_current"])
        self.assertTrue(status["coverage_verified"])


class ContractVersionReplayTests(IngestTestCase):
    """A checkpoint sealed under an older projection contract is never replayed,
    even when it is otherwise internally coherent."""

    def _downgrade_to_v1(self, capture_id):
        """Rewrite a real, freshly-sealed v2 checkpoint to look exactly like a
        coherent v1 one: same capture, same artifact, same attempt -- only the
        contract version differs. This is what a checkpoint sealed before this
        milestone actually looks like, produced without a second code path.

        The checkpoint's composite foreign keys pin its contract_version to
        match the capture's and the attempt's at all times, so the three
        cannot be updated to '1' one at a time without an intermediate
        violation; the checkpoint is dropped and reinserted instead.
        """
        package, run_id, attempt_id, sha256, notice_count = self.conn.execute(
            "select source_package_id, run_id, verification_attempt_id, artifact_sha256,"
            " notice_count from tl_work.package_checkpoint where capture_id = %s",
            (capture_id,),
        ).fetchone()
        self.conn.execute(
            "delete from tl_work.package_checkpoint where capture_id = %s", (capture_id,)
        )
        self.conn.execute(
            "update tl_work.capture set contract_version = '1' where capture_id = %s",
            (capture_id,),
        )
        self.conn.execute(
            "update tl_work.verification_attempt set contract_version = '1'"
            " where capture_id = %s",
            (capture_id,),
        )
        self.conn.execute(
            "insert into tl_work.package_checkpoint (source_package_id, run_id, capture_id,"
            " verification_attempt_id, artifact_sha256, contract_version, notice_count)"
            " values (%s, %s, %s, %s, %s, '1', %s)",
            (package, run_id, capture_id, attempt_id, sha256, notice_count),
        )

    def capture_contract_version(self, capture_id):
        return self.conn.execute(
            "select contract_version from tl_work.capture where capture_id = %s",
            (capture_id,),
        ).fetchone()[0]

    def test_a_v1_checkpoint_is_never_replayed_under_v2_code(self):
        first = self.ingest()
        self.assertEqual(self.capture_contract_version(first.capture_id), "2")
        self._downgrade_to_v1(first.capture_id)
        status_before = self.status()
        self.assertTrue(status_before["checkpoint_is_current"])
        self.assertEqual(status_before["checkpoint_contract_version"], "1")

        again = self.ingest()  # a coherent v1 checkpoint; still not a replay

        self.assertEqual(again.outcome, "processed")
        self.assertNotEqual(again.outcome, "replayed")
        self.assertNotEqual(again.run_id, first.run_id)
        self.assertNotEqual(again.capture_id, first.capture_id)
        self.assertEqual(self.capture_contract_version(again.capture_id), "2")

        # the v1 capture is superseded, not deleted or rewritten
        self.assertEqual(
            self.conn.execute(
                "select status from tl_work.capture where capture_id = %s",
                (first.capture_id,),
            ).fetchone()[0],
            "superseded",
        )
        self.assertEqual(self.notices(), [f"{n}-2023" for n in self.NUMBERS])
        # this run genuinely reprojected: v2 change-reference status is not NULL
        self.assertEqual(
            {
                r[0]
                for r in self.conn.execute(
                    "select change_reference_status from tl_work.notice_capture"
                    " where capture_id = %s", (again.capture_id,)
                )
            },
            {"not_applicable"},  # the fixtures in this module are legacy-only
        )

    def test_a_current_v2_checkpoint_still_replays_with_no_new_work(self):
        first = self.ingest()
        requests_before = len(self.server.received)

        replay = self.ingest(script=[])
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(replay.capture_id, first.capture_id)
        self.assertEqual(len(self.server.received), requests_before)
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.capture where source_package_id = %s",
                (PACKAGE,),
            ).fetchone()[0],
            1,
        )


class ConcurrencyTests(IngestTestCase):
    def test_a_second_ingest_of_one_package_is_refused_while_the_first_holds_it(self):
        holder = self.new_conn()
        holder.execute("select pg_advisory_lock(hashtext(%s)::int8)", (PACKAGE,))
        self.addCleanup(
            holder.execute, "select pg_advisory_unlock(hashtext(%s)::int8)", (PACKAGE,)
        )
        with self.assertRaises(repo.ConcurrentCaptureError):
            self.ingest()
        self.assertEqual(self.run_rows(), [])
        self.assertEqual(len(self.server.received), 0)

    def test_the_exclusion_holds_between_the_internal_steps(self):
        refusals = []

        def check_from_outside(*args, **kwargs):
            for name, call in (
                ("load", lambda: load_package(
                    self.new_conn(), self.destination(), PACKAGE)),
                ("verify", lambda: verify_capture(
                    self.new_conn(), 1, transport=FakeTransport([]))),
            ):
                try:
                    call()
                except repo.ConcurrentCaptureError:
                    refusals.append(name)
            raise RuntimeError("stop the run here")

        with mock.patch.object(ingest, "_ensure_coverage", side_effect=check_from_outside), \
                self.assertRaises(RuntimeError):
            self.ingest()
        self.assertEqual(sorted(refusals), ["load", "verify"])

    def test_the_package_lock_is_balanced_on_success_failure_and_cancellation(self):
        worker = self.new_conn()
        self.assertEqual(self.ingest(conn=worker).outcome, "processed")
        self.assertEqual(self.held_locks(worker), 0)
        self.assertTrue(self.lock_is_free())

        self.server.reply = serve(b"nope", status=404)
        second = self.new_conn()
        self.ingest(conn=second, package=OTHER_PACKAGE, script=[])
        self.assertEqual(self.held_locks(second), 0)
        self.assertTrue(self.lock_is_free(OTHER_PACKAGE))

        third = self.new_conn()
        with mock.patch.object(
            ingest, "_ensure_artifact", side_effect=KeyboardInterrupt
        ), self.assertRaises(KeyboardInterrupt):
            self.ingest(conn=third, package=OTHER_PACKAGE)
        self.assertEqual(self.held_locks(third), 0)
        self.assertTrue(self.lock_is_free(OTHER_PACKAGE))

    def test_cancellation_during_loading_releases_every_nested_acquisition(self):
        worker = self.new_conn()
        with mock.patch(
            "tender_ledger.loader.project_member", side_effect=KeyboardInterrupt
        ), self.assertRaises(KeyboardInterrupt):
            self.ingest(conn=worker)

        self.assertEqual(self.held_locks(worker), 0)
        self.assertTrue(self.lock_is_free())

    def test_different_packages_do_not_block_each_other(self):
        holder = self.new_conn()
        holder.execute("select pg_advisory_lock(hashtext(%s)::int8)", (OTHER_PACKAGE,))
        self.addCleanup(
            holder.execute, "select pg_advisory_unlock(hashtext(%s)::int8)", (OTHER_PACKAGE,)
        )
        self.assertEqual(self.ingest().outcome, "processed")


class ConnectionContractTests(IngestTestCase):
    def test_a_caller_transaction_is_refused_before_any_work(self):
        writer = self.new_conn()
        with writer.transaction(), self.assertRaises(repo.ConnectionStateError):
            self.ingest(conn=writer)
        self.assertEqual(self.run_rows(), [])
        self.assertEqual(len(self.server.received), 0)

    def test_a_connection_without_autocommit_is_refused(self):
        with self.assertRaises(repo.ConnectionStateError):
            self.ingest(conn=self.new_conn(autocommit=False))

    def test_an_unsupported_package_identity_is_refused_before_any_request(self):
        for bad in ("monthly/202301", "daily/20230022", "weekly/202300220"):
            with self.subTest(package=bad), self.assertRaises(UnsupportedPackage):
                self.ingest(package=bad)
        self.assertEqual(len(self.server.received), 0)


class ConstraintTests(IngestTestCase):
    def setUp(self):
        super().setUp()
        self.result = self.ingest()

    def insert_checkpoint(self, **overrides):
        values = {
            "source_package_id": OTHER_PACKAGE,
            "run_id": self.result.run_id,
            "capture_id": self.result.capture_id,
            "verification_attempt_id": self.result.verification_attempt_id,
            "artifact_sha256": self.result.artifact_sha256,
            "contract_version": "1",
            "notice_count": 5,
            **overrides,
        }
        columns = ", ".join(values)
        placeholders = ", ".join(["%s"] * len(values))
        self.conn.execute(
            f"insert into tl_work.package_checkpoint ({columns}) values ({placeholders})",
            tuple(values.values()),
        )

    def test_a_checkpoint_cannot_name_another_package_than_its_capture(self):
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.insert_checkpoint()

    def test_a_checkpoint_cannot_name_an_attempt_from_another_capture(self):
        other = write_package(self.root / "o.tar.gz", [legacy_member(1)])
        loaded = load_package(self.new_conn(), other, OTHER_PACKAGE)
        verified = verify_capture(
            self.new_conn(), loaded.capture_id,
            transport=FakeTransport(source([1], ojs=OTHER_OJS)),
        )
        self.conn.execute(
            "delete from tl_work.package_checkpoint where source_package_id = %s", (PACKAGE,)
        )
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.insert_checkpoint(
                source_package_id=PACKAGE,
                verification_attempt_id=verified.attempt_id,
            )

    def test_a_checkpoint_cannot_carry_unknown_evidence(self):
        for column in ("capture_id", "verification_attempt_id", "artifact_sha256",
                       "contract_version", "notice_count", "run_id"):
            with self.subTest(column=column),                     self.assertRaises(psycopg.errors.NotNullViolation),                     self.conn.transaction():
                self.conn.execute(
                    "delete from tl_work.package_checkpoint where source_package_id = %s",
                    (PACKAGE,),
                )
                self.insert_checkpoint(source_package_id=PACKAGE, **{column: None})

    def test_a_checkpoint_cannot_name_a_different_artifact_than_its_capture(self):
        self.conn.execute(
            "delete from tl_work.package_checkpoint where source_package_id = %s", (PACKAGE,)
        )
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.insert_checkpoint(source_package_id=PACKAGE, artifact_sha256="0" * 64)

    def test_a_checkpoint_cannot_name_a_run_from_another_package(self):
        other_run = self.conn.execute(
            "insert into tl_work.ingest_run (source_package_id, attempt_ordinal)"
            " values (%s, 1) returning run_id",
            (OTHER_PACKAGE,),
        ).fetchone()[0]
        self.conn.execute(
            "delete from tl_work.package_checkpoint where source_package_id = %s", (PACKAGE,)
        )
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.insert_checkpoint(source_package_id=PACKAGE, run_id=other_run)

    def test_a_checkpoint_cannot_change_the_capture_notice_count(self):
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.conn.execute(
                "update tl_work.package_checkpoint set notice_count = notice_count + 1"
                " where source_package_id = %s",
                (PACKAGE,),
            )

    def test_checkpoint_currency_rechecks_that_its_run_is_completed(self):
        self.conn.execute(
            "update tl_work.ingest_run set phase = 'source_verified', completed_at = null"
            " where run_id = %s",
            (self.result.run_id,),
        )
        self.assertFalse(self.status()["checkpoint_is_current"])

    def test_a_completed_run_must_carry_a_completion_time(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "update tl_work.ingest_run set completed_at = null where run_id = %s",
                (self.result.run_id,),
            )

    def test_a_failed_run_must_carry_a_reason(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "update tl_work.ingest_run set phase = 'failed', last_error = null,"
                " completed_at = null where run_id = %s",
                (self.result.run_id,),
            )


class MigrationUpgradeTests(unittest.TestCase):
    UPGRADE_DB = TEST_DB.replace("_test", "_ingest_upgrade_test")

    def setUp(self):
        assert self.UPGRADE_DB.endswith("_test")
        try:
            self.admin = db.connect(load_config(dbname="postgres"), autocommit=True)
        except psycopg.OperationalError as exc:
            self.skipTest(str(exc))
        self.admin.execute(sql.SQL("drop database if exists {} with (force)").format(
            sql.Identifier(self.UPGRADE_DB)))
        self.admin.execute(sql.SQL("create database {}").format(
            sql.Identifier(self.UPGRADE_DB)))
        self.addCleanup(self._drop)
        self.conn = db.connect(load_config(dbname=self.UPGRADE_DB))
        self.addCleanup(self.conn.close)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _drop(self):
        self.admin.execute(sql.SQL("drop database if exists {} with (force)").format(
            sql.Identifier(self.UPGRADE_DB)))
        self.admin.close()

    def test_upgrade_from_0003_keeps_the_capture_and_grants_no_free_checkpoint(self):
        self.assertEqual(
            db.migrate(self.conn, up_to="0003_source_verification"),
            ["0001_core", "0002_capture_batch_size", "0003_source_verification"],
        )
        # A capture and a verified coverage attempt exactly as 0003-era code
        # would have written them -- loaded and verified directly, with no
        # ingest_run in sight, because 0004 has not even been migrated yet.
        capture_id = self.conn.execute(
            "insert into tl_work.capture (source_package_id, artifact_sha256, artifact_bytes,"
            " contract_version, status, member_count, distinct_notice_count,"
            " loaded_row_count, batch_size, coverage_verified)"
            " values (%s, 'abc123', 100, '1', 'published', 4, 4, 4, 2, true)"
            " returning capture_id",
            (PACKAGE,),
        ).fetchone()[0]
        for n in (1, 2, 3, 4):
            self.conn.execute(
                "insert into tl_work.notice_capture (capture_id, publication_year,"
                " publication_number, batch_ordinal, source_format, schema_version,"
                " source_filename, publication_date, publication_date_raw,"
                " buyer_country_status, primary_cpv_status)"
                " values (%s, 2023, %s, 0, 'legacy', 'R2.0.9', %s, '2023-11-15',"
                " '20231115', 'absent', 'absent')",
                (capture_id, n, f"{n}_2023.xml"),
            )
        self.conn.execute(
            "insert into tl_work.published_capture (source_package_id, capture_id)"
            " values (%s, %s)", (PACKAGE, capture_id),
        )
        self.conn.execute(
            "insert into tl_work.verification_attempt (capture_id, artifact_sha256,"
            " contract_version, verifier_version, query_text, query_scope, state,"
            " announced_total, api_record_count, api_distinct_count, api_duplicate_count,"
            " local_distinct_count, only_local_count, only_api_count, api_keys_sha256,"
            " key_digest_recipe, finished_at)"
            " values (%s, 'abc123', '1', 'test', 'OJ = 220/2023', 'ALL', 'verified',"
            " 4, 4, 4, 0, 4, 0, 0, 'digest', 'test', now())",
            (capture_id,),
        )

        self.assertEqual(
            db.migrate(self.conn), ["0004_package_ingest", "0005_projection_contract_v2"]
        )

        row = self.conn.execute(
            "select has_checkpoint, checkpoint_is_current, coverage_verified"
            " from tl_read.package_ingest_status where source_package_id = %s", (PACKAGE,)
        ).fetchone()
        self.assertEqual(row, (False, False, True))
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_read.verification_attempt"
            ).fetchone()[0],
            1,
        )
        # this legacy capture predates contract v2: NULL, never backfilled
        self.assertEqual(
            self.conn.execute(
                "select distinct change_reference_status from tl_work.notice_capture"
                " where capture_id = %s", (capture_id,)
            ).fetchall(),
            [(None,)],
        )

    def test_a_clean_install_applies_every_migration(self):
        self.assertEqual(
            db.migrate(self.conn),
            [
                "0001_core", "0002_capture_batch_size",
                "0003_source_verification", "0004_package_ingest",
                "0005_projection_contract_v2",
            ],
        )


def _rows(conn, capture_id):
    return conn.execute(
        "select publication_year, publication_number, source_format, publication_date,"
        " buyer_country_iso, primary_cpv from tl_work.notice_capture where capture_id = %s"
        " order by publication_year, publication_number",
        (capture_id,),
    ).fetchall()


if __name__ == "__main__":
    unittest.main()
