# The million-notice run (M3e)

Status: the minimum scale gate is met. Twenty-six of the twenty-seven packages in
[`manifests/m3-scale.json`](../manifests/m3-scale.json) were acquired, loaded and
reconciled exactly against the TED Search API, producing **1,393,588 distinct
canonical notices** whose coverage the source confirmed. Counting every published
capture, including the one package the source itself cannot reconcile, the
database holds **1,450,598 observations and 1,447,631 distinct notices**.

The seventeenth entry, `monthly/2021-05`, has no checkpoint and cannot get one:
TED's own Search API reports one notice that TED's own monthly archive for that
month does not contain. That is a measured gap in the source, reproduced twice
and confirmed by a separate probe on 2026-09-06, and it is described in full
below.

The run used an exclusive `tender_ledger_m3_scale` database. The development
database was unchanged before and after: one capture, 2,967 notices, one run,
one checkpoint.

## Environment

Windows 11 Pro, AMD Ryzen 7 7800X3D (16 logical processors host-side), 31.1 GiB
RAM. PostgreSQL 17.11 in the project's Compose stack, limited to 2 CPU and
2 GiB. Python 3.14.3. The host held more than 620 GiB free throughout, above the
100 GiB operating gate, and the project's combined footprint peaked at 13.44 GiB
against the 150 GiB ceiling.

## Reproducing the gate

```sh
python -m tender_ledger ingest-manifest --manifest manifests/m3-scale-verified.json --report report.json
```

The verified manifest names the 26 identities with exact checkpoints. The
companion [`m3-scale.json`](../manifests/m3-scale.json) retains all 27 package
identities in a fixed order: the 24 monthly packages of 2020 and 2021, then
`monthly/2023-11` and `monthly/2024-01` for eForms coverage, then
`daily/202300220`, which overlaps November 2023. Its counts and bytes are
planning metadata observed on 2026-09-06; nothing downstream reads them. The
audit manifest deliberately keeps `monthly/2021-05` and therefore stops at
entry 17. The verified manifest excludes only that documented mismatch; the
runner itself never skips an entry.

The gate itself is a count over the read surface, not over physical rows:

```sql
-- every distinct canonical notice the reader can see
select count(*) as distinct_notices from tl_read.distinct_notice;

-- restricted to packages whose coverage the source confirmed
select count(*) as verified_distinct_notices
from (
    select distinct n.publication_year, n.publication_number
    from tl_read.notice n
    join tl_read.package_ingest_status s using (source_package_id)
    where s.checkpoint_is_current
) verified;
```

The second query is the one that answers the gate as
[scale-and-sql.md](scale-and-sql.md) states it: distinct real notices whose
coverage was reconciled against the source.

This run processed the source-audit manifest in private three-package slices so
resources could be measured between them and the run could stop and resume.
Each slice was a valid manifest of its own with `order` renumbered. The complete
verified manifest provides the reproducible start-to-finish path over every
package whose coverage is exact.

## Recovery under interruption

Before the run proper, `monthly/2020-01` was interrupted mid-load. A separate
process watched `tl_work.capture_batch` and, once ten batches were committed,
terminated the loader's PostgreSQL backend — a real termination, not a test hook.

| Question | Observed |
| --- | --- |
| Did committed batches survive? | 10 batches, 5,000 rows, readable from another connection |
| Was there a checkpoint? | None |
| Could a reader see the partial capture? | No rows through `tl_read.notice` |
| What state was left? | Capture `loading`, run at `artifact_ready` |

The resume kept the same capture and the same run — it did not start a second
one — and left every pre-interruption batch byte-identical, including its commit
timestamp, as a prefix of the finished load. It then published 50,123 notices
(members = distinct keys = loaded rows), matched all 50,123 identifiers against
the API across 202 pages with no difference in either direction, and sealed the
checkpoint.

