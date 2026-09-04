"""The compatibility survey: an inventory that reads, counts, and loads nothing.

A load is all-or-nothing, so the first unsupported member condemns a whole
package. Finding that out after downloading and parsing a monthly archive is the
expensive way. The survey walks the same archive through the same defenses and
reports what is in it instead of stopping at the first thing it cannot use.
"""

import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from ted_fixtures import (
    TEST_DB,
    eforms_member,
    ensure_test_database,
    legacy_member,
    padded_legacy_member,
    truncate_all,
    write_package,
)
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.loader import load_package
from tender_ledger.package_contract import DAILY_POLICY
from tender_ledger.packages import (
    Limits,
    PackageError,
    inspect_package,
    limits_for,
    survey_package,
)

UNSUPPORTED_ROOT = (
    b'<TED_EXPORT xmlns="http://publications.europa.eu/resource/schema/ted/R2.0.7/publication"'
    b' DOC_ID="000042-2020" VERSION="R2.0.7.S01.E01">'
    b"<SECRET>never surfaced</SECRET></TED_EXPORT>"
)


class SurveyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def archive(self, members, name="package.tar.gz"):
        return write_package(self.dir / name, members)

    def survey(self, members, *, name="package.tar.gz", **kwargs):
        return survey_package(self.archive(members, name), **kwargs)

    def run_cli(self, *args):
        env = {**os.environ, "TL_DB_NAME": TEST_DB, "PYTHONPATH": "src", "PYTHONUTF8": "1"}
        return subprocess.run(
            [sys.executable, "-m", "tender_ledger", *args],
            capture_output=True, text=True, env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )


class CompatibleSurveyTests(SurveyTestCase):
    def test_a_mixed_compatible_archive_is_inventoried_and_reported_loadable(self):
        result = self.survey([
            legacy_member(1),
            legacy_member(2, namespace="R2.0.8", version_attr='VERSION="R2.0.8.S04.E01"'),
            eforms_member(3, customization="eforms-sdk-1.7"),
        ])
        self.assertTrue(result["compatible_for_load"])
        self.assertEqual(result["member_count"], 3)
        self.assertEqual(result["xml_member_count"], 3)
        self.assertEqual(result["notice_count"], 3)
        self.assertEqual(result["formats"], {"eforms": 1, "legacy": 2})
        self.assertEqual(
            result["schema_versions"],
            {"R2.0.8.S04.E01": 1, "R2.0.9.S05.E01": 1, "eforms-sdk-1.7": 1},
        )
        # R2.0.8 and R2.0.9 are different namespaces, so three distinct roots.
        self.assertEqual(sorted(result["roots"].values()), [1, 1, 1])
        self.assertEqual(result["incompatible_member_count"], 0)
        self.assertEqual(result["incompatible_reasons"], {})
        self.assertEqual(result["duplicate_identity_count"], 0)

    def test_the_survey_agrees_with_the_inspector_on_size_and_checksum(self):
        members = [legacy_member(1), eforms_member(2)]
        path = self.archive(members)
        surveyed, inspected = survey_package(path), inspect_package(path)
        for field in ("sha256", "compressed_bytes", "expanded_bytes",
                      "xml_member_bytes", "notice_count", "formats", "schema_versions"):
            with self.subTest(field=field):
                self.assertEqual(surveyed[field], inspected[field])

    def test_an_identity_names_the_policy_the_survey_runs_under(self):
        members = [padded_legacy_member(1, member_bytes=DAILY_POLICY.member_bytes + 1024)]
        path = self.archive(members)
        with self.assertRaisesRegex(PackageError, "Member exceeds"):
            survey_package(path, limits_for("daily/202300220"))
        result = survey_package(
            path, limits_for("monthly/2020-01"), source_package_id="monthly/2020-01"
        )
        self.assertTrue(result["compatible_for_load"])
        self.assertEqual(result["source_package_id"], "monthly/2020-01")

    def test_a_reported_source_identity_must_be_canonical(self):
        path = self.archive([legacy_member(1)])
        with self.assertRaisesRegex(ValueError, "canonical package identity"):
            survey_package(path, source_package_id="daily/scratch")

    def test_the_schema_version_inventory_has_a_bounded_number_of_entries(self):
        members = [
            legacy_member(
                number,
                version_attr=f'VERSION="R2.0.9.S{number:03d}.E01"',
            )
            for number in range(1, 26)
        ]
        result = self.survey(members)
        self.assertTrue(result["compatible_for_load"])
        self.assertEqual(result["schema_version_kinds"], 25)
        self.assertEqual(len(result["schema_versions"]), 20)

    def test_an_overlong_schema_version_is_an_incompatibility_not_output(self):
        secret_suffix = "private-value-" * 30
        result = self.survey([
            legacy_member(
                1,
                version_attr=f'VERSION="R2.0.9.{secret_suffix}"',
            )
        ])
        self.assertFalse(result["compatible_for_load"])
        self.assertEqual(result["incompatible_reasons"], {"unsupported_version": 1})
        self.assertNotIn(secret_suffix, json.dumps(result))


