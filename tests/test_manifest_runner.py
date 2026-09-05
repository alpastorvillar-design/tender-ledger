"""The sequential manifest runner against a real PostgreSQL and local HTTP
servers. TED is never contacted: every archive comes from a local socket and
every Search API call from a scripted transport.
"""

import datetime as dt
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import psycopg

from ted_fixtures import (
    TEST_DB,
    FakeTransport,
    api_page,
    ensure_test_database,
    legacy_member,
    package_bytes,
    truncate_all,
)
from tender_ledger import db, ingest
from tender_ledger.config import load_config
from tender_ledger.download import DownloadBudgets
from tender_ledger.ingest import ingest_package
from tender_ledger.manifest import Manifest, ManifestEntry
from tender_ledger.manifest_runner import ManifestReport, report_to_dict, run_manifest

DAILY_OJS = "220/2023"


def daily_source(numbers, *, ojs=DAILY_OJS):
    total = len(numbers)
    pages = [api_page(numbers, total=total, ojs=ojs, token="page-2")]
    if numbers:
        pages.append(api_page([], total=total, ojs=ojs, token="still-here"))
    return pages


def monthly_source(numbers, *, published, year):
    total = len(numbers)
    pages = [
        api_page(numbers, total=total, publication_date=published, year=year, token="page-2")
    ]
    if numbers:
        pages.append(api_page([], total=total, year=year, token="still-here"))
    return pages


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # BaseHTTPRequestHandler's required naming
        self.server.received.append(self.path)
        self.server.reply(self)

    def log_message(self, *args):
        pass


def _serve(payload, *, status=200):
    def reply(handler):
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    return reply


def _manifest(order_ids, *, metadata=None):
    """Build a Manifest directly, bypassing JSON: schema validation is covered
    by test_manifest.py. ``metadata`` optionally overrides the planning fields
    per identity, to prove they never change what the runner decides."""
    metadata = metadata or {}
    entries = tuple(
        ManifestEntry(
            order=position,
            source_package_id=source_package_id,
            **{
                "notice_count_observed": 0,
                "compressed_bytes_observed": 0,
                "observed_at": dt.date(2026, 9, 4),
                "purpose": "test fixture",
                **metadata.get(source_package_id, {}),
            },
        )
        for position, source_package_id in enumerate(order_ids, start=1)
    )
    return Manifest(manifest_version=1, entries=entries)


class _ConnTracker:
    """Wraps a connection factory to prove at most one connection is open at a
    time, without monkeypatching psycopg's own close()."""

    def __init__(self, real_connect):
        self.real_connect = real_connect
        self.opened: list[psycopg.Connection] = []

    def __call__(self) -> psycopg.Connection:
        if self.opened:
            assert self.opened[-1].closed, "a previous connection was still open"
        conn = self.real_connect()
        self.opened.append(conn)
        return conn


def setUpModule():
    ensure_test_database()


