"""Synthetic fixtures, faithful to real TED element paths, exercise the projection.

Real element paths were derived from an authorized bounded download of one daily
package; see the private lab evidence. Fixtures below contain no real notice text
and no contact fields.
"""

import datetime as dt
import unittest

from tender_ledger.packages import PackageError
from tender_ledger.projection import CONTRACT_VERSION, project_notice


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


def eforms(pub_id="00000995-2020", pub_date="2020-01-03Z", issue_date="2019-12-26+01:00",
           customization="eforms-sdk-1.9", version_id="01", buyer_country="DEU",
           buyer_id="ORG-0001",
           main_cpv='<cac:MainCommodityClassification><cbc:ItemClassificationCode listName="cpv">72000000</cbc:ItemClassificationCode></cac:MainCommodityClassification>',
           extra_cpv='<cac:AdditionalCommodityClassification><cbc:ItemClassificationCode listName="cpv">72100000</cbc:ItemClassificationCode></cac:AdditionalCommodityClassification>'):
    country_el = (
        f'<cac:PostalAddress><cac:Country>'
        f'<cbc:IdentificationCode listName="country">{buyer_country}</cbc:IdentificationCode>'
        f'</cac:Country></cac:PostalAddress>' if buyer_country else ""
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
        f'<efac:Organizations><efac:Organization><efac:Company>'
        f'<cac:PartyIdentification><cbc:ID>{buyer_id}</cbc:ID></cac:PartyIdentification>'
        f'{country_el}</efac:Company></efac:Organization>'
        '<efac:Organization><efac:Company>'
        '<cac:PartyIdentification><cbc:ID>ORG-0002</cbc:ID></cac:PartyIdentification>'
        '<cac:PostalAddress><cac:Country>'
        '<cbc:IdentificationCode listName="country">FRA</cbc:IdentificationCode>'
        '</cac:Country></cac:PostalAddress></efac:Company></efac:Organization>'
        '</efac:Organizations>'
        f'<efac:Publication><efbc:NoticePublicationID>{pub_id}</efbc:NoticePublicationID>'
        f'{f"<efbc:PublicationDate>{pub_date}</efbc:PublicationDate>" if pub_date else ""}'
        '</efac:Publication>'
        '</EformsExtension></ext:ExtensionContent></ext:UBLExtension></ext:UBLExtensions>'
        f'<cbc:CustomizationID>{customization}</cbc:CustomizationID>'
        '<cbc:ID schemeName="notice-id">d758d45a-515d-4b92-b441-14c985063716</cbc:ID>'
        f'{f"<cbc:IssueDate>{issue_date}</cbc:IssueDate>" if issue_date else ""}'
        f'<cbc:VersionID>{version_id}</cbc:VersionID>'
        '<cac:ContractingParty><cac:Party><cac:PartyIdentification>'
        '<cbc:ID>ORG-0001</cbc:ID></cac:PartyIdentification></cac:Party></cac:ContractingParty>'
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

    def test_buyer_country_resolved_from_contracting_party_org(self):
        p = project_notice("day/00000995_2020.xml", eforms(buyer_country="DEU"))
        self.assertEqual(p.buyer_country, "DEU")
        self.assertEqual(p.buyer_country_iso, "DE")
        self.assertEqual(p.buyer_country_status, "present")

    def test_buyer_country_absent_when_org_has_no_country(self):
        p = project_notice("day/00000995_2020.xml", eforms(buyer_country=""))
        self.assertIsNone(p.buyer_country)
        self.assertEqual(p.buyer_country_status, "absent")

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


class ContractVersionTests(unittest.TestCase):
    def test_contract_version_is_stable_string(self):
        self.assertIsInstance(CONTRACT_VERSION, str)
        self.assertTrue(CONTRACT_VERSION)


if __name__ == "__main__":
    unittest.main()
