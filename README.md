# Tender Ledger

A recoverable procurement data pipeline with historical ingestion and PostgreSQL analytics.

**Status:** the archive inspector and a transactional package loader into
PostgreSQL are implemented and tested (offline suite plus real-database
integration). API coverage verification, the historical run, benchmarks, and
orchestration are still pending.

## Problem

Procurement analysts need a queryable record of public notices, changes, and data coverage. Failed downloads and repeated batches must not silently produce missing records or double counting.

The historical target is TED notices across countries for 2020–2025, with Spain as one analytical case. Completion requires at least one million distinct real notices, reconciled coverage, measured SQL performance, and recovery evidence.

## Planned system

```text
TED daily/monthly XML packages
             |
     raw archives + manifests
             |
   validation + normalization
             |
      PostgreSQL batch loading
             |
 complete captures + SQL analysis
             |
    coverage and recovery reports

TED Search API -> count/identity checks
Airflow        -> schedule the verified workflow
```

Python implements the ingestion workflow, PostgreSQL stores relational data and transactional state, Docker provides the runtime, and Airflow will coordinate backfills. The first load uses XML packages; the API checks coverage. Cloud is an optional later extension.

## Run the inspector

Requires Python 3.13 or newer; tested with Python 3.14.3. The inspector uses only the standard library and needs no installation or network access. The database loader adds one dependency (`psycopg`).

From the repository root in PowerShell:

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m tender_ledger inspect path/to/daily-package.tar.gz
python -m unittest discover -s tests -v
```

On Linux/macOS:

```sh
PYTHONPATH=src python -m tender_ledger inspect path/to/daily-package.tar.gz
PYTHONPATH=src python -m unittest discover -s tests -v
```

The inspector prints a JSON summary with checksums, distinct notice counts, formats, and schema versions. Invalid archives exit with a nonzero status. It checks gzip integrity, duplicate identities, supported roots, required identity fields, and configured resource limits without extracting XML files to disk.

Defaults are 64 MiB compressed, 512 MiB expanded, 8 MiB per member, and 10,000 notices. Limits are explicit CLI options; see `inspect --help`. The identity set uses memory proportional to the notice limit. This bounded inspector is not the historical database loader or a full XML-schema validator.

A successful inspection does not establish source completeness: the output always marks `source_coverage_verified` false. Empty archives remain unverified until the ingestion workflow corroborates their coverage.

## Load a package into PostgreSQL

Requires the local database (see below) and `psycopg`:

```sh
python -m pip install -e .
python -m tender_ledger db upgrade
python -m tender_ledger load path/to/daily-package.tar.gz --package-id daily/202300220
python -m tender_ledger status --package-id daily/202300220
```

The loader persists a capture identity before loading, streams and projects
members (see [projection contract](docs/projection.md)), writes deterministic
`COPY` batches whose rows and status commit together, reconciles counts, and
publishes the capture with a single transactional pointer swap. Retries resume,
re-acquisitions are new captures, and readers keep the previous complete capture
until a replacement publishes. Full semantics and guarantees:
[transactional loading](docs/loading.md). Example analytical and diagnostic SQL
is under [`queries/`](queries/).

## Verified evidence

- Six annual API counts sum to **4,523,626 reported results** for 2020–2025. These are not loaded database rows.
- HEAD responses for 72 monthly packages total **16,783,140,714 bytes (15.63 GiB)** compressed; the historical packages have not been downloaded.
- A real mixed daily package contained **2,967 distinct notices**: 1,813 legacy and 1,154 eForms. Its complete identifier set matched the API across 12 pages.
- The inspector processed that package successfully; its checks are covered by automated tests.
- That same real package (2,967 notices, 1,813 legacy + 1,154 eForms) was loaded into PostgreSQL as an M1 smoke: members, distinct keys, and loaded rows all reconciled at 2,967, a replay was a no-op, and every view reported `source_coverage_verified = false`.
- The full test suite is **49 tests**: the offline archive/projection tests plus real-database integration tests for replay, resume, publish visibility, corruption, A/B/A, retired members, concurrency, and recovery equivalence.

Inspector checks were performed on 2026-09-02; the loader smoke on 2026-09-03. No cloud deployment or historical performance benchmark has run.

See [design and source contract](docs/design.md), [projection contract](docs/projection.md), [transactional loading](docs/loading.md), and [scale and SQL requirements](docs/scale-and-sql.md).

## Local database setup

[Local development](docs/local-development.md) describes the prepared PostgreSQL
Compose configuration, private password generation, and data persistence. The
PostgreSQL 17.11 container passed connectivity, transaction rollback, and restart
persistence checks on 2026-09-03, and now runs the loader's schema and integration
tests (against a dedicated `tender_ledger_test` database).

## Sources

[TED XML packages](https://docs.ted.europa.eu/ODS/latest/reuse/download-xml.html), [direct download conventions](https://docs.ted.europa.eu/ODS/latest/reuse/download-direct.html), and [Search API](https://docs.ted.europa.eu/api/latest/search.html).
