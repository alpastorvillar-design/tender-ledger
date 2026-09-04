-- Workload 5: monthly coverage calendar.
--
-- Question: for every calendar month in a range, has this system ever
-- touched the corresponding monthly/YYYY-MM package, and what is its ingest
-- and coverage standing?
-- Grain: one row per calendar month in [:'first_month', :'last_month']
-- (inclusive, first-of-month dates). Every month in the range appears exactly
-- once, whether or not a monthly package for it was ever downloaded.
-- Source and filters: a generated month series LEFT JOIN
-- tl_read.package_ingest_status, restricted to source_package_id like
-- 'monthly/%'.
-- NULL/absent semantics: checkpoint_notice_count and coverage_verified are
-- NULL for a month with no package record at all -- never coerced to zero or
-- to false. A verified, empty month (checkpoint_notice_count = 0) is a
-- different, known answer from a month nobody has touched.
-- Order / tie-break: month, ascending; one row per month already makes this
-- unambiguous.
-- Parameters: :'first_month', :'last_month' -- first-of-month date literals,
-- quoted. From psql: `-v first_month=2020-01-01 -v last_month=2025-12-01`.
-- From Python, as the tests do: replace the literal tokens `:'first_month'`
-- and `:'last_month'` with quoted date strings before executing.
-- Does not let you claim: that a month reported 'processed' has been
-- reconciled against the full historical target, or that 'no package record'
-- means the month was never published by TED -- both are about what this
-- system has done, not about the source's own history.
--
--   * calendar_state distinguishes: 'no package record' (nothing was ever
--     touched for that month), 'processed' (a current, verified checkpoint),
--     'empty source, unconfirmed' (a verification ran and found the source
--     empty, which never on its own establishes that the issue was
--     published), 'verification unavailable' (the source could not be
--     reached), 'verification in progress' (an attempt is currently open),
--     'discrepancy' (a mismatch between the local capture and the source),
--     'retired checkpoint' (a checkpoint was sealed and later stopped being
--     current -- by a later acquisition or a later failed check),
--     'verified, no checkpoint' (loaded and verified directly, never through
--     `ingest`) and 'verification not yet run' (an artifact exists but was
--     never checked).
with months as (
    select generate_series(:'first_month'::date, :'last_month'::date, interval '1 month')::date
        as month
),
monthly_packages as (
    select
        s.*,
        to_date(substring(s.source_package_id from 9), 'YYYY-MM') as month
    from tl_read.package_ingest_status s
    where s.source_package_id like 'monthly/%'
)
select
    m.month,
    'monthly/' || to_char(m.month, 'YYYY-MM') as source_package_id,
    case
        when p.source_package_id is null                      then 'no package record'
        when p.checkpoint_is_current                          then 'processed'
        when p.latest_verification_state = 'empty_unconfirmed' then 'empty source, unconfirmed'
        when p.latest_verification_state = 'unavailable'       then 'verification unavailable'
        when p.latest_verification_state = 'in_progress'       then 'verification in progress'
        when p.latest_verification_state = 'mismatch'          then 'discrepancy'
        when p.has_checkpoint and not p.checkpoint_is_current  then 'retired checkpoint'
        when p.latest_verification_state = 'verified'          then 'verified, no checkpoint'
        else 'verification not yet run'
    end as calendar_state,
    p.checkpoint_notice_count,
    p.coverage_verified,
    p.checkpoint_sealed_at
from months m
left join monthly_packages p on p.month = m.month
order by m.month;
