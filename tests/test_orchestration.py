"""The Airflow-independent half of orchestration: what a Dag may ask for."""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg

from ted_fixtures import (
    TEST_DB,
    FakeTransport,
    api_page,
    ensure_test_database,
    legacy_member,
    package_bytes,
    truncate_all,
    write_package,
)
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.download import DownloadBudgets
from tender_ledger.ingest import IngestResult, ingest_package
from tender_ledger.loader import load_package
from tender_ledger.manifest import load_manifest, parse_manifest
from tender_ledger.manifest_runner import manifest_sha256, report_to_dict, run_manifest
from tender_ledger.orchestration import (
    OrchestrationError,
    checkpoint_summary,
    ingest_manifest_command,
    manifest_plan,
    report_file_name,
    resolve_manifest_path,
    run_command,
    summarize_report,
    workspace_from_env,
)


def manifest_document(*identities: str) -> dict:
    return {
        "manifest_version": 1,
        "packages": [
            {
                "order": position,
                "source_package_id": identity,
                "notice_count_observed": 10,
                "compressed_bytes_observed": 1024,
                "observed_at": "2026-09-06",
                "purpose": "orchestration fixture",
            }
            for position, identity in enumerate(identities, start=1)
        ],
    }


class ManifestPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "manifests").mkdir()
        (self.home / "manifests" / "pilot.json").write_text("{}", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def test_accepts_a_relative_path_inside_the_manifest_directory(self):
        resolved = resolve_manifest_path(self.home, "manifests/pilot.json")
        self.assertEqual(resolved, (self.home / "manifests" / "pilot.json").resolve())

    def test_rejects_an_absolute_path(self):
        absolute = (self.home / "manifests" / "pilot.json").resolve()
        with self.assertRaises(OrchestrationError) as caught:
            resolve_manifest_path(self.home, str(absolute))
        self.assertIn("relative", str(caught.exception))

    def test_rejects_a_path_that_traverses_out_of_the_manifest_directory(self):
        (self.home / "data").mkdir()
        (self.home / "data" / "elsewhere.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(OrchestrationError) as caught:
            resolve_manifest_path(self.home, "manifests/../data/elsewhere.json")
        self.assertIn("manifests", str(caught.exception))

    def test_rejects_a_manifest_that_does_not_exist(self):
        with self.assertRaises(OrchestrationError) as caught:
            resolve_manifest_path(self.home, "manifests/absent.json")
        self.assertIn("absent.json", str(caught.exception))

    def test_rejects_a_directory(self):
        (self.home / "manifests" / "nested").mkdir()
        with self.assertRaises(OrchestrationError):
            resolve_manifest_path(self.home, "manifests/nested")


class ManifestPlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "manifests").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, document) -> Path:
        path = self.home / "manifests" / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_describes_a_valid_manifest_without_copying_its_content(self):
        path = self.write("pilot.json", manifest_document("daily/202300220", "monthly/2024-01"))

        plan = manifest_plan(self.home, "manifests/pilot.json")

        self.assertEqual(
            plan,
            {
                "manifest": "manifests/pilot.json",
                "file_name": "pilot.json",
                "sha256": manifest_sha256(load_manifest(path)),
                "package_count": 2,
            },
        )

    def test_refuses_a_manifest_the_strict_schema_rejects(self):
        document = manifest_document("daily/202300220")
        document["packages"][0]["url"] = "https://example.invalid/package.tar.gz"
        self.write("extra-key.json", document)

        with self.assertRaises(OrchestrationError) as caught:
            manifest_plan(self.home, "manifests/extra-key.json")
        self.assertIn("url", str(caught.exception))

    def test_refuses_a_manifest_that_is_not_json(self):
        (self.home / "manifests" / "broken.json").write_text("{not json", encoding="utf-8")

        with self.assertRaises(OrchestrationError):
            manifest_plan(self.home, "manifests/broken.json")


class ReportFileNameTests(unittest.TestCase):
    def test_turns_a_run_identifier_into_a_portable_file_name(self):
        name = report_file_name("manual__2026-09-06T18:00:00+00:00")

        self.assertRegex(name, r"^manifest-run-[A-Za-z0-9._-]+\.json$")
        self.assertTrue(
            name.startswith("manifest-run-manual__2026-09-06T18-00-00-00-00"), name
        )

    def test_distinguishes_identifiers_that_sanitize_alike(self):
        self.assertNotEqual(report_file_name("a:b"), report_file_name("a/b"))

    def test_refuses_an_identifier_with_nothing_usable_in_it(self):
        with self.assertRaises(OrchestrationError):
            report_file_name("   ")


