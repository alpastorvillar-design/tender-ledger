# Queries

SQL over the published consumption views. Every file states its question, grain,
source, null semantics, ordering, parameters and what it does not let you claim,
because most of the wrong answers available here come from one of those things
rather than from the join.

Everything counts **notices** — published procurement announcements. It does not
count awards, contracts, lots, suppliers or money. The current projection does
not extract monetary amounts from notices.

## The six analytical workloads

Each has a correctness fixture in `tests/test_queries.py` and answers exactly the
question its header states — nothing here has been measured for performance yet
(query plans, indexes and partitioning are a later milestone; see
[scale-and-sql.md](../docs/scale-and-sql.md)).

| # | File | Question | Grain |
| --- | --- | --- | --- |
| 1 | [monthly_notice_counts.sql](monthly_notice_counts.sql) | How many distinct notices were published per month, buyer country and CPV division? | One row per (month, country, CPV division) |
| 2 | [latest_capture_per_publication.sql](latest_capture_per_publication.sql) | For each canonical publication, which complete capture observed it most recently? | One row per publication identity |
| 3 | [official_change_references.sql](official_change_references.sql) | Which notices declare official change references, and which resolve to a loaded notice? | One row per (notice, change reference); a notice with none still gets one row |
| 4 | [acquisition_cutoff_state.sql](acquisition_cutoff_state.sql) | What was the latest complete observation of each publication at a point in this system's acquisition order? | One row per publication identity observed by the cutoff |
| 5 | [monthly_coverage_calendar.sql](monthly_coverage_calendar.sql) | Over a calendar range, has each monthly package been touched, and with what standing? | One row per calendar month in the range |
| 6 | [cross_package_overlap_audit.sql](cross_package_overlap_audit.sql) | Between two packages, which identities are exclusive to one, and do the shared ones actually agree? | One row per finding (only-in-A, only-in-B, or content differs) |

### Parameters

Workloads 4, 5 and 6 take parameters, written as `:'name'` tokens (psql's own
quoted-variable syntax):

| File | Parameters |
| --- | --- |
| `acquisition_cutoff_state.sql` | `:'cutoff'` — an `acquisition_ordinal` value |
| `monthly_coverage_calendar.sql` | `:'first_month'`, `:'last_month'` — first-of-month dates, inclusive |
| `cross_package_overlap_audit.sql` | `:'package_a'`, `:'package_b'` — two `source_package_id` values |

From psql, set them with `-v`:

```powershell
Get-Content -Raw .\queries\acquisition_cutoff_state.sql |
    docker compose exec -T postgres psql -U postgres -d tender_ledger `
        -v ON_ERROR_STOP=1 -v cutoff=5 -f -
```

From Python, as `tests/test_queries.py` does: replace the literal token with a
quoted value before executing, e.g. `sql.replace(":'cutoff'", "5")` or
`sql.replace(":'first_month'", "'2020-01-01'")`. Nothing in the query engine
parses `:'name'` itself; it is plain text that both psql and this substitution
convention treat the same way.

## Operational diagnostics

| File | Question | Grain |
| --- | --- | --- |
| [coverage_status.sql](coverage_status.sql) | For each published capture, does the source agree we hold the whole issue? | One row per published capture |
| [verification_history.sql](verification_history.sql) | What has each capture's coverage check said over time? | One row per verification attempt |
| [diagnostics.sql](diagnostics.sql) | Load reconciliation, cross-package overlap, field completeness | Stated per query; run them individually |
| [ingest_status.sql](ingest_status.sql) | For each package, was a checkpoint sealed and does it still describe the package? | One row per source package |
| [ingest_history.sql](ingest_history.sql) | What did each ingest run do, and how far did it get? | One row per ingest run |

`diagnostics.sql` holds several independent queries. Run them one at a time.
These predate the six-workload set above and remain useful for day-to-day
operational questions; they are not a substitute for the six workloads and are
not counted as such.

## Missing values

Absent and zero are different answers and are never merged:

* A notice with no buyer country or no primary CPV is grouped under an explicit
  `(absent)` label in the monthly counts, not dropped and not folded into a real
  code.
* A capture nobody has verified reports `never verified`, not a failure. It keeps
  its row: an unchecked window has to stay visible.
* A verification that never reached the source leaves its difference counts
  `NULL`. That is *unknown*, not *no difference*. Only an attempt that
  enumerated the whole issue records a key digest.
* A source and a capture that are both empty give `unconfirmed empty`. Zero
  results does not establish that the issue was published.
* A package with no ingest checkpoint reports `never checkpointed`, and a
  package whose checkpoint stopped being current says which of the two things
  happened -- a later acquisition, or a later coverage check. None of them is
  merged with "processed".
* An ingest run that never reached a capture or an attempt leaves those columns
  `NULL`. That is *the run never got there*, not "no capture exists".

## Running them

The `tender_ledger_reader` role can run all of these; it has `SELECT` on the
`tl_read` views and nothing else. Substitute your own database name if it is not
`tender_ledger`.

PowerShell:

```powershell
Get-Content -Raw .\queries\coverage_status.sql |
    docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 -f -
```

POSIX shell:

```sh
docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 -f - < queries/coverage_status.sql
```

To run one as the restricted reader in PowerShell:

```powershell
Get-Content -Raw .\queries\coverage_status.sql |
    docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 -c "set role tender_ledger_reader" -f -
```

In a POSIX shell:

```sh
docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 \
  -c "set role tender_ledger_reader" -f - < queries/coverage_status.sql
```

Each query has a correctness fixture in `tests/test_queries.py` covering the
cases the comments claim to handle — missing values, overlapping packages,
captures without attempts, and repeated attempts. The six-query analytical
workload and its measured plans are a later milestone.
