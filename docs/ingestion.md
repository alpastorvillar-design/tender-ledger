# Daily package ingestion

Status: implemented for one daily package at a time. `ingest` acquires the
archive over HTTPS, loads it with the transactional loader, verifies its coverage
against the Search API, and seals a checkpoint. Monthly packages, a publication
calendar, backfill, benchmarks and Airflow are later slices.

```sh
python -m tender_ledger ingest --package-id daily/202300220
```

Exit code 0 means a checkpoint that is current in PostgreSQL, read back after
the commit. Every other outcome exits 1 and prints why.

## Why a third command

`load` establishes that a local archive loaded completely. `verify` establishes
that the source agrees the capture is the whole issue. Neither one means a
workflow may treat the package as processed: a capture can be published with
`source_coverage_verified = false`, and a coverage check can pass on a capture
nobody downloaded through this system. `ingest` is what joins them, and the
checkpoint records all three facts together — the artifact, the capture, and the
exact verification attempt.

## The flow

```text
persist an ingest run (phase 'starting')
      |
      v
download to .<name>.part-<token> ---> validate: length, sha256, gzip CRC, tar
      |                                trailer, member contract, byte limits
      v
fsync, rename into data/packages/daily/<ordinal>.tar.gz
      |
      v
commit the artifact reference ------> phase 'artifact_ready'
      |
      v
load_package(); the run->capture link commits with the capture itself
      |
      v
capture published ------------------> phase 'capture_published'
      |
      v
verify_capture(), or reuse a confirmed one already in the database
      |
      v
coverage confirmed -----------------> phase 'source_verified'
      |
      v
seal: one short transaction re-checks every condition and writes
      tl_work.package_checkpoint --> phase 'completed', read back, exit 0
```

Each arrow is a place a crash can land. The phase before it is already
committed, so the next invocation resumes from there rather than from the start.

## Recovery, one boundary at a time

The filesystem and PostgreSQL share no transaction, so nothing here pretends
they do. The rule is that a database row is never evidence about file content:
every path that reuses an artifact re-reads and re-hashes it.

| Interruption | What the next run does |
| --- | --- |
| Before the rename | The partial `.part-` file is this run's own and is deleted; the download restarts from byte zero. Nothing else in the data directory is touched |
| After the rename, before the reference commits | The file at the destination is validated again and adopted, with no second request |
| After the artifact reference commits | The recorded artifact is re-validated and reused |
| After the capture is created, before the first batch | The run already points at that capture: `on_capture` writes the link inside the transaction that creates it. The loader resumes the same capture |
| After a durable batch | The loader skips committed batch ordinals and does not rewrite them |
| After publish, before verification | The load is a no-op replay of the published capture; verification runs |
| After verification, before the checkpoint | The confirmed attempt is re-checked in the database and reused to seal. The source is not asked again |
| The seal transaction does not commit | No checkpoint, no success, and the run stays `source_verified`. A retry seals without downloading, loading or verifying again |
| Terminal load failure (the capture is `failed`) | The run is marked `failed` and never resumed; a re-run starts a new run against a new capture |
| The cached archive is missing or no longer hashes correctly | It is downloaded again. A checkpoint row alone never produces a successful replay |

Recovery is checked against a clean run over the same bytes: the resulting notice
rows are equal.

## The checkpoint

`tl_work.package_checkpoint` holds one row per package. It names the run, the
capture, the artifact checksum and contract version, the notice count, and the
**specific** verification attempt that justified it. It is not a boolean and not
a `MAX(date)` watermark; the unit is one package.

Sealing happens under the package's advisory lock in one short transaction that
re-checks, in SQL, that:

* the capture is still the published capture of its package;
* it reconciled (members = distinct keys = loaded rows);
* its coverage still stands;
* the cited attempt is that capture's latest and is `verified`;
* the attempt's artifact checksum and contract version still match the capture's.

Every comparison is written `(...) is true`, so an unknown value satisfies
nothing. If the statement writes no row, none of that held and no checkpoint
exists. The committed row is read back before the command reports success.

Composite foreign keys make the wrong reference impossible rather than merely
unlikely: the named run must carry the same package, capture, attempt and
artifact; the capture must carry the same package, artifact, contract and notice
count; and the verification attempt must carry that capture, artifact and
contract. A `CHECK` constraint cannot see another table, so it is not claimed to
enforce any of this.

### Current versus historical

Whether a sealed checkpoint still describes the package is **derived**, not
stored, in `tl_read.package_ingest_status`: the published pointer still points at
the checkpoint's capture, the named run is complete, its counts and references
still agree, that capture's coverage still stands, and its latest attempt is
still the one cited. So:

* `load --force-recapture` publishes another capture and the old checkpoint stops
  being current;
* a `verify` that fails, or one still running, lowers `coverage_verified` and
  changes the latest attempt, and the checkpoint stops being current;
* the checkpoint row is not rewritten or deleted by either. The evidence of what
  was sealed remains.

A run that has neither completed nor failed is reported separately, so a package
can show a current checkpoint and a new attempt in flight at the same time.

