# Transactional package loading

Status: implemented for a single package into an unpartitioned PostgreSQL
baseline. API coverage verification, the historical run, and orchestration are
later slices.

## Data flow

```text
package .tar.gz
      | sha256 of the whole file
      v
begin_capture ---------> tl_work.capture row (status 'acquiring'), identity persisted
      |                   acquisition_ordinal from a global sequence
      v
stream members  -------> project each (see projection.md), no XML written to disk
      |
      v
load_batch (xN) -------> COPY rows + capture_batch record, committed together
      |                   status -> 'loading'
      v
re-hash the file ------> unchanged, or the capture fails
      |
      v
reconcile -------------> member_count == distinct keys == loaded rows, or fail; status -> 'loaded'
      |
      v
publish --------------> one transaction: supersede prior capture, swap
                         tl_work.published_capture pointer, status -> 'published'
```

## Schema

`tl_work` holds the working tables; `tl_read` holds views only. The
`tender_ledger_reader` role has `USAGE` on `tl_read` and `SELECT` on its views and
nothing else - views run with the owner's rights, so a reader sees published
notices through `tl_read.notice` but cannot select `tl_work.notice_capture`.

* `tl_read.notice` - one row per published notice per source package. Daily and
  monthly packages overlap, so a canonical identity can appear more than once
  here.
* `tl_read.distinct_notice` - one row per canonical identity across all published
  packages, resolved to the most recently acquired capture. This is the honest
  distinct-notice count; repeated package content does not inflate it.
* `tl_read.capture_status` - every capture and whether it is the published one.
  No notice rows, so partial captures stay internal.

## Guarantees

| Situation | Behavior |
| --- | --- |
| Replay of the same published artifact | No-op; the existing capture is returned, no new identity |
| Crash mid-load, same artifact | `begin_capture` resumes the `loading` capture; committed batches are skipped; the final state matches a clean run |
| Intentional re-acquisition (A -> B -> A) | Each is a new capture with its own `acquisition_ordinal`; all three are kept |
| Duplicate canonical identity in a package | The batch violates the notice primary key, rolls back, and the capture fails |
| Corrupt / truncated / mid-run-modified archive | `PackageError`; the capture fails and is never published |
| Reader during an incomplete replacement | Still sees the previous complete capture until `publish` commits |
| Failed publish transaction | Visibility and completion status unchanged |
| Recapture with fewer members (a retired notice) | The replacement capture is complete on its own members; the old capture's extra rows are not merged in |
| Empty local package | Publishes as a complete capture with `source_coverage_verified = false` - distinct from a failure |
| Two concurrent captures of one package | Serialized by a session advisory lock; the second is refused (`ConcurrentCaptureError`) unless `--lock-wait` |

"Artifact fully loaded" is tracked separately from "coverage verified against
TED". The API verifier is a later slice; until it runs, `source_coverage_verified`
is `false` on every view and output.

## CLI

```sh
python -m tender_ledger db upgrade
python -m tender_ledger load path/to/daily-package.tar.gz --package-id daily/202300220
python -m tender_ledger status --package-id daily/202300220
```

Connection settings come from `TL_DB_*` / `POSTGRES_*` environment variables or
the local `.env`; see `config.py`.

## Deliberately deferred

Annual partitioning (the baseline keeps year in the natural key so a partitioned
table can be proven equivalent later), indexes tuned against measured plans, the
six-query workload and its benchmark, API reconciliation, backfill, and Airflow.
