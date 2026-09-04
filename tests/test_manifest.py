"""Manifest parsing and strict validation. No HTTP and no database: a rejected
manifest must never reach either."""

import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tender_ledger.manifest import (
    Manifest,
    ManifestEntry,
    ManifestError,
    load_manifest,
    parse_manifest,
)

VALID = {
    "manifest_version": 1,
    "packages": [
        {
            "order": 1,
            "source_package_id": "monthly/2020-01",
            "notice_count_observed": 50123,
            "compressed_bytes_observed": 139301298,
            "observed_at": "2026-09-04",
            "purpose": "legacy",
        },
        {
            "order": 2,
            "source_package_id": "daily/202300220",
            "notice_count_observed": 2967,
            "compressed_bytes_observed": 12377691,
            "observed_at": "2026-09-04",
            "purpose": "accepted mixed daily",
        },
    ],
}


def entry(**overrides):
    base = dict(VALID["packages"][0])
    base.update(overrides)
    return base


def manifest_with(*entries, version=1):
    return {"manifest_version": version, "packages": list(entries)}


class ValidManifestTests(unittest.TestCase):
    def test_a_valid_document_parses_in_order(self):
        manifest = parse_manifest(VALID)
        self.assertIsInstance(manifest, Manifest)
        self.assertEqual(manifest.manifest_version, 1)
        self.assertEqual(len(manifest.entries), 2)
        self.assertEqual(
            [e.source_package_id for e in manifest.entries],
            ["monthly/2020-01", "daily/202300220"],
        )
        self.assertEqual([e.order for e in manifest.entries], [1, 2])
        first = manifest.entries[0]
        self.assertIsInstance(first, ManifestEntry)
        self.assertEqual(first.notice_count_observed, 50123)
        self.assertEqual(first.compressed_bytes_observed, 139301298)
        self.assertEqual(first.observed_at, dt.date(2026, 9, 4))
        self.assertEqual(first.purpose, "legacy")

    def test_load_manifest_reads_a_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps(VALID), encoding="utf-8")
            manifest = load_manifest(path)
        self.assertEqual(len(manifest.entries), 2)

    def test_a_single_entry_manifest_is_valid(self):
        manifest = parse_manifest(manifest_with(entry(order=1)))
        self.assertEqual(len(manifest.entries), 1)

    def test_the_real_pilot_manifest_parses_with_the_five_expected_identities(self):
        repo_root = Path(__file__).resolve().parents[1]
        manifest = load_manifest(repo_root / "manifests" / "m3-pilot.json")
        self.assertEqual(
            [e.source_package_id for e in manifest.entries],
            [
                "monthly/2020-01",
                "monthly/2020-02",
                "monthly/2023-11",
                "monthly/2024-01",
                "daily/202300220",
            ],
        )
        self.assertEqual([e.order for e in manifest.entries], [1, 2, 3, 4, 5])


class InvalidJsonTests(unittest.TestCase):
    def test_malformed_json_text_is_rejected(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_a_json_array_at_the_top_level_is_rejected(self):
        with self.assertRaises(ManifestError):
            parse_manifest([1, 2, 3])

    def test_a_missing_file_is_rejected(self):
        with TemporaryDirectory() as tmp, self.assertRaises(ManifestError):
            load_manifest(Path(tmp) / "does-not-exist.json")


class StrictSchemaTests(unittest.TestCase):
    def test_an_unknown_manifest_version_is_rejected(self):
        for version in (0, 2, "1", 1.0, None):
            with self.subTest(version=version), self.assertRaises(ManifestError):
                parse_manifest(manifest_with(entry(), version=version))

    def test_an_extra_top_level_key_is_rejected(self):
        document = {**VALID, "url": "https://ted.europa.eu/packages"}
        with self.assertRaises(ManifestError):
            parse_manifest(document)

    def test_a_missing_top_level_key_is_rejected(self):
        document = {"manifest_version": 1}
        with self.assertRaises(ManifestError):
            parse_manifest(document)

    def test_an_empty_package_list_is_rejected(self):
        with self.assertRaises(ManifestError):
            parse_manifest(manifest_with())

    def test_packages_must_be_a_list(self):
        with self.assertRaises(ManifestError):
            parse_manifest({"manifest_version": 1, "packages": {}})

    def test_a_non_canonical_identity_is_rejected(self):
        for bad_id in ("weekly/2020-01", "daily/20200001x", "monthly/2020-13", "", "daily"):
            with self.subTest(source_package_id=bad_id), self.assertRaises(ManifestError):
                parse_manifest(manifest_with(entry(source_package_id=bad_id)))

    def test_duplicate_identities_are_rejected(self):
        document = manifest_with(
            entry(order=1, source_package_id="daily/202300220"),
            entry(order=2, source_package_id="daily/202300220"),
        )
        with self.assertRaises(ManifestError):
            parse_manifest(document)

    def test_order_must_match_position(self):
        document = manifest_with(
            entry(order=1, source_package_id="daily/202300220"),
            entry(order=1, source_package_id="monthly/2020-01"),
        )
        with self.assertRaises(ManifestError):
            parse_manifest(document)

    def test_order_out_of_sequence_is_rejected(self):
        document = manifest_with(
            entry(order=2, source_package_id="daily/202300220"),
        )
        with self.assertRaises(ManifestError):
            parse_manifest(document)

    def test_an_extra_entry_key_is_rejected(self):
        for extra in ("url", "output_path", "credentials", "password", "destination"):
            with self.subTest(extra=extra), self.assertRaises(ManifestError):
                parse_manifest(manifest_with(entry(**{extra: "x"})))

    def test_a_missing_entry_key_is_rejected(self):
        for missing in (
            "order", "source_package_id", "notice_count_observed",
            "compressed_bytes_observed", "observed_at", "purpose",
        ):
            reduced = entry()
            del reduced[missing]
            with self.subTest(missing=missing), self.assertRaises(ManifestError):
                parse_manifest(manifest_with(reduced))

    def test_wrong_types_are_rejected(self):
        cases = [
            ("order", "1"),
            ("order", True),
            ("source_package_id", 12345),
            ("notice_count_observed", "50123"),
            ("notice_count_observed", -1),
            ("notice_count_observed", True),
            ("compressed_bytes_observed", 1.5),
            ("compressed_bytes_observed", -1),
            ("observed_at", 20260904),
            ("observed_at", "04-09-2026"),
            ("purpose", 7),
            ("purpose", ""),
            ("purpose", "   "),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(ManifestError):
                parse_manifest(manifest_with(entry(**{field: value})))

    def test_an_entry_that_is_not_an_object_is_rejected(self):
        document = {"manifest_version": 1, "packages": ["daily/202300220"]}
        with self.assertRaises(ManifestError):
            parse_manifest(document)


if __name__ == "__main__":
    unittest.main()
