# Package ingestion

Status: implemented for one package at a time, daily or monthly. `ingest`
acquires the archive over HTTPS, loads it with the transactional loader, verifies
its coverage against the Search API, and seals a checkpoint. A publication
calendar, backfill over several packages, benchmarks and Airflow are later
slices. No monthly package has been acquired from TED yet: monthly support is
covered by fixtures and local servers, not by a real download.

```sh
python -m tender_ledger ingest --package-id daily/202300220
python -m tender_ledger ingest --package-id monthly/2020-01
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
fsync, rename into data/packages/<kind>/<ordinal>.tar.gz
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

Currency alone is not enough to replay: `tl_read.package_ingest_status` also
exposes `checkpoint_contract_version` (and `published_contract_version`),
copied straight from the stored rows. `ingest._replay` compares the checkpoint
value against the running code's `projection.CONTRACT_VERSION` before trusting
it — SQL cannot know that constant, and it is deliberately not duplicated as a
literal there, so there is exactly one place it can drift. A checkpoint sealed
under an older contract declines, even when it is otherwise internally
coherent (capture, artifact checksum and attempt all still agree with each
other): the fresh run this falls through to acquires a *new* capture under the
current contract rather than resuming or overwriting the old evidence, because
`load_package` already keys resumption and the "already published" no-op check
on matching `contract_version`, not only on matching bytes.

## Package identity

Two identity shapes are supported, and everything else about a package is
derived from one of them:

| Identity | Derived TED package path | Local file | Source query |
| --- | --- | --- | --- |
| `daily/YYYYNNNNN` | `/packages/daily/202300220` | `data/packages/daily/202300220.tar.gz` | `OJ = 220/2023` |
| `monthly/YYYY-MM` | `/packages/monthly/2024-1` | `data/packages/monthly/2024-01.tar.gz` | `PD>=20240101 AND PD<=20240131` |

The daily ordinal is an OJ S issue, not a day of the year. The monthly month is
always zero-padded — that matches TED's own `{yyyy}_{mm}` archive filename
convention, and accepting `monthly/2024-1` as well would give one package two
internal identities, two captures and two checkpoints. The path segment used to
fetch it drops that zero.

That path is the **observed endpoint shape**, not a documented one: the published
direct-download page narrates a pattern containing `/notice/` that its own
examples omit, and the endpoints that answer omit it too
(`/packages/notice/daily/202300220` returned 404 where `/packages/daily/202300220`
returned 200). `monthly/2020-1`, `monthly/2020-13`, an out-of-range year, a
trailing space, a backslash and anything resembling traversal are refused rather
than guessed at, and no URL, path or query can be supplied on the command line.

There is no calendar here: `ingest` processes one named package.

## Resource policy

One policy per package kind is the single source of these numbers, and the
archive walker, the downloader and the Search API client each build their own
limits from it. Before that they carried three independent sets of defaults, and
the same archive was validated against ceilings that differed by a factor of
sixteen depending on which command opened it.

| Budget | daily | monthly |
| --- | --- | --- |
| HTTP attempts per acquisition | 3 | 3 |
| One HTTP operation | 20 s, capped by the time left | 20 s |
| Whole acquisition | 300 s | 1,800 s |
| One archive | 64 MiB compressed | 512 MiB |
| All attempts of one acquisition | 192 MiB received | 1,536 MiB |
| Expanded archive | 512 MiB | 8 GiB |
| One member | 8 MiB | 32 MiB |
| Notices / members | 10,000 | 150,000 |
| Enumeration pages × page size | 50 × 250 | 620 × 250 |
| Whole verification | 300 s | 1,800 s |

Four invariants are asserted when a policy is constructed, so a future edit
cannot produce a set of numbers that contradicts itself: every ceiling is
positive; the aggregate byte budget funds every attempt of one artifact; the page
ceiling covers the notice ceiling plus the terminal page; and one member cannot
exceed the whole expanded archive. The archive and the source therefore always
agree on the same notice ceiling, so an archive that loads can also be verified.

These are safety ceilings, not targets, and nothing approves a load by being
under them. The monthly numbers are derived from the 2020–2025 inventory that has
been measured — 512 MiB is 1.23× the largest of 72 observed monthly headers — and
have to be re-derived if the historical target grows past 2025. The expansion
ceiling rests on the only expansion ratio ever measured (6.66×, on one mixed
daily package). Expanded bytes are streamed and never written to disk, so that
ceiling bounds work rather than storage.

`inspect`, `load`, `verify` and `ingest` all resolve their policy from the
identity, so no command can accept an archive another command would refuse.
`inspect --package-id` treats the policy as the maximum: an explicit `--max-*`
flag may narrow it for an ad-hoc look and is refused if it would widen it. The
commands reject an identity this contract does not recognize before downloading
bytes or creating durable database state.

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
`data/packages/<kind>/<ordinal>.tar.gz`. `--data-dir` moves the root. The path
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

More than one package per invocation, a publication calendar, backfill,
retention of old artifacts, the 100,000-notice rehearsal, the million-notice
gate, the measured SQL workload, and Airflow. `ingest` processes one named
package and says whether that package is processed.
