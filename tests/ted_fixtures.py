"""Builders for synthetic TED package archives used by the loader tests.

Fixtures are structurally faithful to the real element paths (derived from an
authorized bounded download) but contain no real notice text and no contact
fields. They are deliberately tiny.
"""

import gzip
import io
import os
import tarfile
import unittest

TEST_DB = os.environ.get("TL_TEST_DB", "tender_ledger_test")


def ensure_test_database():
    """Create a fresh, migrated ``tender_ledger_test`` database.

    Raises unittest.SkipTest when PostgreSQL is unreachable so the suite skips
    loudly instead of passing without touching a database. Never targets the
    development database or its volume.
    """
    import psycopg

    from tender_ledger import db
    from tender_ledger.config import load_config

    assert TEST_DB.endswith("_test") and TEST_DB != "tender_ledger", TEST_DB
    try:
        admin = db.connect(load_config(dbname="postgres"), autocommit=True)
    except psycopg.OperationalError as exc:
        raise unittest.SkipTest(f"PostgreSQL not reachable: {exc}") from exc
    with admin:
        admin.execute(f'drop database if exists "{TEST_DB}" with (force)')
        admin.execute(f'create database "{TEST_DB}"')
    with db.connect(load_config(dbname=TEST_DB)) as conn:
        applied = db.migrate(conn)
        assert "0001_core" in applied


def truncate_all(conn):
    with conn.transaction():
        conn.execute(
            "truncate tl_work.notice_capture, tl_work.capture_batch,"
            " tl_work.published_capture, tl_work.capture restart identity cascade"
        )
        conn.execute("alter sequence tl_work.acquisition_seq restart with 1")


def legacy_member(number, year=2023, *, namespace="R2.0.9",
                  version_attr='VERSION="R2.0.9.S05.E01"',
                  date_pub="20231115", dispatch="20231110", country="PL",
                  cpv=("79000000",)):
    iso = f'<ISO_COUNTRY VALUE="{country}"/>' if country else ""
    cpv_xml = "".join(f'<ORIGINAL_CPV CODE="{code}"/>' for code in cpv)
    dispatch_xml = f"<DS_DATE_DISPATCH>{dispatch}</DS_DATE_DISPATCH>" if dispatch else ""
    xml = (
        f'<TED_EXPORT xmlns="http://publications.europa.eu/resource/schema/ted/{namespace}/publication" '
        f'DOC_ID="{number}-{year}" EDITION="2023220" {version_attr}>'
        "<CODED_DATA_SECTION>"
        f"<REF_OJS><COLL_OJ>S</COLL_OJ><NO_OJ>220</NO_OJ><DATE_PUB>{date_pub}</DATE_PUB></REF_OJS>"
        f"<NOTICE_DATA><NO_DOC_OJS>{year}/S 220-{number}</NO_DOC_OJS>{iso}{cpv_xml}"
        '<PERFORMANCE_NUTS CODE="PL911"/></NOTICE_DATA>'
        f"<CODIF_DATA>{dispatch_xml}<TD_DOCUMENT_TYPE CODE=\"3\">Contract notice</TD_DOCUMENT_TYPE></CODIF_DATA>"
        "</CODED_DATA_SECTION>"
        "<FORM_SECTION><CONTRACT LG=\"EN\"><FD_CONTRACT><CONTRACTING_AUTHORITY>"
        "<ATTENTION>never projected</ATTENTION><E_MAIL>never@example.org</E_MAIL>"
        "</CONTRACTING_AUTHORITY></FD_CONTRACT></CONTRACT></FORM_SECTION>"
        "</TED_EXPORT>"
    )
    return f"{year}-220/{int(number):06d}_{year}.xml", xml.encode("utf-8")


_UNSET = object()


