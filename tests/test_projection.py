"""Synthetic fixtures, faithful to real TED element paths, exercise the projection.

Real element paths were derived from an authorized bounded download of one daily
package; see the private lab evidence. Fixtures below contain no real notice text
and no contact fields.
"""

import datetime as dt
import unittest

from tender_ledger.packages import PackageError
from tender_ledger.projection import CONTRACT_VERSION, ChangeReference, project_notice


def legacy(doc_id="000995-2020", namespace="R2.0.9", version_attr='VERSION="R2.0.9.S05.E01"',
           date_pub="20200103", dispatch="20191226", country='<ISO_COUNTRY VALUE="PL"/>',
           cpv='<ORIGINAL_CPV CODE="79000000"/><ORIGINAL_CPV CODE="79100000"/>'):
    return (
        f'<TED_EXPORT xmlns="http://publications.europa.eu/resource/schema/ted/{namespace}/publication" '
        f'DOC_ID="{doc_id}" EDITION="2020002" {version_attr}>'
        '<CODED_DATA_SECTION>'
        f'<REF_OJS><COLL_OJ>S</COLL_OJ><NO_OJ>2</NO_OJ><DATE_PUB>{date_pub}</DATE_PUB></REF_OJS>'
        f'<NOTICE_DATA><NO_DOC_OJS>2020/S 002-000995</NO_DOC_OJS>{country}{cpv}'
        '<PERFORMANCE_NUTS CODE="PL911"/></NOTICE_DATA>'
        f'<CODIF_DATA>{f"<DS_DATE_DISPATCH>{dispatch}</DS_DATE_DISPATCH>" if dispatch else ""}'
        '<TD_DOCUMENT_TYPE CODE="3">Contract notice</TD_DOCUMENT_TYPE></CODIF_DATA>'
        '</CODED_DATA_SECTION>'
        '<FORM_SECTION><CONTRACT LG="EN"><FD_CONTRACT><CONTRACTING_AUTHORITY>'
        '<ATTENTION>A person name that must never be projected</ATTENTION>'
        '<E_MAIL>contact@example.org</E_MAIL>'
        '</CONTRACTING_AUTHORITY></FD_CONTRACT></CONTRACT></FORM_SECTION>'
        '</TED_EXPORT>'
    ).encode()


_UNSET = object()


def _change_reference_element(value, scheme):
    attr = f' schemeName="{scheme}"' if scheme else ""
    text = value or ""
    return f"<efbc:ChangedNoticeIdentifier{attr}>{text}</efbc:ChangedNoticeIdentifier>"


def _change_references_xml(change_refs):
    """``change_refs``: an iterable of (value, scheme_name) pairs. ``value`` may
    be ``""`` or ``None`` to emit an empty element (whitespace-only text)."""
    if not change_refs:
        return ""
    items = "".join(_change_reference_element(value, scheme) for value, scheme in change_refs)
    return f"<efac:Changes>{items}</efac:Changes>"


