# Design and source contract

Status: source inspection and the transactional PostgreSQL loader are implemented.
See [projection.md](projection.md) for the field contract and [loading.md](loading.md)
for the capture/batch/publish semantics and their guarantees. This document keeps
the source observations and the rationale behind those semantics.

## Source selection and observed behavior

Load daily and monthly TED XML packages first. Use the Search API for counts and identifier reconciliation, with `scope: ALL` and `checkQuerySyntax: false`. A separate API-to-database adapter is deferred until it has a concrete use case.

Observed on 2026-09-02:

| Check | Result |
| --- | --- |
| `/packages/daily/202300220` | HTTP 200, gzip; the documented `/packages/notice/daily/...` variant returned 404 |
| 72 monthly HEAD requests, 2020–2025 | All HTTP 200; sum 16,783,140,714 compressed bytes |
| Six annual API counts | 643,552; 676,734; 735,067; 795,680; 801,444; 871,149 |
| Mixed package, 2023-11-15 | 2,967 XML members and distinct publication keys |
| Mixed-day formats | 1,813 legacy; 1,154 eForms; format determined by each member's root |
| API comparison for the mixed day | 12 pages, 2,967 distinct keys, no missing or extra keys |
| Historical query with ALL / LATEST | 4,523,626 / 0 results, both HTTP 200 |
| Sunday 2020-01-05 with ALL | 0 reported results; zero is not intrinsically an error |

These are observations, not a provider guarantee. Equal counts alone do not establish equal datasets. Archive and API channels share the same provider and can have correlated errors. Record query parameters, capture time, returned total, timeout state, duplicate counts, and available identifier differences.

A mismatch or unavailable verification leaves coverage unresolved; it must not advance a successful checkpoint. A legitimate empty period needs supporting publication-calendar/source evidence. Never infer it solely from HTTP 200 or a missing package.

Sources: [download conventions](https://docs.ted.europa.eu/ODS/latest/reuse/download-direct.html) and [API pagination](https://docs.ted.europa.eu/ODS/latest/reuse/search-api.html).

## Notice identity and formats

Canonical publication identity is `(publication_year, publication_number_int)`. Retain the original source strings. For example, `995-2020`, `000995_2020.xml`, and `00000995-2020` normalize to one key. Enforce uniqueness and check filename/document agreement where the XML exposes that publication identifier.

Select parsers by root and namespace, never by filename width or publication year. The inspected mixed archive contains legacy namespaces R2.0.8/R2.0.9 and eForms SDK versions 1.3, 1.6, 1.7, 1.8, and 1.9. Its R2.0.8 documents omit the VERSION attribute, so the inspector records the namespace version when that optional attribute is absent. Unknown roots fail explicitly.

Publication date is a calendar date. Values such as `2020-01-03+01:00` and dispatch date `2019-12-26Z` retain their original suffix separately; neither supplies an event time.

The curated projection will use an explicit field allowlist and exclude contact details. Raw archives remain private. Constructed test fixtures contain no real contact information. Check reuse terms before publishing any real excerpts: [TED legal notice](https://ted.europa.eu/en/legal-notice).

## Publication changes and acquisition history

A procurement procedure, a published notice, an official change reference, and a capture of source bytes are separate concepts.

The [eForms change specification](https://docs.ted.europa.eu/eforms/latest/schema/change-notice.html) gives a change notice its own notice identifier and an explicit reference to the changed notice. A procedure identifier is useful for grouping; it must not replace that reference.

Observed examples reinforce the distinction: notice 335840-2025 has version 2 without a returned change reference; 335841-2025 has version 1 and references 288027-2025. Absence of eForms fields in legacy does not imply absence of corrections or references. Preserve supported links and unresolved targets, and report their coverage by format.

Do not conclude that published files are immutable from a small unchanged sample. Keep immutable *local captures* and source provenance. A retry reuses a persisted capture identity; an intentional refresh creates a new capture identity. Hashes identify content, not every occurrence of content. A sequence A → B → A for the same package must retain all three captures even when the first and third reuse the same stored bytes.

Capture order describes what this system acquired, not the official publication/version order. Do not sort recaptures using unchanged publication dates. Do not present archival ingestion time as a historical business-event time.

## Loading and reader visibility

Implemented in [loading.md](loading.md). In summary:

1. Hash the whole archive; a mid-run change or truncation cannot publish as complete. (Resumable download is a later slice; the loader takes a local file.)
2. A capture identity is persisted before any row loads, with source package identity, checksum, and contract version, and survives retries.
3. Members stream and apply as deterministic batches into capture-scoped rows; batch data and batch status commit together.
4. Reconcile expected members, distinct keys, and loaded records. Source (API) checks are still deferred, so `source_coverage_verified` stays false.
5. Publish is a single transactional pointer swap. `tl_read` views expose only the published capture per package; `tl_work` tables are internal and unreachable by the reader role.

Readers keep seeing the previous complete capture while a replacement is partial or failed - the pointer swap is the only visibility change. Partial batches are never upserted into an exposed current-state table. `tl_read.distinct_notice` collapses repeated content from overlapping daily/monthly packages so it does not inflate the distinct-publication count.

Raw storage and PostgreSQL do not share a transaction. Preserve recoverable artifacts after failure. Serialize conflicting work initially; do not promise exactly-once provider delivery.

Start with an unpartitioned PostgreSQL baseline and a year-aware publication key. Evaluate indexes and annual partitioning against measured plans before the historical run. Any migration must preserve uniqueness and query results.

## Download integrity and resource bounds

Retain compressed archives and stream XML members. Do not expand the historical archive to disk. Compare advertised and received lengths where available and compute a local checksum.

Observed endpoints advertise byte ranges but lack ETag/Last-Modified validators. The first downloader restarts incomplete downloads; it does not combine byte ranges from potentially different object versions. Reuse completed verified artifacts on replay.

The implemented inspector checks gzip CRC and trailer completion, validates supported member types, and applies byte/record limits. Its duplicate-key set is bounded by the configured notice limit; the historical loader will move large-scale uniqueness enforcement to PostgreSQL.

## Acceptance before database loading is complete

- Identical capture retries do not duplicate records or advance incomplete windows.
- Conflicting identities, unsupported members, corrupt input, or failed verification remain explicit failures.
- A legitimate empty period and an unavailable source are distinct states.
- Interruptions between batches preserve the previous visible complete dataset.
- A failed publication transaction changes neither visibility nor completion status.
- A → B → A recapture remains distinguishable from a retry.
- Recovery produces the same records as a clean execution over identical artifacts.

Airflow follows the verified CLI. Scale, SQL, and recovery requirements remain in [scale-and-sql.md](scale-and-sql.md).
