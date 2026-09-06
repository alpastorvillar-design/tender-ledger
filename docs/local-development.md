# Local PostgreSQL environment

Status: PostgreSQL runtime verified on 2026-09-03 with Docker Desktop and WSL 2.
The transactional loader (migrations `0001`-`0005`, capture/batch/publish), the
coverage verifier and the ingest flow run against PostgreSQL. The development
database holds only the accepted daily sample; the million-notice run used a
separate local database.

## Prerequisites

- Python 3.13 or newer. The inspector (`tender_ledger inspect`) needs only the
  standard library. The loader and its tests need `psycopg`.
- Docker Engine with Compose v2 or newer, running Linux containers.
- On Windows, use Docker Desktop with the WSL 2 backend. Follow the official
  [Windows installation guide](https://docs.docker.com/desktop/setup/install/windows-install/).
  Enabling WSL can require administrator access and a Windows restart; finish
  that setup before starting the database. A standalone Compose validator does
  not provide the Docker Engine.

The initial image is `postgres:17.11-bookworm`, an explicit patch version from the
[official image inventory](https://github.com/docker-library/official-images/blob/master/library/postgres).
Compose pins its registry manifest digest as well. The image was pulled and run
successfully on linux/amd64; PostgreSQL reported version 17.11.
The container has a 2 GiB memory limit, two CPUs, and 256 MiB shared memory. These
are initial development settings, not a historical-load benchmark configuration.

## Start and connect

Run from the repository root:

```sh
python scripts/create_local_env.py
docker compose config --quiet
docker compose up -d --wait postgres
docker compose exec -T postgres psql -U postgres -d tender_ledger -v ON_ERROR_STOP=1 -c "SELECT version(), current_database();"
```

The helper writes the settings the local stack needs into the ignored `.env`
file, adding only the ones that are not there yet and never rewriting an
existing value. Do not commit or print that file. Avoid sharing the expanded
output of `docker compose config`, which includes environment values; use
`--quiet`. The Airflow settings it also creates are only used by the optional
[orchestration stack](orchestration.md); `compose.yaml` on its own needs
`POSTGRES_PASSWORD` and `POSTGRES_PORT`.

The database listens on `127.0.0.1:5433` on the host. Change `POSTGRES_PORT` in
`.env` if that port is already occupied. The `postgres` account is the local
bootstrap and loader administrator. Migrations create the restricted
`tender_ledger_reader` role for the consumption views; the integration tests
check its access. A separate least-privilege writer remains future work.
Do not use these development credentials for a hosted service.

Downloaded archives live in `data/` at the repository root, which is ignored by
Git; `--data-dir` moves that root. PostgreSQL data lives in the Compose-managed
`postgres_data` named volume, not inside the Git working tree. Password initialization applies only to an empty
volume: editing `.env` later does not change the password of an existing database.

## Operate without discarding data

```sh
docker compose ps
docker compose logs --tail 100 postgres
docker compose stop postgres
docker compose start postgres
```

`docker compose down` removes the container and network while retaining the named
volume. Adding `--volumes` deletes the database, so it is not part of routine
cleanup. The service does not restart automatically after a host reboot.

The optional Airflow stack is a separate overlay file and never changes these
commands; see [local orchestration](orchestration.md).

## Runtime verification

Verified on 2026-09-03 using Docker Engine 29.7.2, Docker Desktop 4.89.0, and
Compose 5.5.0:

- PostgreSQL 17.11 responded in the `tender_ledger` database and became healthy.
- The host could connect to `127.0.0.1:5433`; password authentication over TCP
  inside the container succeeded.
- A committed row survived a container restart; a rolled-back row remained absent.
- The disposable probe table was removed and the database remained healthy.

These are environment checks, not ingestion or historical-scale results.
To repeat the environment verification:

Confirm that the health check passes and the SQL command above returns the
expected PostgreSQL version and database. Then run a disposable-table transaction
and persistence check: insert and commit a row, roll back a second insert, restart
the container, and verify that only the committed row remains. Remove only that
test table afterwards. Record the commands, results, image digest, and runtime
versions before calling this environment verified.

## Reproducible environment

```sh
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]" -c constraints.txt
```

`constraints.txt` pins the exact versions this project was validated with
(`psycopg==3.3.5` and its binary wheel, plus the dev tools). `pyproject.toml`
keeps only floors. No tools are installed globally.

## Loader schema and tests

With the container healthy and the environment installed:

```sh
python -m tender_ledger db upgrade          # applies db/migrations/*.sql to tender_ledger
python scripts/run_tests.py                # full suite; any skip is a failure
```

`tests/test_packages.py`, `tests/test_projection.py`, `tests/test_source_api.py`
and `tests/test_download.py` are standard-library only; the last two drive the
HTTP clients against a local test server and never contact TED.
`tests/test_db.py`, `tests/test_verification.py`, `tests/test_ingest.py`,
`tests/test_cli.py`, and `tests/test_queries.py` import `psycopg` and need the
running database; they create and drop a dedicated
`tender_ledger_test` database (never `tender_ledger`, never its volume) and skip
with a clear message when the server is unreachable. `db upgrade`, `load`, and
`status` read connection settings from `TL_DB_*` / `POSTGRES_*` or `.env`.

The strict runner treats those skips as a failed gate. Plain
`python -m unittest discover -s tests -v` remains available for partial local
checks, but an `OK (skipped=...)` result does not establish database correctness.
`TL_TEST_DB` can select another dedicated name ending in `_test`; the suite
replaces that database and its `_upgrade_test` variant. Never point tests at a
shared or production database.
