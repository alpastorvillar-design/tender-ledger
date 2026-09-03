"""Run the full suite; missing database coverage must fail the CI gate."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("Full-suite validation requires PostgreSQL; skipped tests fail this gate.",
              file=sys.stderr)
    return int(not result.wasSuccessful() or bool(result.skipped) or result.testsRun == 0)


if __name__ == "__main__":
    raise SystemExit(main())
