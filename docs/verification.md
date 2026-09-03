# Source coverage verification

Status: implemented for one published capture at a time, daily or monthly,
against the TED Search API. Downloading packages over HTTP and refusing to treat
one as processed until its coverage is confirmed are implemented on top of this,
in [ingestion.md](ingestion.md). Historical coverage and benchmarks are later.
Only a daily capture has ever been verified against the live source.

"The archive loaded completely" and "the source agrees this is the whole issue"
are different claims. The loader establishes the first. `verify` is what can
establish the second, and it is deliberately hard to satisfy.

## What it does

```sh
python -m tender_ledger verify --capture-id 1
```

The capture's own identity produces the query. `daily/202300220` is OJ S issue
**220 of 2023** — an issue ordinal, not the 220th day of the year — so the query
is `OJ = 220/2023` with `scope: ALL`. `monthly/2020-02` asks for that month's
whole inclusive interval, `PD>=20200201 AND PD<=20200229`, with the last day
computed rather than written down. Nothing about the query can be supplied on the
command line: a filter chosen by hand could certify a subset while reporting
coverage of a whole package. Identities that fit neither `daily/YYYYNNNNN` nor
`monthly/YYYY-MM` are refused rather than guessed at.

### Membership, not just set equality

Matching sets prove that two collections agree. They do not prove the source
enumerated the window that was asked for. Each answer therefore has to belong to
the requested package before it can count:

* a daily record must carry the expected `ojs-number`, exactly as before;
* a monthly record must carry a parseable `publication-date` whose calendar day
  falls inside the derived interval, first and last day included.

The rule comes from the identity, never from the expression the source echoes
back, and a query cannot even be constructed without one of the two rules. The
first record that fails leaves the attempt `unavailable`. A month spans many OJ S
issues, so `ojs-number` carries no membership information there and is not turned
into one; a daily package gets no date rule invented for it.

The Search API documents the ITERATION mode and the page size but not its date
operators, their inclusivity or their time-zone handling. The interval predicate
therefore rests on observed behaviour, which is why both edges of a month, a
February in a leap year and one outside it are acceptance tests rather than an
assumption.

The verifier compares the identifiers of **that capture**, not
`tl_read.distinct_notice`. Daily and monthly packages overlap, so a notice
missing from the daily capture could be supplied by the monthly one in the
global view and hide the gap.

Exit code 0 means a `verified` row committed to the database. Every other
outcome exits 1 and prints its reason.

## The four answers

| State | Means | `coverage_verified` |
| --- | --- | --- |
| `verified` | A complete, non-empty, duplicate-free enumeration whose identifier set is exactly the capture's | true |
| `mismatch` | The enumeration was complete, and the sets differ | false |
| `unavailable` | Transport, protocol, contract or budget failure — the enumeration cannot be trusted | false |
| `empty_unconfirmed` | Source and capture are both empty | false |

`empty_unconfirmed` is not a pass. Zero results does not establish that an issue
was published: confirming a genuinely empty period needs publication-calendar
evidence this project does not yet have, and no calendar is invented to reach
`verified`.

`coverage_verified` on a capture is the standing of its **most recent** attempt.
Starting a new verification lowers it to false in the same transaction that
records the attempt, so the capture claims nothing while a check is running. A
later failure keeps it false and leaves the earlier evidence in the history.

## Ending the walk

Pagination is where a verifier quietly goes wrong. Two observations from the
source (2026-09-03, OJ S 220/2023) rule out the obvious stopping rules:

* The last page of data was short (217 of a 250 limit), so "a page smaller than
  the limit means the end" would stop one page early on a longer issue.
* The page after the last record was empty but **still carried a non-empty
  `iterationNextToken`**, so "stop when the token goes away" never fires.

What ends the walk is arithmetic: records received, distinct identifiers, and
the total the source announced must all be equal, confirmed by a terminal page.
Every other ending is a failure:

* an empty page or a missing token before the announced total arrived;
* a total that changed during pagination;
* an iteration token repeated without progress;
* more records than announced;
* any duplicate identifier — duplicates break `records == distinct`, so
  cardinality can never be approved past one;
* `timedOut: true`, a total that is not a count (`true` is not 1), a missing
  requested field, a publication date that is not a calendar date, a
  non-canonical publication number, or a notice from another OJ issue.

Identity is canonical, so `0000042-2023`, `42-2023` and a different page order
are the same identifier. The recorded key digest is
`sha256` over `"<year>:<number>"` lines, sorted ascending and joined with
newlines; every attempt stores that recipe alongside the digest.

