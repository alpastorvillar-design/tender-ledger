# Measured rehearsal (M3d)

Status: the rehearsal now runs against real data at the scale the
100,000-notice engineering gate asks for -- **165,250 published notices,
162,283 distinct canonical identities** -- and two of the five
`manifests/m3-pilot.json` identities are still not processable. Both remaining
blockers are properties of real published archives, measured and quantified
below, not gaps in the archive walker. The million-notice minimum in
[scale-and-sql.md](scale-and-sql.md) remains unmet.

Commit at the start of this run: `6362d68c4616fb0b1e9d6fa7c88c98102bd0b964`.
Database: an exclusive `tender_ledger_m3_rehearsal`, carried forward from the
previous rehearsal (it already held verified captures of `monthly/2020-02` and
`daily/202300220`); the two candidate benchmark indexes were dropped by explicit
DDL before the new baseline. The development database was never touched and is
identical before and after: 1 capture, 2,967 notices, 1 run, 1 checkpoint.

## Environment

Windows 11 Pro, AMD Ryzen 7 7800X3D (16 logical processors host-side), 31.1 GiB
RAM. PostgreSQL 17.11 in the project's own Compose stack, limited to 2 CPU /
2 GiB as configured. Python 3.14.3. Host free disk went from 633.8 GiB to
632.4 GiB (gate: at least 100 GiB free).

## What the archive walker learned

Two real monthly layouts exist. One is flat XML under a per-day directory. The
other wraps each publication day in its own `.tar.gz` inside the month's
archive. The walker now admits exactly one level of that nesting, for a package
identity whose policy opens it, and applies every existing defense inside a
container: safe paths, regular files only, per-member and aggregate byte
ceilings, gzip CRC, tar trailer and trailing-data checks. A container's
expansion is charged to the archive's own expanded-byte budget, so compression
inside a day cannot buy work the identity's ceiling forbids. A daily package
refuses nesting whatever it contains, a container inside a container is refused
everywhere, and an archive mixing flat XML with containers is refused rather
than half-read. See [loading.md](loading.md).

`BusinessRegistrationInformationNotice` -- the eForms SDK's fourth notice
document, which carries its own namespace rather than a UBL one -- was added to
the allow-list by exact root name after a bounded structural read of
`monthly/2023-11` found one among its 61,638 members. See
[projection.md](projection.md).

## Survey of the five real artifacts

Every artifact was downloaded with the project's own bounded client and
surveyed before any load.

| Identity | Layout | Containers | Members | Formats | Loadable |
| --- | --- | ---: | ---: | --- | --- |
| `monthly/2020-01` | nested | 22 | 50,123 | legacy 50,123 | yes |
| `monthly/2020-02` | flat | 0 | 50,522 | legacy 50,522 | yes |
| `monthly/2023-11` | flat | 0 | 61,638 | legacy 35,866 / eForms 25,772 | yes |
| `monthly/2024-01` | nested | 22 | 65,708 | legacy 25,287 / eForms 40,421 | **no** |
| `daily/202300220` | flat | 0 | 2,967 | legacy 1,813 / eForms 1,154 | yes |

`monthly/2024-01` reports 18 members with reason `unprojectable_notice`. Those
are legacy `TED_EXPORT` notices published with no `CODED_DATA_SECTION`: a
supported root with no publication date, buyer country or CPV anywhere in it.
The projection cannot produce a row for them without inventing one, and a load
is all-or-nothing, so the whole month is refused.

That the survey knows this at all is a fix this round. `compatible_for_load` was
computed from the walk alone, so it called `monthly/2024-01` loadable and a load
of it then failed. The survey now projects each member and discards it, which is
the only way its answer can mean what it says.

Acquisition, measured (peak working set of the acquiring process; the archive is
streamed and never held whole in memory):

| Identity | Bytes | Seconds | MiB/s | Peak RSS |
| --- | ---: | ---: | ---: | ---: |
| `monthly/2020-01` | 139,301,298 | 45.0 | 2.95 | 55.2 MB |
| `monthly/2023-11` | 250,854,274 | 65.8 | 3.63 | 66.5 MB |
| `monthly/2024-01` | 291,615,691 | 73.4 | 3.79 | 163.2 MB |

## Manifest run

`ingest-manifest` ran against the unmodified pilot manifest, twice.