The resumed capture was then compared against a clean load of the same bytes in
a disposable database: a SHA-256 over every projected column of every row, in
canonical order, is identical in both, as is the change-reference checksum. The
interruption left no trace in the data.

## Per-package result

Every row below was read back from PostgreSQL, not taken from the command's
return value. "Exact" means the API's identifier set and the capture's are equal:
no identifier only in the capture, none only in the source, no duplicates.

| Package | Notices | Batches | Change refs | API pages | Acquire (s) | Load (s) | Verify (s) | Exact |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| `monthly/2020-01` | 50,123 | 101 | 0 | 202 | 34.6 | 142.2 | 204.4 | yes |
| `monthly/2020-02` | 50,522 | 102 | 0 | 204 | 34.8 | 38.0 | 231.5 | yes |
| `monthly/2020-03` | 53,785 | 108 | 0 | 217 | 44.8 | 40.8 | 220.6 | yes |
| `monthly/2020-04` | 49,106 | 99 | 0 | 198 | 40.2 | 37.1 | 198.7 | yes |
| `monthly/2020-05` | 49,130 | 99 | 0 | 198 | 39.6 | 31.9 | 202.0 | yes |
| `monthly/2020-06` | 52,655 | 106 | 0 | 212 | 39.5 | 36.9 | 223.3 | yes |
| `monthly/2020-07` | 56,815 | 114 | 0 | 229 | 47.3 | 44.5 | 236.1 | yes |
| `monthly/2020-08` | 46,159 | 93 | 0 | 186 | 37.4 | 35.8 | 176.8 | yes |
| `monthly/2020-09` | 51,908 | 104 | 0 | 209 | 42.1 | 39.6 | 200.6 | yes |
| `monthly/2020-10` | 60,553 | 122 | 0 | 244 | 49.5 | 46.6 | 238.6 | yes |
| `monthly/2020-11` | 55,891 | 112 | 0 | 225 | 45.4 | 42.7 | 216.6 | yes |
| `monthly/2020-12` | 66,905 | 134 | 0 | 269 | 59.6 | 53.4 | 259.9 | yes |
| `monthly/2021-01` | 48,745 | 98 | 0 | 196 | 40.3 | 38.2 | 187.2 | yes |
| `monthly/2021-02` | 52,821 | 106 | 0 | 213 | 42.8 | 40.3 | 255.5 | yes |
| `monthly/2021-03` | 60,007 | 121 | 0 | 242 | 48.1 | 45.1 | 234.5 | yes |
| `monthly/2021-04` | 57,027 | 115 | 0 | 230 | 45.7 | 44.4 | 223.4 | yes |
| `monthly/2021-05` | 54,043 | 109 | 0 | 218 | 43.7 | 41.7 | 211.3 | **no** |
| `monthly/2021-06` | 57,249 | 115 | 0 | 230 | 45.4 | 43.7 | 256.7 | yes |
| `monthly/2021-07` | 60,135 | 121 | 0 | 242 | 45.8 | 41.3 | 244.1 | yes |
| `monthly/2021-08` | 50,940 | 102 | 0 | 205 | 42.0 | 38.8 | 194.8 | yes |
| `monthly/2021-09` | 52,241 | 105 | 0 | 210 | 41.3 | 38.9 | 213.1 | yes |
| `monthly/2021-10` | 64,540 | 130 | 0 | 260 | 57.0 | 49.1 | 314.0 | yes |
| `monthly/2021-11` | 54,768 | 110 | 0 | 221 | 44.8 | 41.3 | 245.2 | yes |
| `monthly/2021-12` | 64,217 | 129 | 0 | 258 | 51.8 | 48.5 | 255.1 | yes |
| `monthly/2023-11` | 61,638 | 124 | 4,395 | 248 | 51.8 | 56.2 | 283.1 | yes |
| `monthly/2024-01` | 65,708 | 132 | 7,652 | 264 | 62.6 | 72.1 | 256.6 | yes |
| `daily/202300220` | 2,967 | 6 | 217 | 13 | 1.7 | 2.1 | 5.6 | yes |
| **Total** | **1,450,598** | **2,917** | **12,264** | **5,843** | **1,179.8** | **1,231.0** | **5,989.5** | |

