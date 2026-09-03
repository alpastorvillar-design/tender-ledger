-- Every coverage verification attempt, in the order they were made.
--
-- Grain: one row per attempt. Attempts are ordered by attempt_id, which is the
-- order this system made them; a capture's attempts are numbered from 1 within
-- the capture and each row carries the state of the attempt before it.
--
--   * This is an operational history of *checks*, not a history of the notices.
--     A row here says nothing about official notice versions or corrections.
--   * previous_state is NULL on a capture's first attempt. A 'verified' row
--     followed by an 'unavailable' one is the expected shape of a source that
--     went away: the older evidence stays, and the capture's
--     source_coverage_verified is false because the latest attempt is not
--     'verified'.
--   * Difference counts are NULL when the attempt never enumerated the source.
--     Only api_keys_sha256 being non-NULL means a complete key set was seen;
--     it digests the API identifiers as described in key_digest_recipe.
--   * only_local_sample and only_api_sample hold at most 20 identifiers each.
--     They illustrate a difference; only_local_count and only_api_count size it.
select
    v.source_package_id,
    v.capture_id,
    v.attempt_id,
    row_number() over capture_attempts as attempt_ordinal,
    lag(v.state) over capture_attempts as previous_state,
    v.state,
    v.reason,
    v.started_at,
    v.finished_at,
    v.finished_at - v.started_at       as duration,
    v.query_text,
    v.query_scope,
    v.verifier_version,
    v.announced_total,
    v.api_record_count,
    v.api_distinct_count,
    v.api_duplicate_count,
    v.local_distinct_count,
    v.only_local_count,
    v.only_api_count,
    v.only_local_sample,
    v.only_api_sample,
    v.pages_fetched,
    v.http_attempts,
    v.api_keys_sha256
from tl_read.verification_attempt v
window capture_attempts as (partition by v.capture_id order by v.attempt_id)
order by v.capture_id, v.attempt_id;
