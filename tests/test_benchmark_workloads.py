"""The benchmark harness itself: determinism checking, checksums, and the six
workloads running end to end against tiny fixtures -- never against a real
TED download."""

import subprocess
import sys
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
from tender_ledger.loader import load_package

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import benchmark_workloads as bw  # noqa: E402


def setUpModule():
    ensure_test_database()


class RenderQueryTests(unittest.TestCase):
    def test_substitutes_every_named_token(self):
        text = "select * from t where a = :'x' and b = :'y'"
        rendered = bw.render_query(text, x="1", y="daily/202300220")
        self.assertEqual(rendered, "select * from t where a = '1' and b = 'daily/202300220'")

    def test_quotes_a_parameter_as_data(self):
        rendered = bw.render_query("select :'value'", value="a'; select 2; --")
        self.assertEqual(rendered, "select 'a''; select 2; --'")

    def test_a_token_with_no_matching_param_is_left_untouched(self):
        rendered = bw.render_query("select :'only'", only="42")
        self.assertEqual(rendered, "select '42'")


class DeterminismTests(unittest.TestCase):
    def setUp(self):
        try:
            self.conn = db.connect(load_config(dbname=TEST_DB))
        except Exception as exc:  # pragma: no cover - environment-dependent
            self.skipTest(str(exc))
        self.addCleanup(self.conn.close)

    def test_a_stable_query_reports_one_checksum_across_repetitions(self):
        result = bw.timed_repetitions(
            self.conn, "select 1 as a, 'x' as b", warmup=1, repetitions=5
        )
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["repetitions"], 5)
        self.assertIsNotNone(result["checksum_sha256"])
        self.assertEqual(len(result["durations_ms"]), 5)
        self.assertLessEqual(result["median_ms"], result["max_ms"])

    def test_a_nondeterministic_query_is_caught_not_silently_averaged(self):
        # generate_series with no ORDER BY has no defined row order across
        # separate executions on the same tiny relation size used here; this
        # exercises the harness's own defence, not a real workload.
        with self.assertRaises(RuntimeError):
            bw.timed_repetitions(
                self.conn,
                "select array(select i from generate_series(1, 20) as g(i)"
                " order by random()) as shuffled",
                warmup=0,
                repetitions=8,
            )

    def test_repetitions_must_be_positive(self):
        with self.assertRaises(ValueError):
            bw.timed_repetitions(self.conn, "select 1", warmup=0, repetitions=0)

    def test_warmup_must_not_be_negative(self):
        with self.assertRaises(ValueError):
            bw.timed_repetitions(self.conn, "select 1", warmup=-1, repetitions=1)


