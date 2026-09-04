-- Workload 2: latest complete capture per publication.
--
-- Question: for each source package (a "publication" -- a specific TED
-- package identity, such as one daily OJ issue or one monthly window), which
-- is the most recently acquired *complete* capture this system holds?
-- Grain: one row per source_package_id.
-- Source and filters: tl_read.capture_status, restricted to status in
-- ('published', 'superseded') -- a capture that never finished loading never
-- competes for "latest".
-- NULL/absent semantics: a package with no complete capture at all
-- contributes no row; there is nothing to rank.
-- Order / tie-break: ROW_NUMBER() over (partition by source_package_id order
-- by acquisition_ordinal desc, capture_id desc). acquisition_ordinal alone
-- already totally orders one package's captures (it comes from a single
-- sequence at persist time -- see db/migrations/0001_core.sql), so
-- capture_id only breaks a tie that acquisition_ordinal's own uniqueness
-- constraint makes impossible; it is included so the ordering is total by
-- construction, not by an invariant a reader has to trust separately.
-- Parameters: none.
-- Does not let you claim: this is "the current official version" of a
-- notice, or of the package. Grouping by procedure identifier or by notice
-- UUID would answer a different question. In the overwhelmingly common case
-- the latest complete capture of a package *is* its currently published one;
-- what this proves, on a package this system has re-acquired
-- (--force-recapture), is that a superseded capture is still ranked
-- correctly against the one that replaced it -- it is "last observed by this
-- system", nothing about official notice versioning.
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
) ranked
where ranked.rn = 1
order by ranked.source_package_id;
