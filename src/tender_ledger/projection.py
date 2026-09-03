"""Project one TED notice into the small allow-listed shape the database stores.

Element paths were derived from a real daily package (legacy R2.0.8/R2.0.9 and
eForms SDK 1.3-1.9). The projection is an explicit allowlist: canonical identity,
schema provenance, calendar dates, buyer country, and CPV classification. Contact
details (names, e-mail, phone, addresses) are never read.

Field status is explicit: ``present`` (a value was published), ``absent`` (the
element the contract expects is missing or empty), ``not_applicable`` (reserved
for notice shapes where the field has no meaning). A member that cannot be parsed
at all is rejected upstream, so there is no partial row.
"""

import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .packages import NoticeKey, PackageError, parse_notice

CONTRACT_VERSION = "1"

_CBC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}"
_CAC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}"
_EFAC = "{http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1}"
_EFBC = "{http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1}"

# eForms publishes 3-letter country codes; legacy publishes ISO 3166-1 alpha-2.
# This maps the European codes that dominate TED to alpha-2 for a single
# analytical axis. Unknown codes keep the raw value with status ``present`` and a
# null normalized code rather than being dropped or guessed.
_ALPHA3_TO_ALPHA2 = {
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "HRV": "HR", "CYP": "CY", "CZE": "CZ",
    "DNK": "DK", "EST": "EE", "FIN": "FI", "FRA": "FR", "DEU": "DE", "GRC": "GR",
    "HUN": "HU", "IRL": "IE", "ITA": "IT", "LVA": "LV", "LTU": "LT", "LUX": "LU",
    "MLT": "MT", "NLD": "NL", "POL": "PL", "PRT": "PT", "ROU": "RO", "SVK": "SK",
    "SVN": "SI", "ESP": "ES", "SWE": "SE", "ISL": "IS", "LIE": "LI", "NOR": "NO",
    "CHE": "CH", "GBR": "GB", "ALB": "AL", "MNE": "ME", "MKD": "MK", "SRB": "RS",
    "TUR": "TR", "BIH": "BA", "UKR": "UA", "MDA": "MD", "GEO": "GE", "XKX": "XK",
}


@dataclass(frozen=True)
class ProjectedNotice:
    key: NoticeKey
    source_format: str
    schema_version: str
    source_filename: str
    publication_date: dt.date
    publication_date_raw: str
    dispatch_date: dt.date | None
    dispatch_date_raw: str | None
    buyer_country: str | None
    buyer_country_iso: str | None
    buyer_country_status: str
    primary_cpv: str | None
    primary_cpv_status: str
    additional_cpv: tuple[str, ...]
    notice_uuid: str | None = None
    notice_version: str | None = None
    contract_version: str = CONTRACT_VERSION


def _parse_date(raw: str | None, member_name: str, kind: str) -> tuple[dt.date | None, str | None]:
    if raw is None or not raw.strip():
        return None, None
    raw = raw.strip()
    compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw)
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    match = compact or iso
    if match is None:
        raise PackageError(f"Unparseable {kind} date {raw!r} in {member_name}")
    try:
        value = dt.date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError as exc:
        raise PackageError(f"Invalid {kind} date {raw!r} in {member_name}") from exc
    return value, raw


def _normalize_country(code: str | None) -> tuple[str | None, str | None, str]:
    if not code:
        return None, None, "absent"
    code = code.strip().upper()
    if not code:
        return None, None, "absent"
    if re.fullmatch(r"[A-Z]{2}", code):
        return code, code, "present"
    return code, _ALPHA3_TO_ALPHA2.get(code), "present"


def _project_legacy(root: ET.Element, key, member_name: str, version: str) -> ProjectedNotice:
    ns = root.tag[1 : root.tag.index("}")]

    def q(*names: str) -> str:
        return "/".join(f"{{{ns}}}{n}" for n in names)

    coded = root.find(f"{{{ns}}}CODED_DATA_SECTION")
    if coded is None:
        raise PackageError(f"Legacy notice without CODED_DATA_SECTION: {member_name}")

    pub_raw = coded.findtext(q("REF_OJS", "DATE_PUB"))
    publication_date, publication_raw = _parse_date(pub_raw, member_name, "publication")
    if publication_date is None:
        raise PackageError(f"Legacy notice without a publication date: {member_name}")

    dispatch_date, dispatch_raw = _parse_date(
        coded.findtext(q("CODIF_DATA", "DS_DATE_DISPATCH")), member_name, "dispatch"
    )

    iso = coded.find(q("NOTICE_DATA", "ISO_COUNTRY"))
    buyer_country, buyer_iso, country_status = _normalize_country(
        iso.get("VALUE") if iso is not None else None
    )

    cpv_codes = [
        el.get("CODE")
        for el in coded.findall(q("NOTICE_DATA", "ORIGINAL_CPV"))
        if el.get("CODE")
    ]
    primary_cpv = cpv_codes[0] if cpv_codes else None

    return ProjectedNotice(
        key=key,
        source_format="legacy",
        schema_version=version,
        source_filename=member_name.rsplit("/", 1)[-1],
        publication_date=publication_date,
        publication_date_raw=publication_raw,
        dispatch_date=dispatch_date,
        dispatch_date_raw=dispatch_raw,
        buyer_country=buyer_country,
        buyer_country_iso=buyer_iso,
        buyer_country_status=country_status,
        primary_cpv=primary_cpv,
        primary_cpv_status="present" if primary_cpv else "absent",
        additional_cpv=tuple(cpv_codes[1:]),
    )


