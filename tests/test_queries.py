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
from tender_ledger.db import repository as repo
from tender_ledger.download import DownloadBudgets
from tender_ledger.ingest import ingest_package
from tender_ledger.loader import digest_archive, load_package
from tender_ledger.packages import stream_notices
from tender_ledger.projection import project_member
from tender_ledger.source_api import TransientSourceError
from tender_ledger.verification import verify_capture

QUERIES = Path(__file__).resolve().parents[1] / "queries"
_MONTHLY = (QUERIES / "monthly_notice_counts.sql").read_text()
_COVERAGE = (QUERIES / "coverage_status.sql").read_text()
_HISTORY = (QUERIES / "verification_history.sql").read_text()
_INGEST_STATUS = (QUERIES / "ingest_status.sql").read_text()
_INGEST_HISTORY = (QUERIES / "ingest_history.sql").read_text()
_LATEST_CAPTURE = (QUERIES / "latest_capture_per_publication.sql").read_text()
_CHANGE_REFERENCES = (QUERIES / "official_change_references.sql").read_text()
_CUTOFF = (QUERIES / "acquisition_cutoff_state.sql").read_text()
_CALENDAR = (QUERIES / "monthly_coverage_calendar.sql").read_text()
_OVERLAP = (QUERIES / "cross_package_overlap_audit.sql").read_text()


def _with_params(text, **params):
    """Substitute this repo's `:'name'` psql-style parameter tokens with quoted
    literals, the same convention psql's own `-v name=value` performs."""
    for name, value in params.items():
        text = text.replace(f":'{name}'", f"'{value}'")
    return text


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


class LatestCapturePerPublicationTests(CoverageScenarioTestCase):
    def latest(self):
        return {row["source_package_id"]: row for row in self.rows(_LATEST_CAPTURE)}

    def test_a_recapture_becomes_the_new_latest_for_its_own_package(self):
        first = self.publish("daily/202300220", [1, 2])
        path = write_package(
            self.dir / "v2", [legacy_member(1), legacy_member(2), legacy_member(3)]
        )
        second = load_package(
            self.new_conn(), path, "daily/202300220", force_recapture=True
        )
        self.assertEqual(second.status, "published")

        row = self.latest()["daily/202300220"]
        self.assertEqual(row["capture_id"], second.capture_id)
        self.assertNotEqual(row["capture_id"], first)
        self.assertEqual(row["status"], "published")
        self.assertEqual(row["distinct_notice_count"], 3)

    def test_each_package_ranks_its_own_captures_independently(self):
        a1 = self.publish("daily/a", [1])
        path = write_package(self.dir / "a2", [legacy_member(1), legacy_member(2)])
        a2 = load_package(self.new_conn(), path, "daily/a", force_recapture=True)
        b1 = self.publish("daily/b", [9])

        rows = self.latest()
        self.assertEqual(rows["daily/a"]["capture_id"], a2.capture_id)
        self.assertNotEqual(rows["daily/a"]["capture_id"], a1)
        self.assertEqual(rows["daily/b"]["capture_id"], b1)

    def test_an_incomplete_capture_never_becomes_the_latest(self):
        published = self.publish("daily/incomplete", [1])
        path = write_package(self.dir / "partial", [legacy_member(2), legacy_member(3)])
        sha, size = digest_archive(path)
        writer = self.new_conn()
        begin = repo.begin_capture(
            writer, "daily/incomplete", sha, size, batch_size=10, force_recapture=True
        )
        # one batch committed, never reconciled or published: 'loading'
        repo.load_batch(
            writer, begin.capture.capture_id, 0,
            [project_member(m) for m in stream_notices(path)],
        )

        row = self.latest()["daily/incomplete"]
        self.assertEqual(row["capture_id"], published)


