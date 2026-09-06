"""The local `.env` helper: it must add what a new service needs without ever
touching, printing or regenerating a secret that already exists."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import create_local_env as helper  # noqa: E402


def values(path: Path) -> dict[str, str]:
    settings = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            settings[key] = value
    return settings


class CreateLocalEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / ".env"

    def test_a_new_file_gets_every_setting_the_stack_needs(self):
        added = helper.ensure_local_env(self.path)

        settings = values(self.path)
        self.assertEqual(sorted(added), sorted(helper.REQUIRED_SETTINGS))
        self.assertEqual(sorted(settings), sorted(helper.REQUIRED_SETTINGS))
        self.assertEqual(settings["POSTGRES_PORT"], "5433")
        secrets_written = [
            settings[name] for name in helper.REQUIRED_SETTINGS
            if name.endswith(("PASSWORD", "SECRET", "KEY"))
        ]
        self.assertGreaterEqual(len(secrets_written), 4)
        self.assertEqual(len(set(secrets_written)), len(secrets_written))
        for secret in secrets_written:
            self.assertGreaterEqual(len(secret), 32)

    def test_an_existing_file_keeps_its_values_and_only_gains_what_is_missing(self):
        self.path.write_text(
            "# local\nPOSTGRES_PASSWORD=already-chosen\nPOSTGRES_PORT=5555\n",
            encoding="utf-8",
        )

        added = helper.ensure_local_env(self.path)

        settings = values(self.path)
        self.assertEqual(settings["POSTGRES_PASSWORD"], "already-chosen")
        self.assertEqual(settings["POSTGRES_PORT"], "5555")
        self.assertNotIn("POSTGRES_PASSWORD", added)
        self.assertIn("AIRFLOW__CORE__FERNET_KEY", added)
        self.assertEqual(sorted(settings), sorted(helper.REQUIRED_SETTINGS))

    def test_a_complete_file_is_left_alone(self):
        helper.ensure_local_env(self.path)
        before = self.path.read_text(encoding="utf-8")

        self.assertEqual(helper.ensure_local_env(self.path), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_reports_which_settings_it_added_without_printing_their_values(self):
        with redirect_stdout(io.StringIO()) as output:
            helper.main(self.path)

        printed = output.getvalue()
        self.assertIn("AIRFLOW__CORE__FERNET_KEY", printed)
        for name, value in values(self.path).items():
            if name != "POSTGRES_PORT":
                self.assertNotIn(value, printed)
