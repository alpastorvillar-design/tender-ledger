"""Run one Tender Ledger manifest under Airflow, without owning its state.

Airflow schedules the work, retries a task whose process is lost, and shows
what happened. It is not a second state machine: acquisition, loading,
reconciliation, publication and checkpoints stay entirely inside
``ingest-manifest``, which this Dag calls exactly once per run and which
already recovers a package from whatever it durably reached.

That is also why there is no task per package. The manifest is sequential and
stops at the first package it cannot process; expanding it into mapped tasks
would need Airflow to guarantee an execution order it does not guarantee, and
would put a copy of the pipeline's own progress into XCom and the metadata
database.
"""

import datetime as dt
import json
import logging
import os

from airflow.sdk import DAG, Param, get_current_context, task

from tender_ledger import db
from tender_ledger.manifest import load_manifest
from tender_ledger.manifest_runner import manifest_sha256
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

log = logging.getLogger(__name__)

DAG_ID = "tender_ledger_manifest"

#: The 26 packages whose coverage the source confirmed exactly. The 27-entry
#: audit manifest is deliberately not the default: it stops at the package TED
#: itself cannot reconcile, which is a source finding to reproduce on purpose,
#: not a run to schedule by accident.
DEFAULT_MANIFEST = "manifests/m3-scale-verified.json"

#: A complete replay of that manifest was measured at 740.8 s and a cold run of
#: the same packages at 8,106.8 s. Four hours covers a cold run with room to
#: spare while still ending a task that is stuck, rather than letting it hold
#: the single run slot indefinitely.
INGEST_TIMEOUT = dt.timedelta(hours=4)

#: The child is killed slightly before Airflow would give up on the task, so
#: the failure is this Dag's own bounded message and no ingest survives it.
COMMAND_TIMEOUT_SECONDS = INGEST_TIMEOUT.total_seconds() - 300


def _report_name() -> str:
    """This run's report file name, derived from the run id in both tasks so
    no filesystem path has to travel through XCom."""
    return report_file_name(get_current_context()["dag_run"].run_id)


@task(retries=0)
def validate_manifest() -> dict:
    """Accept or refuse the manifest before anything is downloaded or opened.

    Not retried: a manifest this rejects is rejected for a reason that a second
    attempt one minute later cannot change.
    """
    workspace = workspace_from_env(os.environ)
    plan = manifest_plan(workspace.home, get_current_context()["params"]["manifest"])
    log.info(
        "Manifest %s validated: %d packages, sha256 %s",
        plan["manifest"], plan["package_count"], plan["sha256"],
    )
    return plan


@task(retries=1, retry_delay=dt.timedelta(minutes=1), execution_timeout=INGEST_TIMEOUT)
def ingest_manifest(plan: dict) -> dict:
    """Run the public command once, and fail on anything but exit code 0.

    A retry re-runs the same command. Nothing here decides what that means for
    a package: a current checkpoint replays without a request, an interrupted
    package resumes from its own durable phase, and neither depends on what
    this task remembers.
    """
    workspace = workspace_from_env(os.environ)
    report = workspace.reports / _report_name()
    command = ingest_manifest_command(
        manifest=resolve_manifest_path(workspace.home, plan["manifest"]),
        report=report,
        data_dir=workspace.data,
        expected_sha256=plan["sha256"],
    )
    log.info("Ingesting %s into report %s", plan["manifest"], report.name)
    started = dt.datetime.now(dt.UTC)
    try:
        run_command(command, timeout=COMMAND_TIMEOUT_SECONDS, log=log.info)
    except OrchestrationError as exc:
        raise OrchestrationError(
            f"{exc}; the report {report.name} records how far it got"
        ) from exc
    seconds = (dt.datetime.now(dt.UTC) - started).total_seconds()
    log.info("ingest-manifest finished in %.1f s", seconds)
    return {"seconds": round(seconds, 1)}


@task(retries=1, retry_delay=dt.timedelta(minutes=1))
def summarize_run(plan: dict, ingest: dict) -> dict:
    """Say what the run achieved, from the report and from the database.

    The report describes what the command saw while it ran; the status view
    says what is true now. A checkpoint retired between the two -- by a
    re-acquisition, or by a coverage check that stopped agreeing -- fails here
    even though the command exited 0.
    """
    workspace = workspace_from_env(os.environ)
    document = json.loads((workspace.reports / _report_name()).read_text(encoding="utf-8"))
    summary = summarize_report(
        document,
        expected_sha256=plan["sha256"],
        expected_entries=plan["package_count"],
    )

    manifest = load_manifest(resolve_manifest_path(workspace.home, plan["manifest"]))
    if manifest_sha256(manifest) != plan["sha256"]:
        raise OrchestrationError(
            f"{plan['manifest']} changed while the run was in progress"
        )
    with db.connect() as conn:
        summary |= checkpoint_summary(
            conn, [entry.source_package_id for entry in manifest.entries]
        )

    summary["command_seconds"] = ingest["seconds"]
    log.info(
        "%s: %s over %d packages, %d notices in checkpoints, %d HTTP attempts,"
        " %d bytes, %.1f s",
        plan["manifest"], summary["outcomes"], summary["packages"],
        summary["checkpoint_notices"], summary["http_attempts"],
        summary["downloaded_bytes"],
        summary["duration_seconds"],
    )
    return summary


with DAG(
    dag_id=DAG_ID,
    description="Ingest every package a Tender Ledger manifest names, in order",
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["tender-ledger"],
    params={
        "manifest": Param(
            DEFAULT_MANIFEST,
            type="string",
            title="Manifest",
            description="A repository-relative path under manifests/",
        )
    },
    doc_md=__doc__,
):
    plan = validate_manifest()
    summarize_run(plan, ingest_manifest(plan))
