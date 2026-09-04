-- Workload 4: state at an acquisition cutoff.
--
-- Question: as of a given point in this system's own acquisition order, what
-- did each source package's data look like?
-- Grain: one row per source_package_id that had at least one complete capture
-- at or before the cutoff.
-- Source and filters: tl_read.capture_status, restricted to status in
-- ('published', 'superseded') and acquisition_ordinal <= the cutoff
-- parameter.
-- NULL/absent semantics: a package whose only complete captures were all
-- acquired after the cutoff contributes no row -- as far as this cutoff is
-- concerned, this system held nothing for it yet.
-- Order / tie-break: same total order as workload 2 --
-- (acquisition_ordinal desc, capture_id desc) within each package -- applied
-- only to captures at or before the cutoff.
-- Parameter: :'cutoff' (an acquisition_ordinal value, i.e. a bigint written as
-- a quoted literal). From psql: `-v cutoff=5` and reference :'cutoff'::bigint
-- in the SQL below (already written that way). From Python, as the tests do:
-- replace the literal token `:'cutoff'` with the desired value before
-- executing, e.g. `sql.replace(":'cutoff'", "5")`.
-- Does not let you claim: that acquisition_ordinal 5 corresponds to any
-- particular wall-clock date, or that this is the package's official
-- historical state on some real-world date -- acquisition_ordinal is this
-- system's own acquisition sequence (see db/migrations/0001_core.sql), not a
-- publication or version timestamp. This is "what a backfill run stopped at",
-- not business history.
select
    ranked.source_package_id,
    ranked.capture_id,
    ranked.acquisition_ordinal,
    ranked.status,
    ranked.artifact_sha256,
    ranked.contract_version,
    ranked.distinct_notice_count,
    ranked.source_coverage_verified,
    ranked.acquired_at,
    ranked.published_at
from (
    select
        cs.*,
        row_number() over (
            partition by cs.source_package_id
            order by cs.acquisition_ordinal desc, cs.capture_id desc
        ) as rn
    from tl_read.capture_status cs
    where cs.status in ('published', 'superseded')
      and cs.acquisition_ordinal <= :'cutoff'::bigint
) ranked
where ranked.rn = 1
order by ranked.source_package_id;
