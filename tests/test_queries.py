"""Every shipped query has a correctness fixture and a stated grain."""

import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

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
from tender_ledger.download import DownloadBudgets
from tender_ledger.ingest import ingest_package
from tender_ledger.loader import load_package
from tender_ledger.source_api import TransientSourceError
from tender_ledger.verification import verify_capture

QUERIES = Path(__file__).resolve().parents[1] / "queries"
_MONTHLY = (QUERIES / "monthly_notice_counts.sql").read_text()
_COVERAGE = (QUERIES / "coverage_status.sql").read_text()
_HISTORY = (QUERIES / "verification_history.sql").read_text()
_INGEST_STATUS = (QUERIES / "ingest_status.sql").read_text()
_INGEST_HISTORY = (QUERIES / "ingest_history.sql").read_text()


def setUpModule():
    ensure_test_database()


class QueryTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.conn = db.connect(load_config(dbname=TEST_DB))
        except psycopg.OperationalError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def rows(self, query):
        with self.conn.cursor() as cur:
            cur.execute(query)
            names = [d.name for d in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def new_conn(self):
        conn = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(conn.close)
        return conn


class MonthlyNoticeCountsTests(QueryTestCase):
    def test_grain_is_month_country_cpv_division_over_distinct_notices(self):
        november = [
            legacy_member(1, date_pub="20231115", country="PL", cpv=("79000000",)),
            legacy_member(2, date_pub="20231120", country="PL", cpv=("79500000", "79600000")),
            eforms_member(3, pub_date="2023-11-15Z", buyer_country="POL", main_cpv="72000000"),
            legacy_member(4, date_pub="20231122", country="", cpv=()),  # absent country + cpv
        ]
        december = [legacy_member(5, date_pub="20231205", country="ES", cpv=("45000000",))]
        load_package(self.conn, write_package(self.dir / "nov", november), "daily/nov")
        load_package(self.conn, write_package(self.dir / "dec", december), "daily/dec")

        rows = self.conn.execute(_MONTHLY).fetchall()
        as_dict = {(str(m), c, cpv): n for m, c, cpv, n in rows}

        self.assertEqual(as_dict[("2023-11-01", "PL", "79000000")], 2)
        self.assertEqual(as_dict[("2023-11-01", "PL", "72000000")], 1)
        self.assertEqual(as_dict[("2023-11-01", "(absent)", "(absent)")], 1)
        self.assertEqual(as_dict[("2023-12-01", "ES", "45000000")], 1)
        self.assertEqual(sum(as_dict.values()), 5)

    def test_overlapping_packages_do_not_double_count(self):
        daily = [legacy_member(10, date_pub="20231115", country="DE", cpv=("30000000",))]
        monthly = [legacy_member(10, date_pub="20231115", country="DE", cpv=("30000000",))]
        load_package(self.conn, write_package(self.dir / "d", daily), "daily/2023220")
        load_package(self.conn, write_package(self.dir / "m", monthly), "monthly/202311")

        rows = self.conn.execute(_MONTHLY).fetchall()
        self.assertEqual(sum(n for *_, n in rows), 1)


class CoverageScenarioTestCase(QueryTestCase):
    """Shared helpers for the coverage and history queries."""

    def publish(self, package_id, numbers):
        path = write_package(
            self.dir / package_id.replace("/", "_"), [legacy_member(n) for n in numbers]
        )
        result = load_package(self.new_conn(), path, package_id)
        self.assertEqual(result.status, "published")
        return result.capture_id

    def verify(self, capture_id, script):
        return verify_capture(
            self.new_conn(), capture_id, transport=FakeTransport(script)
        )

    def source(self, numbers, ojs):
        return [
            api_page(numbers, total=len(numbers), ojs=ojs, token="page-2"),
            api_page([], total=len(numbers), ojs=ojs, token="still-here"),
        ]

    def scenario(self):
        """Four published captures, one of each coverage standing."""
        verified = self.publish("daily/202300220", [1, 2])
        self.verify(verified, self.source([1, 2], "220/2023"))

        mismatched = self.publish("daily/202300221", [3])
        self.verify(mismatched, self.source([3, 4], "221/2023"))

        unavailable = self.publish("daily/202300222", [5])
        self.verify(unavailable, [TransientSourceError("connection reset")] * 3)

        never = self.publish("daily/202300223", [6])
        return verified, mismatched, unavailable, never


class CoverageQueryTests(CoverageScenarioTestCase):
    """Coverage reporting has to survive captures nobody verified and captures
    verified many times, without losing either."""

    def test_one_row_per_published_capture_including_the_unverified_one(self):
        verified, mismatched, unavailable, never = self.scenario()
        rows = {row["capture_id"]: row for row in self.rows(_COVERAGE)}

        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[verified]["coverage_state"], "verified")
        self.assertTrue(rows[verified]["source_coverage_verified"])
        self.assertEqual(rows[verified]["source_distinct_notices"], 2)

        self.assertEqual(rows[mismatched]["coverage_state"], "mismatch")
        self.assertEqual(rows[mismatched]["only_api_count"], 1)
        self.assertFalse(rows[mismatched]["source_coverage_verified"])

        self.assertEqual(rows[unavailable]["coverage_state"], "source unavailable")
        # Unknown, not zero: the attempt never learned anything about the source.
        self.assertIsNone(rows[unavailable]["only_local_count"])
        self.assertIsNone(rows[unavailable]["source_announced_total"])

        self.assertEqual(rows[never]["coverage_state"], "never verified")
        self.assertIsNone(rows[never]["attempt_id"])
        self.assertEqual(rows[never]["capture_notices"], 1)

    def test_repeated_attempts_do_not_multiply_a_capture_s_row(self):
        capture = self.publish("daily/202300220", [1])
        for _ in range(3):
            self.verify(capture, [TransientSourceError("gone")] * 3)
        self.verify(capture, self.source([1], "220/2023"))

        rows = [row for row in self.rows(_COVERAGE) if row["capture_id"] == capture]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["coverage_state"], "verified")

    def test_an_empty_period_reads_as_unconfirmed_not_verified(self):
        capture = self.publish("daily/202300220", [])
        self.verify(capture, [api_page([], total=0, ojs="220/2023", token="t")])
        row = next(r for r in self.rows(_COVERAGE) if r["capture_id"] == capture)
        self.assertEqual(row["coverage_state"], "unconfirmed empty")
        self.assertFalse(row["source_coverage_verified"])
        self.assertEqual(row["source_announced_total"], 0)

    def test_a_superseded_capture_leaves_the_report(self):
        first = self.publish("daily/202300220", [1])
        self.verify(first, self.source([1], "220/2023"))
        path = write_package(
            self.dir / "replacement", [legacy_member(1), legacy_member(2)]
        )
        replacement = load_package(
            self.new_conn(), path, "daily/202300220", force_recapture=True
        )

        rows = self.rows(_COVERAGE)
        self.assertEqual([r["capture_id"] for r in rows], [replacement.capture_id])
        self.assertNotEqual(replacement.capture_id, first)
        self.assertEqual(rows[0]["coverage_state"], "never verified")