class IngestManifestCommandTests(unittest.TestCase):
    def test_builds_the_public_command_as_an_argument_vector(self):
        command = ingest_manifest_command(
            executable="/usr/local/bin/python",
            manifest=Path("/opt/tender-ledger/manifests/pilot.json"),
            report=Path("/opt/tender-ledger/reports/manifest-run-x.json"),
            data_dir=Path("/opt/tender-ledger/data"),
        )

        self.assertEqual(
            command,
            [
                "/usr/local/bin/python", "-m", "tender_ledger", "ingest-manifest",
                "--manifest", str(Path("/opt/tender-ledger/manifests/pilot.json")),
                "--report", str(Path("/opt/tender-ledger/reports/manifest-run-x.json")),
                "--data-dir", str(Path("/opt/tender-ledger/data")),
            ],
        )

    def test_passes_the_optional_flags_only_when_they_are_asked_for(self):
        options = {
            "manifest": Path("manifests/pilot.json"),
            "report": Path("reports/report.json"),
            "data_dir": Path("data"),
        }

        plain = ingest_manifest_command(**options)
        tuned = ingest_manifest_command(**options, batch_size=250, lock_wait=True)

        self.assertNotIn("--batch-size", plain)
        self.assertNotIn("--lock-wait", plain)
        self.assertEqual(tuned[len(plain):], ["--batch-size", "250", "--lock-wait"])

    def test_refuses_a_batch_size_that_is_not_a_positive_number(self):
        for batch_size in (0, -1, "500"):
            with self.subTest(batch_size=batch_size), self.assertRaises(OrchestrationError):
                ingest_manifest_command(
                    manifest=Path("manifests/pilot.json"),
                    report=Path("reports/report.json"),
                    data_dir=Path("data"),
                    batch_size=batch_size,
                )


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.logged: list[str] = []

    def test_reports_the_exit_code_and_the_output_of_the_child(self):
        code = run_command(
            [sys.executable, "-c", "import sys; print('loaded 26'); sys.exit(3)"],
            timeout=60,
            log=self.logged.append,
        )

        self.assertEqual(code, 3)
        self.assertIn("loaded 26", "\n".join(self.logged))

    def test_kills_a_child_that_outlives_its_timeout(self):
        with TemporaryDirectory() as directory:
            finished = Path(directory) / "finished"
            child = (
                "import sys, time; time.sleep(1.5);"
                " open(sys.argv[1], 'w').write('finished')"
            )

            with self.assertRaises(OrchestrationError) as caught:
                run_command(
                    [sys.executable, "-c", child, str(finished)],
                    timeout=0.3,
                    log=self.logged.append,
                )

            self.assertIn("timeout", str(caught.exception).lower())
            time.sleep(2.0)
            self.assertFalse(finished.exists(), "the child kept running after the timeout")

    def test_bounds_the_output_it_logs(self):
        child = "print('x' * 5000)\nfor line in range(500): print(line)"

        code = run_command(
            [sys.executable, "-c", child], timeout=60, log=self.logged.append
        )

        self.assertEqual(code, 0)
        self.assertLessEqual(len(self.logged), 202)
        self.assertLessEqual(max(len(line) for line in self.logged), 1000)
        self.assertIn("suppressed", self.logged[-1])


class _ClosableConnection:
    """Enough of a connection for the runner to open and close one."""

    def close(self):
        pass


def completed_report(*identities: str) -> dict:
    """A real runner report, produced by the real runner over stub results."""
    manifest = parse_manifest(manifest_document(*identities))

    def ingest_one(_conn, source_package_id, **_options):
        return IngestResult(
            run_id=1, source_package_id=source_package_id, phase="completed",
            outcome="replayed", resumed=True, artifact_action="replayed",
            artifact_path=f"packages/{source_package_id}.tar.gz",
            artifact_sha256="0" * 64, artifact_bytes=1024, notice_count=10,
            capture_id=2, verification_attempt_id=3, checkpoint_is_current=True,
            http_attempts=0, downloaded_bytes=0,
        )

    report = run_manifest(
        manifest, connect_factory=_ClosableConnection,
        ingest_one=ingest_one, manifest_path="manifests/pilot.json",
    )
    return report_to_dict(report)


