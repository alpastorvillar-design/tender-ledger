"""What an orchestrator may ask this pipeline to do, decided without Airflow.

A Dag file is awkward to test: importing it needs a scheduler's runtime. The
decisions that actually matter -- which manifest a manual trigger may name,
what the ``ingest-manifest`` argument vector is, and whether a finished report
proves every package is processed -- are ordinary functions here instead, so
the suite covers them against real files and a real database.

Nothing in this module writes durable pipeline state. It resolves paths,
builds a command, runs it as a child process, and reads back what the command
and the database already recorded.
"""

import hashlib
import string
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .manifest import ManifestError, load_manifest
from .manifest_runner import manifest_sha256

#: The container's mount points, relative to the workspace root.
MANIFEST_DIRECTORY = "manifests"
DATA_DIRECTORY = "data"
REPORT_DIRECTORY = "reports"

_HOME_VARIABLE = "TL_ORCHESTRATION_HOME"

_SAFE_NAME_CHARACTERS = frozenset(string.ascii_letters + string.digits + "._-")
#: Long enough to keep a run identifier recognizable, short enough that the
#: name stays well inside any filesystem limit once the digest is appended.
_MAX_NAME_CHARACTERS = 60

#: The command reports through its JSON report, not its console output, so a
#: task log only has to carry enough of that output to explain a failure.
_MAX_LOG_LINES = 200
_MAX_LOG_LINE_CHARACTERS = 1000

#: The two outcomes that mean the package is processed: this run sealed its
#: checkpoint, or an existing one was re-validated and replayed.
_PROCESSED_OUTCOMES = frozenset({"processed", "replayed"})

#: How many package identities a failure message names before it counts the
#: rest, so a manifest of any size still produces a message worth reading.
_MAX_SAMPLE = 5


class OrchestrationError(RuntimeError):
    """The orchestrator asked for something this pipeline will not do."""


@dataclass(frozen=True)
class Workspace:
    """Where the orchestrated run reads and writes, inside its container.

    One variable names the root; the three directories under it are fixed, so
    a Dag cannot be pointed at a manifest directory and a data directory that
    belong to different checkouts.
    """

    home: Path
    manifests: Path
    data: Path
    reports: Path


def workspace_from_env(env: Mapping[str, str]) -> Workspace:
    """Resolve the workspace, refusing an environment that is not usable.

    Every directory has to be mounted already. Creating a missing one here
    would turn a wrong mount into an empty, silently useless run.
    """
    raw = env.get(_HOME_VARIABLE, "").strip()
    if not raw:
        raise OrchestrationError(f"{_HOME_VARIABLE} is not set")
    home = Path(raw).resolve()
    directories = {
        name: (home / name).resolve()
        for name in (MANIFEST_DIRECTORY, DATA_DIRECTORY, REPORT_DIRECTORY)
    }
    missing = sorted(name for name, path in directories.items() if not path.is_dir())
    if missing:
        raise OrchestrationError(
            f"{_HOME_VARIABLE} is {home}, but these directories are not mounted:"
            f" {', '.join(missing)}"
        )
    return Workspace(
        home=home,
        manifests=directories[MANIFEST_DIRECTORY],
        data=directories[DATA_DIRECTORY],
        reports=directories[REPORT_DIRECTORY],
    )