class VerificationHistoryQueryTests(CoverageScenarioTestCase):
    def test_every_attempt_appears_once_in_a_deterministic_order(self):
        capture = self.publish("daily/202300220", [1])
        self.verify(capture, self.source([1], "220/2023"))
        self.verify(capture, [TransientSourceError("gone")] * 3)
        self.verify(capture, self.source([1, 9], "220/2023"))

        rows = [r for r in self.rows(_HISTORY) if r["capture_id"] == capture]
        self.assertEqual([r["attempt_ordinal"] for r in rows], [1, 2, 3])
        self.assertEqual(
            [r["state"] for r in rows], ["verified", "unavailable", "mismatch"]
        )
        self.assertEqual(
            [r["previous_state"] for r in rows], [None, "verified", "unavailable"]
        )
        self.assertIsNotNone(rows[0]["api_keys_sha256"])
        self.assertIsNone(rows[1]["api_keys_sha256"])
        self.assertEqual(rows[2]["only_api_sample"], ["9-2023"])
        self.assertTrue(all(r["duration"] is not None for r in rows))

    def test_attempts_are_numbered_within_their_own_capture(self):
        first = self.publish("daily/202300220", [1])
        second = self.publish("daily/202300221", [2])
        self.verify(first, self.source([1], "220/2023"))
        self.verify(second, self.source([2], "221/2023"))
        self.verify(second, self.source([2], "221/2023"))

        rows = self.rows(_HISTORY)
        by_capture = {}
        for row in rows:
            by_capture.setdefault(row["capture_id"], []).append(row["attempt_ordinal"])
        self.assertEqual(by_capture, {first: [1], second: [1, 2]})

    def test_an_interrupted_attempt_is_visible_without_a_finish_time(self):
        capture = self.publish("daily/202300220", [1])

        def die(_n):
            raise KeyboardInterrupt("operator stopped the run")

        with self.assertRaises(KeyboardInterrupt):
            verify_capture(
                self.new_conn(),
                capture,
                transport=FakeTransport(self.source([1], "220/2023"), before_request=die),
            )

        row = next(r for r in self.rows(_HISTORY) if r["capture_id"] == capture)
        self.assertEqual(row["state"], "in_progress")
        self.assertIsNone(row["finished_at"])
        self.assertIsNone(row["duration"])
        coverage = next(r for r in self.rows(_COVERAGE) if r["capture_id"] == capture)
        self.assertEqual(coverage["coverage_state"], "verification in progress")