`monthly/2020-01`'s load time spans its interruption and resume and is therefore
not a clean single-load measurement; the other twenty-six are.

Every package's publication dates fall inside the interval its identity derives,
with no null dates. Formats split 1,383,251 legacy and 67,347 eForms: the 2020
and 2021 months are entirely legacy, which is also why they carry no official
change references — that field is an eForms one. Schema versions range from two
in a 2020 month to nine in January 2024.

## A notice TED published daily and omitted monthly

`monthly/2021-05` loaded 54,043 notices and reconciled internally: members,
distinct keys and loaded rows all agree. The API announced 54,044 for the same
window and named the difference: publication `231901-2021`.

The archive was inspected rather than assumed, because a comparable-looking
count mismatch during the previous rehearsal turned out to be a projection
defect, not a source one. This time it is not:

- Walking every member of `2021-05.tar.gz` finds 54,043 files, no nested
  containers, and no member whose name carries `231901`. The notice is not in
  the archive; nothing dropped it.
- The Search API describes it as a `can-standard` notice published 2021-05-07 in
  OJ S 89/2021 — inside the month the package claims to cover.
- The daily package for that issue, `daily/202100089`, **does** contain it, as
  `20210507_089/231901_2021.xml` among its 5,059 notices.

So TED published the notice in its daily XML package and left it out of the
monthly aggregate. Two verification attempts 71 minutes apart report identical
figures — 54,044 announced, 54,043 local, one identifier only in the source, 218
pages — so this is not a transient index state.

The pipeline's response is the designed one and needs no exception: coverage is
`mismatch`, the capture stays published but unverified, no checkpoint is sealed,
and `ingest-manifest` stops there without attempting later entries. Making this
package verifiable would require either accepting a capture whose coverage is not
exact, or assembling one capture from two different archives — the first relaxes
the invariant the checkpoint exists to enforce, the second breaks the
one-artifact-one-capture correspondence recovery depends on. Neither is done
here. The gap is reported instead.

A consequence worth stating plainly: a single run of the public manifest stops at
entry 17 and always will while the source disagrees with itself.

## Replay

The source-audit replay was measured in two parts because of that stop. A
separate run of the verified manifest exercises all 26 checkpoints in one call.

| Run | Entries | Outcome | Acquisition requests | Bytes | Seconds |
| --- | ---: | --- | ---: | ---: | ---: |
| Public manifest, entries 1-16 | 16 | all `replayed` | 0 | 0 | 844.4 |
| Public manifest, entry 17 | 1 | `incomplete`, stops the run | 0 | 0 | (included) |
| Slices covering entries 18-27 | 10 | all `replayed` | 0 | 0 | 388.4 |
| Verified manifest | 26 | all `replayed`, exit 0 | 0 | 0 | 740.8 |

Twenty-six of twenty-seven entries replay: each re-validates its stored artifact
and accepts the existing checkpoint without a single acquisition request. Durable
state was identical before and after except for one new verification attempt —
entry 17 legitimately re-checking the coverage that failed, which queried the API
over 218 pages and failed again the same way. No new capture, run, batch,
checkpoint or notice row was created by either replay.

The complete verified-manifest replay left all durable counts and the WAL
position unchanged, including 28 verification attempts before and after. Its
26 report entries each carry `http_attempts = 0` and `downloaded_bytes = 0`.

Revalidating 4.14 GiB of archives is not free: the first sixteen entries alone
took 844 seconds and peaked at 145.5 MB of process memory. Replay is cheap
compared with loading, not costless.

## Scale and overlap

