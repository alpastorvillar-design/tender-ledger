# Measured rehearsal (M3d)

Status: the five real identities in `manifests/m3-pilot.json` completed under
projection contract v3. They produced **230,958 published observations and
227,991 distinct canonical notices**, every package matched the TED Search API
exactly, and a complete second run replayed all five with no HTTP request or new
database state. The 100,000-notice engineering gate is complete. The
1,000,000-notice minimum in [scale-and-sql.md](scale-and-sql.md) remains open.

The run used an exclusive `tender_ledger_m3_rehearsal` database. The development
database was unchanged before and after: one capture, 2,967 notices, one run,
one checkpoint and two verification attempts.

## Environment

Windows 11 Pro, AMD Ryzen 7 7800X3D (16 logical processors host-side), 31.1 GiB
RAM. PostgreSQL 17.11 ran in the project's Compose stack, limited to 2 CPU and
2 GiB. Python 3.14.3. The host had more than 600 GiB free throughout, above the
100 GiB operating gate.

## Archive layouts and projection findings

Two real monthly layouts exist. One is flat XML under per-day directories. The
other wraps publication days in `.tar.gz` containers inside the monthly
archive. The walker admits exactly one level of nesting when the package policy
allows it and applies the same safe-path, regular-file, member-size, aggregate
size, gzip-integrity, tar-trailer and trailing-data checks inside it. Inner
expansion spends the outer archive's expanded-byte budget. Daily packages do
not allow containers, second-level nesting is rejected, and a package cannot
mix flat XML and nested containers.

The real run also established two projection rules now encoded by contract v3:

1. eForms can contain more than one `efbc:PublicationDate`. The authoritative
   value is the child of `efac:Publication`; a privacy date elsewhere in the
   document is a different field and may occur first.
2. Some legacy `TED_EXPORT` roots are namespaced while their direct
   `CODED_DATA_SECTION` resets to the empty namespace. The section and its
   descendants must be read using the namespace actually present there.

Before these structural paths were enforced, 16 notices in November 2023 and
217 in January 2024 appeared to fall outside their packages, and 18 January
legacy notices appeared to have no coded section. Inspection of the failing XML
members showed that all three counts came from the two path-selection defects.
After the correction, every member projects, both monthly intervals hold, and
the API confirms the complete identifier sets. Because the projected facts
changed, the contract advanced from v2 to v3; checkpoints under v2 were
reprojected instead of replayed.

`BusinessRegistrationInformationNotice`, the fourth observed eForms root, is
allow-listed by exact name. It carries no buyer or procurement project, so
buyer, CPV and change-reference fields remain explicitly absent rather than
being inferred from other parties.

## Survey of the five artifacts

All artifacts were acquired with the bounded client and surveyed using the same
walker and projector as the loader.

| Identity | Layout | Containers | Members | Formats | Loadable under v3 |
| --- | --- | ---: | ---: | --- | --- |
| `monthly/2020-01` | nested | 22 | 50,123 | legacy 50,123 | yes |
| `monthly/2020-02` | flat | 0 | 50,522 | legacy 50,522 | yes |
| `monthly/2023-11` | flat | 0 | 61,638 | legacy 35,866 / eForms 25,772 | yes |
| `monthly/2024-01` | nested | 22 | 65,708 | legacy 25,287 / eForms 40,421 | yes |
| `daily/202300220` | flat | 0 | 2,967 | legacy 1,813 / eForms 1,154 | yes |

Acquisition measurements for the three packages added during this rehearsal:

| Identity | Bytes | Seconds | MiB/s | Peak process RSS |
| --- | ---: | ---: | ---: | ---: |
| `monthly/2020-01` | 139,301,298 | 45.0 | 2.95 | 55.2 MB |
| `monthly/2023-11` | 250,854,274 | 65.8 | 3.63 | 66.5 MB |
| `monthly/2024-01` | 291,615,691 | 73.4 | 3.79 | 163.2 MB |

The archive is streamed; it is not held whole in memory. A failed acquisition
also reports the requests and bytes it consumed.

## Complete manifest and replay

The unmodified pilot manifest ran in order against already acquired artifacts.
Contract v2 checkpoints were intentionally not considered current under v3.

| # | Identity | v3 run | Complete replay |
| ---: | --- | --- | --- |
| 1 | `monthly/2020-01` | processed | replayed, 0 HTTP |
| 2 | `monthly/2020-02` | processed | replayed, 0 HTTP |
| 3 | `monthly/2023-11` | processed | replayed, 0 HTTP |
| 4 | `monthly/2024-01` | processed | replayed, 0 HTTP |
| 5 | `daily/202300220` | processed | replayed, 0 HTTP |

The v3 run took 1,324.4 seconds. Its acquisition counters stayed at zero because
the five checksummed artifacts were already on disk; source verification still
queried TED. The replay took 138.9 seconds because it revalidated each local
archive before accepting its checkpoint. It performed no HTTP request and left
the durable counts exactly unchanged: 10 captures, 445,208 historical
`notice_capture` rows, 9 ingest runs, 5 current checkpoints and 10 verification
attempts.

