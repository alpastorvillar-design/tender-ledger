-- Ingest runs and the package checkpoint: what lets a workflow call a package
-- processed.
--
-- The filesystem and PostgreSQL do not share a transaction, so a run records
-- what it has durably achieved -- the validated artifact, the capture it is
-- loading into, the verification attempt it is relying on -- and each of those
-- is committed before the next step starts. A crash therefore leaves evidence
-- to resume from rather than a gap to guess at.
--
-- The checkpoint is not a boolean and not a MAX(date) watermark: the unit here
-- is one package, and the row names the run, the capture, the artifact checksum
-- and contract, and the exact verification attempt that justified it. Whether
-- that checkpoint is still *current* is derived in tl_read.package_ingest_status
-- from the published pointer and the capture's latest attempt, so a later
-- re-acquisition or a failed check retires it without anyone rewriting a flag.
--
-- Forward-only. Existing captures, notices, batches, the published pointer and
-- the verification history are untouched; a package verified before this
-- migration has no run and no checkpoint, because no download ever happened.

-- Foreign key targets. capture_id and attempt_id are already primary keys, so
-- these add no uniqueness; they exist so a referencing row can be forced to
-- agree with the *rest* of the referenced row -- the package a capture belongs
-- to, the artifact it was taken from, the capture an attempt checked. A CHECK
-- constraint cannot see another table; these can.
alter table tl_work.capture
    add constraint capture_package_key unique (capture_id, source_package_id);
alter table tl_work.capture
    add constraint capture_artifact_key
    unique (capture_id, source_package_id, artifact_sha256, contract_version);
alter table tl_work.verification_attempt
    add constraint verification_attempt_capture_key unique (attempt_id, capture_id);

create table tl_work.ingest_run (
    run_id            bigint generated always as identity primary key,
    source_package_id text    not null,
    -- Which attempt at this package this run is. Acquisition order within the
    -- package, assigned under its advisory lock; unrelated to publication order.
    attempt_ordinal   integer not null check (attempt_ordinal > 0),
    -- The last thing that is durably true, not a copy of the loader's or the
    -- verifier's own state machine. Those two remain authoritative about the
    -- capture and the attempt; this says how far the flow got.
    phase             text    not null default 'starting' check (phase in (
        'starting', 'artifact_ready', 'capture_published', 'source_verified',
        'completed', 'failed'
    )),
    -- Relative to the data root, so a checkout on another machine can still
    -- read the row. Archive bytes, HTTP bodies and tokens are never stored.
    artifact_path     text,
    artifact_sha256   text,
    artifact_bytes    bigint check (artifact_bytes >= 0),
    capture_id        bigint,
    verification_attempt_id bigint,
    last_error        text,
    started_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    completed_at      timestamptz,

    unique (source_package_id, attempt_ordinal),
    foreign key (capture_id, source_package_id)
        references tl_work.capture (capture_id, source_package_id),
    foreign key (verification_attempt_id, capture_id)
        references tl_work.verification_attempt (attempt_id, capture_id),

    constraint ingest_run_completion_time check (
        (phase = 'completed') = (completed_at is not null)
    ),
    -- 'failed' is terminal: no later run resumes it, so it has to say why.
    -- A recoverable interruption keeps its phase and also records the error.
    constraint ingest_run_failure_reason check (
        phase <> 'failed' or last_error is not null
    ),
    -- Each phase has to carry the evidence that produced it. NULL is unknown,
    -- and unknown never satisfies a requirement.
    constraint ingest_run_artifact_evidence check (
        phase in ('starting', 'failed') or ((
            artifact_path is not null
            and artifact_sha256 is not null
            and artifact_bytes is not null
        ) is true)
    ),
    constraint ingest_run_capture_evidence check (
        phase in ('starting', 'artifact_ready', 'failed') or capture_id is not null
    ),
    constraint ingest_run_verification_evidence check (
        phase in ('starting', 'artifact_ready', 'capture_published', 'failed')
        or verification_attempt_id is not null
    )
);

create index ingest_run_package_idx on tl_work.ingest_run (source_package_id, run_id desc);

-- One row per package: the most recent sealed checkpoint. Superseded
-- checkpoints are not kept here -- the runs that produced them are the history,
-- and keeping two records of the same fact invites them to disagree.
create table tl_work.package_checkpoint (
    source_package_id       text    primary key,
    run_id                  bigint  not null references tl_work.ingest_run (run_id),
    capture_id              bigint  not null,
    verification_attempt_id bigint  not null,
    artifact_sha256         text    not null,
    contract_version        text    not null,
    notice_count            integer not null check (notice_count >= 0),
    sealed_at               timestamptz not null default now(),

    -- The capture must belong to this package and be the one taken from this
    -- artifact under this contract, and the attempt must be an attempt on that
    -- same capture. Currency -- whether the capture is still published and that
    -- attempt is still its latest -- is a condition between tables that changes
    -- over time, so it is derived in the view below and re-checked inside the
    -- sealing transaction, not asserted by a constraint.
    foreign key (capture_id, source_package_id, artifact_sha256, contract_version)
        references tl_work.capture
            (capture_id, source_package_id, artifact_sha256, contract_version),
    foreign key (verification_attempt_id, capture_id)
        references tl_work.verification_attempt (attempt_id, capture_id)
);

-- Run history for the reader role: phases, errors and what each run referenced.
create view tl_read.ingest_run as
    select
        r.run_id,
        r.source_package_id,
        r.attempt_ordinal,
        r.phase,
        r.artifact_path,
        r.artifact_sha256,
        r.artifact_bytes,
        r.capture_id,
        c.acquisition_ordinal,
        c.status as capture_status,
        r.verification_attempt_id,
        v.state  as verification_state,
        r.last_error,
        r.started_at,
        r.updated_at,
        r.completed_at
    from tl_work.ingest_run r
    left join tl_work.capture c on c.capture_id = r.capture_id
    left join tl_work.verification_attempt v on v.attempt_id = r.verification_attempt_id;

-- Ingest standing per package: the checkpoint that was sealed (history) and
-- whether it still describes the package (currency), plus any run still in
-- flight. Every package this system has captured appears, so one loaded by hand
-- shows an absent checkpoint instead of disappearing from the report.
--
-- Currency is evidence in the database. It cannot see the filesystem: the
-- ingest command validates the artifact bytes separately before replaying a
-- checkpoint, and a missing file makes it re-acquire rather than replay.
create view tl_read.package_ingest_status as
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
            and latest.attempt_id = cp.verification_attempt_id
            and latest.state = 'verified'
        ) is true, false)                           as checkpoint_is_current,
        openrun.run_id                              as open_run_id,
        openrun.phase                               as open_run_phase,
        openrun.started_at                          as open_run_started_at,
        openrun.last_error                          as open_run_last_error
    from packages p
    left join tl_work.package_checkpoint cp on cp.source_package_id = p.source_package_id
    left join tl_work.published_capture pub on pub.source_package_id = p.source_package_id
    left join tl_work.capture cap on cap.capture_id = pub.capture_id
    left join lateral (
        select v.attempt_id, v.state
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

grant select on tl_read.ingest_run, tl_read.package_ingest_status
    to tender_ledger_reader;
