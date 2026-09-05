"""The SDK's fourth eForms document type, and what it may not be widened into.

A real monthly package (2023-11) carries exactly one
``BusinessRegistrationInformationNotice`` among 61,638 members. Without it in
the allow-list a whole month is unloadable; with a looser allow-list any
unknown root would be. So the root is added by exact name, held to the same
identifiers every other eForms notice must carry, and projected under the same
contract -- which for this document means the buyer, the classification and the
change references are ``absent``, because the paths that would resolve them do
not exist in it and no neighbouring party is a substitute.
"""

import unittest

from ted_fixtures import business_registration_member
from tender_ledger.packages import EFORMS_ROOTS, PackageError, inspect_notice
from tender_ledger.projection import (
    CHANGE_REFERENCE_ABSENT,
    CONTRACT_VERSION,
    project_notice,
)

ROOT = (
    "{http://data.europa.eu/p27/eforms-business-registration-information-notice/1}"
    "BusinessRegistrationInformationNotice"
)


class AllowListTests(unittest.TestCase):
    def test_only_the_observed_root_is_admitted(self):
        self.assertIn(ROOT, EFORMS_ROOTS)
        self.assertEqual(len(EFORMS_ROOTS), 4)

    def test_a_neighbouring_namespace_version_stays_unsupported(self):
        name, xml = business_registration_member(
            1,
            namespace=(
                "http://data.europa.eu/p27/"
                "eforms-business-registration-information-notice/2"
            ),
        )
        with self.assertRaisesRegex(PackageError, "Unsupported XML root"):
            inspect_notice(name, xml)


class ProjectionTests(unittest.TestCase):
    def test_a_valid_notice_projects_with_its_unresolvable_fields_absent(self):
        name, xml = business_registration_member(4242, 2023)
        self.assertEqual(inspect_notice(name, xml), (
            inspect_notice(name, xml)[0], "eforms", "eforms-sdk-1.9"
        ))

        projected = project_notice(name, xml)
        self.assertEqual(projected.source_format, "eforms")
        self.assertEqual(projected.schema_version, "eforms-sdk-1.9")
        self.assertEqual(projected.contract_version, CONTRACT_VERSION)
        self.assertEqual(projected.key.number, 4242)
        self.assertEqual(projected.key.year, 2023)
        self.assertEqual(projected.publication_date.isoformat(), "2023-11-15")
        self.assertEqual(projected.dispatch_date.isoformat(), "2023-11-14")
        self.assertEqual(projected.notice_version, "01")
        self.assertIsNotNone(projected.notice_uuid)

        self.assertIsNone(projected.buyer_country)
        self.assertIsNone(projected.buyer_country_iso)
        self.assertEqual(projected.buyer_country_status, "absent")
        self.assertIsNone(projected.primary_cpv)
        self.assertEqual(projected.primary_cpv_status, "absent")
        self.assertEqual(projected.additional_cpv, ())
        self.assertEqual(projected.change_reference_status, CHANGE_REFERENCE_ABSENT)
        self.assertEqual(projected.change_references, ())

    def test_the_publication_date_still_comes_from_the_extension(self):
        name, xml = business_registration_member(1, pub_date=None)
        with self.assertRaisesRegex(PackageError, "without a publication date"):
            project_notice(name, xml)

    def test_a_missing_dispatch_date_is_not_a_rejection(self):
        name, xml = business_registration_member(1, issue_date="")
        projected = project_notice(name, xml)
        self.assertIsNone(projected.dispatch_date)
        self.assertIsNone(projected.dispatch_date_raw)


class MandatoryIdentifierTests(unittest.TestCase):
    """Each identifier every eForms notice owes, refused one at a time."""

    def assert_rejected(self, expected_code, **overrides):
        name, xml = business_registration_member(1, **overrides)
        with self.assertRaises(PackageError) as raised:
            inspect_notice(name, xml)
        self.assertEqual(raised.exception.code, expected_code)

    def test_a_missing_customization_identifier_is_refused(self):
        self.assert_rejected("unsupported_customization", customization=None)

    def test_a_customization_identifier_from_another_family_is_refused(self):
        self.assert_rejected("unsupported_customization", customization="ted-sdk-1.9")

    def test_an_overlong_customization_identifier_is_refused(self):
        self.assert_rejected(
            "unsupported_customization", customization="eforms-sdk-" + "9" * 200
        )

    def test_an_identifier_without_the_notice_id_scheme_is_refused(self):
        self.assert_rejected("missing_notice_identifier", notice_id_scheme=None)
        self.assert_rejected("missing_notice_identifier", notice_id_scheme="other-id")

    def test_an_empty_identifier_is_refused(self):
        self.assert_rejected("missing_notice_identifier", notice_id="")

    def test_a_filename_that_is_not_a_publication_reference_is_refused(self):
        _, xml = business_registration_member(1)
        with self.assertRaises(PackageError) as raised:
            inspect_notice("notice.xml", xml)
        self.assertEqual(raised.exception.code, "invalid_identity")


if __name__ == "__main__":
    unittest.main()