Currency is database evidence. It says nothing about the archive still being on
disk — `ingest` re-validates the bytes before replaying — and nothing about the
source being unchanged since the check. A replay certifies stored evidence, not
freshness; periodic refresh is an orchestration decision. There is deliberately
no `--refresh` or `--force` on `ingest`: re-acquisition stays the existing,
explicit `load --force-recapture`, which correctly retires the checkpoint.

## Download contract

The URL is derived from the package identity — `daily/202300220` becomes
`https://ted.europa.eu/packages/daily/202300220` — and cannot be supplied on the
command line. Only `daily/YYYYNNNNN` identities are supported; the ordinal is an
OJ S issue, not a day of the year. There is no calendar here and no guessing at
packages that do not exist.

| Budget | Value |
| --- | --- |
| HTTP attempts per acquisition | 3 |
| One HTTP operation | 20 s, capped by the time left |
| Whole acquisition | 300 s |
| One archive | 64 MiB compressed |
| All attempts of one acquisition | 192 MiB received |
| Expanded archive / member / notices | 512 MiB / 8 MiB / 10,000 |

The archive limits are the daily flow's own. The historical loader raises them
for monthly packages; a daily package does not inherit that silently, so
validation and loading use the same values.

Retried: connection failures, timeouts, HTTP 408/429/500/502/503/504, and a body
that ends before its declared `Content-Length`. Not retried: a rejected TLS
certificate, HTTP 400/401/403/404, a redirect off the requested origin, an
unsupported `Content-Encoding`, a partial `206` nobody asked for, an exhausted
budget, and any body that fails archive validation. A valid `Retry-After` is
honoured in full; if it exceeds the time left the run stops rather than hitting
the source early. **A 404 is not an empty window.**

Interrupted downloads restart from byte zero. The observed endpoints advertise
byte ranges but carry no `ETag` or `Last-Modified`, so a range request cannot
prove it is continuing the same object; combining two halves that might come from
different versions is worse than paying for the bytes again.

The request asks for `Accept-Encoding: identity` — a decoded body would no longer
be the bytes the checksum names. Received bytes are counted and compared with any
declared length, and the limit applies even when no length is declared. HTTP 200
with JSON or HTML is not an archive: nothing is adopted until the file has been
walked as a complete gzip-tar package of valid members.

Like the Search API client, `urllib` applies its timeout to individual socket
operations. The deadline is checked between body reads, so a blocked socket can
delay cancellation by one operation timeout, and DNS resolution and header
parsing have no hard wall-clock deadline. That limit is real and is not papered
over with a promise the transport cannot keep.

## Files

Archives live under `data/` (ignored by Git), at
`data/packages/daily/<ordinal>.tar.gz`. `--data-dir` moves the root. The path
comes from the validated identity, never from `Content-Disposition`, and
containment inside the root is asserted where the path is built. Temporary files
are exclusive to one attempt, sit beside the destination so the rename stays on
one filesystem, and are removed by the attempt that created them. Nothing sweeps
files it did not write.

The run stores the path relative to the data root, the checksum and the byte
count. Archive contents, HTTP bodies and pagination tokens are never stored.

## Concurrency

Everything for one package — including the download — runs under that package's
session advisory lock, the same key space `load` and `verify` use. Those two are
called while it is held: PostgreSQL counts advisory locks per session, so their
own balanced acquire and release nests inside this one, and a concurrent `load`,
re-acquisition or `verify` of the same package is still refused. The lock is
released on success, on failure and on cancellation; `pg_advisory_unlock_all` is
never used to paper over an imbalance. Different packages do not block each
other. `--lock-wait` waits instead of failing fast.

No SQL transaction is open while HTTP runs — not during the download and not
during the coverage check. `ingest` refuses a connection that is not autocommit
and idle, before locking or writing anything, exactly as the loader and the
verifier do.

## What a run records

`tl_work.ingest_run`, exposed as `tl_read.ingest_run`: the package, which attempt
at it this run is, the phase reached, the artifact path/checksum/bytes, the
capture, the verification attempt, the last bounded error, and the timestamps.

`phase` says what is durably true; it is not a copy of the loader's or the
verifier's state, which remain authoritative about the capture and the attempt.
`failed` is terminal and carries a reason; a recoverable interruption keeps its
phase and also records the error, so the two are never confused.

[`queries/ingest_status.sql`](../queries/ingest_status.sql) reports every package
this system has captured, separating *never checkpointed*, *processed*, *retired
by a later acquisition* and *retired by a later coverage check*.
[`queries/ingest_history.sql`](../queries/ingest_history.sql) lists every run in
order with how far it got.

## Running the diagnostics

PowerShell:

```powershell
Get-Content -Raw .\queries\ingest_status.sql |
    docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 -f -
```

POSIX shell:

```sh
docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 -f - < queries/ingest_status.sql
```

Both queries run as `tender_ledger_reader`, which has `SELECT` on the `tl_read`
views and nothing else.

## Deliberately not done here

Monthly packages, more than one package per invocation, a publication calendar,
backfill, retention of old artifacts, the 100,000-notice rehearsal, the
million-notice gate, the measured SQL workload, and Airflow. `ingest` processes
one named daily package and says whether that package is processed.