class IngestQueryTestCase(QueryTestCase):
    """The ingest flow's own diagnostics, driven through the real command."""

    NUMBERS = (1, 2, 3)

    def setUp(self):
        super().setUp()
        self.archive = package_bytes([legacy_member(n) for n in self.NUMBERS])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveHandler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, address: None
        self.server.payload = self.archive
        self.server.status = 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/p"

    def ingest(self, package="daily/202300220", *, numbers=None, ojs="220/2023"):
        numbers = self.NUMBERS if numbers is None else numbers
        return ingest_package(
            self.new_conn(), package, data_root=self.dir, url=self.url,
            transport=FakeTransport([
                api_page(list(numbers), total=len(numbers), ojs=ojs, token="page-2"),
                api_page([], total=len(numbers), ojs=ojs, token="still-here"),
            ]),
            budgets=DownloadBudgets(max_attempts=1),
        )

    def by_package(self, query):
        return {row["source_package_id"]: row for row in self.rows(query)}


class IngestStatusQueryTests(IngestQueryTestCase):
    def test_grain_is_one_row_per_package_whatever_its_standing(self):
        self.ingest()

        # a package loaded by hand: no download, so no checkpoint of one
        manual = load_package(
            self.new_conn(),
            write_package(self.dir / "manual", [legacy_member(9)]),
            "daily/202300221",
        )
        self.assertEqual(manual.status, "published")

        # a package whose only run never got past the source
        self.server.status = 404
        crashed = self.ingest("daily/202300222", numbers=[9], ojs="222/2023")
        self.assertEqual(crashed.outcome, "incomplete")

        rows = self.by_package(_INGEST_STATUS)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["daily/202300220"]["checkpoint_state"], "processed")
        self.assertEqual(rows["daily/202300220"]["checkpoint_notice_count"], 3)
        self.assertEqual(rows["daily/202300221"]["checkpoint_state"], "never checkpointed")
        self.assertEqual(
            rows["daily/202300221"]["latest_verification_state"], "never verified"
        )
        self.assertIsNone(rows["daily/202300221"]["checkpoint_notice_count"])
        self.assertEqual(rows["daily/202300222"]["checkpoint_state"], "never checkpointed")
        self.assertIsNotNone(rows["daily/202300222"]["open_run_id"])
        self.assertIn("404", rows["daily/202300222"]["open_run_last_error"])

    def test_a_later_acquisition_and_a_later_failed_check_read_differently(self):
        first = self.ingest()
        load_package(
            self.new_conn(), self.dir / first.artifact_path,
            "daily/202300220", force_recapture=True,
        )
        self.assertEqual(
            self.by_package(_INGEST_STATUS)["daily/202300220"]["checkpoint_state"],
            "retired by a later acquisition",
        )

        again = self.ingest()
        self.assertEqual(again.outcome, "processed")
        verify_capture(
            self.new_conn(), again.capture_id,
            transport=FakeTransport([TransientSourceError("reset")] * 3),
        )
        row = self.by_package(_INGEST_STATUS)["daily/202300220"]
        self.assertEqual(row["checkpoint_state"], "retired by a later coverage check")
        self.assertEqual(row["latest_verification_state"], "unavailable")
        self.assertEqual(row["checkpoint_capture_id"], again.capture_id)  # evidence kept
        self.assertFalse(row["coverage_verified"])


class IngestHistoryQueryTests(IngestQueryTestCase):
    def test_every_run_appears_once_numbered_within_its_package(self):
        self.server.status = 503
        self.ingest()
        self.server.status = 200
        completed = self.ingest()

        rows = self.rows(_INGEST_HISTORY)
        self.assertEqual(len(rows), 1)  # the failed attempt was resumed, not replaced
        self.assertEqual(rows[0]["attempt_ordinal"], 1)
        self.assertEqual(rows[0]["run_id"], completed.run_id)
        self.assertEqual(rows[0]["reached"], "checkpoint sealed")
        self.assertIsNone(rows[0]["last_error"])
        self.assertIsNotNone(rows[0]["completed_at"])

    def test_an_interrupted_run_keeps_its_phase_error_and_unknown_columns(self):
        self.server.status = 404
        self.ingest()

        row = self.rows(_INGEST_HISTORY)[0]
        self.assertEqual(row["phase"], "starting")
        self.assertEqual(row["reached"], "interrupted before an artifact was accepted")
        self.assertIn("404", row["last_error"])
        for unknown in ("capture_id", "verification_attempt_id", "verification_state",
                        "artifact_sha256", "completed_at"):
            with self.subTest(column=unknown):
                self.assertIsNone(row[unknown])
        self.assertIsNotNone(row["elapsed"])


class _ArchiveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # BaseHTTPRequestHandler's required naming
        payload = self.server.payload if self.server.status == 200 else b"unavailable"
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    unittest.main()
