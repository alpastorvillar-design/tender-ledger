# Manifest and sequential backfill

Status: a versioned manifest format and a sequential runner over `ingest`
exist and are tested against fixtures and a local server. `manifests/m3-pilot.json`
names five real identities; running it for real stopped at the first one,
`monthly/2020-01`, which is not loadable by the current code -- see
[measured-rehearsal.md](measured-rehearsal.md). Listing an identity in the
manifest is not evidence it was downloaded, loaded or verified. That
evidence, if it exists, is in `tl_read.package_ingest_status` and in a report
this command wrote, not in the manifest file.

```sh
python -m tender_ledger ingest-manifest --manifest manifests/m3-pilot.json
python -m tender_ledger ingest-manifest --manifest manifests/m3-pilot.json --report report.json
```

Exit code 0 means every package in the manifest reported a current, processed
or replayed checkpoint, in order. Any other outcome exits 1.

## Why a fourth command

`ingest` processes one named package. A rehearsal or a backfill needs several,
in a fixed order, without inventing a second recovery mechanism on top of the
one `ingest` already has. `ingest-manifest` adds nothing durable of its own: no
manifest-execution table, no cross-package transaction. It calls `ingest` once
per entry and stops at the first one that is not a processed package, so
everything a retry needs is already sitting in `tl_work.ingest_run` and
`tl_work.package_checkpoint` for that specific package.

## The manifest format

```json
{
  "manifest_version": 1,
  "packages": [
    {
      "order": 1,
      "source_package_id": "monthly/2020-01",
      "notice_count_observed": 50123,
      "compressed_bytes_observed": 139301298,
      "observed_at": "2026-09-04",
      "purpose": "legacy"
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `manifest_version` | Must equal `1`, the only schema this runner accepts |
| `packages` | A non-empty, ordered array |
| `order` | The entry's 1-based position; it must equal that position, a redundant check against silent reordering |
| `source_package_id` | A canonical `daily/YYYYNNNNN` or `monthly/YYYY-MM` identity |
| `notice_count_observed`, `compressed_bytes_observed`, `observed_at`, `purpose` | Planning metadata: what was seen when the manifest was written |

No other key is accepted at either level -- a URL, an output path or a
credential is not a field this format has, so one present in a document is
rejected the same way an unknown key would be. Rejected before any network or
database access: invalid JSON, an unsupported `manifest_version`, an extra or
missing key, an empty package list, a non-canonical identity, a duplicate
identity, an `order` that does not match its position, and a wrong type
anywhere.

The planning metadata remains in the manifest and is covered by its report
digest, but is not copied into each result row or read by any decision. Nothing
compares it against what a run observes; changing it changes nothing about
which packages run, in what order, or with what result. It exists so a manifest
is self-describing without becoming a second source of truth about outcomes --
`manifests/m3-pilot.json`'s counts and bytes are the
`P2_M3_CONTRACT.md` planning numbers, current as of the date recorded, and nothing
downstream treats them as a target, a gate, or evidence that a download
happened.

## What the runner does

For each entry, in order, with concurrency one:

1. Open one PostgreSQL connection.
2. Call `ingest_package` for that entry's identity with the run's shared
   options (`--data-dir`, `--batch-size`, `--lock-wait`); this is the same
   function [`ingest`](ingestion.md) calls, unmodified.
3. Close the connection, whether the call returned or raised.
4. If the result is not a current checkpoint with outcome `processed` or
   `replayed`, or the call raised, stop. The entries after that point are
   never attempted -- `ingest_package` is never called for them, so no
   connection is opened and no HTTP request is made on their behalf.

At most one connection is open at any time, and every package gets its own:
a stale or held connection from one package can never leak into the next.
There is no manifest-level lock, transaction or table; recovery for any one
package is exactly [`ingest`'s recovery](ingestion.md#recovery-one-boundary-at-a-time).

## Recovery across a manifest

A second run over the same manifest is a second `ingest_package` call per
identity, nothing more:

| Package's prior state | What the second run does |
| --- | --- |
| Current checkpoint, matching contract | Replayed: the stored artifact is re-validated and nothing is requested |
| Interrupted mid-flow (any phase before `completed`) | Resumed from that phase, exactly as a bare `ingest` retry would be |
| Never attempted, because an earlier entry stopped the manifest | Processed for the first time |
| Checkpoint sealed under an older projection contract | Declined as a replay and reprojected under the current one -- a *new* capture, not resumed or overwritten evidence |

A package is reported "skipped" (its `outcome` is `replayed`) only when
`ingest_package`'s own contract-aware replay accepted it -- a checkpoint from
an older contract version is never treated as current here, because the
runner adds no check of its own on top of that one.

## The report

JSON, written to stdout or to `--report PATH` -- never both, and never split
across the two. It carries:

* `report_version`, and an echo of which manifest ran (`manifest_version`,
  file name, SHA-256 of its validated content, and package count) -- not a copy
  of the manifest's own content or an absolute private path;
* `started_at`, `finished_at`, `duration_seconds`;
* `status` (`"completed"` or `"failed"`) and `failed_entry`, the identity that
  stopped the run, or `null`;
* one row per **attempted** entry, in order: `order`, `source_package_id`,
  `outcome` (`processed`, `replayed`, `incomplete`, or `error` for an entry
  where the call raised instead of returning), the run/capture/verification
  IDs and phase when they exist, `http_attempts`, `downloaded_bytes`, and a
  bounded `error` string (at most 2,000 characters; never a full traceback, an
  HTTP body, a secret, or an absolute private path).

`http_attempts` and `downloaded_bytes` count the acquisition, not the artifact:
requests made and bytes received across all of them, reported the same way
whether it succeeded or failed. A body that arrived in full and was then refused
as not a package therefore reads as the transfer it was, and a retried
acquisition reports more bytes than the artifact holds.

An entry after the one that stopped the manifest never appears -- not marked
skipped, simply absent, because it was never attempted.

`--report` replaces the target file atomically: the report is written to an
exclusive temporary file beside it, fsynced, then renamed into place. The
parent directory is never created automatically -- only a path whose parent
already exists is accepted, so a typo fails loudly instead of leaving a report
in an unexpected new directory. A failure while writing the report also exits
non-zero.

## Deliberately not done here

Parallelism, date-range or calendar-driven manifest generation, a global
retry across packages, a manifest-execution table, and any flag that would
relax the resource budgets `ingest` already enforces per package. The
100,000-notice rehearsal, the million-notice gate, storage/WAL/duration
measurement, and the SQL benchmark are later slices; see
[scale-and-sql.md](scale-and-sql.md).