| Measure | Notices |
| --- | ---: |
| Published observations across 27 captures | 1,450,598 |
| Distinct canonical notices | 1,447,631 |
| Distinct notices in exactly-verified packages | 1,393,588 |
| Identities carried by exactly one package | 1,444,664 |
| Identities carried by two packages | 2,967 |

The only overlap in the whole set is the accepted daily package inside
`monthly/2023-11`, and it is exact: 2,967 shared identities, none only in the
day. `tl_read.distinct_notice` collapses that overlap; the physical table holds
1,450,598 rows because every package was acquired exactly once and no capture was
superseded.

## Resources

| Measure | Value |
| --- | ---: |
| Compressed artifacts on disk | 4,446,120,792 B (4.14 GiB) |
| Scale database | 439,580,339 B |
| `tl_work.notice_capture` heap / indexes | 318,251,008 B / 109,789,184 B |
| PostgreSQL data directory | 2,509,373,509 B |
| Temporary I/O recorded by the scale database | 4,532 files, 34,568,675,036 B cumulative |
| Docker VHDX | 9,982,443,520 B (9.30 GiB) |
| Host free space at the end | 634.8 GiB |
| Peak loader process memory | 179.6 MB |

Time splits into acquisition 1,179.8 s, loading 1,231.0 s and source verification
5,989.5 s, for 8,106.8 s of package work inside 8,152 s of slice wall time.
Loading ran at about 1,178 notices per second. **Verification, not loading, is
74% of the cost**, and it is bounded by the source: 5,843 API pages over 8,001
HTTP attempts, because the archive is one request and the reconciliation is one
request per 250 notices.

Database growth is modest — about 303 bytes per distinct notice including
indexes — so at this scale storage is dominated by the compressed archives, not
by PostgreSQL. The temporary-byte counter is cumulative I/O rather than retained
disk usage. The VHDX nevertheless grew as the benchmark and partitioning work
spilled and does not shrink automatically, which is why host footprint and
logical dataset size are reported separately.

## SQL benchmark at 1.45 million rows

