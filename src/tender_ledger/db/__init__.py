"""PostgreSQL connection and forward-only schema migrations."""

import re
from importlib import resources
from pathlib import Path

import psycopg

from ..config import DbConfig, load_config

_MIGRATION_NAME = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")


def connect(config: DbConfig | None = None, *, dbname: str | None = None,
            autocommit: bool = True) -> psycopg.Connection:
    """Open a connection. Autocommit by default: the loader and repository own
    their transaction boundaries with explicit ``with conn.transaction()`` blocks
    and never leave an implicit transaction open on the caller's connection."""
    config = config or load_config(dbname=dbname)
    return psycopg.connect(**config.conninfo(), autocommit=autocommit)


def _migrations() -> list[tuple[str, str]]:
    root = resources.files(__package__) / "migrations"
    files = sorted(
        p for p in root.iterdir()
        if p.name.endswith(".sql") and _MIGRATION_NAME.match(p.name)
    )
    return [(Path(p.name).stem, p.read_text(encoding="utf-8")) for p in files]


def _recorded_versions(conn: psycopg.Connection) -> set[str]:
    if conn.execute("select to_regclass('tl_work.schema_migrations')").fetchone()[0] is None:
        return set()
    return {r[0] for r in conn.execute("select version from tl_work.schema_migrations")}


def migrate(conn: psycopg.Connection, *, up_to: str | None = None) -> list[str]:
    """Apply every migration not yet recorded, each in its own transaction.

    ``up_to`` stops after that version (inclusive); used by tests that exercise
    the upgrade path from an earlier schema. Returns the versions applied by this
    call; empty when already current.
    """
    applied: list[str] = []
    known = _recorded_versions(conn)
    for version, sql in _migrations():
        if version not in known:
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "insert into tl_work.schema_migrations (version) values (%s)",
                    (version,),
                )
            applied.append(version)
        if up_to is not None and version == up_to:
            break
    return applied
