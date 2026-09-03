-- Ingest standing of every package this system has captured, including the ones
-- with no checkpoint at all.
--
-- Grain: one row per source package. The view is driven from a union of
-- captures, runs and checkpoints, so a package loaded by hand, a package whose
-- only run crashed, and a package with a retired checkpoint all keep their row.
-- A report that only listed processed packages would answer the wrong question.
--
--   * checkpoint_state separates four different things a person might otherwise
--     read as "not done": no checkpoint was ever sealed; one was sealed and is
--     still current; one was sealed and a later re-acquisition replaced the
--     capture under it; one was sealed and the capture's coverage no longer
--     stands (a failed, unavailable or still-running check).
--   * A checkpoint that stops being current is not deleted. The row keeps the
--     capture, attempt and checksum it was sealed on, because that evidence is
--     what happened; only its currency changes.
--   * open_run_* is a run that has neither completed nor failed. It is shown
--     next to a current checkpoint on purpose: the package can be processed and
--     have a new attempt in flight at the same time.
--   * Currency here is database evidence. It does not prove the archive file is
--     still on disk -- `ingest` re-reads and re-hashes the bytes before it will
--     replay a checkpoint -- and it does not claim the source is unchanged since
--     the check.
select
    s.source_package_id,
    case
        when not s.has_checkpoint            then 'never checkpointed'
        when s.checkpoint_is_current         then 'processed'
        when not s.capture_still_published   then 'retired by a later acquisition'
        else 'retired by a later coverage check'
    end                                      as checkpoint_state,
    s.checkpoint_notice_count,
    s.checkpoint_sealed_at,
    s.checkpoint_capture_id,
    s.published_capture_id,
    s.checkpoint_attempt_id,
    s.latest_attempt_id,
    coalesce(s.latest_verification_state, 'never verified') as latest_verification_state,
    s.coverage_verified,
    s.checkpoint_artifact_sha256,
    s.open_run_id,
    s.open_run_phase,
    s.open_run_started_at,
    s.open_run_last_error
from tl_read.package_ingest_status s
order by s.source_package_id;
