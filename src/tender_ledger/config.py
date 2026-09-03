"""Database connection settings from the environment or the local ``.env`` file.

Precedence: explicit ``TL_DB_*`` environment variables, then the values the
Compose stack uses (``POSTGRES_*``) whether exported or written to ``.env``, then
conservative local defaults. The ``.env`` file is developer-local and never
committed; nothing here prints the password.
"""

import os
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def default_data_root() -> Path:
    """Where downloaded archives live. Ignored by Git, like the ``.env`` beside it."""
    return _REPO_ROOT / "data"


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    connect_timeout: int = 10

    def conninfo(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "connect_timeout": self.connect_timeout,
        }


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_config(dbname: str | None = None, *, env_file: Path | None = None) -> DbConfig:
    file_values = _read_env_file(env_file or _REPO_ROOT / ".env")

    def pick(*names: str, default: str = "") -> str:
        for name in names:
            if name in os.environ and os.environ[name] != "":
                return os.environ[name]
        for name in names:
            if name in file_values and file_values[name] != "":
                return file_values[name]
        return default

    resolved_db = (
        dbname
        or pick("TL_DB_NAME", "POSTGRES_DB", default="tender_ledger")
    )
    return DbConfig(
        host=pick("TL_DB_HOST", "POSTGRES_HOST", default="127.0.0.1"),
        port=int(pick("TL_DB_PORT", "POSTGRES_PORT", default="5433")),
        dbname=resolved_db,
        user=pick("TL_DB_USER", "POSTGRES_USER", default="postgres"),
        password=pick("TL_DB_PASSWORD", "POSTGRES_PASSWORD", default=""),
    )
