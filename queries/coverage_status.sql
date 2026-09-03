-- Coverage standing of every published capture, including the ones nobody has
-- verified yet.
--
-- Grain: one row per published capture. The LEFT JOIN LATERAL picks at most one
-- attempt, so a capture with a hundred attempts still produces one row and a
-- capture with none does not disappear -- which is the whole point: an
-- unverified window has to stay visible, not fall out of the report.
--
--   * coverage_state is the state of the *latest* attempt. 'never verified' is
--     a missing attempt (NULL), not a failure, and neither is 'unconfirmed
--     empty': zero notices on both sides does not establish that the issue was
--     published at all.
--   * source_coverage_verified on the capture is true only while the latest
--     attempt is 'verified'. A later failure lowers it and keeps the earlier
--     evidence in verification_history.sql.
--   * Counts are NULL when the attempt never learned them. NULL is not zero:
--     an unreachable source has an unknown difference, not an empty one.
--   * These are published notices, not awarded contracts.
select
    cs.source_package_id,
    cs.capture_id,
    cs.acquisition_ordinal,
    cs.distinct_notice_count                       as capture_notices,
    cs.source_coverage_verified,
    case attempt.state
        when 'verified'          then 'verified'
        when 'mismatch'          then 'mismatch'
        when 'unavailable'       then 'source unavailable'
        when 'empty_unconfirmed' then 'unconfirmed empty'
        when 'in_progress'       then 'verification in progress'
        else 'never verified'
    end                                            as coverage_state,
    attempt.attempt_id,
    attempt.started_at                             as verification_started_at,
    attempt.finished_at                            as verification_finished_at,
    attempt.announced_total                        as source_announced_total,
    attempt.api_distinct_count                     as source_distinct_notices,
    attempt.only_local_count,
    attempt.only_api_count,
    attempt.reason
from tl_read.capture_status cs
left join lateral (
    select
        v.attempt_id, v.state, v.reason, v.started_at, v.finished_at,
        v.announced_total, v.api_distinct_count, v.only_local_count, v.only_api_count
    from tl_read.verification_attempt v
    where v.capture_id = cs.capture_id
    order by v.attempt_id desc
    limit 1
) attempt on true
where cs.is_published
order by cs.source_package_id;
