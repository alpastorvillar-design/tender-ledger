# Procurement Notice Pipeline

A reliable ingestion pipeline for public procurement notices from TED.

**Status:** repository setup and live source validation. The pipeline is not implemented yet.

## Problem

An analyst needs a queryable record of published notices and observed changes, with enough provenance to identify incomplete batches and safely repeat failed work.

The project focuses on ingestion correctness: pagination, immutable source artifacts, validation, transactional loading, and recovery. It does not submit tenders or make procurement decisions.

## Planned MVP

```text
TED Search API / offline fixtures
              |
      raw pages + manifest
              |
     validation and normalization
              |
 PostgreSQL: notices, observations, run state
```

- A Python CLI ingests one bounded window.
- Offline fixtures make the default demonstration independent of network access.
- Repeating the same input has no duplicate effect in the destination.
- Data and checkpoint changes commit together.
- Invalid or incomplete input produces an explicit failure.
- Tests exercise plausible API, data, and transaction failures.

Docker will provide a reproducible local runtime. Airflow comes after the CLI and loading semantics are verified. Cloud deployment, Spark, streaming, and LLM features are outside the MVP.

## Current evidence

A bounded unauthenticated search returned two pages of two notices using an iteration token, with no duplicate publication numbers between these two pages. Source observations and open design questions are recorded in [the design note](docs/design.md).

There are no runnable pipeline commands or passing application tests to report yet.

## Source

[TED Search API documentation](https://docs.ted.europa.eu/api/latest/search.html) and [pagination guidance](https://docs.ted.europa.eu/ODS/latest/reuse/search-api.html).
