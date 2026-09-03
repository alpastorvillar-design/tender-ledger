# Transactional package loading

Status: implemented for a single package into an unpartitioned PostgreSQL
baseline. Coverage against the Search API is a separate command, described in
[verification.md](verification.md); acquiring the archive and checkpointing the
whole flow is a third, described in [ingestion.md](ingestion.md). The historical
run and orchestration are later slices.

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

Memory over one package is one XML member at a time plus the reconciliation sets:
one canonical key per row written. That second term is O(notices in the package),
not O(batch), and it is deliberate — it is what proves a stream reassembled after
a restart produces the same partition, where the primary key cannot fire for the
batches a resume skips. It is a term to measure, not one to remove.

## Connection contract

The loader and the repository own their transaction boundaries, so they require
a connection that has none of its own: **autocommit and idle**, which is what
`db.connect()` returns. Each atomic unit - a batch, the reconcile, the publish -
is an explicit `with conn.transaction()` block, so it is a real `COMMIT` visible
to other connections the moment it returns. A committed batch survives the writer
disconnecting; nothing here commits or rolls back a transaction the caller opened.

Autocommit alone is not sufficient, so it is not what is checked. Inside
`with conn.transaction()` the flag stays true while every block below it becomes
a savepoint: the load would report a published capture that no other session can
see, release the advisory lock before the real commit, and lose everything on the
caller's rollback. A connection already in a transaction is refused - before any
write and before the lock - with `ConnectionStateError`; the caller's own unit of
work is left untouched and needs a separate connection. `db.migrate` refuses the
same way.

## Failure classification

A database error that cancels or loses work in flight (`57xxx` including
`statement_timeout`, deadlock and serialization failures, `08xxx` connection loss,
`53xxx` resource exhaustion) says nothing about the artifact: committed batches
and the capture identity stay valid, so the capture keeps its recoverable state
and the result carries `load_error`. An error that condemns the artifact - a
duplicate canonical identity, a constraint violation, unreadable XML, a
reconciliation mismatch - marks the capture `failed`, which no retry resumes.
Either way `load` reports the error and exits non-zero. If the connection itself
is gone, the original error is raised rather than replaced by the failure of the
bookkeeping write that could not happen on it.

## Schema

`tl_work` holds the working tables; `tl_read` holds views only. The
`tender_ledger_reader` role has `USAGE` on `tl_read` and `SELECT` on its views and
nothing else - views run with the owner's rights, so a reader sees published
notices through `tl_read.notice` but cannot select `tl_work.notice_capture`.
Migration `0002` persists the batch size on the capture so a resumed load splits
the archive the same way even if `--batch-size` changes. Migration `0003` adds
the coverage attempt history behind `source_coverage_verified`. Migration `0004`
adds the ingest runs and the package checkpoint described in
[ingestion.md](ingestion.md), and the composite unique constraints its foreign
keys point at; captures, batches, notices and the published pointer are
unchanged by it.

* `tl_read.notice` - one row per published notice per source package. Daily and
  monthly packages overlap, so a canonical identity can appear more than once
  here.
* `tl_read.distinct_notice` - one row per canonical identity across all published
  packages, resolved to the most recently acquired capture. This is the honest
  distinct-notice count; repeated package content does not inflate it.
* `tl_read.capture_status` - every capture, whether it is the published one, and
  the state of its most recent coverage verification. No notice rows, so partial
  captures stay internal.
* `tl_read.verification_attempt` - one row per coverage verification attempt.
  See [verification.md](verification.md).

## Guarantees

| Situation | Behavior |
| --- | --- |
| Replay of the same published artifact | No-op; the existing capture is returned, no new identity |
| Crash mid-load, same artifact | `begin_capture` resumes the `loading` capture; committed batches are skipped and not rewritten; the final state matches a clean run |
| Transient database error mid-load (cancelled statement, deadlock, lost connection) | The capture keeps its committed batches and identity; the result carries `load_error` and a non-zero exit; a retry resumes it |
| Terminal database error mid-load (duplicate identity, constraint violation) | The capture is marked `failed` and is never resumed; a re-run acquires a new capture |
| Crash between reconcile and publish | The `loaded` capture is durable; a retry resumes it and only publishes |
| Publish fails (DB error or a rejecting `before_publish` hook) | Capture stays `loaded` and retriable; the result carries `publish_error`; the previous capture stays visible |
| Intentional re-acquisition (`--force-recapture`, A -> B -> A) | Each is a new capture with its own `acquisition_ordinal`; identical bytes still get a new identity; all are kept |
| Interrupted re-acquisition of identical bytes | The retry resumes that capture, not the older published one it supersedes: recovery selects the most recently acquired capture first |
| Open attempt that a later capture has completed past | Left alone; a re-run acquires a new capture rather than publishing work acquired before the current one |
| Resuming a capture written before migration `0002` | Refused before writing: its batch partition was never recorded, so the ordinals cannot be lined up. Publishing an already `loaded` one still works |
| Duplicate canonical identity in a package | The batch violates the notice primary key, rolls back with nothing left, and the capture fails |
| Corrupt / truncated / mid-run-modified archive | `PackageError`; the capture fails and is never published |
| Reader during an incomplete replacement | Still sees the previous complete capture until `publish` commits |
| Recapture with fewer members (a retired notice) | The replacement capture is complete on its own members; the old capture's extra rows are not merged in |
| Empty local package | Publishes as a complete capture with `source_coverage_verified = false` - distinct from a failure |
| Verification running on the package | Shares the loader's advisory lock, so a concurrent load or re-acquisition is refused (or waits with `--lock-wait`) |
| Two concurrent captures of one package | Serialized by a session advisory lock held until publish commits; the second is refused (`ConcurrentCaptureError`) unless `--lock-wait` |
| Different `--batch-size` on a resumed load | The size persisted on the capture wins; the requested value is ignored |