def eforms(pub_id="00000995-2020", pub_date="2020-01-03Z", issue_date="2019-12-26+01:00",
           customization="eforms-sdk-1.9", version_id="01", buyer_country="DEU",
           contracting_ref=_UNSET, orgs=_UNSET, change_refs=(),
           main_cpv='<cac:MainCommodityClassification><cbc:ItemClassificationCode listName="cpv">72000000</cbc:ItemClassificationCode></cac:MainCommodityClassification>',
           extra_cpv='<cac:AdditionalCommodityClassification><cbc:ItemClassificationCode listName="cpv">72100000</cbc:ItemClassificationCode></cac:AdditionalCommodityClassification>'):
    # orgs: list of (id, country). contracting_ref: the buyer reference the
    # ContractingParty carries -- default matches the first org, None omits the
    # ContractingParty, "" leaves an empty reference.
    if orgs is _UNSET:
        orgs = [("ORG-0001", buyer_country)]
    if contracting_ref is _UNSET:
        contracting_ref = orgs[0][0]

    def org_xml(org_id, country):
        country_el = (
            '<cac:PostalAddress><cac:Country>'
            f'<cbc:IdentificationCode listName="country">{country}</cbc:IdentificationCode>'
            '</cac:Country></cac:PostalAddress>' if country else ""
        )
        return (
            '<efac:Organization><efac:Company>'
            f'<cac:PartyIdentification><cbc:ID>{org_id}</cbc:ID></cac:PartyIdentification>'
            f'{country_el}</efac:Company></efac:Organization>'
        )

    organizations = "".join(org_xml(oid, c) for oid, c in orgs)
    contracting_party = (
        '<cac:ContractingParty><cac:Party><cac:PartyIdentification>'
        f'<cbc:ID>{contracting_ref}</cbc:ID></cac:PartyIdentification></cac:Party></cac:ContractingParty>'
        if contracting_ref is not None else ""
    )
    return (
        '<ContractNotice '
        'xmlns="urn:oasis:names:specification:ubl:schema:xsd:ContractNotice-2" '
        'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2" '
        'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
        'xmlns:efac="http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1" '
        'xmlns:efbc="http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1" '
        'xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2">'
        '<ext:UBLExtensions><ext:UBLExtension><ext:ExtensionContent><EformsExtension '
        'xmlns="http://data.europa.eu/p27/eforms-ubl-extensions/1">'
        f'<efac:Organizations>{organizations}</efac:Organizations>'
        f'<efac:Publication><efbc:NoticePublicationID>{pub_id}</efbc:NoticePublicationID>'
        f'{f"<efbc:PublicationDate>{pub_date}</efbc:PublicationDate>" if pub_date else ""}'
        '</efac:Publication>'
        f'{_change_references_xml(change_refs)}'
        '</EformsExtension></ext:ExtensionContent></ext:UBLExtension></ext:UBLExtensions>'
        f'<cbc:CustomizationID>{customization}</cbc:CustomizationID>'
        '<cbc:ID schemeName="notice-id">d758d45a-515d-4b92-b441-14c985063716</cbc:ID>'
        f'{f"<cbc:IssueDate>{issue_date}</cbc:IssueDate>" if issue_date else ""}'
        f'<cbc:VersionID>{version_id}</cbc:VersionID>'
        f'{contracting_party}'
        f'<cac:ProcurementProject><cbc:ID>PROJ-1</cbc:ID>{main_cpv}{extra_cpv}'
        '<cac:RealizedLocation><cac:Address><cac:Country>'
        '<cbc:IdentificationCode listName="country">ITA</cbc:IdentificationCode>'
        '</cac:Country></cac:Address></cac:RealizedLocation>'
        '</cac:ProcurementProject>'
        '</ContractNotice>'
    ).encode()


class LegacyProjectionTests(unittest.TestCase):
    def test_r209_core_fields(self):
        p = project_notice("day/000995_2020.xml", legacy())
        self.assertEqual((p.key.year, p.key.number), (2020, 995))
        self.assertEqual(p.source_format, "legacy")
        self.assertEqual(p.schema_version, "R2.0.9.S05.E01")
        self.assertEqual(p.publication_date, dt.date(2020, 1, 3))
        self.assertEqual(p.publication_date_raw, "20200103")
        self.assertEqual(p.dispatch_date, dt.date(2019, 12, 26))
        self.assertEqual(p.buyer_country, "PL")
        self.assertEqual(p.buyer_country_iso, "PL")
        self.assertEqual(p.buyer_country_status, "present")
        self.assertEqual(p.primary_cpv, "79000000")
        self.assertEqual(p.primary_cpv_status, "present")
        self.assertEqual(p.additional_cpv, ("79100000",))

    def test_r208_without_version_attribute_uses_namespace(self):
        p = project_notice("day/995_2020.xml", legacy(namespace="R2.0.8", version_attr=""))
        self.assertEqual(p.source_format, "legacy")
        self.assertEqual(p.schema_version, "R2.0.8")

    def test_unqualified_sections_under_a_namespaced_root_are_projected(self):
        xml = legacy().replace(
            b"<CODED_DATA_SECTION>", b'<CODED_DATA_SECTION xmlns="">', 1
        )
        p = project_notice("day/000995_2020.xml", xml)
        self.assertEqual(p.publication_date, dt.date(2020, 1, 3))
        self.assertEqual(p.buyer_country_iso, "PL")
        self.assertEqual(p.primary_cpv, "79000000")

    def test_absent_country_and_cpv_are_marked_absent_not_null_guessed(self):
        p = project_notice("day/995_2020.xml", legacy(country="", cpv=""))
        self.assertIsNone(p.buyer_country)
        self.assertEqual(p.buyer_country_status, "absent")
        self.assertIsNone(p.primary_cpv)
        self.assertEqual(p.primary_cpv_status, "absent")
        self.assertEqual(p.additional_cpv, ())

    def test_absent_dispatch_date_is_null(self):
        p = project_notice("day/995_2020.xml", legacy(dispatch=""))
        self.assertIsNone(p.dispatch_date)
        self.assertIsNone(p.dispatch_date_raw)

    def test_contact_fields_are_never_projected(self):
        p = project_notice("day/995_2020.xml", legacy())
        blob = repr(p).lower()
        self.assertNotIn("person name", blob)
        self.assertNotIn("example.org", blob)
        self.assertNotIn("@", blob)