The dataset was frozen, both candidate indexes were dropped explicitly, and
`VACUUM (ANALYZE)` ran before the baseline. Each variant used
`effective_cache_size = 1536MB`, one warmup, 20 timed repetitions and one
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`. All six row counts and SHA-256
checksums match between variants.

| Workload | Rows | Baseline median (ms) | Candidate median (ms) | Change |
| --- | ---: | ---: | ---: | ---: |
| monthly_notice_counts | 34,330 | 1,383.8 | 1,410.9 | +2.0% |
| latest_capture_per_publication | 1,447,631 | 3,910.4 | 3,963.9 | +1.4% |
| official_change_references | 1,450,691 | 5,561.6 | 5,606.8 | +0.8% |
| acquisition_cutoff_state | 252,666 | 684.9 | 691.0 | +0.9% |
| monthly_coverage_calendar | 72 | 0.9 | 0.9 | +0.4% |
| cross_package_overlap_audit | 58,671 | 113.5 | 113.2 | -0.2% |

Neither candidate index is adopted, and this run is the reason the earlier one
should not have settled the question. At 227,991 notices,
`official_change_references` improved 18.5% with the candidate index — the one
result that looked like a case for adopting it. At 1,447,631 it improves
nothing: +0.8%, inside the noise, with five of six workloads marginally slower
under the extra write and planning cost. The [scale requirements](scale-and-sql.md)
warned that a 228,000-notice dataset can sit entirely in cache and that its
conclusions must be re-measured; they were, and one of them did not survive.

Growth is close to linear and slightly sub-linear where the work is bounded by
something other than the table. Against the earlier rehearsal the dataset grew
6.35x, while `monthly_notice_counts` grew 6.1x, `latest_capture_per_publication`
5.0x and `official_change_references` 3.9x. `cross_package_overlap_audit` barely
moved (111.5 ms to 113.5 ms) because it still compares the same two packages
over the same 58,671 identities: the audit's cost follows the overlap it is
asked about, not the size of the archive around it.

`acquisition_cutoff_state` answers a different question at this scale than it
did before. Its cutoff of 5 now selects the first five acquisitions — the
252,666 notices of January to May 2020 — rather than most of the dataset.

## Annual partitioning at scale

An isolated database loaded the same 1,450,598 rows into a plain heap and an
annual range-partitioned table, with equivalent indexes. Every workload returned
the same row count and checksum.

| Workload | Heap median (ms) | Partitioned median (ms) | Change | Relations scanned |
| --- | ---: | ---: | ---: | --- |
| monthly_notice_counts | 1,231.6 | 1,245.2 | +1.1% | all partitions |
| one_year_window | 804.5 | 799.4 | -0.6% | all partitions |
| cross_year_window | 765.1 | 759.6 | -0.7% | all partitions |
| direct_one_year_window | 86.1 | 86.3 | +0.2% | **one partition** |
| direct_all_years | 175.2 | 179.4 | +2.4% | all partitions |
| latest_capture_per_publication | 1,457.2 | 1,470.8 | +0.9% | all partitions |

The conclusion is the rehearsal's, now on 6.35x the data and with one correction.
Pruning still works only against the table directly: a date-restricted query
reaches one of nine partitions, while everything going through the `DISTINCT ON`
read surface scans all of them. The 24.3% gain the rehearsal saw on
`one_year_window` does not reappear (-0.6%), which confirms that reading was
variance rather than pruning, exactly as its plan already suggested.

The partitioned copy occupied 450,912,256 bytes against 431,579,136 for the
heap, 4.5% more, and its primary key would still need `publication_date`,
changing the canonical uniqueness constraint. Partitioning is not adopted. What
would have to change first is the read surface, not the storage layout.

## What the 2020-2025 run would cost

Rates measured here, extrapolated to the 72 monthly packages and 4,523,626
API-reported notices of the historical target:

| Measure | Measured now | Projected for 2020-2025 |
| --- | ---: | ---: |
| Database bytes per distinct notice | 303.5 | 1.28 GiB total |
| Compressed archive bytes per distinct notice | 3,071.3 | 15.63 GiB total (header inventory) |
| Logical artifacts plus scale database | 4.55 GiB | 16.9 GiB, 25.4 GiB with a 50% margin |
| Current host footprint: artifacts plus Docker VHDX | 13.44 GiB | Not projected; the VHDX also contains other local databases, benchmark spill history and engine overhead |
| Seconds per monthly package | 311.4 | 6.2 h, 9.3 h with a 50% margin |
| Loading throughput | 1,178 notices/s | — |
| Share of time spent verifying | 73.9% | — |

Neither storage nor time is the obstacle: the projection sits far under the
150 GiB operating ceiling and inside a single long session. Capacity does not
force artifact deletion, although the historical run still needs an explicit
retention policy for repeatability and local disk management. Two caveats
belong with those numbers. The
rates come from 2020 and 2021, and later years are larger per package, so the
time estimate is optimistic. And verification cost is set by the source — one
request per 250 notices — so a slower API moves the wall clock and nothing else.

The real obstacle is the one this run found. A manifest stops at the first
package whose coverage the source cannot confirm, so a 72-month run would stop
at the first gap like May 2021's rather than at its end. Deciding how a package
in that state should be represented — and whether an archive gap that the daily
packages can fill should be filled — is a design question, not a capacity one,
and it is the prerequisite for the historical run rather than more hardware.

## What this run does not establish

- Complete 2020-2025 historical coverage; 26 of 72 monthly packages are loaded,
  one of those is unverified, and one additional daily package is loaded.
- That the source is internally consistent. One package proves it is not.
- Production orchestration, distributed processing or cloud operation.
- Performance on other hardware, or under concurrency: every measurement here is
  one machine, one connection at a time, PostgreSQL capped at 2 CPU and 2 GiB.
- Awards, spending or supplier outcomes; this dataset models published notices.
