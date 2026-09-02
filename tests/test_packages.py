"""Small constructed XML fixtures exercise archive and identity failures."""

import gzip
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tender_ledger.packages import Limits, NoticeKey, PackageError, inspect_notice, inspect_package

LEGACY = b'''<TED_EXPORT xmlns="http://publications.europa.eu/resource/schema/ted/R2.0.9/publication"
DOC_ID="000995-2020" VERSION="R2.0.9.S05.E01"/>'''
EFORMS = b'''<ContractNotice xmlns="urn:oasis:names:specification:ubl:schema:xsd:ContractNotice-2"
xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
<cbc:CustomizationID>eforms-sdk-1.9</cbc:CustomizationID>
<cbc:ID schemeName="notice-id">00000000-0000-4000-8000-000000000001</cbc:ID>
</ContractNotice>'''


def archive_bytes(members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(output.getvalue(), mtime=0)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sample.tar.gz"

    def write(self, members):
        self.path.write_bytes(archive_bytes(members))

    def test_reference_padding_and_separator_have_one_identity(self):
        keys = [NoticeKey.parse(x) for x in ("995-2020", "000995_2020.xml", "00000995-2020")]
        self.assertEqual(keys, [NoticeKey(2020, 995)] * 3)
        for bad in ("0-2020", "995-0000", "-1-2020", "995-2020.exe", "995-20"):
            with self.subTest(bad=bad), self.assertRaises(PackageError):
                NoticeKey.parse(bad)

    def test_mixed_formats_are_selected_by_root_not_filename_width(self):
        self.write([("day/000995_2020.xml", LEGACY), ("day/996_2020.xml", EFORMS)])
        result = inspect_package(self.path)
        self.assertEqual(result["notice_count"], 2)
        self.assertEqual(result["formats"], {"eforms": 1, "legacy": 1})
        self.assertFalse(result["source_coverage_verified"])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])
        self.assertEqual(inspect_package(self.path), result)

    def test_legacy_namespace_208_is_supported(self):
        xml = LEGACY.replace(b"R2.0.9", b"R2.0.8")
        self.assertEqual(inspect_notice("995_2020.xml", xml)[1], "legacy")

    def test_missing_optional_legacy_version_uses_namespace(self):
        xml = LEGACY.replace(b'VERSION="R2.0.9.S05.E01"', b'')
        self.assertEqual(inspect_notice("995_2020.xml", xml)[2], "R2.0.9")

    def test_duplicate_canonical_identity_fails(self):
        self.write([("000995_2020.xml", LEGACY), ("00000995_2020.xml", LEGACY)])
        with self.assertRaisesRegex(PackageError, "Duplicate"):
            inspect_package(self.path)

    def test_filename_and_document_id_must_agree(self):
        self.write([("996_2020.xml", LEGACY)])
        with self.assertRaisesRegex(PackageError, "disagree"):
            inspect_package(self.path)

    def test_truncated_gzip_footer_is_not_success(self):
        self.write([("995_2020.xml", LEGACY)])
        self.path.write_bytes(self.path.read_bytes()[:-5])
        with self.assertRaises(PackageError):
            inspect_package(self.path)

    def test_corrupt_gzip_crc_is_not_success(self):
        self.write([("995_2020.xml", LEGACY)])
        body = bytearray(self.path.read_bytes())
        body[-8] ^= 1
        self.path.write_bytes(body)
        with self.assertRaises(PackageError):
            inspect_package(self.path)

    def test_non_padding_after_tar_end_marker_is_rejected(self):
        body = gzip.decompress(archive_bytes([("995_2020.xml", LEGACY)]))
        # Put unexpected bytes after both end blocks, within tarfile's default buffer.
        body = body[:2048] + b"unexpected" + body[2058:]
        self.path.write_bytes(gzip.compress(body, mtime=0))
        with self.assertRaises(PackageError):
            inspect_package(self.path)

    def test_each_resource_limit_fails_explicitly(self):
        self.write([("995_2020.xml", LEGACY), ("996_2020.xml", EFORMS)])
        for limits in (Limits(compressed_bytes=1), Limits(expanded_bytes=100),
                       Limits(member_bytes=10), Limits(notices=1)):
            with self.subTest(limits=limits), self.assertRaises(PackageError):
                inspect_package(self.path, limits)

    def test_unknown_root_missing_fields_and_malformed_xml_fail(self):
        for xml in (b"<unknown/>", b"<broken", EFORMS.replace(b"notice-id", b"other"),
                    LEGACY.replace(b'DOC_ID="000995-2020"', b'')):
            with self.subTest(xml=xml), self.assertRaises(PackageError):
                inspect_notice("995_2020.xml", xml)

    def test_dtd_and_alternative_encoding_are_rejected(self):
        for xml in (b'<!DOCTYPE x [<!ENTITY x "test">]>' + LEGACY,
                    LEGACY.decode().encode("utf-16")):
            with self.subTest(xml=xml[:20]), self.assertRaises(PackageError):
                inspect_notice("995_2020.xml", xml)

    def test_unsafe_paths_and_non_xml_members_fail(self):
        for name in ("../995_2020.xml", "/995_2020.xml", "C:/995_2020.xml", "dir\\995_2020.xml", "readme.txt"):
            self.write([(name, LEGACY)])
            with self.subTest(name=name), self.assertRaises(PackageError):
                inspect_package(self.path)

    def test_symbolic_link_is_rejected(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            info = tarfile.TarInfo("995_2020.xml")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        self.path.write_bytes(gzip.compress(output.getvalue(), mtime=0))
        with self.assertRaises(PackageError):
            inspect_package(self.path)

    def test_empty_archive_is_inspectable_but_not_verified_coverage(self):
        self.write([])
        result = inspect_package(self.path)
        self.assertEqual(result["notice_count"], 0)
        self.assertFalse(result["source_coverage_verified"])

    def test_cli_emits_json_on_success_and_no_success_on_failure(self):
        self.write([("995_2020.xml", LEGACY)])
        command = [sys.executable, "-m", "tender_ledger", "inspect", str(self.path)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["notice_count"], 1)
        self.path.write_bytes(b"broken")
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Inspection failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
