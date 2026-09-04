"""Every command reaches its resource policy through the package identity.

The discriminator is one archive whose single member is larger than a daily
package may hold and smaller than a monthly one may. It compresses to a few
kilobytes, so these tests prove which policy each path selected without moving
the volume a real monthly package would.
"""

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
    padded_legacy_member,
    truncate_all,
    write_package,
)
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.ingest import ingest_package
from tender_ledger.package_contract import DAILY_POLICY, MONTHLY_POLICY
from tender_ledger.packages import PackageError, inspect_package, limits_for

DAILY = "daily/202300220"
MONTHLY = "monthly/2023-11"
#: Over the daily member ceiling (8 MiB), under the monthly one (32 MiB).
OVERSIZED = DAILY_POLICY.member_bytes + 1024


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


class PolicyPathTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.members = [padded_legacy_member(1, member_bytes=OVERSIZED), legacy_member(2)]
        self.archive = write_package(self.dir / "wide.tar.gz", self.members)

    def run_cli(self, *args):
        env = {**os.environ, "TL_DB_NAME": TEST_DB, "PYTHONPATH": "src", "PYTHONUTF8": "1"}
        return subprocess.run(
            [sys.executable, "-m", "tender_ledger", *args],
            capture_output=True, text=True, env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )


class InspectPolicyTests(PolicyPathTestCase):
    def test_the_fixture_separates_the_two_policies(self):
        self.assertLess(DAILY_POLICY.member_bytes, OVERSIZED)
        self.assertLess(OVERSIZED, MONTHLY_POLICY.member_bytes)
        self.assertLess(self.archive.stat().st_size, 1024 * 1024)  # tiny on disk

    def test_the_library_limits_follow_the_identity(self):
        with self.assertRaisesRegex(PackageError, "Member exceeds"):
            inspect_package(self.archive, limits_for(DAILY))
        summary = inspect_package(self.archive, limits_for(MONTHLY))
        self.assertEqual(summary["notice_count"], 2)

    def test_inspect_selects_the_policy_named_by_the_package_identity(self):
        refused = self.run_cli("inspect", str(self.archive), "--package-id", DAILY)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Member exceeds", refused.stderr)

        allowed = self.run_cli("inspect", str(self.archive), "--package-id", MONTHLY)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(json.loads(allowed.stdout)["notice_count"], 2)

    def test_an_override_may_narrow_the_policy_but_never_widen_it(self):
        narrower = self.run_cli(
            "inspect", str(self.archive), "--package-id", MONTHLY, "--max-notices", "1"
        )
        self.assertEqual(narrower.returncode, 1)
        self.assertIn("notice limit", narrower.stderr)

        wider = self.run_cli(
            "inspect", str(self.archive), "--package-id", DAILY, "--max-member-mib", "16"
        )
        self.assertEqual(wider.returncode, 1)
        self.assertIn("above the policy", wider.stderr)
        self.assertEqual(wider.stdout, "")

    def test_without_an_identity_the_manual_flags_keep_their_own_defaults(self):
        result = self.run_cli("inspect", str(self.archive))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Member exceeds", result.stderr)
        result = self.run_cli("inspect", str(self.archive), "--max-member-mib", "16")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_unrecognized_package_identity_is_not_treated_as_daily(self):
        result = self.run_cli(
            "inspect", str(self.archive), "--package-id", "daily/scratch"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("canonical package identity", result.stderr)


class LoadPolicyTests(PolicyPathTestCase):
    setUpClass = classmethod(lambda cls: ensure_test_database())

    def setUp(self):
        super().setUp()
        self.conn = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)

    def test_load_refuses_under_a_daily_identity_and_accepts_under_a_monthly_one(self):
        refused = self.run_cli("load", str(self.archive), "--package-id", DAILY)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(json.loads(refused.stdout)["status"], "failed")
        self.assertIn("Member exceeds", refused.stdout)

        loaded = self.run_cli("load", str(self.archive), "--package-id", MONTHLY)
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        payload = json.loads(loaded.stdout)
        self.assertEqual(payload["status"], "published")
        self.assertEqual(payload["loaded_row_count"], 2)

    def test_an_unrecognized_identity_is_refused_before_a_capture_exists(self):
        result = self.run_cli("load", str(self.archive), "--package-id", "daily/scratch")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("canonical package identity", result.stderr)
        self.assertEqual(
            self.conn.execute("select count(*) from tl_work.capture").fetchone()[0], 0
        )


class IngestPolicyTests(PolicyPathTestCase):
    setUpClass = classmethod(lambda cls: ensure_test_database())

    def setUp(self):
        super().setUp()
        self.conn = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveHandler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, address: None
        self.server.received = []
        self.server.payload = package_bytes(self.members)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/packages/x"

    def ingest(self, package_id):
        return ingest_package(
            self.conn, package_id, data_root=self.dir / "data", url=self.url,
            transport=FakeTransport([
                api_page([1, 2], total=2, token="page-2"),
                api_page([], total=2, token="still-here"),
            ]),
        )

    def test_a_daily_identity_refuses_the_body_a_monthly_identity_accepts(self):
        refused = self.ingest(DAILY)
        self.assertEqual(refused.outcome, "incomplete")
        self.assertIn("not a usable TED package", refused.error)
        # The oversized member is refused while validating the temporary file, so
        # nothing is renamed into place and no partial file is left behind.
        self.assertEqual(list((self.dir / "data" / "packages" / "daily").iterdir()), [])

    def test_a_monthly_identity_ingests_the_same_bytes_end_to_end(self):
        result = self.ingest(MONTHLY)
        self.assertEqual(result.outcome, "processed", result.error)
        self.assertTrue(result.checkpoint_is_current)
        self.assertEqual(result.notice_count, 2)
        self.assertEqual(
            Path(result.artifact_path).as_posix(), "packages/monthly/2023-11.tar.gz"
        )


if __name__ == "__main__":
    unittest.main()