class AcquisitionCutoffStateTests(CoverageScenarioTestCase):
    def cutoff(self, ordinal):
        text = _with_params(_CUTOFF, cutoff=ordinal)
        return {row["source_package_id"]: row for row in self.rows(text)}

    def test_the_cutoff_selects_the_capture_current_at_that_point(self):
        first_id = self.publish("daily/202300220", [1, 2])
        first_ordinal = self.rows(
            "select acquisition_ordinal from tl_read.capture_status"
            f" where capture_id = {first_id}"
        )[0]["acquisition_ordinal"]
        path = write_package(
            self.dir / "v2", [legacy_member(1), legacy_member(2), legacy_member(3)]
        )
        second = load_package(
            self.new_conn(), path, "daily/202300220", force_recapture=True
        )

        at_first = self.cutoff(first_ordinal)
        self.assertEqual(at_first["daily/202300220"]["capture_id"], first_id)
        self.assertEqual(at_first["daily/202300220"]["distinct_notice_count"], 2)

        at_second = self.cutoff(second.acquisition_ordinal)
        self.assertEqual(at_second["daily/202300220"]["capture_id"], second.capture_id)
        self.assertEqual(at_second["daily/202300220"]["distinct_notice_count"], 3)

    def test_a_package_acquired_after_the_cutoff_is_absent(self):
        self.publish("daily/202300220", [1])
        rows = self.cutoff(0)
        self.assertNotIn("daily/202300220", rows)


class OfficialChangeReferencesTests(QueryTestCase):
    def references(self):
        return self.rows(_CHANGE_REFERENCES)

    def test_status_and_resolution_are_distinguished_per_shape(self):
        members = [
            legacy_member(1),                                          # not_applicable
            eforms_member(2),                                          # absent
            eforms_member(3, change_refs=[("2-2023", "notice-id-ref")]),  # resolvable, loaded
            eforms_member(4, change_refs=[("uid-999/02", None)]),        # unresolved shape
            eforms_member(5, change_refs=[("999999-2023", None)]),       # shape ok, not loaded
        ]
        load_package(self.conn, write_package(self.dir / "p", members), "daily/refs")

        rows = {(r["publication_ref"], r["ordinal"]): r for r in self.references()}

        self.assertEqual(rows[("1-2023", None)]["change_reference_status"], "not_applicable")
        self.assertIsNone(rows[("1-2023", None)]["value"])
        self.assertEqual(rows[("2-2023", None)]["change_reference_status"], "absent")

        resolved = rows[("3-2023", 0)]
        self.assertEqual(resolved["change_reference_status"], "present")
        self.assertEqual(resolved["value"], "2-2023")
        self.assertEqual(resolved["value_shape"], "publication_reference")
        self.assertEqual(resolved["resolved_target"], "2-2023")
        self.assertTrue(resolved["target_loaded"])

        unresolved_shape = rows[("4-2023", 0)]
        self.assertEqual(unresolved_shape["value_shape"], "unresolved_shape")
        self.assertIsNone(unresolved_shape["resolved_target"])
        self.assertFalse(unresolved_shape["target_loaded"])

        not_found = rows[("5-2023", 0)]
        self.assertEqual(not_found["value_shape"], "publication_reference")
        self.assertIsNone(not_found["resolved_target"])
        self.assertFalse(not_found["target_loaded"])

    def test_a_v1_row_reports_null_never_absent(self):
        capture_id = self.conn.execute(
            "insert into tl_work.capture (source_package_id, artifact_sha256, artifact_bytes,"
            " contract_version, status, member_count, distinct_notice_count, loaded_row_count,"
            " batch_size) values ('daily/v1', 'sha', 10, '1', 'published', 1, 1, 1, 1)"
            " returning capture_id"
        ).fetchone()[0]
        self.conn.execute(
            "insert into tl_work.notice_capture (capture_id, publication_year,"
            " publication_number, batch_ordinal, source_format, schema_version,"
            " source_filename, publication_date, publication_date_raw,"
            " buyer_country_status, primary_cpv_status)"
            " values (%s, 2023, 42, 0, 'eforms', 'eforms-sdk-1.9', '42.xml',"
            " '2023-11-15', '2023-11-15Z', 'absent', 'absent')",
            (capture_id,),
        )
        self.conn.execute(
            "insert into tl_work.published_capture (source_package_id, capture_id)"
            " values ('daily/v1', %s)", (capture_id,),
        )
        rows = {r["publication_ref"]: r for r in self.references()}
        self.assertIsNone(rows["42-2023"]["change_reference_status"])