def resolve_manifest_path(home: Path, requested: str) -> Path:
    """Resolve ``requested`` against ``home``, refusing anything outside
    ``home/manifests``."""
    candidate = Path(requested)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise OrchestrationError(f"The manifest must be a relative path: {requested!r}")
    root = (Path(home) / MANIFEST_DIRECTORY).resolve()
    resolved = (Path(home) / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise OrchestrationError(
            f"The manifest must stay under {MANIFEST_DIRECTORY}/: {requested!r}"
        )
    if not resolved.is_file():
        raise OrchestrationError(f"No manifest file at {requested!r}")
    return resolved


def manifest_plan(home: Path, requested: str) -> dict:
    """Describe the manifest a run may execute, or refuse it.

    Returns only what a later task needs to find the same document again and
    prove it is the same one: a repository-relative path, its file name, the
    digest of its validated content, and how many packages it names. The
    manifest's own entries are deliberately not part of this, so nothing
    downstream can grow with the size of the manifest.
    """
    path = resolve_manifest_path(home, requested)
    try:
        manifest = load_manifest(path)
    except ManifestError as exc:
        raise OrchestrationError(str(exc)) from exc
    return {
        "manifest": path.relative_to(Path(home).resolve()).as_posix(),
        "file_name": path.name,
        "sha256": manifest_sha256(manifest),
        "package_count": len(manifest.entries),
    }


def report_file_name(run_identifier: str) -> str:
    """A report file name derived from an orchestrator's run identifier.

    Airflow run ids carry colons and plus signs, which are not portable file
    name characters, and two different ids can reduce to the same safe string.
    A digest of the original id is therefore part of the name: a report can
    always be traced back to one run, and no run can quietly overwrite
    another's report.
    """
    safe = "".join(
        character if character in _SAFE_NAME_CHARACTERS else "-"
        for character in run_identifier.strip()
    ).strip("-")
    if not safe:
        raise OrchestrationError(f"Unusable run identifier: {run_identifier!r}")
    digest = hashlib.sha256(run_identifier.encode("utf-8")).hexdigest()[:12]
    return f"manifest-run-{safe[:_MAX_NAME_CHARACTERS]}-{digest}.json"


def ingest_manifest_command(
    *,
    manifest: Path,
    report: Path,
    data_dir: Path,
    batch_size: int | None = None,
    lock_wait: bool = False,
    executable: str = sys.executable,
) -> list[str]:
    """The argument vector for the public ``ingest-manifest`` command.

    A list, never a shell string: no path, identity or option this Dag passes
    can be interpreted as another command, whatever a manifest name contains.
    """
    command = [
        executable, "-m", "tender_ledger", "ingest-manifest",
        "--manifest", str(manifest),
        "--report", str(report),
        "--data-dir", str(data_dir),
    ]
    if batch_size is not None:
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise OrchestrationError(f"batch_size must be a positive integer: {batch_size!r}")
        command += ["--batch-size", str(batch_size)]
    if lock_wait:
        command.append("--lock-wait")
    return command


def run_command(command: list[str], *, timeout: float, log: Callable[[str], None]) -> None:
    """Run ``command`` to completion, failing on anything but exit code 0.

    Output is captured rather than streamed: the command this Dag runs writes
    its result to a report file and prints nothing until it is done, so there
    is no progress to stream, and capturing keeps the task log bounded.

    A command that exceeds ``timeout`` is killed before this returns, so a
    task Airflow gives up on cannot leave an ingest running against the
    database behind it.
    """
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed the child and reaped it, so nothing
        # is left holding this package's advisory lock or writing to its data
        # directory once this propagates.
        raise OrchestrationError(
            f"The command exceeded its timeout of {timeout} seconds and was killed"
        ) from exc
    lines = (completed.stdout + completed.stderr).splitlines()
    for line in lines[:_MAX_LOG_LINES]:
        log(line[:_MAX_LOG_LINE_CHARACTERS])
    if len(lines) > _MAX_LOG_LINES:
        log(f"... {len(lines) - _MAX_LOG_LINES} further output lines suppressed")
    if completed.returncode != 0:
        raise OrchestrationError(f"The command exited {completed.returncode}")


def summarize_report(document: dict, *, expected_sha256: str, expected_entries: int) -> dict:
    """Reduce a finished ``ingest-manifest`` report to a bounded summary.

    The command's exit code already says whether it succeeded. This says the
    same thing from the report it wrote, and refuses anything it cannot
    account for.
    """
    reported = document["manifest"]["sha256"]
    if reported != expected_sha256:
        raise OrchestrationError(
            f"The report names manifest sha256 {reported}, not the validated"
            f" {expected_sha256}"
        )
    if document["status"] != "completed":
        raise OrchestrationError(
            f"The manifest run is {document['status']!r}, stopped at"
            f" {document['failed_entry']!r}"
        )

    entries = document["entries"]
    if len(entries) != expected_entries:
        raise OrchestrationError(
            f"The report covers {len(entries)} packages, not the {expected_entries}"
            " the manifest names"
        )
    for entry in entries:
        if entry["outcome"] not in _PROCESSED_OUTCOMES:
            raise OrchestrationError(
                f"{entry['source_package_id']} ended as {entry['outcome']!r}"
            )
        if entry["checkpoint_is_current"] is not True:
            raise OrchestrationError(
                f"{entry['source_package_id']} has no current checkpoint"
            )

    outcomes: dict[str, int] = {}
    for entry in entries:
        outcomes[entry["outcome"]] = outcomes.get(entry["outcome"], 0) + 1
    return {
        "report": document["manifest"]["file_name"],
        "manifest_sha256": document["manifest"]["sha256"],
        "entry_count": len(entries),
        "outcomes": outcomes,
        "http_attempts": sum(entry["http_attempts"] or 0 for entry in entries),
        "downloaded_bytes": sum(entry["downloaded_bytes"] or 0 for entry in entries),
        "duration_seconds": round(document["duration_seconds"], 3),
    }


def checkpoint_summary(conn: psycopg.Connection, identities: Sequence[str]) -> dict:
    """Confirm every named package still has a current checkpoint.

    The report says what the command observed while it ran; this asks the
    database what is true now, through the same public view an analyst reads.
    A checkpoint a later re-acquisition or a later failed coverage check has
    retired is not accepted here, whatever the report said.

    ``checkpoint_notices`` sums each package's own count, so a notice carried
    by two overlapping packages is counted twice. It says how much these
    checkpoints account for, not how many distinct notices exist --
    ``tl_read.distinct_notice`` answers that.
    """
    wanted = list(dict.fromkeys(identities))
    with conn.cursor() as cur:
        cur.execute(
            "select source_package_id, checkpoint_is_current, checkpoint_notice_count"
            " from tl_read.package_ingest_status where source_package_id = any(%s)",
            (wanted,),
        )
        rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    missing = [identity for identity in wanted if identity not in rows]
    if missing:
        raise OrchestrationError(
            f"The status view has no row for {_sample(missing)}"
        )
    stale = [identity for identity in wanted if rows[identity][0] is not True]
    if stale:
        raise OrchestrationError(
            f"No current checkpoint for {_sample(stale)}"
        )
    return {
        "packages": len(wanted),
        "checkpoint_notices": sum(rows[identity][1] or 0 for identity in wanted),
    }


def _sample(identities: list[str]) -> str:
    """Name a few of them, and say how many were left out."""
    shown = ", ".join(identities[:_MAX_SAMPLE])
    if len(identities) <= _MAX_SAMPLE:
        return shown
    return f"{shown} and {len(identities) - _MAX_SAMPLE} more"
