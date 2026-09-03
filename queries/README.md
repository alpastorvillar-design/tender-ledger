# Queries

SQL over the published consumption views. Every file states its grain and how it
treats missing values, because most of the wrong answers available here come from
one of those two things rather than from the join.

Everything counts **notices** — published procurement announcements. It does not
count awards, contracts, lots, suppliers or money. A notice announces an
intention or an outcome; it does not carry the value of a contract, and nothing
in this schema does either.

| File | Question | Grain |
| --- | --- | --- |
| [monthly_notice_counts.sql](monthly_notice_counts.sql) | How many distinct notices were published per month, buyer country and CPV division? | One row per (month, country, CPV division) |
| [coverage_status.sql](coverage_status.sql) | For each published capture, does the source agree we hold the whole issue? | One row per published capture |
| [verification_history.sql](verification_history.sql) | What has each capture's coverage check said over time? | One row per verification attempt |
| [diagnostics.sql](diagnostics.sql) | Load reconciliation, cross-package overlap, field completeness | Stated per query; run them individually |

`diagnostics.sql` holds several independent queries. Run them one at a time.

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

## Running them

The `tender_ledger_reader` role can run all of these; it has `SELECT` on the
`tl_read` views and nothing else. Substitute your own database name if it is not
`tender_ledger`.

PowerShell:

```powershell
docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 -f - < queries/coverage_status.sql
```

POSIX shell:

```sh
docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 -f - < queries/coverage_status.sql
```

To run one as the restricted reader, prefix the file with `SET ROLE`:

```sh
docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 \
  -c "set role tender_ledger_reader" -f - < queries/coverage_status.sql
```

Each query has a correctness fixture in `tests/test_queries.py` covering the
cases the comments claim to handle — missing values, overlapping packages,
captures without attempts, and repeated attempts. The six-query analytical
workload and its measured plans are a later milestone.
