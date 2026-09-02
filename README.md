# Tender Ledger

A recoverable procurement data pipeline with historical ingestion and PostgreSQL analytics.

**Status:** source validation and design. The pipeline is not implemented yet; the scale requirements below are targets, not benchmark results.

## Problem

Procurement analysts need a queryable record of public notices, observed changes, and data coverage. A failed download, repeated batch, or correction must not silently produce missing records or double counting.

Tender Ledger will ingest TED notices across countries, using Spain as one analytical case. It will retain source provenance, reconcile incomplete work, and measure ingestion and SQL performance on real historical data.

## Planned system

```text
TED historical XML packages     TED Search API
             |                       |
             +---- raw artifacts ----+
                         |
              validation + normalization
                         |
               PostgreSQL staging/load
                         |
        notices + observations + ingestion state
                         |
             SQL analysis + coverage reports
```

Python provides the CLI and ingestion logic. PostgreSQL provides relational storage, transactional state, and SQL analysis. Docker provides the local runtime. Airflow will orchestrate the verified ingestion workflow and backfills.

The first milestone uses small offline fixtures to prove correctness. Completion requires at least **one million distinct real notices**, a measured SQL workload, and recovery evidence. The historical target is the **2020–2025 TED archive**. Dataset definitions, resource limits, and acceptance criteria are in [Scale and SQL requirements](docs/scale-and-sql.md).

## Current evidence

On 2026-09-02, an unauthenticated API probe returned two pages of two notices using an iteration token, without overlapping publication numbers. A separate one-record query for 2020–2025 reported **4,523,626 results**. This is a provider-reported count, not a downloaded or independently deduplicated dataset.

No historical packages have been downloaded, and no pipeline tests or benchmarks have run. [Design and source observations](docs/design.md) records what is verified and what remains open.

## Scope

The project will demonstrate idempotent ingestion, bounded resource use, bulk loading, query optimization, late-change reconciliation, and observable failures on a single machine. Cloud deployment is a separate optional extension. Spark, streaming, a procurement portal, and automated procurement decisions are outside this project's scope.

## Sources

[TED Search API](https://docs.ted.europa.eu/api/latest/search.html), [pagination guidance](https://docs.ted.europa.eu/ODS/latest/reuse/search-api.html), and [historical XML downloads](https://docs.ted.europa.eu/ODS/latest/reuse/download-xml.html).