Per-package evidence was read back from PostgreSQL rather than trusted from the
runner's own return value:

| Identity | Capture | Members = keys = rows | Batches | Change refs | API pages | Only local | Only API | API duplicates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `monthly/2020-01` | 6 | 50,123 | 101 | 0 | 202 | 0 | 0 | 0 |
| `monthly/2020-02` | 7 | 50,522 | 102 | 0 | 204 | 0 | 0 | 0 |
| `monthly/2023-11` | 8 | 61,638 | 124 | 4,395 | 248 | 0 | 0 | 0 |
| `monthly/2024-01` | 9 | 65,708 | 132 | 7,652 | 264 | 0 | 0 | 0 |
| `daily/202300220` | 10 | 2,967 | 6 | 217 | 13 | 0 | 0 | 0 |

The API page counts include the terminal confirmation request. Every checkpoint
records contract v3 and a named verified attempt.

## Scale and overlap

| Measure | Notices |
| --- | ---: |
| Published observations across five captures | 230,958 |
| Distinct canonical notices | 227,991 |
| `daily/202300220` intersect `monthly/2023-11` | 2,967 |
| In the month but outside that daily issue | 58,671 |
| In the daily issue but outside the month | 0 |

This distinguishes physical observations from analytical notice grain. The
daily package is an exact subset of its month; `tl_read.distinct_notice`
deduplicates that overlap. The rehearsal database has 445,208 physical rows
because it deliberately retains superseded v2 captures as provenance.

The five compressed artifacts occupy 833,157,516 bytes. At the end of the v3
run the rehearsal database occupied 168,138,419 bytes, including superseded and
failed history plus candidate benchmark indexes. These are measured local
figures, not a storage forecast. The 1,000,000-notice run must remeasure database
growth, WAL, temporary spill and parser memory rather than linearly assuming
them.

## SQL benchmark

The dataset was frozen, candidate indexes were dropped explicitly, and
`VACUUM (ANALYZE)` ran before the baseline. Each variant used
`effective_cache_size = 1536MB`, one warmup, 20 timed repetitions and one
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` per workload. All six result counts
and SHA-256 checksums match between variants.

| Workload | Rows | Baseline median (ms) | Candidate median (ms) | Change |
| --- | ---: | ---: | ---: | ---: |
| monthly_notice_counts | 5,281 | 227.8 | 230.5 | +1.1% |
| latest_capture_per_publication | 227,991 | 786.1 | 841.8 | +7.1% |
| official_change_references | 396,341 | 1,419.9 | 1,156.6 | -18.5% |
| acquisition_cutoff_state | 162,283 | 444.3 | 443.9 | -0.1% |
| monthly_coverage_calendar | 72 | 0.7 | 0.7 | -0.4% |
| cross_package_overlap_audit | 58,671 | 111.5 | 111.4 | -0.2% |

The official-reference workload improves materially, but the latest-capture
workload regresses and the other four do not justify the extra index at this
scale. Neither broad candidate index is adopted. The historical
change-reference query now includes `capture_id` in its final ordering so
superseded and current observations have a total, repeatable order; the first
benchmark attempt exposed the missing tie-breaker by returning the same rows in
a different order.

## Annual partitioning

An isolated database loaded the same 445,208 historical rows into a plain heap
and an annual range-partitioned table, with equivalent indexes. Every workload
returned the same row count and checksum.

| Workload | Heap median (ms) | Partitioned median (ms) | Change | Relations scanned through the read surface |
| --- | ---: | ---: | ---: | --- |
| monthly_notice_counts | 222.2 | 238.0 | +7.1% | all partitions |
| one_year_window | 158.5 | 120.1 | -24.3% | all partitions |
| cross_year_window | 120.4 | 124.4 | +3.3% | all partitions |
| direct_one_year_window | 23.2 | 23.2 | +0.1% | **one partition** |
| direct_all_years | 50.3 | 49.8 | -1.0% | all partitions |
| latest_capture_per_publication | 339.3 | 330.7 | -2.5% | all partitions |

The apparent gain for `one_year_window` did not come from pruning: the
`DISTINCT ON` read surface prevented the date filter from reaching the base
table, and its plan scanned all partitions. A direct date-restricted table
query proves that PostgreSQL can prune to one partition. Real workloads would
need a redesigned read surface before gaining that property.

The partitioned copy occupied 140,091,392 bytes against 133,087,232 bytes for
the heap, 5.3% more. Its primary key would also need to include
`publication_date`, changing a core uniqueness constraint. Partitioning is not
adopted at this scale.

## What this rehearsal does not establish

- The 1,000,000-distinct-notice minimum gate.
- Complete 2020-2025 historical coverage.
- Benchmark or partitioning behavior at the minimum gate's scale.
- Production orchestration, distributed processing or cloud operation.
- Awards, spending or supplier outcomes; this dataset models published notices.

The next scale slice should extend the monthly manifest past one million
distinct real notices, preserve exact per-package source verification, exercise
recovery during that run, and repeat the resource and SQL measurements before
accepting any physical-design change.
