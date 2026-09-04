"""The load/ingest/verify/status CLI wiring works end to end against the test database."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ted_fixtures import (
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
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.ingest import ingest_package
from tender_ledger.verification import verify_capture


class _ArchiveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # BaseHTTPRequestHandler's required naming
        self.server.received.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.server.payload)))
        self.end_headers()
        self.wfile.write(self.server.payload)

    def log_message(self, *args):
        pass


def setUpModule():
    ensure_test_database()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = {**os.environ, "TL_DB_NAME": TEST_DB, "PYTHONPATH": "src", "PYTHONUTF8": "1"}

    def local_package_server(self, payload):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveHandler)
        server.daemon_threads = True
        server.handle_error = lambda request, address: None
        server.received = []
        server.payload = payload
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_address[1]}/packages/daily/202300220"

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tender_ledger", *args],
            capture_output=True, text=True, env=self.env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )

    def test_load_then_status_reports_the_published_capture(self):
        pkg = write_package(self.dir / "d.tar.gz", [legacy_member(1), legacy_member(2)])
        load = self.run_cli(
            "load", str(pkg), "--package-id", "daily/202300221", "--batch-size", "1"
        )
        self.assertEqual(load.returncode, 0, load.stderr)
        payload = json.loads(load.stdout)
        self.assertEqual(payload["status"], "published")
        self.assertEqual(payload["loaded_row_count"], 2)

        status = self.run_cli("status", "--package-id", "daily/202300221")
        self.assertEqual(status.returncode, 0, status.stderr)
        rows = json.loads(status.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "published")
        self.assertTrue(rows[0]["is_published"])
        self.assertFalse(rows[0]["source_coverage_verified"])

    def test_force_recapture_acquires_the_package_again(self):
        pkg = write_package(self.dir / "d.tar.gz", [legacy_member(1)])
        first = self.run_cli("load", str(pkg), "--package-id", "daily/202300222")
        self.assertEqual(first.returncode, 0, first.stderr)

        replay = self.run_cli("load", str(pkg), "--package-id", "daily/202300222")
        self.assertEqual(json.loads(replay.stdout)["capture_id"],
                         json.loads(first.stdout)["capture_id"])

        forced = self.run_cli(
            "load", str(pkg), "--package-id", "daily/202300222", "--force-recapture"
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        payload = json.loads(forced.stdout)
        self.assertEqual(payload["status"], "published")
        self.assertNotEqual(payload["capture_id"], json.loads(first.stdout)["capture_id"])

        rows = json.loads(
            self.run_cli("status", "--package-id", "daily/202300222").stdout
        )
        self.assertEqual([r["status"] for r in rows], ["superseded", "published"])

    def test_a_failed_load_exits_non_zero_and_says_why(self):
        pkg = write_package(self.dir / "dup.tar.gz", [legacy_member(7), eforms_member(7)])
        result = self.run_cli("load", str(pkg), "--package-id", "daily/202300223")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "failed")
        self.assertIn("UniqueViolation", result.stderr)

    def test_verify_refuses_a_capture_it_cannot_check_and_exits_non_zero(self):
        # No network: the run has to stop on the capture itself, before any HTTP.
        pkg = write_package(self.dir / "d.tar.gz", [legacy_member(1)])
        self.run_cli("load", str(pkg), "--package-id", "daily/202300224")
        result = self.run_cli("verify", "--capture-id", "999999")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)

        rows = json.loads(
            self.run_cli("status", "--package-id", "daily/202300224").stdout
        )
        self.assertEqual(rows[0]["verification_state"], None)
        self.assertFalse(rows[0]["source_coverage_verified"])

    def test_status_reports_the_latest_verification_state(self):
        pkg = write_package(self.dir / "v.tar.gz", [legacy_member(1)])
        load = self.run_cli("load", str(pkg), "--package-id", "daily/202300220")
        capture_id = json.loads(load.stdout)["capture_id"]
        verify_capture(
            self.conn, capture_id,
            transport=FakeTransport([
                api_page([1], total=1, token="page-2"),
                api_page([], total=1, token="still-here"),
            ]),
        )
        rows = json.loads(self.run_cli("status", "--package-id", "daily/202300220").stdout)
        self.assertEqual(rows[0]["verification_state"], "verified")
        self.assertTrue(rows[0]["source_coverage_verified"])
        self.assertIsNotNone(rows[0]["verification_finished_at"])

    def test_ingest_refuses_an_unsupported_package_and_exits_non_zero(self):
        # No server: an unsupported identity has to stop before any request, and
        # the flow must not invent a checkpoint for it.
        result = self.run_cli(
            "ingest", "--package-id", "monthly/202301",
            "--data-dir", str(self.dir / "data"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("canonical package identity", result.stderr)
        self.assertFalse((self.dir / "data").exists())
        self.assertEqual(
            self.conn.execute("select count(*) from tl_work.ingest_run").fetchone()[0], 0
        )

    def test_ingest_replays_a_current_checkpoint_without_any_request(self):
        # The CLI has no URL override on purpose, so the download is set up
        # in-process against a local server and the command exercises the replay
        # path: no request of any kind leaves this test.
        server, url = self.local_package_server(
            package_bytes([legacy_member(1), legacy_member(2)])
        )
        first = ingest_package(
            self.conn, "daily/202300220", data_root=self.dir / "data", url=url,
            transport=FakeTransport([
                api_page([1, 2], total=2, token="page-2"),
                api_page([], total=2, token="still-here"),
            ]),
        )
        self.assertEqual(first.outcome, "processed")
        requests_before = len(server.received)

        result = self.run_cli(
            "ingest", "--package-id", "daily/202300220",
            "--data-dir", str(self.dir / "data"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outcome"], "replayed")
        self.assertTrue(payload["checkpoint_is_current"])
        self.assertEqual(payload["capture_id"], first.capture_id)
        self.assertEqual(payload["http_attempts"], 0)
        self.assertEqual(len(server.received), requests_before)

    def test_db_upgrade_is_idempotent(self):
        result = self.run_cli("db", "upgrade")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"applied": []})


if __name__ == "__main__":
    unittest.main()
