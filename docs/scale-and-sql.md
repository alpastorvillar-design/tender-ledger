# Scale and SQL requirements

Status: acceptance plan. No load or benchmark results are available yet.

## Dataset and completion gates

The historical target is TED notices published during 2020–2025 across countries. A one-record API probe on 2026-09-02 reported 4,523,626 matching results. Actual available packages, canonical identities, supported formats, and reconciliation determine the final loaded count. Spain is an analytical subset, not the size limit of the archive.

| Stage | Required evidence |
| --- | --- |
| Correctness | Small deterministic fixtures containing duplicates, changes, invalid input, and incomplete work |
| Engineering rehearsal | At least 100,000 distinct real notices, with resource measurements and a restart test |
| Minimum scale gate | At least 1,000,000 distinct real notices, reconciled to source artifacts, with the SQL workload and recovery evidence below |
| Historical target | Process the available 2020–2025 archive; account for missing periods, unsupported formats, and differences from the API count |

The minimum gate alone does not establish complete historical coverage. Completing the project requires either the historical target or a documented scope decision explaining the measured constraint and exact coverage achieved. An unmet minimum gate remains unfinished work.

Count notices, observations, lots, organizations, and raw records separately. Repeated ingestion, multiple languages, expanded joins, or synthetic duplication must not inflate the distinct-notice claim. Synthetic fixtures are useful for correctness and adversarial cases; they do not count toward real-data scale.

## Historical loading and resources

Use TED XML packages for historical bootstrap after validating the download contract; use bounded Search API windows for incremental ingestion and reconciliation. Stream archive members and XML records rather than loading the historical dataset into memory. Verify checksums, archive completion, member counts, parser failures, and raw-to-destination reconciliation.

Load bounded batches through PostgreSQL staging and set-based statements. Define transaction boundaries, deterministic batch identity, duplicate handling, and completion visibility before implementing multi-batch loading. A corrupt archive, incompatible format, exhausted limit, or missing batch must not become a successful window.

Before a large run, measure a representative sample from legacy XML and eForms periods. Record compressed bytes, parsed bytes, database/index sizes, temporary space, and WAL growth; estimate the intended run with headroom. Record the hardware, runtime versions, database settings, and available resources in each benchmark report.

Initial planning limits are a 150 GiB total project-data budget and at least 100 GiB free on the host volume. These are conservative operating limits, not measured storage requirements. Include container storage, database files, indexes, raw archives, temporary files, and WAL. Recheck host and container capacity before each run; stop safely before a limit is exhausted. If the estimate exceeds the budget, revise retention or the run plan before downloading the archive. Do not silently remove recovery artifacts or reduce coverage.

Measure parser peak RSS at a fixed batch size across increasing input sizes. Memory use must remain bounded by the batch configuration rather than grow with the whole dataset. Database memory and temporary disk use are measured separately. Start with limited concurrency and increase it only when source behavior and local measurements justify doing so.

Large runs are explicit local operations, separate from routine CI. Raw datasets, database volumes, and large benchmark outputs stay out of Git. Commit reproducible commands, small permitted fixtures, source manifests without sensitive fields, and compact measurements. Confirm reuse and attribution terms before publishing examples.

## SQL workload

The data model must support a documented notice grain, observed changes, source provenance, and complete-window status. Implement at least six useful queries, each with a correctness fixture and an explanation of its grain:

1. Monthly notice counts by country and primary CPV, with explicit treatment of missing values and multi-valued classifications.
2. Latest known business observation per notice, with deterministic ordering and ties.
3. Observed field changes using window functions; separate observation time from publication time.
4. The dataset as known at an observation-time cutoff, without claiming unavailable official historical versions.
5. Coverage and ingestion freshness by period, distinguishing an empty source period from an incomplete extraction.
6. Late changes, duplicate source records, and reconciliation discrepancies across ingestion windows.

Extend buyer-level analysis only after verifying organization identifiers and join cardinality. Do not infer awards, expenditure, or supplier outcomes from notices that do not contain those facts.

Work covers joins and null semantics, CTEs, window functions, constraints, bulk loading, upserts, transaction isolation, and lock behavior. For performance, compare a correct baseline with justified changes using query plans, statistics, indexes, and, where useful, partitioning. Partitioning is a hypothesis to measure; its interaction with uniqueness and pruning must be explained.

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
