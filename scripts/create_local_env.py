"""Create local configuration without overwriting an existing file.

The file holds one developer's local credentials: the database password the
Compose stack initializes PostgreSQL with, and the secrets the local Airflow
stack needs. Values already in the file are never read back out, reprinted or
regenerated -- changing the PostgreSQL password after the volume exists would
not change the database, and rotating Airflow's Fernet key would make every
connection it already encrypted unreadable.
"""

import base64
import os
import secrets
from pathlib import Path


def _hex_secret() -> str:
    return secrets.token_hex(32)


def _fernet_key() -> str:
    """A 32-byte urlsafe-base64 key, the encoding Airflow's Fernet expects."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


#: Every setting the local stack reads, and how a missing one is filled in.
REQUIRED_SETTINGS: dict[str, "callable"] = {
    "POSTGRES_PASSWORD": _hex_secret,
    "POSTGRES_PORT": lambda: "5433",
    "AIRFLOW_METADATA_PASSWORD": _hex_secret,
    "AIRFLOW_ADMIN_PASSWORD": _hex_secret,
    "AIRFLOW__API_AUTH__JWT_SECRET": _hex_secret,
    "AIRFLOW__CORE__FERNET_KEY": _fernet_key,
}


def _existing_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        line.partition("=")[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def ensure_local_env(path: Path) -> list[str]:
    """Add the settings ``path`` does not have yet. Returns their names."""
    present = _existing_names(path)
    missing = [name for name in REQUIRED_SETTINGS if name not in present]
    if not missing:
        return []
    lines = "".join(f"{name}={REQUIRED_SETTINGS[name]()}\n" for name in missing)
    if present:
        with open(path, "a", encoding="utf-8", newline="\n") as config:
            config.write(lines)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as config:
            config.write(lines)
    return missing


def main(destination: Path | None = None) -> int:
    destination = destination or Path(__file__).resolve().parents[1] / ".env"
    added = ensure_local_env(destination)
    if added:
        print(f"Added to .env: {', '.join(added)}. Keep this file private.")
    else:
        print("Existing .env already has every setting; nothing changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