class IncompatibleSurveyTests(SurveyTestCase):
    def test_an_unsupported_root_is_counted_and_the_inventory_finishes(self):
        result = self.survey([
            legacy_member(1),
            ("2020-220/000042_2020.xml", UNSUPPORTED_ROOT),
            legacy_member(3),
        ])
        self.assertFalse(result["compatible_for_load"])
        self.assertEqual(result["member_count"], 3)
        self.assertEqual(result["notice_count"], 2)  # the two that could be loaded
        self.assertEqual(result["incompatible_member_count"], 1)
        self.assertEqual(result["incompatible_reasons"], {"unsupported_root": 1})
        self.assertEqual(
            result["unsupported_roots"],
            {"{http://publications.europa.eu/resource/schema/ted/R2.0.7/publication}"
             "TED_EXPORT": 1},
        )
        self.assertEqual(result["unsupported_root_kinds"], 1)
        self.assertEqual(
            result["incompatible_sample"],
            [{"member": "000042_2020.xml", "reason": "unsupported_root"}],
        )

    def test_the_survey_never_reports_xml_content(self):
        result = self.survey([("2020-220/000042_2020.xml", UNSUPPORTED_ROOT)])
        payload = json.dumps(result)
        self.assertNotIn("never surfaced", payload)
        self.assertNotIn("SECRET", payload)
        self.assertNotIn(str(self.dir), payload)
        self.assertNotIn("2020-220", payload)  # member paths are reduced to a name

    def test_each_member_level_rejection_gets_its_own_reason(self):
        legacy_name, legacy_xml = legacy_member(1)
        cases = {
            "unsupported_member_name": ("notes.txt", b"not xml at all"),
            "invalid_identity": ("0-2020.xml", legacy_xml),
            "malformed_xml": ("000002_2023.xml", b"<broken"),
            "unsupported_declaration": (
                "000003_2023.xml", b'<!DOCTYPE x [<!ENTITY y "z">]>' + legacy_xml
            ),
            "unsupported_encoding": ("000004_2023.xml", legacy_xml.decode().encode("utf-16")),
            "identity_mismatch": ("000005_2023.xml", legacy_xml),
            "unsupported_customization": (
                "000006_2023.xml", eforms_member(6, customization="other")[1]
            ),
        }
        for reason, member in cases.items():
            with self.subTest(reason=reason):
                result = self.survey([member], name=f"{reason}.tar.gz")
                self.assertFalse(result["compatible_for_load"])
                self.assertEqual(result["incompatible_reasons"], {reason: 1})
                self.assertEqual(result["incompatible_member_count"], 1)

    def test_repeated_identities_are_counted_rather_than_stopping_the_walk(self):
        result = self.survey([
            legacy_member(1),
            ("2023-220/00000001_2023.xml", legacy_member(1)[1]),
            legacy_member(2),
        ])
        self.assertFalse(result["compatible_for_load"])
        self.assertEqual(result["duplicate_identity_count"], 1)
        self.assertEqual(result["duplicate_identity_sample"], ["1-2023"])
        self.assertEqual(result["notice_count"], 2)
        self.assertEqual(result["member_count"], 3)

    def test_repeated_identities_remain_part_of_the_schema_inventory(self):
        duplicate_name = "2023-220/00000001_2023.xml"
        result = self.survey([
            legacy_member(1),
            (duplicate_name, eforms_member(1, customization="eforms-sdk-1.7")[1]),
            legacy_member(2),
        ])
        self.assertFalse(result["compatible_for_load"])
        self.assertEqual(result["duplicate_identity_count"], 1)
        self.assertEqual(result["formats"], {"eforms": 1, "legacy": 2})
        self.assertEqual(result["schema_version_kinds"], 2)

    def test_a_non_xml_member_is_a_rejection_and_not_a_repeated_identity(self):
        # The two counters are computed from different populations; a member that
        # is not XML belongs to neither the loadable identities nor the XML ones.
        result = self.survey([legacy_member(1), ("readme.txt", b"notes"), legacy_member(2)])
        self.assertEqual(result["member_count"], 3)
        self.assertEqual(result["xml_member_count"], 2)
        self.assertEqual(result["notice_count"], 2)
        self.assertEqual(result["incompatible_reasons"], {"unsupported_member_name": 1})
        self.assertEqual(result["duplicate_identity_count"], 0)
        self.assertEqual(result["duplicate_identity_sample"], [])
        self.assertFalse(result["compatible_for_load"])

    def test_samples_are_bounded_while_the_counts_are_not(self):
        malformed = [(f"{n:06d}_2023.xml", b"<broken") for n in range(1, 31)]
        # The canonical identity comes from the member's own name, so the same
        # notice filed under 25 directories is one identity repeated 24 times.
        name, xml = legacy_member(1)
        repeated = [(f"part-{n}/{name.rsplit('/', 1)[-1]}", xml) for n in range(25)]
        result = self.survey(malformed + repeated)

        self.assertEqual(result["incompatible_member_count"], 30)
        self.assertEqual(len(result["incompatible_sample"]), 20)
        self.assertEqual(result["duplicate_identity_count"], 24)
        self.assertEqual(len(result["duplicate_identity_sample"]), 20)
        self.assertEqual(set(result["duplicate_identity_sample"]), {"1-2023"})
        self.assertEqual(result["notice_count"], 1)
        # A stable prefix in archive order, not an arbitrary selection.
        self.assertEqual(result["incompatible_sample"][0]["member"], "000001_2023.xml")
        self.assertEqual(result["incompatible_sample"][-1]["member"], "000020_2023.xml")


