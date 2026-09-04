-- Contract v2: preserve official eForms change references as an ordered
-- one-to-many relation, expose complete (published or superseded) capture
-- history to the reader, and surface the stored contract versions a replay
-- has to compare against the running code.
--
-- Forward-only. Existing captures, notices, batches, the published pointer,
-- verification history, ingest runs and checkpoints are untouched; a capture
-- loaded before this migration simply has no change-reference rows.
--
-- change_reference_status is NULL for every row loaded under contract v1 --
-- deliberately, not backfilled to 'absent'. The one real v1 capture (a mixed
-- daily package) does carry eForms notices with official change references in
-- their source XML; writing 'absent' for those rows would assert something
-- the v1 projection never checked. NULL here means "not projected under this
-- contract", never treated as equivalent to 'absent' by the reading surfaces
-- below. Every row loaded under contract v2 always writes one of the three
-- states -- enforced by the Python projection, which never returns a null
-- status, not by a CHECK that would have to see another table's column.

alter table tl_work.notice_capture
    add column change_reference_status text
    check (change_reference_status is null
           or change_reference_status in ('present', 'absent', 'not_applicable'));

-- One row per efbc:ChangedNoticeIdentifier occurrence, in document order. A
-- notice's references are written in the same COPY / transaction as the
-- notice row itself (see repository.load_batch), so an interruption can never
-- publish a notice's identity without its references, or the reverse.
-- ON DELETE CASCADE mirrors that for clear_uncommitted_rows: it deletes an
-- uncommitted notice_capture row directly and relies on this to take that
-- row's references with it, rather than repeating the "batch never
-- committed" condition against a second table.
create table tl_work.notice_change_reference (
    capture_id          bigint  not null,
    publication_year    integer not null,
    publication_number  bigint  not null,
    ordinal             integer not null check (ordinal >= 0),
    value               text    not null check (btrim(value) <> ''),
    scheme_name         text,
    primary key (capture_id, publication_year, publication_number, ordinal),
    foreign key (capture_id, publication_year, publication_number)
        references tl_work.notice_capture (capture_id, publication_year, publication_number)
        on delete cascade
);

-- Complete-capture history for the reader role: published *and* superseded
-- captures, never a partial one (acquiring, loading, loaded or failed).
-- tl_read.notice and tl_read.distinct_notice are unchanged in grain -- the
-- currently published surface -- these two views are additive.
create view tl_read.notice_history as
    select
        c.source_package_id,
        c.capture_id,
        c.acquisition_ordinal,
        c.status                                          as capture_status,
        c.contract_version,
        n.publication_number || '-' || n.publication_year as publication_ref,
        n.publication_year,
        n.publication_number,
        n.source_format,
        n.schema_version,
        n.notice_version,
        n.publication_date,
        n.publication_date_raw,
        n.dispatch_date,
        n.dispatch_date_raw,
        n.buyer_country,
        n.buyer_country_iso,
        n.buyer_country_status,
        n.primary_cpv,
        n.primary_cpv_status,
        n.additional_cpv,
        n.change_reference_status,
        c.coverage_verified                               as source_coverage_verified,
        c.acquired_at,
        c.published_at
    from tl_work.capture c
    join tl_work.notice_capture n on n.capture_id = c.capture_id
    where c.status in ('published', 'superseded');

-- One row per change reference in a complete capture. Never joined against
-- notice_history directly in a single view: a notice's own grain there stays
-- one row regardless of how many references it carries.
create view tl_read.notice_change_reference_history as
    select
        c.source_package_id,
        c.capture_id,
        c.acquisition_ordinal,
        c.status                                          as capture_status,
        r.publication_number || '-' || r.publication_year as publication_ref,
        r.publication_year,
        r.publication_number,
        r.ordinal,
        r.value,
        r.scheme_name
    from tl_work.capture c
    join tl_work.notice_change_reference r on r.capture_id = c.capture_id
    where c.status in ('published', 'superseded');

-- Append the new status to the existing published-capture surfaces.
-- CREATE OR REPLACE VIEW only allows adding columns at the end, which is
-- exactly what both of these do: every existing column keeps its name,
-- position and type, so grants and any consumer relying on column order by
-- name survive.
create or replace view tl_read.notice as
    select
        c.source_package_id,
        n.capture_id,
        c.acquisition_ordinal,
        n.publication_number || '-' || n.publication_year as publication_ref,
        n.publication_year,
        n.publication_number,
        n.source_format,
        n.schema_version,
        n.notice_version,
        n.publication_date,
        n.publication_date_raw,
        n.dispatch_date,
        n.dispatch_date_raw,
        n.buyer_country,
        n.buyer_country_iso,
        n.buyer_country_status,
        n.primary_cpv,
        n.primary_cpv_status,
        n.additional_cpv,
        c.coverage_verified as source_coverage_verified,
        c.published_at,
        n.change_reference_status
    from tl_work.published_capture p
    join tl_work.capture c        on c.capture_id = p.capture_id
    join tl_work.notice_capture n on n.capture_id = c.capture_id;

