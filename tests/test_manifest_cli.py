"""The ``ingest-manifest`` CLI command: stdout, ``--report``, exit codes and
atomic file replacement, driven as a real subprocess. No real TED download or
Search API call is exercised here: every package that must do real work is
pre-seeded through a local server before the subprocess runs, so a genuinely
successful entry through the CLI is always a replay -- the same pattern the
existing ``ingest`` CLI tests use, since the command has no URL override."""

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
    ensure_test_database,
    legacy_member,
    package_bytes,
    truncate_all,
)
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.ingest import ingest_package
from tender_ledger.manifest import load_manifest
from tender_ledger.manifest_runner import manifest_sha256

DAILY_OJS = "220/2023"


def daily_source(numbers, *, ojs=DAILY_OJS):
    total = len(numbers)
    pages = [api_page(numbers, total=total, ojs=ojs, token="page-2")]
    if numbers:
        pages.append(api_page([], total=total, ojs=ojs, token="still-here"))
    return pages


class _Handler(BaseHTTPRequestHandler):
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


class ManifestCliTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.data_dir = self.dir / "data"
        self.env = {**os.environ, "TL_DB_NAME": TEST_DB, "PYTHONPATH": "src", "PYTHONUTF8": "1"}

    def local_server(self, source_package_id, members):
        payload = package_bytes(members)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.handle_error = lambda request, address: None
        server.received = []
        server.payload = payload
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/packages/{source_package_id}"
        return server, url

    def seed_checkpoint(self, source_package_id, numbers):
        """Fully ingest one package in-process, against a local server, so the
        CLI subprocess that follows only ever needs to replay it."""
        server, url = self.local_server(source_package_id, [legacy_member(n) for n in numbers])
        result = ingest_package(
            self.conn, source_package_id, data_root=self.data_dir, url=url,
            transport=FakeTransport(daily_source(numbers)),
        )
        self.assertEqual(result.outcome, "processed")
        return result

    def write_manifest(self, name, source_package_ids):
        entries = [
            {
                "order": i,
                "source_package_id": pid,
                "notice_count_observed": 0,
                "compressed_bytes_observed": 0,
                "observed_at": "2026-09-04",
                "purpose": "cli test fixture",
            }
            for i, pid in enumerate(source_package_ids, start=1)
        ]
        path = self.dir / name
        path.write_text(
            json.dumps({"manifest_version": 1, "packages": entries}), encoding="utf-8"
        )
        return path

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tender_ledger", *args],
            capture_output=True, text=True, env=self.env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )


class InvalidManifestTests(ManifestCliTests):
    def test_an_invalid_manifest_is_rejected_before_any_database_use(self):
        path = self.dir / "bad.json"
        path.write_text('{"manifest_version": 99, "packages": []}', encoding="utf-8")

        result = self.run_cli("ingest-manifest", "--manifest", str(path))

        self.assertEqual(result.returncode, 1)
        self.assertIn("Manifest rejected", result.stderr)
        self.assertEqual(
            self.conn.execute("select count(*) from tl_work.ingest_run").fetchone()[0], 0
        )

    def test_a_missing_manifest_file_is_rejected(self):
        result = self.run_cli(
            "ingest-manifest", "--manifest", str(self.dir / "does-not-exist.json")
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Manifest rejected", result.stderr)

    def test_a_manifest_changed_after_orchestrator_validation_is_rejected_before_database_use(self):
        path = self.write_manifest("changed.json", ["daily/202300220"])

        result = self.run_cli(
            "ingest-manifest",
            "--manifest", str(path),
            "--expected-manifest-sha256", "f" * 64,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match", result.stderr)
        self.assertEqual(
            self.conn.execute("select count(*) from tl_work.ingest_run").fetchone()[0], 0
        )


class StdoutAndExitCodeTests(ManifestCliTests):
    def test_a_completed_manifest_prints_json_to_stdout_and_exits_zero(self):
        self.seed_checkpoint("daily/202300220", [1, 2])
        manifest = self.write_manifest("m.json", ["daily/202300220"])

        result = self.run_cli(
            "ingest-manifest",
            "--manifest", str(manifest),
            "--data-dir", str(self.data_dir),
            "--expected-manifest-sha256", manifest_sha256(load_manifest(manifest)),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertIsNone(payload["failed_entry"])
        self.assertEqual(payload["manifest"]["file_name"], "m.json")
        self.assertEqual(len(payload["manifest"]["sha256"]), 64)
        self.assertNotIn(str(self.dir), result.stdout)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["outcome"], "replayed")
        self.assertEqual(payload["entries"][0]["source_package_id"], "daily/202300220")

    def test_a_failed_manifest_exits_non_zero_and_stops_before_the_untouched_entry(self):
        self.seed_checkpoint("daily/202300220", [1])
        third = "daily/202300222"
        holder = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(holder.close)
        holder.execute("select pg_advisory_lock(hashtext(%s)::int8)", (third,))
        self.addCleanup(
            holder.execute, "select pg_advisory_unlock(hashtext(%s)::int8)", (third,)
        )
        manifest = self.write_manifest(
            "m.json", ["daily/202300220", third, "daily/202300223"]
        )

        result = self.run_cli(
            "ingest-manifest", "--manifest", str(manifest), "--data-dir", str(self.data_dir)
        )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failed_entry"], third)
        self.assertEqual(len(payload["entries"]), 2)
        self.assertEqual(payload["entries"][0]["outcome"], "replayed")
        self.assertEqual(payload["entries"][1]["outcome"], "error")
        self.assertIn("ConcurrentCaptureError", payload["entries"][1]["error"])
        self.assertIn(third, result.stderr)
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.ingest_run where source_package_id = %s",
                ("daily/202300223",),
            ).fetchone()[0],
            0,
        )


class ReportFileTests(ManifestCliTests):
    def test_report_writes_json_to_the_given_file_instead_of_stdout(self):
        self.seed_checkpoint("daily/202300220", [1])
        manifest = self.write_manifest("m.json", ["daily/202300220"])
        report_path = self.dir / "report.json"

        result = self.run_cli(
            "ingest-manifest", "--manifest", str(manifest),
            "--data-dir", str(self.data_dir), "--report", str(report_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(len(payload["entries"]), 1)

    def test_report_replaces_existing_content_atomically_with_no_leftover_temp_file(self):
        self.seed_checkpoint("daily/202300220", [1])
        manifest = self.write_manifest("m.json", ["daily/202300220"])
        report_path = self.dir / "report.json"
        report_path.write_text("PLACEHOLDER STALE CONTENT", encoding="utf-8")

        result = self.run_cli(
            "ingest-manifest", "--manifest", str(manifest),
            "--data-dir", str(self.data_dir), "--report", str(report_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        content = report_path.read_text(encoding="utf-8")
        self.assertNotIn("PLACEHOLDER", content)
        json.loads(content)  # fully valid JSON, not a mix of old and new bytes
        leftover = [p.name for p in self.dir.iterdir() if "report" in p.name and p.name != "report.json"]
        self.assertEqual(leftover, [])

    def test_a_report_path_whose_parent_does_not_exist_fails_without_creating_it(self):
        self.seed_checkpoint("daily/202300220", [1])
        manifest = self.write_manifest("m.json", ["daily/202300220"])
        report_path = self.dir / "missing-parent" / "report.json"

        result = self.run_cli(
            "ingest-manifest", "--manifest", str(manifest),
            "--data-dir", str(self.data_dir), "--report", str(report_path),
        )

        self.assertEqual(result.returncode, 1)
        self.assertFalse(report_path.parent.exists())
        self.assertIn("report", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