class ArchiveRiskTests(SurveyTestCase):
    """An archive-level fault stops the survey. It is not an inventory finding."""

    def raw(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, content in members:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    def test_unsafe_paths_stop_both_the_inspector_and_the_survey(self):
        for name in ("../000001_2023.xml", "/000001_2023.xml", "C:/000001_2023.xml",
                     "dir\\000001_2023.xml"):
            path = self.archive([(name, legacy_member(1)[1])], name="unsafe.tar.gz")
            with self.subTest(name=name):
                for walk in (inspect_package, survey_package):
                    with self.assertRaisesRegex(PackageError, "Unsafe member path"):
                        walk(path)

    def test_a_symbolic_link_stops_both(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            info = tarfile.TarInfo("000001_2023.xml")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        path = self.dir / "link.tar.gz"
        path.write_bytes(gzip.compress(output.getvalue(), mtime=0))
        for walk in (inspect_package, survey_package):
            with self.assertRaises(PackageError):
                walk(path)

    def test_an_exhausted_limit_stops_both(self):
        path = self.archive([legacy_member(1), legacy_member(2)])
        for limits in (Limits(compressed_bytes=1), Limits(expanded_bytes=100),
                       Limits(member_bytes=10), Limits(notices=1)):
            with self.subTest(limits=limits):
                for walk in (inspect_package, survey_package):
                    with self.assertRaises(PackageError):
                        walk(path, limits)

    def test_truncation_corruption_and_trailing_data_stop_both(self):
        good = self.archive([legacy_member(1)], name="good.tar.gz")
        truncated = self.dir / "truncated.tar.gz"
        truncated.write_bytes(good.read_bytes()[:-5])
        corrupt = self.dir / "corrupt.tar.gz"
        body = bytearray(good.read_bytes())
        body[-8] ^= 1
        corrupt.write_bytes(bytes(body))
        trailing = self.dir / "trailing.tar.gz"
        raw = self.raw([legacy_member(1)])
        trailing.write_bytes(gzip.compress(raw[:2048] + b"unexpected" + raw[2058:], mtime=0))
        for path in (truncated, corrupt, trailing):
            with self.subTest(path=path.name):
                for walk in (inspect_package, survey_package):
                    with self.assertRaises(PackageError):
                        walk(path)


class SurveyCliTests(SurveyTestCase):
    def test_a_compatible_survey_prints_its_inventory_and_exits_zero(self):
        path = self.archive([legacy_member(1), eforms_member(2)])
        result = self.run_cli(
            "inspect", str(path), "--package-id", "monthly/2020-01", "--survey"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["compatible_for_load"])
        self.assertEqual(payload["source_package_id"], "monthly/2020-01")

    def test_an_incompatible_survey_still_prints_the_inventory_and_exits_non_zero(self):
        path = self.archive([
            legacy_member(1), ("2020-220/000042_2020.xml", UNSUPPORTED_ROOT)
        ])
        result = self.run_cli("inspect", str(path), "--survey")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["compatible_for_load"])
        self.assertEqual(payload["incompatible_member_count"], 1)
        self.assertIn("not loadable", result.stderr)
        self.assertNotIn("never surfaced", result.stdout + result.stderr)

    def test_an_archive_fault_prints_no_inventory_at_all(self):
        path = self.dir / "broken.tar.gz"
        path.write_bytes(b"not a gzip archive")
        result = self.run_cli("inspect", str(path), "--survey")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Survey failed", result.stderr)


class LoadStaysFailClosedTests(SurveyTestCase):
    # The only case here that needs a database; the survey itself never uses one.
    setUpClass = classmethod(lambda cls: ensure_test_database())

    def test_one_unsupported_member_condemns_the_whole_capture(self):
        conn = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(conn.close)
        truncate_all(conn)
        path = self.archive([
            legacy_member(1), ("2020-220/000042_2020.xml", UNSUPPORTED_ROOT), legacy_member(3)
        ])
        result = load_package(conn, path, "monthly/2020-01", batch_size=1)

        self.assertEqual(result.status, "failed")
        self.assertIn("Unsupported XML root", result.failure_reason)
        self.assertEqual(
            conn.execute("select count(*) from tl_read.notice").fetchone()[0], 0
        )
        self.assertEqual(
            conn.execute("select count(*) from tl_work.published_capture").fetchone()[0], 0
        )
        # The survey knows the same archive is not loadable, without writing a row.
        self.assertFalse(survey_package(path)["compatible_for_load"])
        self.assertEqual(
            conn.execute("select count(*) from tl_work.capture").fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()
