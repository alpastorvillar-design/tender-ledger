-- Workload 2: latest complete observation per publication.
--
-- Question: for each canonical publication identity, which complete capture is
-- the most recent observation held by this system?
-- Grain: one row per (publication_year, publication_number).
-- Source and filters: tl_read.notice_history, which contains only published and
-- superseded captures. Partial and failed captures never compete.
-- NULL/absent semantics: a publication with no complete observation contributes
-- no row. Projected field statuses retain their own absent/unknown meanings.
-- Order / tie-break: acquisition_ordinal desc, then capture_id desc, within the
-- canonical publication identity.
-- Parameters: none.
-- Does not let you claim: that this is the current official notice version.
-- Acquisition order records when this system observed a complete capture.
select
    ranked.publication_ref,
    ranked.publication_year,
    ranked.publication_number,
    ranked.source_package_id,
    ranked.capture_id,
    ranked.acquisition_ordinal,
    ranked.capture_status,
    ranked.contract_version,
    ranked.source_format,
    ranked.schema_version,
    ranked.notice_version,
    ranked.publication_date,
    ranked.buyer_country,
    ranked.buyer_country_iso,
    ranked.buyer_country_status,
    ranked.primary_cpv,
    ranked.primary_cpv_status,
    ranked.change_reference_status
from (
    select
        h.*,
        row_number() over (
            partition by h.publication_year, h.publication_number
            order by h.acquisition_ordinal desc, h.capture_id desc
        ) as rn
    from tl_read.notice_history h
) ranked
where ranked.rn = 1
order by ranked.publication_year, ranked.publication_number;
