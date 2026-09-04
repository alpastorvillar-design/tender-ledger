-- Workload 4: publication state at an acquisition cutoff.
--
-- Question: for each canonical publication identity observed by a cutoff, what
-- is this system's most recent complete observation at that point?
-- Grain: one row per (publication_year, publication_number) with a complete
-- observation at or before the cutoff.
-- Source and filters: tl_read.notice_history with acquisition_ordinal <= cutoff.
-- The view already excludes partial and failed captures.
-- NULL/absent semantics: a publication first observed after the cutoff has no
-- row. Projected field statuses retain their own absent/unknown meanings.
-- Order / tie-break: acquisition_ordinal desc, then capture_id desc, within the
-- canonical publication identity after applying the cutoff.
-- Parameter: :'cutoff', a bigint acquisition ordinal. In psql use
-- `-v cutoff=5`; tests substitute the same token with a numeric literal.
-- Does not let you claim: legal or official validity at a real-world time.
-- Acquisition order is this system's observation sequence.
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
    where h.acquisition_ordinal <= :'cutoff'::bigint
) ranked
where ranked.rn = 1
order by ranked.publication_year, ranked.publication_number;
