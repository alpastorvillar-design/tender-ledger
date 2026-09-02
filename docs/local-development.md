# Local PostgreSQL environment

Status: configuration prepared. A PostgreSQL container has not been started or
tested yet. The current Windows development host needs WSL and Docker Desktop.
Database loading and migrations remain the next implementation task.

## Prerequisites

- Python 3.13 or newer for the current inspector and configuration helper.
- Docker Engine with Compose v2 or newer, running Linux containers.
- On Windows, use Docker Desktop with the WSL 2 backend. Follow the official
  [Windows installation guide](https://docs.docker.com/desktop/setup/install/windows-install/).
  Enabling WSL can require administrator access and a Windows restart; finish
  that setup before starting the database. A standalone Compose validator does
  not provide the Docker Engine.

The initial image is `postgres:17.11-bookworm`, an explicit patch version from the
[official image inventory](https://github.com/docker-library/official-images/blob/master/library/postgres).
Compose pins its registry manifest digest as well. The tag and manifest were
verified against the public registry; the image has not been pulled or run yet.
Record the actual platform image identity when validating the runtime.
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

The helper creates a random password in the ignored `.env` file and preserves an
existing file. Do not commit or print that file. Avoid sharing the expanded output
of `docker compose config`, which includes environment values; use `--quiet`.

The database listens on `127.0.0.1:5433` on the host. Change `POSTGRES_PORT` in
`.env` if that port is already occupied. The `postgres` account is the local
bootstrap administrator; application and reader roles belong to the upcoming
database implementation. Do not use these development credentials for a hosted
service.

PostgreSQL data lives in the Compose-managed `postgres_data` named volume, not
inside the Git working tree. Password initialization applies only to an empty
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

## Runtime verification still required

Confirm that the health check passes and the SQL command above returns the
expected PostgreSQL version and database. Then run a disposable-table transaction
and persistence check: insert and commit a row, roll back a second insert, restart
the container, and verify that only the committed row remains. Remove only that
test table afterwards. Record the commands, results, image digest, and runtime
versions before calling this environment verified.

Migrations, ingestion tests, reader permissions, and recovery tests will be added
with the transactional loader. A healthy empty database alone does not validate
the data pipeline.
