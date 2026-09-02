# Tender Ledger: design and source observations

Status: proposed semantics, pending review and implementation.

## Verified source behavior

A bounded source probe on 2026-09-02 used the public, unauthenticated endpoint:

```text
POST https://api.ted.europa.eu/v3/notices/search
```

The request used `query: CY=ESP`, fields `publication-number` and `publication-date`, `limit: 2`, `scope: ALL`, and `paginationMode: ITERATION`.

With `checkQuerySyntax: true`, the probe returned HTTP 200 with an empty notice array and no result count. That response validated syntax; it did not establish that the query had no matching notices. A subsequent request with this flag false returned two notices and an iteration token.

One observed pair was `301759-2016` and `2016-09-01+02:00`. The date value is a calendar date with a timezone suffix, not an update timestamp. A query without a date filter returned historical notices; result order must not become an implicit checkpoint rule.

A second request using that token returned another two notices, with no overlap in publication numbers between the two pages. This probe verifies connectivity, the selected fields, and one successful pagination step. It does not establish complete historical coverage, snapshot consistency, correction semantics, or stable identifiers across every notice format. The meaning of the country alias and the intended date filter must be confirmed before finalizing the source contract.

Sources: [Search API](https://docs.ted.europa.eu/api/latest/search.html), [pagination and iteration](https://docs.ted.europa.eu/ODS/latest/reuse/search-api.html).

A separate probe used `query: PD>=20200101 AND PD<=20251231`, `limit: 1`, `scope: ALL`, `checkQuerySyntax: false`, and iteration mode. It returned HTTP 200, `timedOut: false`, one notice (`1-2020`, `2020-01-02+01:00`), and a reported total of 4,523,626. This establishes a plausible source pool for historical ingestion, not loaded volume or completeness.

TED documents daily and monthly XML packages without sign-in. Historical bootstrap will use those packages if the source contract and a bounded download validate them; the Search API will support recent windows and reconciliation. Both adapters represent the same source and must map to the same identity and business fields. Legacy TED XML and eForms require explicit format coverage. See [XML downloads](https://docs.ted.europa.eu/ODS/latest/reuse/download-xml.html) and [direct download conventions](https://docs.ted.europa.eu/ODS/latest/reuse/download-direct.html).

The direct-download documentation contains different route shapes in its pattern and example. Resolve the actual route with a bounded request before building a bulk client. XML filename padding also differs from the observed API identifier: retain original references and verify a shared canonical identity rather than treating formatting differences as distinct notices.

## Proposed processing model

1. Extract one bounded window into immutable raw pages or validate an archive and its member inventory.
2. Mark its manifest complete only when extraction finishes successfully.
3. Validate and normalize the selected business fields.
4. Apply a bounded batch, quality checks, and its applied-state marker in one PostgreSQL transaction. The initial small window is one batch.
5. Expose run status and queryable current notices and observations.

Raw storage and PostgreSQL do not share a transaction. A crash may leave an unused raw artifact; a retry must recognize a complete manifest and apply it safely. It must never treat a truncated extraction as a complete window.

Historical archives must not become a single transaction over millions of records. The proposed extension uses deterministic bounded batches, `COPY FROM STDIN` into staging, and set-based application. A window becomes complete only after all expected batches pass reconciliation. Review must settle how partial batches are exposed to readers, how window completion is committed, and how replay behaves after any batch boundary. Until then, multi-batch visibility is an open design decision, not an implemented guarantee.

Candidate notice key: `publication-number`, subject to checking the supported formats. Candidate observation key: notice key plus a hash of canonical business fields. Retrieval time is provenance and must not create a new business observation on every rerun.

The first implementation should serialize overlapping work for a source. It should not claim exactly-once delivery from TED. The goal is an idempotent destination effect under the tested failure model.

## Questions to resolve before locking the schema

- What identifies an original notice, correction, and publication version?
- Which date field and country filter express the intended dataset?
- Can a result set change during iteration, and how is incompleteness detected?
- How do token expiration, window splitting, and bounded retries interact?
- How are changes older than a lookback window found and reconciled?
- Which normalized fields belong in the observation hash?
- How should a content sequence A → B → A retain its observed chronology while identical retries remain idempotent? A content hash alone identifies a value, not every occurrence of that value.
- What reuse and attribution requirements apply to checked-in source examples?
- How do XML and API projections produce equivalent identities and hashes across legacy forms and eForms?
- What batch size, completion model, and reader visibility preserve recovery without a historical transaction of unbounded size?
- Which archive routes, compressed sizes, expansion ratios, and format distributions are observed in a bounded sample?

## MVP acceptance scenarios

- Reapplying identical input leaves canonical rows and observation counts unchanged.
- Changed business content creates a distinct observation without duplicate current rows.
- A failed or incomplete extraction cannot advance its checkpoint.
- A database failure rolls back both data and checkpoint changes.
- Invalid required fields fail explicitly and preserve enough provenance to diagnose the record.
- A replay after a crash produces the same business result as a clean run.

The MVP has a Python CLI and PostgreSQL. Airflow orchestration follows once these scenarios are verified. No user interface, paid service, second source, streaming system, or model API is required.

The MVP is an intermediate correctness milestone. Completion additionally requires the real-data, SQL, resource, and recovery evidence in [Scale and SQL requirements](scale-and-sql.md).
