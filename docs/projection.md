# Notice projection contract

`contract_version = 3`.

The loader stores a small allow-listed projection of each notice, not the raw
XML. Fields were derived from a real daily package (2023-11-15) containing legacy
`R2.0.8`/`R2.0.9` and eForms SDK 1.3-1.9. Monthly packages from 2020, 2023 and
2024 exercise flat and nested archive layouts, legacy documents and mixed
legacy/eForms documents.

## Identity

Canonical key `(publication_year, publication_number)`, parsed from the member
filename and, for legacy, cross-checked against `@DOC_ID`. For eForms the
`cbc:ID[@schemeName="notice-id"]` value is a UUID and is **not** the key; it is
kept as `notice_uuid` for provenance. `notice_version` (`cbc:VersionID`) is
eForms-only and null for legacy.

## Fields and source paths

| Field | Legacy (`TED_EXPORT`) | eForms (UBL) |
| --- | --- | --- |
| `publication_date` (required) | Direct child `CODED_DATA_SECTION/REF_OJS/DATE_PUB` (`YYYYMMDD`) | `efac:Publication/efbc:PublicationDate` (`YYYY-MM-DDZ`) |
| `dispatch_date` (optional) | `CODED_DATA_SECTION/CODIF_DATA/DS_DATE_DISPATCH` | `cbc:IssueDate` |
| `buyer_country` | `CODED_DATA_SECTION/NOTICE_DATA/ISO_COUNTRY/@VALUE` (alpha-2) | buyer org's `cac:PostalAddress/cac:Country/cbc:IdentificationCode` (alpha-3) |
| `primary_cpv` / `additional_cpv` | `CODED_DATA_SECTION/NOTICE_DATA/ORIGINAL_CPV/@CODE` (first / rest) | `cac:ProcurementProject/cac:MainCommodityClassification` then `.../AdditionalCommodityClassification` |
| `schema_version` | `@VERSION`, or the namespace token when `R2.0.8` omits it | `cbc:CustomizationID` |

The eForms buyer organisation is resolved by matching
`cac:ContractingParty/cac:Party/cac:PartyIdentification/cbc:ID` against the
`efac:Organizations/efac:Organization` entries; place-of-performance country is
deliberately ignored.

Contract v3 makes both publication-date paths structural instead of searching
by local name anywhere in the document. Some eForms notices also contain a
privacy date named `efbc:PublicationDate`; it is not the publication date and
must never win because it appears first. Some legacy roots declare an R2.0.9
namespace while their direct `CODED_DATA_SECTION` child resets to the empty
namespace. The projection accepts those two observed section forms and then
uses the section's own namespace for its descendants. It does not search for a
similarly named element elsewhere.

## Supported roots

Legacy: `TED_EXPORT` in the `R2.0.8` and `R2.0.9` namespaces. eForms: the three
UBL procurement documents (`ContractNotice`, `ContractAwardNotice`,
`PriorInformationNotice`) and the SDK's fourth notice document,
`BusinessRegistrationInformationNotice`, which carries its own namespace rather
than a UBL one. Each is allow-listed by exact root name; anything else is
rejected, including another version of the same namespace.

A business registration notice is not a procurement procedure, so it publishes
no `cac:ContractingParty` and no `cac:ProcurementProject`. It is held to the
same identifiers as every other eForms notice -- `cbc:CustomizationID`,
`cbc:ID[@schemeName="notice-id"]` and `efbc:PublicationDate` -- and its buyer
country, CPV and change references project as `absent`. The parties it does
carry (a sender, a registered business) are not buyers and are never read as a
substitute. One real month, `monthly/2023-11`, contained exactly one of these
among 61,638 members: without the root the whole month is unloadable, and with
a looser rule any unknown root would be.

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

## Official change references (introduced in contract v2)

An eForms notice may declare that it corrects or supersedes another notice, via
one or more `efbc:ChangedNoticeIdentifier` elements (located by local name
under the extension block, not by an exact nested path — see
`_change_references` in `projection.py`). A real mixed daily package showed
this is genuinely one-to-many: 217 occurrences across 1,154 eForms notices, two
of them carrying more than one. It is kept as an ordered, one-to-many relation
in `tl_work.notice_change_reference` (see [loading](loading.md)), never
collapsed into a scalar column and never deduplicated — two identical values
are two references, not one.

Each notice carries a `change_reference_status`:

| Status | Meaning |
| --- | --- |
| `present` | At least one `efbc:ChangedNoticeIdentifier`; every occurrence is kept, in document order, with its raw value and optional `schemeName` |
| `absent` | An eForms notice that published none |
| `not_applicable` | A legacy notice; the element does not exist in that schema family, and this does not claim a legacy notice never corrected another |
| `NULL` | Loaded before contract v2; "not projected under this contract", never treated as equivalent to `absent` |

An element that is present but empty or whitespace-only is not downgraded to
`absent`: it names a value the source claims to have published and did not, so
the whole member is rejected the same way a missing publication date is.
Observed value shapes vary (a publication reference, a UUID plus version, or
other schemes such as `notice-id-ref`/`ojs-notice-id`); the raw value and
`schemeName` are stored exactly as published, and resolving a reference against
a loaded identity is a reading-time concern (see
[`queries/official_change_references.sql`](../queries/official_change_references.sql)),
not something the projection guesses at.

## Excluded

No contact names, e-mail, phone, or postal addresses. No monetary amounts (a
notice is not an award). Raw archives are never expanded to disk or committed.
