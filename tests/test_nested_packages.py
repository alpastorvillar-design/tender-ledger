"""Monthly archives that carry one day per nested ``.tar.gz`` container.

TED publishes monthly packages in two shapes. One is flat: a directory per
publication day holding XML members. The other wraps each day in its own
``.tar.gz`` inside the month's archive. Both are the same month, one capture and
one checkpoint; only the archive layout differs.

Everything here is about keeping that second shape from widening what a package
may cost or contain. Exactly one level of nesting is admitted, only for a
package identity whose policy allows it, and every defense the flat walk already
had -- safe paths, regular files only, per-member and aggregate byte ceilings,
gzip CRC, tar trailer, trailing data -- applies inside a container too. The
expanded bytes of a container are charged to the same budget as the outer
archive, so compression cannot hide work from the ceiling the identity set.
"""

import dataclasses
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
    daily_container,
    eforms_member,
    ensure_test_database,
    legacy_member,
    package_bytes,
    truncate_all,
    write_package,
)
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.loader import load_package
from tender_ledger.package_contract import DAILY_POLICY, MONTHLY_POLICY
from tender_ledger.packages import (
    PackageError,
    inspect_package,
    limits_for,
    survey_package,
)

MONTHLY = "monthly/2020-01"
DAILY = "daily/202300220"


def legacy_2020(number, **kwargs):
    kwargs.setdefault("date_pub", "20200115")
    kwargs.setdefault("dispatch", "20200110")
    return legacy_member(number, 2020, **kwargs)


def raw_tar(members):
    """The uncompressed tar of ``members``, for building damaged archives."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def typed_member(name, tar_type, **attributes):
    """A tar holding one member of a type a package may not contain."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo(name)
        info.type = tar_type
        info.size = 0
        for key, value in attributes.items():
            setattr(info, key, value)
        archive.addfile(info)
    return gzip.compress(output.getvalue(), mtime=0)


class NestedArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def archive(self, members, name="package.tar.gz"):
        return write_package(self.dir / name, members)

    def raw_archive(self, body, name="package.tar.gz"):
        path = self.dir / name
        path.write_bytes(body)
        return path

    def monthly_limits(self, **overrides):
        return dataclasses.replace(limits_for(MONTHLY), **overrides)

    def assert_archive_fault(self, path, limits=None, *, message=None):
        """Both walks stop. An archive-level fault is never a survey finding."""
        limits = limits or self.monthly_limits()
        for walk in (inspect_package, survey_package):
            with self.subTest(walk=walk.__name__):
                with self.assertRaises(PackageError) as raised:
                    walk(path, limits)
                if message is not None:
                    self.assertRegex(str(raised.exception), message)


class LayoutTests(NestedArchiveTestCase):
    def test_a_flat_monthly_archive_is_reported_flat(self):
        path = self.archive([legacy_2020(1), legacy_2020(2)])
        result = survey_package(path, self.monthly_limits(), source_package_id=MONTHLY)
        self.assertTrue(result["compatible_for_load"])
        self.assertEqual(result["layout"], "flat")
        self.assertEqual(result["container_count"], 0)
        self.assertEqual(result["member_count"], 2)
        self.assertEqual(result["notice_count"], 2)

    def test_two_daily_containers_are_one_month_of_notices(self):
        path = self.archive([
            daily_container("01/20200115_2020010.tar.gz", [legacy_2020(1), legacy_2020(2)]),
            daily_container("01/20200116_2020011.tar.gz", [eforms_member(3, 2020)]),
        ])
        result = survey_package(path, self.monthly_limits(), source_package_id=MONTHLY)
        self.assertTrue(result["compatible_for_load"])
        self.assertEqual(result["layout"], "nested")
        self.assertEqual(result["container_count"], 2)
        # Containers are not notices: the counts describe XML members.
        self.assertEqual(result["member_count"], 3)
        self.assertEqual(result["xml_member_count"], 3)
        self.assertEqual(result["notice_count"], 3)
        self.assertEqual(result["formats"], {"eforms": 1, "legacy": 2})

    def test_an_empty_container_contributes_no_notices(self):
        path = self.archive([
            daily_container("01/20200115_2020010.tar.gz", []),
            daily_container("01/20200116_2020011.tar.gz", [legacy_2020(1)]),
        ])
        result = survey_package(path, self.monthly_limits(), source_package_id=MONTHLY)
        self.assertTrue(result["compatible_for_load"])
        self.assertEqual(result["container_count"], 2)
        self.assertEqual(result["notice_count"], 1)

    def test_a_daily_package_still_refuses_every_nested_archive(self):
        path = self.archive([daily_container("20200115.tar.gz", [legacy_2020(1)])])
        with self.assertRaises(PackageError):
            inspect_package(path, limits_for(DAILY))
        result = survey_package(path, limits_for(DAILY), source_package_id=DAILY)
        self.assertFalse(result["compatible_for_load"])
        self.assertEqual(
            result["incompatible_reasons"], {"nested_container_not_allowed": 1}
        )
        self.assertEqual(result["container_count"], 0)
        self.assertEqual(result["layout"], "empty")

    def test_a_second_level_of_nesting_is_refused(self):
        inner = package_bytes([legacy_2020(1)])
        container = package_bytes([("20200115/deeper.tar.gz", inner)])
        path = self.archive([("01/20200115_2020010.tar.gz", container)])
        self.assert_archive_fault(path, message="Nested archive")

    def test_flat_members_and_containers_cannot_be_mixed(self):
        container = daily_container("01/20200116_2020011.tar.gz", [legacy_2020(2)])
        for order, members in (
            ("flat first", [legacy_2020(1), container]),
            ("container first", [container, legacy_2020(1)]),
        ):
            with self.subTest(order=order):
                path = self.archive(members, name=f"{order.replace(' ', '-')}.tar.gz")
                self.assert_archive_fault(path, message="mixes")