def _project_eforms(root: ET.Element, key, member_name: str, version: str) -> ProjectedNotice:
    pub_date_el = next(root.iter(f"{_EFBC}PublicationDate"), None)
    publication_date, publication_raw = _parse_date(
        pub_date_el.text if pub_date_el is not None else None, member_name, "publication"
    )
    if publication_date is None:
        raise PackageError(f"eForms notice without a publication date: {member_name}")

    dispatch_date, dispatch_raw = _parse_date(
        root.findtext(f"{_CBC}IssueDate"), member_name, "dispatch"
    )

    notice_version = root.findtext(f"{_CBC}VersionID")
    uuid_el = root.find(f"{_CBC}ID")
    notice_uuid = uuid_el.text if uuid_el is not None else None

    buyer_country, buyer_iso, country_status = _normalize_country(_eforms_buyer_country(root))

    project = root.find(f"{_CAC}ProcurementProject")
    cpv_main: list[str] = []
    cpv_extra: list[str] = []
    if project is not None:
        for node in project.findall(f"{_CAC}MainCommodityClassification"):
            code = node.findtext(f"{_CBC}ItemClassificationCode")
            if code:
                cpv_main.append(code.strip())
        for node in project.findall(f"{_CAC}AdditionalCommodityClassification"):
            code = node.findtext(f"{_CBC}ItemClassificationCode")
            if code:
                cpv_extra.append(code.strip())
    primary_cpv = cpv_main[0] if cpv_main else None

    return ProjectedNotice(
        key=key,
        source_format="eforms",
        schema_version=version,
        source_filename=member_name.rsplit("/", 1)[-1],
        publication_date=publication_date,
        publication_date_raw=publication_raw,
        dispatch_date=dispatch_date,
        dispatch_date_raw=dispatch_raw,
        buyer_country=buyer_country,
        buyer_country_iso=buyer_iso,
        buyer_country_status=country_status,
        primary_cpv=primary_cpv,
        primary_cpv_status="present" if primary_cpv else "absent",
        additional_cpv=tuple(cpv_main[1:] + cpv_extra),
        notice_uuid=notice_uuid,
        notice_version=notice_version,
    )


def _eforms_buyer_country(root: ET.Element) -> str | None:
    """Country of the organisation the ContractingParty points at, or None.

    Without an unambiguous buyer reference the country stays unresolved: another
    organisation in the notice (a review body, a supplier) is not a substitute.
    """
    contracting = root.find(f"{_CAC}ContractingParty")
    if contracting is None:
        return None
    buyer_id = (
        contracting.findtext(f"{_CAC}Party/{_CAC}PartyIdentification/{_CBC}ID") or ""
    ).strip()
    if not buyer_id:
        return None
    for org in root.iter(f"{_EFAC}Organization"):
        company = org.find(f"{_EFAC}Company")
        if company is None:
            continue
        org_id = (company.findtext(f"{_CAC}PartyIdentification/{_CBC}ID") or "").strip()
        if org_id != buyer_id:
            continue
        return (
            company.findtext(f"{_CAC}PostalAddress/{_CAC}Country/{_CBC}IdentificationCode")
            or None
        )
    return None


def project_notice(member_name: str, xml: bytes) -> ProjectedNotice:
    root, key, source_format, version = parse_notice(member_name, xml)
    return _project(root, key, member_name, source_format, version)


def project_member(member) -> ProjectedNotice:
    """Project a ``NoticeMember`` already parsed and validated by ``stream_notices``."""
    return _project(
        member.root, member.key, member.member_name,
        member.source_format, member.schema_version,
    )


def _project(root, key, member_name, source_format, version) -> ProjectedNotice:
    if source_format == "legacy":
        return _project_legacy(root, key, member_name, version)
    return _project_eforms(root, key, member_name, version)