class EformsProjectionTests(unittest.TestCase):
    def test_contract_notice_core_fields(self):
        p = project_notice("day/00000995_2020.xml", eforms())
        self.assertEqual((p.key.year, p.key.number), (2020, 995))
        self.assertEqual(p.source_format, "eforms")
        self.assertEqual(p.schema_version, "eforms-sdk-1.9")
        self.assertEqual(p.notice_version, "01")
        self.assertEqual(p.notice_uuid, "d758d45a-515d-4b92-b441-14c985063716")
        self.assertEqual(p.publication_date, dt.date(2020, 1, 3))
        self.assertEqual(p.publication_date_raw, "2020-01-03Z")
        self.assertEqual(p.dispatch_date, dt.date(2019, 12, 26))
        self.assertEqual(p.dispatch_date_raw, "2019-12-26+01:00")

    def test_publication_date_comes_from_the_publication_block(self):
        xml = eforms().replace(
            b"<efac:Publication>",
            b"<efac:NoticeResult><efac:FieldsPrivacy>"
            b"<efbc:PublicationDate>2053-01-01Z</efbc:PublicationDate>"
            b"</efac:FieldsPrivacy></efac:NoticeResult><efac:Publication>",
            1,
        )
        p = project_notice("day/00000995_2020.xml", xml)
        self.assertEqual(p.publication_date, dt.date(2020, 1, 3))
        self.assertEqual(p.publication_date_raw, "2020-01-03Z")

    def test_buyer_country_resolved_from_contracting_party_org(self):
        p = project_notice("day/00000995_2020.xml", eforms(buyer_country="DEU"))
        self.assertEqual(p.buyer_country, "DEU")
        self.assertEqual(p.buyer_country_iso, "DE")
        self.assertEqual(p.buyer_country_status, "present")

    def test_buyer_country_absent_when_org_has_no_country(self):
        p = project_notice("day/00000995_2020.xml", eforms(buyer_country=""))
        self.assertIsNone(p.buyer_country)
        self.assertEqual(p.buyer_country_status, "absent")

    def test_buyer_country_not_guessed_without_a_contracting_party_reference(self):
        # One organisation with a known country, but no ContractingParty pointing
        # at it: the organisation could be a review body or supplier.
        xml = eforms(contracting_ref=None, orgs=[("ORG-0007", "DEU")])
        p = project_notice("day/00000995_2020.xml", xml)
        self.assertIsNone(p.buyer_country)
        self.assertEqual(p.buyer_country_status, "absent")

    def test_buyer_country_absent_when_reference_is_empty(self):
        xml = eforms(contracting_ref="", orgs=[("ORG-0007", "DEU")])
        p = project_notice("day/00000995_2020.xml", xml)
        self.assertEqual(p.buyer_country_status, "absent")

    def test_buyer_country_absent_when_reference_matches_no_organisation(self):
        xml = eforms(contracting_ref="ORG-9999", orgs=[("ORG-0001", "DEU")])
        p = project_notice("day/00000995_2020.xml", xml)
        self.assertEqual(p.buyer_country_status, "absent")

    def test_buyer_country_resolved_when_a_foreign_org_precedes_the_buyer(self):
        xml = eforms(contracting_ref="ORG-0001",
                     orgs=[("ORG-0002", "FRA"), ("ORG-0001", "DEU")])
        p = project_notice("day/00000995_2020.xml", xml)
        self.assertEqual(p.buyer_country, "DEU")
        self.assertEqual(p.buyer_country_iso, "DE")

    def test_cpv_primary_and_additional(self):
        p = project_notice("day/00000995_2020.xml", eforms())
        self.assertEqual(p.primary_cpv, "72000000")
        self.assertEqual(p.additional_cpv, ("72100000",))

    def test_cpv_absent_when_no_main_classification(self):
        p = project_notice("day/00000995_2020.xml", eforms(main_cpv="", extra_cpv=""))
        self.assertIsNone(p.primary_cpv)
        self.assertEqual(p.primary_cpv_status, "absent")

    def test_publication_key_uses_filename_not_notice_uuid(self):
        p = project_notice("day/00012345_2021.xml", eforms(pub_id="00012345-2021"))
        self.assertEqual((p.key.year, p.key.number), (2021, 12345))

    def test_missing_publication_date_is_an_error(self):
        with self.assertRaises(PackageError):
            project_notice("day/00000995_2020.xml", eforms(pub_date=""))


