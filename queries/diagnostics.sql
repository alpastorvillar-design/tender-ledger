-- Diagnostic queries. Run individually.

-- 1. Load reconciliation per capture: do member count, distinct keys, and loaded
--    rows agree, and is the artifact load distinct from external coverage?
select
    source_package_id,
    capture_id,
    acquisition_ordinal,
    status,
    is_published,
    member_count,
    distinct_notice_count,
    loaded_row_count,
    (member_count = distinct_notice_count
     and distinct_notice_count = loaded_row_count) as reconciles,
    source_coverage_verified
from tl_read.capture_status
order by source_package_id, acquisition_ordinal;

-- 2. Canonical identities published by more than one source package (expected
--    where daily and monthly windows overlap; an anti-join baseline for M3).
select
    publication_ref,
    count(*)                                          as package_count,
    array_agg(source_package_id order by source_package_id) as packages
from tl_read.notice
group by publication_ref
having count(*) > 1
order by publication_ref;

-- 3. Field completeness on the published distinct-notice surface.
select
    count(*)                                                        as notices,
    count(*) filter (where buyer_country_status = 'present')        as with_country,
    count(*) filter (where primary_cpv_status = 'present')          as with_primary_cpv,
    count(*) filter (where dispatch_date is not null)               as with_dispatch_date
from tl_read.distinct_notice;
