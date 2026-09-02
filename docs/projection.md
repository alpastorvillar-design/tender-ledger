# Notice projection contract

`contract_version = 1`.

The loader stores a small allow-listed projection of each notice, not the raw
XML. Fields were derived from a real daily package (2023-11-15) containing legacy
`R2.0.8`/`R2.0.9` and eForms SDK 1.3-1.9. Broader-era validation (2020-2022 is
legacy-only; older eForms SDKs) happens at M3 against monthly artifacts.

## Identity

Canonical key `(publication_year, publication_number)`, parsed from the member
filename and, for legacy, cross-checked against `@DOC_ID`. For eForms the
`cbc:ID[@schemeName="notice-id"]` value is a UUID and is **not** the key; it is
kept as `notice_uuid` for provenance. `notice_version` (`cbc:VersionID`) is
eForms-only and null for legacy.

## Fields and source paths

| Field | Legacy (`TED_EXPORT`) | eForms (UBL) |
| --- | --- | --- |
| `publication_date` (required) | `CODED_DATA_SECTION/REF_OJS/DATE_PUB` (`YYYYMMDD`) | `efbc:PublicationDate` (`YYYY-MM-DDZ`) |
| `dispatch_date` (optional) | `CODED_DATA_SECTION/CODIF_DATA/DS_DATE_DISPATCH` | `cbc:IssueDate` |
| `buyer_country` | `CODED_DATA_SECTION/NOTICE_DATA/ISO_COUNTRY/@VALUE` (alpha-2) | buyer org's `cac:PostalAddress/cac:Country/cbc:IdentificationCode` (alpha-3) |
| `primary_cpv` / `additional_cpv` | `CODED_DATA_SECTION/NOTICE_DATA/ORIGINAL_CPV/@CODE` (first / rest) | `cac:ProcurementProject/cac:MainCommodityClassification` then `.../AdditionalCommodityClassification` |
| `schema_version` | `@VERSION`, or the namespace token when `R2.0.8` omits it | `cbc:CustomizationID` |

The eForms buyer organisation is resolved by matching
`cac:ContractingParty/cac:Party/cac:PartyIdentification/cbc:ID` against the
`efac:Organizations/efac:Organization` entries; place-of-performance country is
deliberately ignored.

## Absent vs not applicable

`publication_date` is required: a member without it is rejected and the capture
fails. `buyer_country_status` and `primary_cpv_status` are `present`, `absent`
(the expected element is missing or empty), or `not_applicable` (reserved for
notice shapes where the field has no meaning). A value is never guessed, defaulted
to zero, or dropped silently. A member that cannot be parsed at all fails the
capture, so there is no partially-populated row.

## Country normalization

`buyer_country` keeps the code exactly as published (alpha-2 for legacy, alpha-3
for eForms). `buyer_country_iso` is the alpha-2 form for the European codes that
dominate TED, so country can be a single analytical axis. An unmapped code keeps
`buyer_country` with status `present` and a null `buyer_country_iso`.

## Excluded

No contact names, e-mail, phone, or postal addresses. No monetary amounts (a
notice is not an award). Raw archives are never expanded to disk or committed.
