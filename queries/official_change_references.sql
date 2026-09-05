-- Workload 3: official change references.
--
-- Question: which notices carry official change references (eForms
-- efbc:ChangedNoticeIdentifier), what do those references say, and which of
-- them point at a notice this system has actually loaded?
-- Grain: one row per (notice, change reference) in a complete (published or
-- superseded) capture. A notice with zero references still contributes
-- exactly one row, with every reference column NULL, so its
-- change_reference_status is never lost by an inner join.
-- Source and filters: tl_read.notice_history LEFT JOIN
-- tl_read.notice_change_reference_history (same capture_id + canonical
-- identity), LEFT JOIN tl_read.distinct_notice for resolution.
-- NULL/absent semantics: change_reference_status distinguishes 'present'
-- (references follow below), 'absent' (an eForms notice published none),
-- 'not_applicable' (a legacy notice; the element does not exist for that
-- schema family) and NULL (loaded before contract v2 -- "not projected under
-- this contract", never the same claim as 'absent'). target_loaded is NULL
-- when there is no reference or its shape cannot be mapped safely to a
-- publication identity. It is false for a publication-shaped reference whose
-- target is not loaded, because that is a known answer rather than an unknown.
-- Order / tie-break: source_package_id, capture_id, publication_year,
-- publication_number, ordinal. A package may have several complete historical
-- captures containing the same notice and ordinal, so capture_id must separate
-- them before the reference's document-order position can be deterministic.
-- Parameters: none.
-- Does not let you claim: that a resolved target is the same procedure, or a
-- later version of the same notice UUID -- resolution here is deliberately
-- narrow. A value is only ever resolved when it takes the exact
-- "<number>-<year>" shape this system uses for its own publication reference
-- (see NoticeKey in packages.py); a UUID+version value, an OJ S reference, or
-- any other shape is reported unresolved by design, not guessed at through
-- procedure identifiers, notice UUIDs or textual similarity.
select
    h.source_package_id,
    h.capture_id,
    h.capture_status,
    h.publication_ref,
    h.change_reference_status,
    r.ordinal,
    r.value,
    r.scheme_name,
    case
        when r.value is null then null
        when r.value ~ '^[0-9]{1,8}-[1-9][0-9]{3}$' then 'publication_reference'
        else 'unresolved_shape'
    end as value_shape,
    target.publication_ref as resolved_target,
    case
        when r.value is null then null
        when r.value !~ '^[0-9]{1,8}-[1-9][0-9]{3}$' then null
        else target.publication_ref is not null
    end as target_loaded
from tl_read.notice_history h
left join tl_read.notice_change_reference_history r
    on r.capture_id = h.capture_id
   and r.publication_year = h.publication_year
   and r.publication_number = h.publication_number
-- regexp_match returns NULL (not an error) when the pattern fails to match,
-- and indexing or casting a NULL never raises -- unlike split_part, which
-- would otherwise be asked to cast text such as "uid" to bigint whenever a
-- non-conforming value reaches this join, regardless of evaluation order.
left join tl_read.distinct_notice target
    on (regexp_match(r.value, '^([0-9]{1,8})-([1-9][0-9]{3})$'))[1]::bigint
        = target.publication_number
   and (regexp_match(r.value, '^([0-9]{1,8})-([1-9][0-9]{3})$'))[2]::bigint
        = target.publication_year
order by h.source_package_id, h.capture_id,
         h.publication_year, h.publication_number, r.ordinal;
