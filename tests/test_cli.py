"""The load/status/db CLI wiring works end to end against the test database."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tender_ledger", *args],
            capture_output=True, text=True, env=self.env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )

    def test_load_then_status_reports_the_published_capture(self):
        pkg = write_package(self.dir / "d.tar.gz", [legacy_member(1), legacy_member(2)])
        load = self.run_cli("load", str(pkg), "--package-id", "daily/cli", "--batch-size", "1")
        self.assertEqual(load.returncode, 0, load.stderr)
        payload = json.loads(load.stdout)
        self.assertEqual(payload["status"], "published")
        self.assertEqual(payload["loaded_row_count"], 2)

        status = self.run_cli("status", "--package-id", "daily/cli")
        self.assertEqual(status.returncode, 0, status.stderr)
        rows = json.loads(status.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "published")
        self.assertTrue(rows[0]["is_published"])
        self.assertFalse(rows[0]["source_coverage_verified"])

    def test_force_recapture_acquires_the_package_again(self):
        pkg = write_package(self.dir / "d.tar.gz", [legacy_member(1)])
        first = self.run_cli("load", str(pkg), "--package-id", "daily/force")
        self.assertEqual(first.returncode, 0, first.stderr)

        replay = self.run_cli("load", str(pkg), "--package-id", "daily/force")
        self.assertEqual(json.loads(replay.stdout)["capture_id"],
                         json.loads(first.stdout)["capture_id"])

        forced = self.run_cli(
            "load", str(pkg), "--package-id", "daily/force", "--force-recapture"
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        payload = json.loads(forced.stdout)
        self.assertEqual(payload["status"], "published")
        self.assertNotEqual(payload["capture_id"], json.loads(first.stdout)["capture_id"])

        rows = json.loads(self.run_cli("status", "--package-id", "daily/force").stdout)
        self.assertEqual([r["status"] for r in rows], ["superseded", "published"])

    def test_a_failed_load_exits_non_zero_and_says_why(self):
        pkg = write_package(self.dir / "dup.tar.gz", [legacy_member(7), eforms_member(7)])
        result = self.run_cli("load", str(pkg), "--package-id", "daily/clifail")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "failed")
        self.assertIn("UniqueViolation", result.stderr)

    def test_db_upgrade_is_idempotent(self):
        result = self.run_cli("db", "upgrade")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"applied": []})


if __name__ == "__main__":
    unittest.main()