## Budgets and retries

| Budget | daily | monthly |
| --- | --- | --- |
| Notices in one verification | 10,000 | 150,000 |
| Page size | 250 | 250 |
| Pages | 50 | 620 |
| Attempts per page | 3 | 3 |
| Response body | 4 MiB | 4 MiB |
| Total run | 300 s | 1,800 s |
| One HTTP operation | 20 s, capped by the time left | 20 s |

These come from the same per-kind policy the archive walker and the downloader
read, so the notice ceiling here cannot drift from the one that admitted the
archive — see [the resource policy](ingestion.md#resource-policy). All of them
are injectable, and tests drive each one. Exhausting any budget is
`unavailable`; a budget can never produce a result.

Connection failures, timeouts and HTTP 408/429/500/502/503/504 get up to three
bounded attempts with exponential backoff. A valid `Retry-After` (delta-seconds
or HTTP-date) is honoured in full; if it exceeds the time left, the run stops as
`unavailable` rather than sleeping less and hitting the source early. HTTP 400,
401, 403 and 404, a rejected TLS certificate, malformed JSON and any contract
failure are never retried. A retry of the whole command opens a **new attempt**;
it never resumes a remote cursor, so a response lost after a cursor was consumed
cannot be stitched onto identifiers from an earlier walk.

The client is `urllib` from the standard library with a default TLS context,
which validates the certificate chain and hostname. Its timeout applies to
individual socket operations, not to a whole request. The deadline is checked
before requests and sleeps, between bounded body reads, and before accepting a
completed enumeration. A response received after the budget expires cannot
verify coverage. A blocked socket read can delay cancellation by an operation
timeout; DNS resolution and response-header parsing do not have a hard
wall-clock deadline in urllib. There is no HTTP dependency to install, and CI
never contacts TED. Premature EOF against a declared Content-Length is a
transport failure, even if the received prefix is valid JSON.

## Transactions, locking and durability

* The verifier owns its transaction boundaries and refuses a connection that is
  not autocommit and idle, before locking or writing anything — the same
  contract the loader has.
* Invalid requests — an unknown capture, one that is not the published capture
  of its package, one that never reconciled, an unsupported package identity —
  are refused before any lock and before any write.
* The package's session advisory lock is the **same key space the loader uses**,
  so a verification and a re-acquisition of the same package serialize against
  each other. The capture is re-validated under the lock. `--lock-wait` waits
  instead of failing fast.
* Opening the attempt is one committed transaction; closing it and setting
  `coverage_verified` is another. No SQL transaction is open while HTTP runs.
* The returned result is read back from the database, so `verified` means a
  committed `verified` row. If the closing commit fails, the original error is
  reported and no success is claimed; the attempt stays `in_progress` and
  visible.
* An interruption leaves an `in_progress` attempt and a released lock, not a
  silent gap.

The schema enforces the claim independently of the code: a `verified` row must
carry a non-empty announced total, zero duplicates, record and distinct counts
equal to that total, a local count equal to it, zero differences on both sides,
and a key digest. No code path can write the claim without the numbers.

## What is recorded

One row per attempt in `tl_work.verification_attempt`, exposed to the reader
role as `tl_read.verification_attempt`: start and finish, state and reason,
query and scope, the capture's checksum and contract version at the time, the
announced total, records / distinct / duplicates, the local count, pages and
HTTP attempts, difference counts with at most 20 sorted identifiers per side,
the verifier version, and the key digest with its recipe.

Counts are `NULL` when the attempt never learned them. **Unknown is not zero**:
an unreachable source has an unknown difference, not an empty one. Iteration
tokens and response bodies are never stored or logged.

`tl_read.capture_status` gains the latest attempt's id, state and timestamps
alongside the existing columns; its one-row-per-capture grain is unchanged.
[`queries/coverage_status.sql`](../queries/coverage_status.sql) reports every
published capture including the never-verified ones, and
[`queries/verification_history.sql`](../queries/verification_history.sql) shows
each capture's attempts in order with the state that preceded them.

## Deliberately not done here

`load` still means "the local archive loaded completely" and still publishes
with `source_coverage_verified = false`; running `verify` by hand does not mark
a package processed either. What does is the checkpoint `ingest` seals, which
also requires an artifact this system acquired and validated - see
[ingestion.md](ingestion.md). Monthly packages, the publication calendar,
backfill, Airflow, and the measured SQL workload are all later slices.