create or replace view tl_read.distinct_notice as
    select distinct on (publication_year, publication_number)
        publication_ref,
        publication_year,
        publication_number,
        source_package_id,
        capture_id,
        acquisition_ordinal,
        source_format,
        schema_version,
        notice_version,
        publication_date,
        publication_date_raw,
        dispatch_date,
        dispatch_date_raw,
        buyer_country,
        buyer_country_iso,
        buyer_country_status,
        primary_cpv,
        primary_cpv_status,
        additional_cpv,
        source_coverage_verified,
        published_at,
        change_reference_status
    from tl_read.notice
    order by publication_year, publication_number, acquisition_ordinal desc;

-- Surface the stored contract versions so the application can compare them
-- against its own before trusting a replay (ingest._replay). The comparison
-- against the running code's projection.CONTRACT_VERSION happens in Python;
-- SQL only exposes what is stored, so there is exactly one place that
-- constant can drift out of sync with itself.
create or replace view tl_read.package_ingest_status as
    with packages as (
        select source_package_id from tl_work.capture
        union
        select source_package_id from tl_work.ingest_run
        union
        select source_package_id from tl_work.package_checkpoint
    )
    select
        p.source_package_id,
        (cp.source_package_id is not null)          as has_checkpoint,
        cp.run_id                                   as checkpoint_run_id,
        cp.capture_id                               as checkpoint_capture_id,
        cp.verification_attempt_id                  as checkpoint_attempt_id,
        cp.artifact_sha256                          as checkpoint_artifact_sha256,
        cp.notice_count                             as checkpoint_notice_count,
        cp.sealed_at                                as checkpoint_sealed_at,
        pub.capture_id                              as published_capture_id,
        coalesce(cp.capture_id = pub.capture_id, false)  as capture_still_published,
        coalesce(cap.coverage_verified, false)      as coverage_verified,
        latest.attempt_id                           as latest_attempt_id,
        latest.state                                as latest_verification_state,
        coalesce((
            cp.capture_id = pub.capture_id
            and cap.status = 'published'
            and cap.coverage_verified
            and cap.artifact_sha256 = cp.artifact_sha256
            and cap.contract_version = cp.contract_version
            and cap.member_count = cp.notice_count
            and cap.distinct_notice_count = cp.notice_count
            and cap.loaded_row_count = cp.notice_count
            and checkpoint_run.phase = 'completed'
            and checkpoint_run.source_package_id = cp.source_package_id
            and checkpoint_run.capture_id = cp.capture_id
            and checkpoint_run.verification_attempt_id = cp.verification_attempt_id
            and checkpoint_run.artifact_sha256 = cp.artifact_sha256
            and latest.attempt_id = cp.verification_attempt_id
            and latest.state = 'verified'
            and latest.artifact_sha256 = cp.artifact_sha256
            and latest.contract_version = cp.contract_version
        ) is true, false)                           as checkpoint_is_current,
        openrun.run_id                              as open_run_id,
        openrun.phase                               as open_run_phase,
        openrun.started_at                          as open_run_started_at,
        openrun.last_error                          as open_run_last_error,
        cp.contract_version                         as checkpoint_contract_version,
        cap.contract_version                        as published_contract_version
    from packages p
    left join tl_work.package_checkpoint cp on cp.source_package_id = p.source_package_id
    left join tl_work.ingest_run checkpoint_run on checkpoint_run.run_id = cp.run_id
    left join tl_work.published_capture pub on pub.source_package_id = p.source_package_id
    left join tl_work.capture cap on cap.capture_id = pub.capture_id
    left join lateral (
        select v.attempt_id, v.state, v.artifact_sha256, v.contract_version
        from tl_work.verification_attempt v
        where v.capture_id = pub.capture_id
        order by v.attempt_id desc
        limit 1
    ) latest on true
    left join lateral (
        select r.run_id, r.phase, r.started_at, r.last_error
        from tl_work.ingest_run r
        where r.source_package_id = p.source_package_id
          and r.phase not in ('completed', 'failed')
        order by r.run_id desc
        limit 1
    ) openrun on true;

grant select on
    tl_read.notice_history, tl_read.notice_change_reference_history
    to tender_ledger_reader;
