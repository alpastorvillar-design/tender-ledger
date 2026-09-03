# Scale and SQL requirements

Status: acceptance plan for the historical workload. Archive inspection and
transactional PostgreSQL loading are implemented, with a verified daily load of
2,967 real notices. The 100,000-notice rehearsal, minimum million-notice gate,
historical coverage, and SQL benchmarks remain pending.

## Dataset and completion gates

The historical target is TED notices published during 2020–2025 across countries. A one-record API probe on 2026-09-02 reported 4,523,626 matching results. Actual available packages, canonical identities, supported formats, and reconciliation determine the final loaded count. Spain is an analytical subset, not the size limit of the archive.

An independent header inventory on the same date found all 72 monthly packages, advertising 16,783,140,714 compressed bytes (15.63 GiB). The 2020–2021 subset advertises 3,891,273,136 bytes and its API counts sum to 1,320,286 notices, making it a candidate for the minimum-scale delivery. These are source/header measurements, not database storage measurements or loaded counts. Validate modern and mixed-format periods as well as that legacy subset.

| Stage | Required evidence |
| --- | --- |
| Correctness | Small deterministic fixtures containing duplicates, changes, invalid input, and incomplete work |
| Engineering rehearsal | Complete monthly artifacts until at least 100,000 distinct real notices, plus legacy/eForms samples, resource measurements, and a restart test |
| Minimum scale gate | At least 1,000,000 distinct real notices, reconciled to source artifacts, with the SQL workload and recovery evidence below |
| Historical target | Process the available 2020–2025 archive; account for missing periods, unsupported formats, and differences from the API count |

The minimum gate alone does not establish complete historical coverage. Completing the project requires either the historical target or a documented scope decision explaining the measured constraint and exact coverage achieved. An unmet minimum gate remains unfinished work.

Count notices, observations, lots, organizations, and raw records separately. Repeated ingestion, multiple languages, expanded joins, or synthetic duplication must not inflate the distinct-notice claim. Synthetic fixtures are useful for correctness and adversarial cases; they do not count toward real-data scale.

## Historical loading and resources

Use TED XML packages as the first loading path, including daily updates; use bounded Search API queries for reconciliation. Stream archive members and XML records rather than loading the historical dataset into memory. Verify checksums, archive completion, member counts, parser failures, and raw-to-destination reconciliation. Record counts and compare identifier sets where feasible: equal totals alone can conceal a missing key and an extra one.

Load bounded batches through PostgreSQL staging and set-based statements. Define transaction boundaries, deterministic batch identity, duplicate handling, and completion visibility before implementing multi-batch loading. A corrupt archive, incompatible format, exhausted limit, or missing batch must not become a successful window.

Before a large run, measure a representative sample from legacy XML and eForms periods. Record compressed bytes, parsed bytes, database/index sizes, temporary space, and WAL growth; estimate the intended run with headroom. Record the hardware, runtime versions, database settings, and available resources in each benchmark report.

Initial planning limits are a 150 GiB total project-data budget and at least 100 GiB free on the host volume. These are conservative operating limits, not measured storage requirements. Include container storage, database files, indexes, raw archives, temporary files, and WAL. Recheck host and container capacity before each run; stop safely before a limit is exhausted. If the estimate exceeds the budget, revise retention or the run plan before downloading the archive. Do not silently remove recovery artifacts or reduce coverage.

Measure parser peak RSS at a fixed batch size across increasing input sizes. The XML stream holds one member at a time and rows are written in fixed-size batches, but the reconciliation sets are not O(batch): the inspector keeps one canonical key per distinct notice in the archive, and the loader keeps one per row it writes, so both grow with the package. That is deliberate — the loader's set is what proves a stream reassembled after a restart produces the same partition, where the primary key cannot fire for batches it skips — and it is the term to measure. Database memory and temporary disk use are measured separately. Start with limited concurrency and increase it only when source behavior and local measurements justify doing so.

Large runs are explicit local operations, separate from routine CI. Raw datasets, database volumes, and large benchmark outputs stay out of Git. Commit reproducible commands, small permitted fixtures, source manifests without sensitive fields, and compact measurements. Confirm reuse and attribution terms before publishing examples.

## SQL workload

The data model must support a documented notice grain, observed changes, source provenance, and complete-window status. Implement at least six useful queries, each with a correctness fixture and an explanation of its grain:

1. Monthly notice counts by country and primary CPV, with explicit treatment of missing values and multi-valued classifications.
2. Latest completed capture per publication, with deterministic capture ordering. Grouping by procedure answers a different question.
3. Supported official change references and observed content differences, with explicit format coverage and unresolved targets; use window functions where their ordering has a valid meaning.
4. The dataset as captured by this system at an acquisition-time cutoff, without presenting backfill ingestion dates as official historical versions.
5. Coverage and ingestion freshness over a calendar axis with LEFT JOIN, distinguishing empty source periods, missing captures, and unavailable verification.
6. Duplicate source records and reconciliation discrepancies using NOT EXISTS or EXCEPT over publication keys.

Extend buyer-level analysis only after verifying organization identifiers and join cardinality. Do not infer awards, expenditure, or supplier outcomes from notices that do not contain those facts.

Work covers joins and null semantics, CTEs, window functions, constraints, bulk loading, upserts, transaction isolation, and lock behavior. For performance, compare a correct baseline with justified changes using query plans, statistics, indexes, and, where useful, partitioning. Partitioning is a hypothesis to measure; its interaction with uniqueness and pruning must be explained.

The initial PostgreSQL baseline is unpartitioned, with publication year included in the natural key. Evaluate annual partitioning against that baseline and plan any migration before the full historical load. Plans, estimated/actual rows, buffers, throughput, and latency provide complementary evidence.

References: PostgreSQL [COPY](https://www.postgresql.org/docs/current/sql-copy.html), [EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html), and [partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html). Pin implementation documentation to the chosen supported PostgreSQL version.

## Measurement and recovery

Before the main benchmark, freeze the workload, dataset identity, repeat count, runtime configuration, and target budgets in a benchmark protocol. Set latency and throughput targets after the 100,000-notice rehearsal; preserve baseline results rather than inventing performance targets without hardware evidence.

For each measured dataset size, record:

- Source selection, artifact checksums, distinct counts, rejected records, and coverage gaps.
- Extraction, parsing, loading, and total duration separately; notices per second and bytes processed.
- Peak parser RSS, database/index size, temporary space, WAL growth, and available disk.
- SQL results and representative `EXPLAIN (ANALYZE, BUFFERS)` plans before and after optimization.
- At least 20 repeated timings per query for a labelled warm-cache comparison, with raw timings, median, and p95. Describe preparation and exclude warmup consistently; do not label a run cold unless cache state is controlled.
- Equality of query results before and after optimization on fixtures and the measured real dataset.
- Recovery after interrupting a real load between batches: completed work reused, incomplete work visible, no false checkpoint, and final counts/content matching a clean execution over the same selected artifacts.

Record regressions and optimizations that do not help. A single-machine result demonstrates measured capacity on that machine; it does not establish distributed execution, production operations, or performance on other hardware.