class RunBenchmarkTests(unittest.TestCase):
    """All six workloads, over two tiny published packages with real overlap."""

    def setUp(self):
        try:
            self.conn = db.connect(load_config(dbname=TEST_DB))
        except Exception as exc:  # pragma: no cover - environment-dependent
            self.skipTest(str(exc))
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)
        self.tmp_dir = self._make_tmp_dir()

        daily_members = [
            legacy_member(1, year=2023, country="PL", cpv=("79000000",),
                          date_pub="20231115"),
            eforms_member(2, year=2023, buyer_country="DEU", main_cpv="72000000",
                          pub_date="2023-11-15Z",
                          change_refs=[("1-2023", None)]),
        ]
        monthly_members = [
            eforms_member(2, year=2023, buyer_country="DEU", main_cpv="72000000",
                          pub_date="2023-11-15Z", change_refs=[("1-2023", None)]),
            eforms_member(3, year=2023, buyer_country="FRA", main_cpv="45000000",
                          pub_date="2023-11-20Z"),
        ]
        daily_path = write_package(self.tmp_dir / "daily.tar.gz", daily_members)
        monthly_path = write_package(self.tmp_dir / "monthly.tar.gz", monthly_members)

        self.daily = load_package(self.conn, daily_path, "daily/202300220")
        self.monthly = load_package(self.conn, monthly_path, "monthly/2023-11")
        self.assertEqual(self.daily.status, "published")
        self.assertEqual(self.monthly.status, "published")

    def _make_tmp_dir(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _workloads(self):
        cutoff = str(self.monthly.acquisition_ordinal)
        return (
            bw.Workload("monthly_notice_counts", "monthly_notice_counts.sql"),
            bw.Workload("latest_capture_per_publication", "latest_capture_per_publication.sql"),
            bw.Workload("official_change_references", "official_change_references.sql"),
            bw.Workload(
                "acquisition_cutoff_state", "acquisition_cutoff_state.sql", {"cutoff": cutoff}
            ),
            bw.Workload(
                "monthly_coverage_calendar", "monthly_coverage_calendar.sql",
                {"first_month": "2023-11-01", "last_month": "2023-11-01"},
            ),
            bw.Workload(
                "cross_package_overlap_audit", "cross_package_overlap_audit.sql",
                {"package_a": "daily/202300220", "package_b": "monthly/2023-11"},
            ),
        )

    def test_every_workload_runs_and_reports_a_plan_and_checksum(self):
        report = bw.run_benchmark(
            self.conn, workloads=self._workloads(), warmup=1, repetitions=2,
            effective_cache_size="64MB", label="fixture-baseline",
        )
        self.assertEqual(report["label"], "fixture-baseline")
        self.assertEqual(report["session_settings"]["effective_cache_size"]["setting"], "8192")
        names = [w["name"] for w in report["workloads"]]
        self.assertEqual(
            names,
            [
                "monthly_notice_counts", "latest_capture_per_publication",
                "official_change_references", "acquisition_cutoff_state",
                "monthly_coverage_calendar", "cross_package_overlap_audit",
            ],
        )
        for workload in report["workloads"]:
            self.assertIsInstance(workload["plan"], dict)
            self.assertIn("Plan", workload["plan"])
            self.assertGreaterEqual(workload["row_count"], 0)
            self.assertIsNotNone(workload["checksum_sha256"])

        overlap = next(w for w in report["workloads"] if w["name"] == "cross_package_overlap_audit")
        self.assertGreater(overlap["row_count"], 0, "the two fixture packages share a notice")

    def test_a_candidate_index_changes_no_workload_s_checksum(self):
        workloads = self._workloads()
        before = bw.run_benchmark(
            self.conn, workloads=workloads, warmup=1, repetitions=2,
            effective_cache_size="64MB", label="before",
        )
        candidate_sql = (REPO_ROOT / "queries" / "benchmark_candidate_indexes.sql").read_text(
            encoding="utf-8"
        )
        with self.conn.transaction():
            self.conn.execute(candidate_sql)
        bw.analyze(self.conn)
        after = bw.run_benchmark(
            self.conn, workloads=workloads, warmup=1, repetitions=2,
            effective_cache_size="64MB", label="after",
        )
        before_checksums = {w["name"]: w["checksum_sha256"] for w in before["workloads"]}
        after_checksums = {w["name"]: w["checksum_sha256"] for w in after["workloads"]}
        self.assertEqual(before_checksums, after_checksums)


class BenchmarkCliTests(unittest.TestCase):
    def test_cli_writes_a_json_report_to_the_requested_file(self):
        import json
        import tempfile

        try:
            with db.connect(load_config(dbname=TEST_DB)) as conn:
                truncate_all(conn)
        except Exception as exc:  # pragma: no cover - environment-dependent
            self.skipTest(str(exc))

        config = load_config(dbname=TEST_DB)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            result = subprocess.run(
                [
                    sys.executable, str(REPO_ROOT / "scripts" / "benchmark_workloads.py"),
                    "--dbname", config.dbname, "--host", config.host, "--port", str(config.port),
                    "--user", config.user, "--password", config.password,
                    "--warmup", "0", "--repetitions", "1",
                    "--cutoff", "0",
                    "--first-month", "2023-11-01", "--last-month", "2023-11-01",
                    "--package-a", "daily/none-a", "--package-b", "daily/none-b",
                    "--output", str(output),
                ],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(report["workloads"]), 6)


if __name__ == "__main__":
    unittest.main()