| # | Identity | First run | Second run |
| ---: | --- | --- | --- |
| 1 | `monthly/2020-01` | processed | replayed, 0 HTTP |
| 2 | `monthly/2020-02` | replayed, 0 HTTP | replayed, 0 HTTP |
| 3 | `monthly/2023-11` | incomplete -- stops the manifest | incomplete |
| 4 | `monthly/2024-01` | not attempted | not attempted |
| 5 | `daily/202300220` | not attempted | not attempted |

The second run created no capture, batch, run or checkpoint and made no request
of any kind for entries 1 and 2. `daily/202300220`, which the manifest never
reaches, replays the same way when asked directly.

Per package, read back from the database rather than from the run's own report:

| Identity | Capture | Members = keys = rows | Batches | Refs | API pages | Only-local | Only-API | Duplicates | Checkpoint |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `monthly/2020-01` | 3 | 50,123 | 101 | 0 | 202 | 0 | 0 | 0 | current |
| `monthly/2020-02` | 1 | 50,522 | 102 | 0 | 204 | 0 | 0 | 0 | current |
| `monthly/2023-11` | 4 | 61,638 | 124 | 4,395 | — | — | — | — | none |
| `daily/202300220` | 2 | 2,967 | 6 | 217 | 13 | 0 | 0 | 0 | current |

Membership holds for every verified capture: `monthly/2020-01` spans
2020-01-02..2020-01-31 and `monthly/2020-02` spans 2020-02-03..2020-02-28.

## Blocker: publication dates the source itself contradicts

`monthly/2023-11` loads and reconciles -- 61,638 members, 61,638 distinct keys,
61,638 rows, 4,395 change references -- and is then refused before the source is
asked. 16 of its notices carry an `efbc:PublicationDate` in 2028, 2033 or 2053
while their dispatch date and canonical identity are November 2023, and the
monthly contract requires every row of a monthly capture to be published inside
the month it is filed under.

A bounded probe of the TED Search API for exactly those 16 identities answers
what the archive cannot: the source's own indexed publication dates are
2023-11-03 through 2023-11-30, all inside the month. The same query the verifier
would use reports 61,638 notices for November 2023, which is the archive's
member count exactly. The archive's XML element is wrong for those 16 documents;
the package is the month it claims to be.

This is not confined to one month. The partial load of `monthly/2024-01`
(49,000 of 65,708 members, before it failed for the unrelated reason above)
already carries 217 rows dated outside January 2024, spread over 2025-2034 with
176 of them in 2034.

There is no fix to make here. The rule refusing these captures is the one that
proves a monthly archive is the month it names, and loosening it by a tolerance
would prove nothing. Whether that proof should instead rest on the source's own
enumeration -- every remote row inside the interval, and the local key set equal
to it exactly, which is already required and is a stronger claim than trusting
the archive's own date element -- is a contract decision, and it is left open.

## Scale reached

| | Notices |
| --- | ---: |
| Observations across published captures | 165,250 |
| Distinct canonical identities | 162,283 |
| Overlap, `daily/202300220` and `monthly/2023-11` | 2,967 |

The overlap is the positive case the previous rehearsal could not reach, and it
resolves in both directions: every one of the daily package's 2,967 identities
is in the month, the month holds 58,671 the day does not, and none is only in
the day. The rehearsal database also retains 49,000 rows from the failed
`monthly/2024-01` capture; they are internal to that capture and appear in no
reading surface.

The 100,000-notice engineering gate is met. The 1,000,000-notice minimum is not.

## Resources and extrapolation

| | Value |
| --- | ---: |
| Raw artifacts on disk (5 packages) | 833,157,516 B |
| Rehearsal database | 83,924,659 B |
| `notice_capture` heap / indexes | 48,226,304 B / 24,862,720 B |
| PostgreSQL data directory (shared with development) | 485,728,986 B |
| Docker WSL virtual disk | 3.80 GiB, from 3.43 GiB |
| Host free disk | 632.4 GiB |
| Temporary files written (cumulative, this database) | 589 files / 4,211,220,870 B |

Per notice that is about 3,607 compressed bytes on disk and about 392 stored
bytes across every table and index. Extrapolating linearly, the million-notice
minimum would cost roughly 3.6 GB of artifacts and 0.4 GB of database, and the
2020-2025 target -- for which TED announces 4,523,626 results -- roughly 16 GB
and 1.8 GB. Both sit far inside the 150 GiB budget and leave the 100 GiB free
gate untouched. The figure that does not extrapolate comfortably is temporary
files: 4.2 GB of sort spill at 165,250 rows with `work_mem` at 4 MB. That is a
tuning question the million-row run has to answer rather than assume.

