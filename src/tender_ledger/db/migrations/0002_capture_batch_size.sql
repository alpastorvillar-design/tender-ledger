-- Persist the batch-partition size on the capture.
--
-- Resuming a crashed load skips the batch ordinals already committed, so the
-- resumed run must split the archive into the same batches. Storing the size the
-- first run used lets a retry reuse it and ignore a different --batch-size.
--
-- Forward-only. The column is nullable: captures written by 0001 (before this
-- migration) keep NULL and, being already published, are never resumed.

alter table tl_work.capture
    add column batch_size integer
    check (batch_size is null or batch_size > 0);

-- Refresh the operational view so a resumed load's batch size is visible.
-- CREATE OR REPLACE appends the new column and keeps existing grants.
create or replace view tl_read.capture_status as
    select
        c.source_package_id,
        c.capture_id,
        c.acquisition_ordinal,
        c.status,
        (p.capture_id is not null) as is_published,
        c.artifact_sha256,
        c.artifact_bytes,
        c.contract_version,
        c.member_count,
        c.distinct_notice_count,
        c.loaded_row_count,
        c.coverage_verified as source_coverage_verified,
        c.failure_reason,
        c.acquired_at,
        c.published_at,
        c.batch_size
    from tl_work.capture c
    left join tl_work.published_capture p on p.capture_id = c.capture_id;