class ContainerContentTests(NestedArchiveTestCase):
    def test_an_unsafe_path_inside_a_container_stops_the_walk(self):
        for name in ("../000001_2020.xml", "/000001_2020.xml", "C:/000001_2020.xml",
                     "dir\\000001_2020.xml"):
            with self.subTest(name=name):
                container = package_bytes([(name, legacy_2020(1)[1])])
                path = self.archive(
                    [("01/day.tar.gz", container)], name="unsafe.tar.gz"
                )
                self.assert_archive_fault(path, message="Unsafe member path")

    def test_a_symbolic_link_inside_a_container_stops_the_walk(self):
        container = typed_member(
            "000001_2020.xml", tarfile.SYMTYPE, linkname="/etc/passwd"
        )
        path = self.archive([("01/day.tar.gz", container)])
        self.assert_archive_fault(path, message="Unsupported archive member")

    def test_a_sparse_member_inside_a_container_stops_the_walk(self):
        container = typed_member("000001_2020.xml", tarfile.GNUTYPE_SPARSE)
        path = self.archive([("01/day.tar.gz", container)])
        self.assert_archive_fault(path, message="Unsupported archive member")

    def test_a_non_xml_member_inside_a_container_is_a_member_level_rejection(self):
        container = package_bytes([("notes.txt", b"not xml"), legacy_2020(1)])
        path = self.archive([("01/day.tar.gz", container)])
        result = survey_package(path, self.monthly_limits(), source_package_id=MONTHLY)
        self.assertFalse(result["compatible_for_load"])
        self.assertEqual(result["incompatible_reasons"], {"unsupported_member_name": 1})
        self.assertEqual(result["notice_count"], 1)
        self.assertEqual(result["member_count"], 2)
        with self.assertRaises(PackageError):
            inspect_package(path, self.monthly_limits())

    def test_a_repeated_identity_across_two_containers_is_still_a_duplicate(self):
        path = self.archive([
            daily_container("01/20200115_2020010.tar.gz", [legacy_2020(1)]),
            daily_container("01/20200116_2020011.tar.gz", [legacy_2020(1)]),
        ])
        result = survey_package(path, self.monthly_limits(), source_package_id=MONTHLY)
        self.assertFalse(result["compatible_for_load"])
        self.assertEqual(result["duplicate_identity_count"], 1)
        self.assertEqual(result["duplicate_identity_sample"], ["1-2020"])
        self.assertEqual(result["notice_count"], 1)
        with self.assertRaisesRegex(PackageError, "Duplicate"):
            inspect_package(path, self.monthly_limits())

    def test_the_survey_reports_no_container_paths_or_xml_content(self):
        container = package_bytes([
            ("20200115_2020010/000001_2020.xml", b"<secret>never surfaced</secret>")
        ])
        path = self.archive([("01/20200115_2020010.tar.gz", container)])
        result = survey_package(path, self.monthly_limits(), source_package_id=MONTHLY)
        payload = repr(result)
        self.assertNotIn("never surfaced", payload)
        self.assertNotIn(str(self.dir), payload)
        self.assertNotIn("20200115_2020010/", payload)
        self.assertEqual(
            result["incompatible_sample"],
            [{"member": "000001_2020.xml", "reason": "unsupported_root"}],
        )