## Benchmark

165,250 published notices, `effective_cache_size` fixed to 1536 MB in every
session, `VACUUM (ANALYZE)` before the baseline and `ANALYZE` after applying
[`queries/benchmark_candidate_indexes.sql`](../queries/benchmark_candidate_indexes.sql).
One `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, one warmup and 20 timed
repetitions per variant. Every workload's checksum and row count are identical
between variants; a differing result would have invalidated that comparison
before any timing was read.

| Workload | Rows | Baseline median (ms) | Candidate median (ms) | Change | Plan changed? |
| --- | ---: | ---: | ---: | ---: | --- |
| monthly_notice_counts | 3,945 | 177.2 | 171.4 | −3.2% | yes |
| latest_capture_per_publication | 162,283 | 540.4 | 557.0 | +3.1% | yes |
| official_change_references | 165,290 | 707.6 | 582.6 | −17.7% | yes |
| acquisition_cutoff_state | 162,283 | 554.2 | 550.5 | −0.7% | yes |
| monthly_coverage_calendar | 72 | 0.7 | 0.8 | +5.4% | no |
| cross_package_overlap_audit | 58,671 | 213.8 | 209.9 | −1.8% | no |

Four plans changed and one median improved materially.
`official_change_references` moved from a sequential scan to an index-only scan
on the candidate index, and its planner execution time fell from 760 ms to
610 ms. The other three plan changes bought nothing: `monthly_notice_counts`
switched to a nested loop over the candidate index and its *planner* time got
worse (205 ms to 274 ms) while its client-side median improved slightly, which
is exactly the kind of disagreement that argues against reading a decision out
of one workload. The candidate index costs 8.5 MB at this size.

Client-side medians and planner execution times are reported separately and are
not comparable: the client figure includes fetching up to 162,283 rows.

Nothing is adopted. Two of six workloads moved outside the noise, in opposite
directions, and the whole working set still fits in cache.

## Annual partitioning

Evaluated on an isolated, disposable copy: the same 214,250 rows loaded twice
into one database, once as a plain heap and once range-partitioned by year on
`publication_date`, with equivalent indexes and the same protocol. Every
workload's checksum and row count match between the two.

| Workload | Heap median (ms) | Partitioned median (ms) | Change | Partitions scanned |
| --- | ---: | ---: | ---: | ---: |
| monthly_notice_counts | 172.3 | 178.3 | +3.5% | 9 of 9 |
| one_year_window | 125.3 | 130.8 | +4.4% | 9 of 9 |
| cross_year_window | 91.5 | 91.4 | −0.1% | 9 of 9 |
| direct_one_year_window | 21.6 | 22.4 | +3.5% | **1 of 9** |
| direct_all_years | 21.1 | 21.6 | +2.2% | 9 of 9 |
| latest_capture_per_publication | 234.2 | 235.7 | +0.6% | 9 of 9 |

Two results matter more than the timings. Pruning works: a date-restricted query
straight at the table read one partition instead of nine. And pruning never
fires for the shipped read surface: `distinct_notice` is a `DISTINCT ON` view
whose distinct key does not include `publication_date`, so the planner cannot
push a date restriction below it, and every query through it scanned all nine
partitions -- including the two written specifically to be prunable.

Structurally, a partitioned table's primary key must contain the partition key,
so the canonical identity key would become
`(capture_id, publication_year, publication_number, publication_date)` instead
of three columns. The partitioned copy is 6.1% larger: 68.6 MB against 64.6 MB.

At this size partitioning costs a little and buys nothing, and a partitioned
schema would additionally need the read surface reshaped before pruning could
reach a query anyone actually runs. That is the finding, not a deferral.

## What this rehearsal does not establish

- The 1,000,000-notice minimum gate in [scale-and-sql.md](scale-and-sql.md).
- That the pilot manifest can be run end to end. Two identities are blocked by
  real archive content, and both need a contract decision rather than a fix.
- Benchmark or partitioning conclusions at the minimum gate's scale.
- A decision on the 2020-2025 historical target, which needs a survey of how
  common each real layout and each anomaly is across the whole archive.
- Anything about legacy notices with no `CODED_DATA_SECTION` beyond the 18 found
  in one month, or about how many months carry publication dates the source's
  own index contradicts.