def eforms_member(number, year=2023, *, customization="eforms-sdk-1.9",
                  pub_date="2023-11-15Z", issue_date="2023-11-14+01:00",
                  version_id="01", buyer_country="DEU", buyer_ref=_UNSET,
                  orgs=None, main_cpv="72000000", extra_cpv=("72100000",)):
    """A synthetic eForms notice.

    ``orgs`` is a list of (organisation id, country) pairs; the default is a
    single buyer organisation ``ORG-0001``. ``buyer_ref`` is the reference the
    ContractingParty carries: the default matches the first org, ``None`` omits
    the ContractingParty entirely, ``""`` leaves an empty reference.
    """
    if orgs is None:
        orgs = [("ORG-0001", buyer_country)]
    if buyer_ref is _UNSET:
        buyer_ref = orgs[0][0]

    def org_block(org_id, country):
        country_el = (
            "<cac:PostalAddress><cac:Country>"
            f'<cbc:IdentificationCode listName="country">{country}</cbc:IdentificationCode>'
            "</cac:Country></cac:PostalAddress>" if country else ""
        )
        return (
            "<efac:Organization><efac:Company>"
            f"<cac:PartyIdentification><cbc:ID>{org_id}</cbc:ID></cac:PartyIdentification>"
            f"{country_el}</efac:Company></efac:Organization>"
        )

    organizations = "".join(org_block(oid, c) for oid, c in orgs)
    contracting_party = (
        "<cac:ContractingParty><cac:Party><cac:PartyIdentification>"
        f"<cbc:ID>{buyer_ref}</cbc:ID></cac:PartyIdentification></cac:Party></cac:ContractingParty>"
        if buyer_ref is not None else ""
    )
    main = (
        f'<cac:MainCommodityClassification><cbc:ItemClassificationCode listName="cpv">{main_cpv}'
        "</cbc:ItemClassificationCode></cac:MainCommodityClassification>" if main_cpv else ""
    )
    extra = "".join(
        f'<cac:AdditionalCommodityClassification><cbc:ItemClassificationCode listName="cpv">{code}'
        "</cbc:ItemClassificationCode></cac:AdditionalCommodityClassification>"
        for code in extra_cpv
    )
    pub_date_el = f"<efbc:PublicationDate>{pub_date}</efbc:PublicationDate>" if pub_date else ""
    issue_el = f"<cbc:IssueDate>{issue_date}</cbc:IssueDate>" if issue_date else ""
    xml = (
        "<ContractNotice "
        'xmlns="urn:oasis:names:specification:ubl:schema:xsd:ContractNotice-2" '
        'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2" '
        'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
        'xmlns:efac="http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1" '
        'xmlns:efbc="http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1" '
        'xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2">'
        "<ext:UBLExtensions><ext:UBLExtension><ext:ExtensionContent><EformsExtension "
        'xmlns="http://data.europa.eu/p27/eforms-ubl-extensions/1">'
        f"<efac:Organizations>{organizations}</efac:Organizations>"
        f"<efac:Publication><efbc:NoticePublicationID>{int(number):08d}-{year}</efbc:NoticePublicationID>"
        f"{pub_date_el}</efac:Publication>"
        "</EformsExtension></ext:ExtensionContent></ext:UBLExtension></ext:UBLExtensions>"
        f"<cbc:CustomizationID>{customization}</cbc:CustomizationID>"
        '<cbc:ID schemeName="notice-id">d758d45a-515d-4b92-b441-14c985063716</cbc:ID>'
        f"{issue_el}<cbc:VersionID>{version_id}</cbc:VersionID>"
        f"{contracting_party}"
        f"<cac:ProcurementProject><cbc:ID>PROJ-{number}</cbc:ID>{main}{extra}</cac:ProcurementProject>"
        "</ContractNotice>"
    )
    return f"{year}-220/{int(number):08d}_{year}.xml", xml.encode("utf-8")


def package_bytes(members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(output.getvalue(), mtime=0)


def write_package(path, members):
    path.write_bytes(package_bytes(members))
    return path