class DamagedContainerTests(NestedArchiveTestCase):
    def test_a_corrupt_container_gzip_crc_stops_the_walk(self):
        container = bytearray(package_bytes([legacy_2020(1)]))
        container[-8] ^= 1
        path = self.archive([("01/day.tar.gz", bytes(container))])
        self.assert_archive_fault(path, message="Unreadable or corrupt")

    def test_a_truncated_container_stops_the_walk(self):
        container = package_bytes([legacy_2020(1)])[:-5]
        path = self.archive([("01/day.tar.gz", container)])
        self.assert_archive_fault(path, message="Unreadable or corrupt")

    def test_a_container_that_is_not_gzip_at_all_stops_the_walk(self):
        path = self.archive([("01/day.tar.gz", b"not a gzip stream")])
        self.assert_archive_fault(path, message="Unreadable or corrupt")

    def test_data_after_a_container_tar_end_marker_stops_the_walk(self):
        body = raw_tar([legacy_2020(1)])
        damaged = gzip.compress(body[:2048] + b"unexpected" + body[2058:], mtime=0)
        path = self.archive([("01/day.tar.gz", damaged)])
        self.assert_archive_fault(path, message="Unexpected data")

    def test_bytes_after_the_container_gzip_stream_stop_the_walk(self):
        container = package_bytes([legacy_2020(1)]) + b"trailing"
        path = self.archive([("01/day.tar.gz", container)])
        self.assert_archive_fault(path, message="Unreadable or corrupt")


class NestedLimitTests(NestedArchiveTestCase):
    def two_containers(self):
        return self.archive([
            daily_container("01/20200115_2020010.tar.gz", [legacy_2020(1)]),
            daily_container("01/20200116_2020011.tar.gz", [legacy_2020(2)]),
        ])

    def test_the_number_of_containers_is_bounded(self):
        path = self.two_containers()
        self.assert_archive_fault(
            path, self.monthly_limits(container_count=1), message="container limit"
        )

    def test_one_container_may_not_exceed_its_own_compressed_ceiling(self):
        path = self.two_containers()
        self.assert_archive_fault(
            path, self.monthly_limits(container_bytes=64), message="Nested container"
        )

    def test_an_xml_member_inside_a_container_obeys_the_member_ceiling(self):
        path = self.two_containers()
        self.assert_archive_fault(
            path, self.monthly_limits(member_bytes=64), message="Member exceeds"
        )

    def test_notices_inside_containers_count_towards_the_notice_ceiling(self):
        path = self.two_containers()
        self.assert_archive_fault(
            path, self.monthly_limits(notices=1), message="notice limit"
        )

    def test_container_expansion_is_charged_to_the_archive_budget(self):
        """A container's compression may not buy expansion the ceiling forbids."""
        padded = legacy_2020(1)[1] + b"<!--" + b"p" * 200_000 + b"-->"
        container = package_bytes([("20200115/000001_2020.xml", padded)])
        path = self.archive([("01/20200115_2020010.tar.gz", container)])
        outer_expanded = len(gzip.decompress(path.read_bytes()))
        self.assertLess(outer_expanded, 200_000)  # the container hides its size

        allowed = survey_package(
            path, self.monthly_limits(expanded_bytes=400_000), source_package_id=MONTHLY
        )
        self.assertTrue(allowed["compatible_for_load"])
        self.assertGreater(allowed["expanded_bytes"], 200_000)
        self.assert_archive_fault(
            path,
            self.monthly_limits(expanded_bytes=outer_expanded + 1024),
            message="Expanded archive exceeds",
        )


