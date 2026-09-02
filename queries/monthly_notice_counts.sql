-- Monthly distinct-notice counts by buyer country and primary CPV division.
--
-- Grain: one row per (publication month, buyer country, CPV division).
--
--   * Source is tl_read.distinct_notice, so a publication that appears in both a
--     daily and a monthly package is counted once, not twice.
--   * A notice with no buyer country, or no primary CPV, is counted under an
--     explicit '(absent)' label. Missing values are never dropped, never merged
--     into a real code, and never treated as zero.
--   * buyer_country_iso is preferred so the legacy alpha-2 and eForms alpha-3
--     code spaces line up on one axis; the raw published code is the fallback.
--   * CPV division is the first two digits of the 8-digit code (e.g. 79000000).
--   * These are published notices, not awarded contracts, and carry no amount.
select
    date_trunc('month', publication_date)::date            as publication_month,
    coalesce(buyer_country_iso, buyer_country, '(absent)') as buyer_country,
    case
        when primary_cpv_status = 'present' then left(primary_cpv, 2) || '000000'
        else '(absent)'
    end                                                    as cpv_division,
    count(*)                                               as notice_count
from tl_read.distinct_notice
group by 1, 2, 3
order by publication_month, buyer_country, cpv_division;