class ChangeReferenceProjectionTests(unittest.TestCase):
    def test_legacy_is_not_applicable_with_no_references(self):
        p = project_notice("day/000995_2020.xml", legacy())
        self.assertEqual(p.change_reference_status, "not_applicable")
        self.assertEqual(p.change_references, ())

    def test_eforms_without_the_element_is_absent(self):
        p = project_notice("day/00000995_2020.xml", eforms())
        self.assertEqual(p.change_reference_status, "absent")
        self.assertEqual(p.change_references, ())

    def test_eforms_with_one_reference_is_present_and_keeps_value_and_scheme(self):
        p = project_notice(
            "day/00000995_2020.xml",
            eforms(change_refs=[("00012345-2019", "notice-id-ref")]),
        )
        self.assertEqual(p.change_reference_status, "present")
        self.assertEqual(
            p.change_references, (ChangeReference(0, "00012345-2019", "notice-id-ref"),)
        )

    def test_a_missing_scheme_name_is_kept_as_none_not_guessed(self):
        p = project_notice(
            "day/00000995_2020.xml", eforms(change_refs=[("00012345-2019", None)])
        )
        self.assertIsNone(p.change_references[0].scheme_name)

    def test_multiple_references_keep_document_order_and_duplicates(self):
        refs = [
            ("00012345-2019", "notice-id-ref"),
            ("00012345-2019", "notice-id-ref"),  # a genuine duplicate is not collapsed
            ("d758d45a-515d-4b92-b441-14c985063716/02", None),
        ]
        p = project_notice("day/00000995_2020.xml", eforms(change_refs=refs))
        self.assertEqual(p.change_reference_status, "present")
        self.assertEqual([r.ordinal for r in p.change_references], [0, 1, 2])
        self.assertEqual([r.value for r in p.change_references], [v for v, _ in refs])

    def test_an_empty_reference_element_condemns_the_member(self):
        with self.assertRaises(PackageError):
            project_notice("day/00000995_2020.xml", eforms(change_refs=[("", None)]))

    def test_a_whitespace_only_reference_element_condemns_the_member(self):
        with self.assertRaises(PackageError):
            project_notice("day/00000995_2020.xml", eforms(change_refs=[("   ", None)]))

    def test_one_valid_and_one_empty_reference_still_condemns_the_member(self):
        refs = [("00012345-2019", "notice-id-ref"), ("", None)]
        with self.assertRaises(PackageError):
            project_notice("day/00000995_2020.xml", eforms(change_refs=refs))


class ContractVersionTests(unittest.TestCase):
    def test_contract_version_is_stable_string(self):
        self.assertIsInstance(CONTRACT_VERSION, str)
        self.assertTrue(CONTRACT_VERSION)

    def test_contract_version_is_three(self):
        # M3d's measured rehearsal fixed two source-format paths. Replays of
        # captures projected with the earlier paths must therefore recapture.
        self.assertEqual(CONTRACT_VERSION, "3")


if __name__ == "__main__":
    unittest.main()