class NestedPolicyTests(unittest.TestCase):
    def test_only_a_monthly_policy_admits_containers(self):
        self.assertEqual(DAILY_POLICY.container_count, 0)
        self.assertGreater(MONTHLY_POLICY.container_count, 0)

    def test_a_container_may_not_be_larger_than_the_archive_holding_it(self):
        for policy in (DAILY_POLICY, MONTHLY_POLICY):
            with self.subTest(policy=policy.notices):
                self.assertLessEqual(policy.container_bytes, policy.compressed_bytes)

    def test_the_daily_ceilings_are_unchanged_by_monthly_support(self):
        self.assertEqual(DAILY_POLICY.compressed_bytes, 64 * 1024**2)
        self.assertEqual(DAILY_POLICY.expanded_bytes, 512 * 1024**2)
        self.assertEqual(DAILY_POLICY.member_bytes, 8 * 1024**2)
        self.assertEqual(DAILY_POLICY.notices, 10_000)


class NestedSurveyCliTests(NestedArchiveTestCase):
    def run_cli(self, *args):
        env = {**os.environ, "PYTHONPATH": "src", "PYTHONUTF8": "1"}
        return subprocess.run(
            [sys.executable, "-m", "tender_ledger", *args],
            capture_output=True, text=True, env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )

    def test_the_command_line_survey_reads_the_identity_s_whole_policy(self):
        """The CLI must not refuse an archive its own `load` would accept."""
        path = self.archive([
            daily_container("01/20200115_2020010.tar.gz", [legacy_2020(1)]),
            daily_container("01/20200116_2020011.tar.gz", [legacy_2020(2)]),
        ])
        result = self.run_cli(
            "inspect", str(path), "--package-id", MONTHLY, "--survey"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["compatible_for_load"])
        self.assertEqual(payload["layout"], "nested")
        self.assertEqual(payload["container_count"], 2)
        self.assertEqual(payload["notice_count"], 2)

    def test_the_same_archive_under_a_daily_identity_is_refused(self):
        path = self.archive([daily_container("20200115.tar.gz", [legacy_2020(1)])])
        result = self.run_cli("inspect", str(path), "--package-id", DAILY, "--survey")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["compatible_for_load"])
        self.assertEqual(
            payload["incompatible_reasons"], {"nested_container_not_allowed": 1}
        )


class NestedLoadTests(NestedArchiveTestCase):
    """The survey, the inspector and a real load agree about a nested archive."""

    setUpClass = classmethod(lambda cls: ensure_test_database())

    def setUp(self):
        super().setUp()
        self.conn = db.connect(load_config(dbname=TEST_DB))
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)

    def test_a_nested_month_loads_exactly_what_the_survey_counted(self):
        path = self.archive([
            daily_container(
                "01/20200115_2020010.tar.gz", [legacy_2020(1), legacy_2020(2)]
            ),
            daily_container("01/20200116_2020011.tar.gz", [eforms_member(3, 2020)]),
        ])
        surveyed = survey_package(path, self.monthly_limits(), source_package_id=MONTHLY)
        inspected = inspect_package(path, self.monthly_limits())
        result = load_package(self.conn, path, MONTHLY, batch_size=2)

        self.assertTrue(surveyed["compatible_for_load"])
        self.assertEqual(inspected["notice_count"], surveyed["notice_count"])
        self.assertEqual(inspected["layout"], "nested")
        self.assertEqual(inspected["container_count"], 2)
        self.assertEqual(result.status, "published")
        self.assertEqual(result.member_count, surveyed["member_count"])
        self.assertEqual(result.distinct_notice_count, surveyed["notice_count"])
        self.assertEqual(result.loaded_row_count, surveyed["notice_count"])
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_read.notice where source_format = 'eforms'"
            ).fetchone()[0],
            1,
        )
        # The stored filename is the member's own, not the container path.
        self.assertEqual(
            [
                row[0] for row in self.conn.execute(
                    "select source_filename from tl_work.notice_capture order by 1"
                ).fetchall()
            ],
            ["00000003_2020.xml", "000001_2020.xml", "000002_2020.xml"],
        )

    def test_a_nested_month_a_survey_refuses_publishes_nothing(self):
        path = self.archive([
            daily_container("01/20200115_2020010.tar.gz", [legacy_2020(1)]),
            daily_container("01/20200116_2020011.tar.gz", [legacy_2020(1)]),
        ])
        self.assertFalse(
            survey_package(path, self.monthly_limits(), source_package_id=MONTHLY)[
                "compatible_for_load"
            ]
        )
        result = load_package(self.conn, path, MONTHLY, batch_size=2)
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            self.conn.execute("select count(*) from tl_read.notice").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute(
                "select count(*) from tl_work.published_capture"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