class SummarizeReportTests(unittest.TestCase):
    def setUp(self):
        self.document = completed_report("daily/202300220", "monthly/2024-01")
        self.digest = self.document["manifest"]["sha256"]

    def summarize(self, document=None, **overrides):
        options = {"expected_sha256": self.digest, "expected_entries": 2} | overrides
        return summarize_report(self.document if document is None else document, **options)

    def test_accepts_a_run_whose_every_package_ended_with_a_current_checkpoint(self):
        summary = self.summarize()

        self.assertEqual(summary["entry_count"], 2)
        self.assertEqual(summary["outcomes"], {"replayed": 2})
        self.assertEqual(summary["http_attempts"], 0)
        self.assertEqual(summary["downloaded_bytes"], 0)
        self.assertEqual(summary["manifest_sha256"], self.digest)

    def test_rejects_a_run_that_did_not_complete(self):
        self.document["status"] = "failed"
        self.document["failed_entry"] = "monthly/2024-01"

        with self.assertRaises(OrchestrationError) as caught:
            self.summarize()
        self.assertIn("monthly/2024-01", str(caught.exception))

    def test_rejects_a_report_written_by_a_different_manifest(self):
        other = completed_report("monthly/2020-01")["manifest"]["sha256"]

        with self.assertRaises(OrchestrationError) as caught:
            self.summarize(expected_sha256=other)
        self.assertIn("sha256", str(caught.exception).lower())

    def test_rejects_a_report_that_does_not_cover_every_package(self):
        with self.assertRaises(OrchestrationError) as caught:
            self.summarize(expected_entries=3)
        self.assertIn("3", str(caught.exception))

    def test_rejects_an_outcome_that_is_not_a_processed_package(self):
        self.document["entries"][1]["outcome"] = "incomplete"

        with self.assertRaises(OrchestrationError) as caught:
            self.summarize()
        self.assertIn("incomplete", str(caught.exception))

    def test_rejects_an_entry_whose_checkpoint_is_not_current(self):
        self.document["entries"][0]["checkpoint_is_current"] = False

        with self.assertRaises(OrchestrationError) as caught:
            self.summarize()
        self.assertIn("daily/202300220", str(caught.exception))

    def test_summarizes_without_repeating_a_filesystem_path(self):
        summary = json.dumps(self.summarize())

        self.assertNotIn("/", summary)
        self.assertNotIn("\\\\", summary)


class _ArchiveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # BaseHTTPRequestHandler's required naming
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.server.payload)))
        self.end_headers()
        self.wfile.write(self.server.payload)

    def log_message(self, *args):
        pass


def setUpModule():
    ensure_test_database()


class CheckpointSummaryTests(unittest.TestCase):
    """What the public status view says, read the way the Dag reads it."""

    NUMBERS = (1, 2, 3)

    def setUp(self):
        try:
            self.conn = db.connect(load_config(dbname=TEST_DB))
        except psycopg.OperationalError as exc:
            self.skipTest(f"PostgreSQL unavailable: {exc}")
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveHandler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, address: None
        self.server.payload = package_bytes([legacy_member(n) for n in self.NUMBERS])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/p"

    def new_conn(self):
        conn = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(conn.close)
        return conn

    def ingest(self, package="daily/202300220"):
        result = ingest_package(
            self.new_conn(), package, data_root=self.dir, url=self.url,
            transport=FakeTransport([
                api_page(list(self.NUMBERS), total=len(self.NUMBERS), ojs="220/2023",
                         publication_date="2023-11-15Z", token="page-2"),
                api_page([], total=len(self.NUMBERS), ojs="220/2023",
                         publication_date="2023-11-15Z", token="still-here"),
            ]),
            budgets=DownloadBudgets(max_attempts=1),
        )
        self.assertEqual(result.outcome, "processed")
        return result

    def test_reports_the_notices_the_current_checkpoints_account_for(self):
        self.ingest()

        summary = checkpoint_summary(self.conn, ["daily/202300220"])

        self.assertEqual(summary, {"packages": 1, "notices": len(self.NUMBERS)})

    def test_refuses_a_package_the_status_view_has_never_heard_of(self):
        self.ingest()

        with self.assertRaises(OrchestrationError) as caught:
            checkpoint_summary(self.conn, ["daily/202300220", "monthly/2024-01"])
        self.assertIn("monthly/2024-01", str(caught.exception))

    def test_refuses_a_checkpoint_a_later_acquisition_retired(self):
        self.ingest()
        superseded = load_package(
            self.new_conn(),
            write_package(self.dir / "again", [legacy_member(n) for n in self.NUMBERS]),
            "daily/202300220",
            force_recapture=True,
        )
        self.assertEqual(superseded.status, "published")

        with self.assertRaises(OrchestrationError) as caught:
            checkpoint_summary(self.conn, ["daily/202300220"])
        self.assertIn("daily/202300220", str(caught.exception))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for name in ("manifests", "data", "reports"):
            (self.home / name).mkdir()

    def test_derives_the_three_directories_a_run_needs_from_one_variable(self):
        workspace = workspace_from_env({"TL_ORCHESTRATION_HOME": str(self.home)})

        self.assertEqual(workspace.home, self.home.resolve())
        self.assertEqual(workspace.manifests, (self.home / "manifests").resolve())
        self.assertEqual(workspace.data, (self.home / "data").resolve())
        self.assertEqual(workspace.reports, (self.home / "reports").resolve())

    def test_refuses_an_unset_or_empty_variable(self):
        for env in ({}, {"TL_ORCHESTRATION_HOME": "  "}):
            with self.subTest(env=env), self.assertRaises(OrchestrationError) as caught:
                workspace_from_env(env)
            self.assertIn("TL_ORCHESTRATION_HOME", str(caught.exception))

    def test_refuses_a_home_that_is_missing_a_mounted_directory(self):
        (self.home / "reports").rmdir()

        with self.assertRaises(OrchestrationError) as caught:
            workspace_from_env({"TL_ORCHESTRATION_HOME": str(self.home)})
        self.assertIn("reports", str(caught.exception))