class CrossPackageOverlapAuditTests(QueryTestCase):
    def overlap(self, a, b):
        return self.rows(_with_params(_OVERLAP, package_a=a, package_b=b))

    def test_both_directions_are_checked_without_assuming_a_subset(self):
        daily = [
            legacy_member(1, date_pub="20231115", country="PL"),
            legacy_member(2, date_pub="20231116", country="PL"),
        ]
        monthly = [legacy_member(1, date_pub="20231115", country="PL")]  # missing 2
        load_package(self.conn, write_package(self.dir / "d", daily), "daily/202300220")
        load_package(self.conn, write_package(self.dir / "m", monthly), "monthly/2023-11")

        forward = {(r["finding"], r["publication_ref"])
                   for r in self.overlap("daily/202300220", "monthly/2023-11")}
        self.assertIn(("only_in_a", "2-2023"), forward)
        self.assertNotIn(("only_in_b", "2-2023"), forward)

        # swapping the arguments finds the identical fact from the other side,
        # rather than the query assuming which one is the superset
        reverse = {(r["finding"], r["publication_ref"])
                   for r in self.overlap("monthly/2023-11", "daily/202300220")}
        self.assertIn(("only_in_b", "2-2023"), reverse)
        self.assertNotIn(("only_in_a", "2-2023"), reverse)

    def test_content_differences_are_reported_not_just_counted(self):
        a = [legacy_member(1, date_pub="20231115", country="PL", cpv=("79000000",))]
        b = [legacy_member(1, date_pub="20231115", country="DE", cpv=("79000000",))]
        load_package(self.conn, write_package(self.dir / "a", a), "daily/a")
        load_package(self.conn, write_package(self.dir / "b", b), "daily/b")

        findings = self.overlap("daily/a", "daily/b")
        self.assertEqual([f["finding"] for f in findings], ["content_differs"])
        self.assertIn("country=PL", findings[0]["detail"])

    def test_an_exact_match_reports_no_findings(self):
        same = [legacy_member(1, date_pub="20231115")]
        load_package(self.conn, write_package(self.dir / "a", same), "daily/a")
        load_package(self.conn, write_package(self.dir / "b", same), "daily/b")
        self.assertEqual(self.overlap("daily/a", "daily/b"), [])


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

    def ingest(self, package="daily/202300220", *, numbers=None, ojs="220/2023",
               publication_date="2023-11-15Z"):
        numbers = self.NUMBERS if numbers is None else numbers
        return ingest_package(
            self.new_conn(), package, data_root=self.dir, url=self.url,
            transport=FakeTransport([
                api_page(list(numbers), total=len(numbers), ojs=ojs,
                         publication_date=publication_date, token="page-2"),
                api_page([], total=len(numbers), ojs=ojs,
                         publication_date=publication_date, token="still-here"),
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


class MonthlyCoverageCalendarTests(IngestQueryTestCase):
    """Every calendar_state the query claims to distinguish, on real rows."""

    def calendar(self, first, last):
        text = _with_params(_CALENDAR, first_month=first, last_month=last)
        return {row["source_package_id"]: row for row in self.rows(text)}

    def api_pages(self, numbers, publication_date, ojs, *, total=None):
        total = len(numbers) if total is None else total
        return [
            api_page(numbers, total=total, ojs=ojs, publication_date=publication_date,
                     token="page-2"),
            api_page([], total=total, ojs=ojs, publication_date=publication_date,
                     token="still-here"),
        ]

    def test_the_calendar_distinguishes_every_state_it_claims_to(self):
        # 'processed': a full ingest run, checkpointed. self.archive (from
        # setUp) holds self.NUMBERS = (1, 2, 3), all dated 2023-11-15 by
        # legacy_member's default -- the script must report the same notices.
        self.server.payload = self.archive
        processed = self.ingest("monthly/2023-11", ojs="X")
        self.assertEqual(processed.outcome, "processed")

        # 'retired checkpoint': checkpointed, then force-recaptured directly.
        self.server.payload = package_bytes([legacy_member(1, date_pub="20231205")])
        retired = self.ingest(
            "monthly/2023-12", numbers=[1], ojs="X", publication_date="2023-12-05Z"
        )
        self.assertEqual(retired.outcome, "processed")
        load_package(
            self.new_conn(), self.dir / retired.artifact_path,
            "monthly/2023-12", force_recapture=True,
        )

        # 'verified, no checkpoint': loaded and verified directly, no ingest run.
        path = write_package(
            self.dir / "2024-01.tar.gz", [legacy_member(1, date_pub="20240110")]
        )
        loaded = load_package(self.new_conn(), path, "monthly/2024-01")
        verify_capture(
            self.new_conn(), loaded.capture_id,
            transport=FakeTransport(self.api_pages([1], "2024-01-10Z", "1/2024")),
        )

        # 'verification unavailable': the transport never reaches the source.
        path = write_package(
            self.dir / "2024-02.tar.gz", [legacy_member(1, date_pub="20240210")]
        )
        loaded = load_package(self.new_conn(), path, "monthly/2024-02")
        verify_capture(
            self.new_conn(), loaded.capture_id,
            transport=FakeTransport([TransientSourceError("connection reset")] * 3),
        )

        # 'discrepancy': the source reports an identifier this capture lacks.
        path = write_package(
            self.dir / "2024-03.tar.gz", [legacy_member(1, date_pub="20240310")]
        )
        loaded = load_package(self.new_conn(), path, "monthly/2024-03")
        verify_capture(
            self.new_conn(), loaded.capture_id,
            transport=FakeTransport(
                self.api_pages([1, 2], "2024-03-10Z", "3/2024", total=2)
            ),
        )

        # 'empty source, unconfirmed': both sides empty, never proof of publication.
        path = write_package(self.dir / "2024-04.tar.gz", [])
        loaded = load_package(self.new_conn(), path, "monthly/2024-04")
        verify_capture(
            self.new_conn(), loaded.capture_id,
            transport=FakeTransport([api_page([], total=0, ojs="4/2024", token=None)]),
        )

        # 'verification in progress': an attempt opened and never closed.
        path = write_package(
            self.dir / "2024-05.tar.gz", [legacy_member(1, date_pub="20240510")]
        )
        loaded = load_package(self.new_conn(), path, "monthly/2024-05")
        self.conn.execute(
            "insert into tl_work.verification_attempt (capture_id, artifact_sha256,"
            " contract_version, verifier_version, query_text, query_scope, state)"
            " select capture_id, artifact_sha256, contract_version, 'test',"
            " 'PD>=20240501 AND PD<=20240531', 'ALL', 'in_progress'"
            " from tl_work.capture where capture_id = %s",
            (loaded.capture_id,),
        )

        rows = self.calendar("2023-10-01", "2024-06-01")

        self.assertEqual(rows["monthly/2023-10"]["calendar_state"], "no package record")
        self.assertIsNone(rows["monthly/2023-10"]["checkpoint_notice_count"])
        self.assertEqual(rows["monthly/2023-11"]["calendar_state"], "processed")
        self.assertEqual(rows["monthly/2023-11"]["checkpoint_notice_count"], 3)
        self.assertEqual(rows["monthly/2023-12"]["calendar_state"], "retired checkpoint")
        self.assertEqual(rows["monthly/2024-01"]["calendar_state"], "verified, no checkpoint")
        self.assertEqual(rows["monthly/2024-02"]["calendar_state"], "verification unavailable")
        self.assertEqual(rows["monthly/2024-03"]["calendar_state"], "discrepancy")
        self.assertEqual(rows["monthly/2024-04"]["calendar_state"], "empty source, unconfirmed")
        self.assertEqual(rows["monthly/2024-04"]["checkpoint_notice_count"], None)
        self.assertEqual(rows["monthly/2024-05"]["calendar_state"], "verification in progress")
        self.assertEqual(rows["monthly/2024-06"]["calendar_state"], "no package record")

        # every month in the range appears, in order, exactly once
        self.assertEqual(len(rows), 9)


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
