"""The Dag's contract, asserted inside the pinned Airflow image.

These tests need a real Airflow runtime, so they are not part of the suite
`scripts/run_tests.py` gates on. They run in the image the stack actually uses:

    docker compose -f compose.yaml -f compose.airflow.yaml run --rm --no-deps \
        airflow-scheduler python -m unittest discover -s /opt/tender-ledger/tests_airflow

An Airflow that cannot be imported makes them error, never skip.
"""

import datetime as dt
import unittest

from airflow.dag_processing.dagbag import DagBag

DAG_FOLDER = "/opt/airflow/dags"
DAG_ID = "tender_ledger_manifest"


class DagContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bag = DagBag(dag_folder=DAG_FOLDER)

    def dag(self):
        dag = self.bag.dags.get(DAG_ID)
        self.assertIsNotNone(dag, f"{DAG_ID} is not in {sorted(self.bag.dags)}")
        return dag

    def test_the_dag_folder_imports_without_errors(self):
        self.assertEqual(self.bag.import_errors, {})

    def test_nothing_starts_the_run_except_a_person(self):
        dag = self.dag()

        self.assertEqual(type(dag.timetable).__name__, "NullTimetable")
        self.assertFalse(dag.timetable.can_be_scheduled)
        self.assertFalse(dag.catchup)

    def test_one_run_at_a_time(self):
        self.assertEqual(self.dag().max_active_runs, 1)

    def test_the_run_validates_then_ingests_then_summarizes(self):
        dag = self.dag()

        self.assertEqual(
            sorted(dag.task_dict), ["ingest_manifest", "summarize_run", "validate_manifest"]
        )
        self.assertEqual(dag.get_task("validate_manifest").upstream_task_ids, set())
        self.assertEqual(
            dag.get_task("ingest_manifest").upstream_task_ids, {"validate_manifest"}
        )
        # The summary reads the validated plan as well as the finished run, so
        # it depends on both; it still cannot start before the ingest task ends.
        self.assertEqual(
            dag.get_task("summarize_run").upstream_task_ids,
            {"validate_manifest", "ingest_manifest"},
        )
        self.assertEqual(dag.get_task("summarize_run").downstream_task_ids, set())

    def test_the_ingest_task_retries_once_under_a_bounded_timeout(self):
        ingest = self.dag().get_task("ingest_manifest")

        self.assertEqual(ingest.retries, 1)
        self.assertEqual(ingest.retry_delay, dt.timedelta(minutes=1))
        self.assertEqual(ingest.execution_timeout, dt.timedelta(hours=4))

    def test_validating_a_manifest_is_not_retried(self):
        self.assertEqual(self.dag().get_task("validate_manifest").retries, 0)

    def test_the_default_manifest_is_the_verified_scale_selection(self):
        self.assertEqual(
            self.dag().params["manifest"], "manifests/m3-scale-verified.json"
        )


if __name__ == "__main__":
    unittest.main()
