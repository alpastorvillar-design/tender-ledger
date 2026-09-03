"""Create local PostgreSQL configuration without overwriting an existing file."""

import os
import secrets
from pathlib import Path


def main() -> int:
    destination = Path(__file__).resolve().parents[1] / ".env"
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as config:
            config.write(f"POSTGRES_PASSWORD={secrets.token_hex(32)}\nPOSTGRES_PORT=5433\n")
    except FileExistsError:
        print("Existing .env preserved.")
    else:
        print("Created .env with a generated local password. Keep this file private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