"Artifact fully loaded" is tracked separately from "coverage verified against
TED". `load` never sets `source_coverage_verified`: a freshly published capture
is `false` on the views, on `status`, and on the `LoadResult` returned by `load`
(including replay and failure) until `verify` succeeds against the API. See
[verification.md](verification.md).

## CLI

```sh
python -m tender_ledger db upgrade
python -m tender_ledger inspect path/to/package.tar.gz --package-id monthly/2020-01 --survey
python -m tender_ledger load path/to/package.tar.gz --package-id daily/202300220
python -m tender_ledger load path/to/package.tar.gz --package-id daily/202300220 --force-recapture
python -m tender_ledger verify --capture-id 1
python -m tender_ledger status --package-id daily/202300220
python -m tender_ledger ingest --package-id daily/202300220   # all of the above, checkpointed
```

`load` re-runs are safe: an unfinished capture of the same archive is resumed and
an already published one is a no-op. `--force-recapture` is the explicit way to
acquire the same package again as a new capture; if that recapture is interrupted,
a plain `load` resumes it. Anything other than `published` exits 1 and prints the
reason on stderr.

Connection settings come from `TL_DB_*` / `POSTGRES_*` environment variables or
the local `.env`; see `config.py`.

## Surveying an archive before loading it

A load is all-or-nothing on purpose: one member this contract cannot project
condemns the whole capture, and no partially compatible subset is ever published.
That is the right behaviour for a load and an expensive way to discover what is
inside a period nobody has opened — the only archive era this project has ever
walked is one mixed day of 2023.

```sh
python -m tender_ledger inspect path/to/package.tar.gz --package-id monthly/2020-01 --survey
```

The survey streams the same archive through the same walker, the same archive
defenses and the same resource limits as a load, then counts what a load would
have stopped at:

| Reported | What it answers |
| --- | --- |
| `sha256`, compressed / expanded / XML bytes | Which bytes were surveyed, and what they cost to read |
| `member_count`, `xml_member_count`, `notice_count` | Members admitted, members named `.xml`, and distinct loadable identities |
| `formats`, `schema_versions`, `roots` | Which supported eras and schema versions are actually in there |
| `incompatible_member_count`, `incompatible_reasons` | How many members a load would reject, by reason code |
| `unsupported_roots`, `unsupported_root_kinds` | Which roots they turned out to have — the finding that decides whether the contract should grow |
| `duplicate_identity_count`, `duplicate_identity_sample` | Repeated canonical identities, which the primary key would refuse |
| `incompatible_sample`, samples above | A stable, bounded illustration in archive order |
| `compatible_for_load` | Whether a load of this archive would publish anything at all |

Two kinds of rejection stay deliberately different. A **member-level** fault —
a non-XML member, an unparseable identity, a bad encoding, a DTD or entity
declaration, malformed XML, an unsupported root, a filename that disagrees with
its `DOC_ID`, a missing eForms customization or identifier — is counted and the
walk continues, which is what makes an inventory possible. An **archive-level**
fault — an unsafe member path, a member that is not a regular file, an exhausted
byte or notice limit, truncation, a bad gzip CRC, a corrupt tar, data after the
end marker — means further reading is unsafe or meaningless, so the survey stops
and prints no inventory at all.

The command exits 0 only when `compatible_for_load` is true. It writes nothing:
no capture, no rows, no checkpoint, and no coverage claim of any kind — a survey
says what an archive contains, never what the source published. Samples carry
sanitized member names and reason codes; XML content, notice fields and
filesystem paths are never reported.

## Deliberately deferred

Annual partitioning (the baseline keeps year in the natural key so a partitioned
table can be proven equivalent later), indexes tuned against measured plans, the
six-query workload and its benchmark, backfill over several packages, and
Airflow.
The HTTP downloader and the coverage checkpoint now exist; see
[ingestion.md](ingestion.md).