class ManifestRunnerTestCase(unittest.TestCase):
    """One local HTTP server plus one scripted transport per package identity,
    keyed by source_package_id and reused for the lifetime of the test."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = self.new_conn()
        truncate_all(self.conn)
        self.fixtures: dict[str, dict] = {}
        self.servers: list[ThreadingHTTPServer] = []

    def new_conn(self, **kw) -> psycopg.Connection:
        conn = db.connect(load_config(dbname=TEST_DB), **kw)
        self.addCleanup(conn.close)
        return conn

    def add_fixture(self, source_package_id, members, script):
        archive = package_bytes(members)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.handle_error = lambda request, address: None
        server.received = []
        server.reply = _serve(archive)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.servers.append(server)
        url = f"http://127.0.0.1:{server.server_address[1]}/packages/{source_package_id}"
        self.fixtures[source_package_id] = {
            "server": server,
            "url": url,
            "transport": FakeTransport(script),
            "archive": archive,
        }

    def make_five_package_manifest(self):
        """The five identities of the real pilot manifest, tiny synthetic archives."""
        self.add_fixture(
            "monthly/2020-01",
            [legacy_member(1, year=2020, date_pub="20200105"),
             legacy_member(2, year=2020, date_pub="20200125")],
            monthly_source([1, 2], published="2020-01-15Z", year=2020),
        )
        self.add_fixture(
            "monthly/2020-02",
            [legacy_member(3, year=2020, date_pub="20200205"),
             legacy_member(4, year=2020, date_pub="20200225")],
            monthly_source([3, 4], published="2020-02-15Z", year=2020),
        )
        self.add_fixture(
            "monthly/2023-11",
            [legacy_member(5, year=2023, date_pub="20231105"),
             legacy_member(6, year=2023, date_pub="20231125")],
            monthly_source([5, 6], published="2023-11-15Z", year=2023),
        )
        self.add_fixture(
            "monthly/2024-01",
            [legacy_member(7, year=2024, date_pub="20240105"),
             legacy_member(8, year=2024, date_pub="20240125")],
            monthly_source([7, 8], published="2024-01-15Z", year=2024),
        )
        self.add_fixture(
            "daily/202300220",
            [legacy_member(9), legacy_member(10)],
            daily_source([9, 10]),
        )
        return [
            "monthly/2020-01", "monthly/2020-02", "monthly/2023-11",
            "monthly/2024-01", "daily/202300220",
        ]

    def ingest_options(self, entry: ManifestEntry) -> dict:
        fixture = self.fixtures[entry.source_package_id]
        return {
            "url": fixture["url"],
            "transport": fixture["transport"],
            "data_root": self.root,
            "budgets": DownloadBudgets(backoff_base_seconds=0.01),
        }

    def run_manifest(self, manifest, **kwargs) -> ManifestReport:
        kwargs.setdefault("connect_factory", self.new_conn)
        kwargs.setdefault("ingest_options", self.ingest_options)
        return run_manifest(manifest, **kwargs)

    def run_rows(self, source_package_id):
        with self.conn.cursor() as cur:
            cur.execute(
                "select run_id from tl_work.ingest_run where source_package_id = %s",
                (source_package_id,),
            )
            return cur.fetchall()

    def checkpoint_contract_version(self, source_package_id):
        row = self.conn.execute(
            "select contract_version from tl_work.package_checkpoint"
            " where source_package_id = %s",
            (source_package_id,),
        ).fetchone()
        return None if row is None else row[0]


class FullManifestTests(ManifestRunnerTestCase):
    def test_five_successes_call_once_per_entry_and_produce_a_complete_summary(self):
        ids = self.make_five_package_manifest()
        manifest = _manifest(ids)

        report = self.run_manifest(manifest, manifest_path="manifests/m3-pilot.json")

        self.assertEqual(report.status, "completed")
        self.assertIsNone(report.failed_entry)
        self.assertEqual(report.manifest.manifest_version, 1)
        self.assertEqual(report.manifest.package_count, 5)
        self.assertEqual(report.manifest.file_name, "m3-pilot.json")
        self.assertEqual(len(report.manifest.sha256), 64)
        self.assertLessEqual(report.started_at, report.finished_at)
        self.assertGreaterEqual(report.duration_seconds, 0)
        self.assertEqual(len(report.entries), 5)

        for position, (source_package_id, entry) in enumerate(
            zip(ids, report.entries, strict=True), start=1
        ):
            with self.subTest(package=source_package_id):
                self.assertEqual(entry.order, position)
                self.assertEqual(entry.source_package_id, source_package_id)
                self.assertEqual(entry.outcome, "processed")
                self.assertIsNotNone(entry.run_id)
                self.assertIsNotNone(entry.capture_id)
                self.assertIsNotNone(entry.verification_attempt_id)
                self.assertTrue(entry.checkpoint_is_current)
                self.assertEqual(entry.phase, "completed")
                self.assertEqual(entry.http_attempts, 1)
                self.assertGreater(entry.downloaded_bytes, 0)
                self.assertIsNone(entry.error)
                self.assertEqual(len(self.fixtures[source_package_id]["server"].received), 1)

    def test_a_second_run_replays_every_current_checkpoint_with_no_new_request(self):
        ids = self.make_five_package_manifest()
        manifest = _manifest(ids)
        self.run_manifest(manifest)
        received_before = {
            pid: len(self.fixtures[pid]["server"].received) for pid in ids
        }
        requests_before = {
            pid: len(self.fixtures[pid]["transport"].requests) for pid in ids
        }

        report = self.run_manifest(manifest)

        self.assertEqual(report.status, "completed")
        self.assertEqual([e.outcome for e in report.entries], ["replayed"] * 5)
        for pid in ids:
            self.assertEqual(
                len(self.fixtures[pid]["server"].received), received_before[pid]
            )
            self.assertEqual(
                len(self.fixtures[pid]["transport"].requests), requests_before[pid]
            )


class StopOnFailureTests(ManifestRunnerTestCase):
    def test_a_failure_on_the_third_entry_stops_the_runner_before_the_rest(self):
        ids = self.make_five_package_manifest()
        # Break the third package's archive so the load never publishes.
        self.fixtures[ids[2]]["server"].reply = _serve(b"not a valid archive")
        manifest = _manifest(ids)

        report = self.run_manifest(manifest)

        self.assertEqual(report.status, "failed")
        self.assertEqual(report.failed_entry, ids[2])
        self.assertEqual(len(report.entries), 3)
        self.assertEqual([e.outcome for e in report.entries], ["processed", "processed", "incomplete"])
        self.assertEqual([e.order for e in report.entries], [1, 2, 3])

        # The stopping entry transferred a body and had it refused, so the
        # report says what that attempt cost rather than reading like a package
        # that never reached the network.
        stopped = report.entries[2]
        self.assertEqual(stopped.http_attempts, 1)
        self.assertEqual(stopped.downloaded_bytes, len(b"not a valid archive"))

        for pid in ids[3:]:
            with self.subTest(package=pid):
                self.assertEqual(self.fixtures[pid]["server"].received, [])
                self.assertEqual(self.run_rows(pid), [])

    def test_an_exception_on_an_entry_closes_its_connection_and_reports_a_bounded_error(self):
        ids = self.make_five_package_manifest()
        second = ids[1]

        def flaky_ingest(conn, source_package_id, **kwargs):
            if source_package_id == second:
                raise psycopg.OperationalError("simulated connection loss")
            return ingest_package(conn, source_package_id, **kwargs)

        tracker = _ConnTracker(self.new_conn)
        manifest = _manifest(ids)

        report = self.run_manifest(
            manifest, connect_factory=tracker, ingest_one=flaky_ingest
        )

        self.assertEqual(report.status, "failed")
        self.assertEqual(report.failed_entry, second)
        self.assertEqual(len(report.entries), 2)
        failed = report.entries[1]
        self.assertEqual(failed.outcome, "error")
        self.assertIn("OperationalError", failed.error)
        self.assertIn("simulated connection loss", failed.error)
        self.assertLessEqual(len(failed.error), 2000)
        self.assertIsNone(failed.run_id)

        for pid in ids[2:]:
            self.assertEqual(self.fixtures[pid]["server"].received, [])

        # exactly one connection per attempted entry, and each one was closed
        self.assertEqual(len(tracker.opened), 2)
        for opened in tracker.opened:
            self.assertTrue(opened.closed)


class ConnectionLifecycleTests(ManifestRunnerTestCase):
    def test_each_entry_uses_a_distinct_connection_that_is_always_closed(self):
        ids = self.make_five_package_manifest()
        tracker = _ConnTracker(self.new_conn)
        manifest = _manifest(ids)

        report = self.run_manifest(manifest, connect_factory=tracker)

        self.assertEqual(report.status, "completed")
        self.assertEqual(len(tracker.opened), 5)
        self.assertEqual(len({id(c) for c in tracker.opened}), 5)
        for opened in tracker.opened:
            self.assertTrue(opened.closed)


class ResumeAndReplayTests(ManifestRunnerTestCase):
    def test_a_second_execution_replays_finished_packages_and_resumes_the_interrupted_one(self):
        ids = self.make_five_package_manifest()
        fourth = ids[3]
        manifest = _manifest(ids)

        original_ensure_coverage = ingest._ensure_coverage
        triggered = {"done": False}

        def flaky(conn, run, **kwargs):
            if run.source_package_id == fourth and not triggered["done"]:
                triggered["done"] = True
                raise psycopg.OperationalError("simulated crash before verification")
            return original_ensure_coverage(conn, run, **kwargs)

        with mock.patch.object(ingest, "_ensure_coverage", side_effect=flaky):
            first = self.run_manifest(manifest)

        self.assertEqual(first.status, "failed")
        self.assertEqual(first.failed_entry, fourth)
        self.assertEqual([e.outcome for e in first.entries], ["processed"] * 3 + ["error"])
        self.assertEqual(self.fixtures[ids[4]]["server"].received, [])  # E5 never touched

        received_after_first = {
            pid: len(self.fixtures[pid]["server"].received) for pid in ids[:3]
        }

        second = self.run_manifest(manifest)

        self.assertEqual(second.status, "completed")
        self.assertEqual(len(second.entries), 5)
        self.assertEqual(
            [e.outcome for e in second.entries],
            ["replayed", "replayed", "replayed", "processed", "processed"],
        )
        for pid in ids[:3]:
            self.assertEqual(
                len(self.fixtures[pid]["server"].received), received_after_first[pid]
            )
        # the fourth package resumed from the artifact it already downloaded
        self.assertEqual(len(self.fixtures[fourth]["server"].received), 1)
        self.assertEqual(len(self.fixtures[ids[4]]["server"].received), 1)
        for entry in second.entries:
            self.assertTrue(entry.checkpoint_is_current)


class ContractVersionReplayTests(ManifestRunnerTestCase):
    """A checkpoint sealed under an older projection contract is never treated
    as a valid replay by the runner -- this is ingest_package's own contract
    check, carried through unchanged."""

    def test_a_v1_checkpoint_does_not_count_as_a_replay_under_the_runner(self):
        self.add_fixture(
            "daily/202300220", [legacy_member(1), legacy_member(2)], daily_source([1, 2])
        )
        manifest = _manifest(["daily/202300220"])
        first = self.run_manifest(manifest)
        self.assertEqual(first.entries[0].outcome, "processed")
        first_capture_id = first.entries[0].capture_id

        self._downgrade_to_v1(first_capture_id)
        self.assertEqual(self.checkpoint_contract_version("daily/202300220"), "1")

        # A v1 checkpoint declines: the reprojection that follows genuinely
        # re-verifies, so it needs its own, un-consumed script.
        self.fixtures["daily/202300220"]["transport"] = FakeTransport(daily_source([1, 2]))

        second = self.run_manifest(manifest)

        self.assertEqual(second.status, "completed")
        entry = second.entries[0]
        self.assertEqual(entry.outcome, "processed")  # not "replayed"
        self.assertNotEqual(entry.capture_id, first_capture_id)
        self.assertEqual(self.checkpoint_contract_version("daily/202300220"), "2")

    def _downgrade_to_v1(self, capture_id):
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


class PlanningMetadataIsInertTests(ManifestRunnerTestCase):
    def test_altered_planning_metadata_does_not_change_outcomes(self):
        self.add_fixture(
            "daily/202300220", [legacy_member(1)], daily_source([1])
        )
        manifest_a = _manifest(
            ["daily/202300220"],
            metadata={"daily/202300220": {
                "notice_count_observed": 1, "compressed_bytes_observed": 100,
                "purpose": "original planning note",
            }},
        )
        manifest_b = _manifest(
            ["daily/202300220"],
            metadata={"daily/202300220": {
                "notice_count_observed": 999999, "compressed_bytes_observed": 1,
                "purpose": "a completely different, wrong planning note",
            }},
        )

        report_a = self.run_manifest(manifest_a)
        self.assertEqual(report_a.status, "completed")
        self.assertEqual(report_a.entries[0].outcome, "processed")

        report_b = self.run_manifest(manifest_b)  # same package, now replays
        self.assertEqual(report_b.status, "completed")
        self.assertEqual(report_b.entries[0].outcome, "replayed")
        self.assertTrue(report_b.entries[0].checkpoint_is_current)


class ReportShapeTests(ManifestRunnerTestCase):
    def test_a_manifest_with_no_path_reports_no_file_name(self):
        self.add_fixture("daily/202300220", [legacy_member(1)], daily_source([1]))
        report = self.run_manifest(_manifest(["daily/202300220"]))
        self.assertIsNone(report.manifest.file_name)

    def test_an_absolute_manifest_path_is_not_exposed(self):
        self.add_fixture("daily/202300220", [legacy_member(1)], daily_source([1]))
        report = self.run_manifest(
            _manifest(["daily/202300220"]),
            manifest_path="C:\\Users\\someone\\private\\pilot.json",
        )
        payload = report_to_dict(report)
        self.assertEqual(payload["manifest"]["file_name"], "pilot.json")
        self.assertNotIn("Users", json.dumps(payload))

    def test_a_replay_does_not_leave_a_dangling_error(self):
        self.add_fixture("daily/202300220", [legacy_member(1)], daily_source([1]))
        manifest = _manifest(["daily/202300220"])
        self.run_manifest(manifest)
        report = self.run_manifest(manifest)
        self.assertIsNone(report.entries[0].error)


if __name__ == "__main__":
    unittest.main()
