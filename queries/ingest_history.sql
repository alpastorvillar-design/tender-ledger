-- Every ingest run in the order this system made them, with how far each got.
--
-- Grain: one row per run. attempt_ordinal numbers a package's runs from 1; it is
-- acquisition order for this system, not publication or version order.
--
--   * reached says how far the run got and whether it can still be resumed.
--     A failed run is terminal and is never resumed; an interrupted one keeps
--     its phase and its error, and the next invocation continues it.
--   * The LEFT JOINs in the view keep a run that never reached a capture or an
--     attempt. Those columns are NULL because the run never got there --
--     unknown, not "no capture".
--   * A run that completed still shows its capture and attempt even after a
--     later acquisition retired its checkpoint; see ingest_status.sql for which
--     checkpoint is current now.
--   * last_error is the last bounded reason recorded. On a completed run it is
--     cleared, so a run that succeeded after two interruptions shows none.
select
    r.source_package_id,
    r.run_id,
    r.attempt_ordinal,
    r.phase,
    case r.phase
        when 'completed'         then 'checkpoint sealed'
        when 'failed'            then 'terminal failure, not resumable'
        when 'source_verified'   then 'interrupted before the checkpoint'
        when 'capture_published' then 'interrupted before coverage was confirmed'
        when 'artifact_ready'    then 'interrupted before the capture was published'
        else 'interrupted before an artifact was accepted'
    end                                as reached,
    r.artifact_path,
    r.artifact_bytes,
    r.artifact_sha256,
    r.capture_id,
    r.acquisition_ordinal              as capture_acquisition_ordinal,
    r.capture_status,
    r.verification_attempt_id,
    r.verification_state,
    r.last_error,
    r.started_at,
    r.updated_at,
    r.completed_at,
    coalesce(r.completed_at, r.updated_at) - r.started_at as elapsed
from tl_read.ingest_run r
order by r.source_package_id, r.attempt_ordinal;
