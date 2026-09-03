-- Persist TED Search API coverage verification as evidence, not as a flag.
--
-- "The archive loaded completely" and "the source agrees this is the whole
-- issue" are different claims. 0001 kept the second one as a boolean that
-- nothing could set. This adds the attempt history behind it: every run of the
-- verifier writes a row before touching the network and closes it with what it
-- observed, so a later false is still backed by the earlier true's evidence.
--
-- Forward-only. Existing captures, notices, batches and the published pointer
-- are untouched; captures loaded before this migration simply have no attempts.

create table tl_work.verification_attempt (
    attempt_id       bigint generated always as identity primary key,
    capture_id       bigint not null references tl_work.capture (capture_id),
    -- The capture contract as it stood when the attempt started. A capture's
    -- identity never changes, so this makes each attempt self-describing
    -- evidence rather than a row that only means something joined to today's
    -- capture table.
    artifact_sha256  text   not null,
    contract_version text   not null,
    verifier_version text   not null,
    -- The query is derived from the package identity, never supplied, so the
    -- recorded expression is the whole of what was asked for.
    query_text       text   not null,
    query_scope      text   not null,
    state            text   not null check (
        state in ('in_progress', 'verified', 'mismatch', 'unavailable', 'empty_unconfirmed')
    ),
    reason           text,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,

    -- Counts are nullable because unknown and zero are different answers: an
    -- attempt that never reached the source knows nothing about its totals.
    announced_total      integer check (announced_total >= 0),
    api_record_count     integer check (api_record_count >= 0),
    api_distinct_count   integer check (api_distinct_count >= 0),
    api_duplicate_count  integer check (api_duplicate_count >= 0),
    local_distinct_count integer check (local_distinct_count >= 0),
    only_local_count     integer check (only_local_count >= 0),
    only_api_count       integer check (only_api_count >= 0),
    -- Bounded illustrations of the difference, never the difference itself.
    only_local_sample    text[],
    only_api_sample      text[],
    pages_fetched        integer check (pages_fetched >= 0),
    http_attempts        integer check (http_attempts >= 0),
    api_keys_sha256      text,
    key_digest_recipe    text,

    constraint verification_attempt_finish_time check (
        (state = 'in_progress') = (finished_at is null)
    ),
    constraint verification_attempt_sample_bounds check (
        coalesce(array_length(only_local_sample, 1), 0) <= 20
        and coalesce(array_length(only_api_sample, 1), 0) <= 20
    ),
    -- 'verified' is a claim about the source, so the row has to carry the
    -- evidence for it: a complete, non-empty, duplicate-free enumeration whose
    -- identifier set matched the capture exactly. No code path can record the
    -- claim without the numbers that justify it.
    constraint verification_attempt_verified_evidence check (
        state <> 'verified' or ((
            announced_total > 0
            and api_duplicate_count = 0
            and api_record_count = announced_total
            and api_distinct_count = announced_total
            and local_distinct_count = announced_total
            and only_local_count = 0
            and only_api_count = 0
            and api_keys_sha256 is not null
        ) is true)
    ),
    -- An empty result is only ever "unconfirmed": both sides have to be empty,
    -- and zero on its own still does not establish that the issue was published.
    constraint verification_attempt_empty_evidence check (
        state <> 'empty_unconfirmed' or ((
            announced_total = 0
            and api_record_count = 0
            and api_distinct_count = 0
            and local_distinct_count = 0
        ) is true)
    )
);

create index verification_attempt_capture_idx
    on tl_work.verification_attempt (capture_id, attempt_id desc);

-- Attempt history for the reader role. It carries counts, differences and the
-- key digest; iteration tokens and response bodies are never stored, so there is
-- nothing here to withhold.
create view tl_read.verification_attempt as
    select
        a.attempt_id,
        a.capture_id,
        c.source_package_id,
        c.acquisition_ordinal,
        a.state,
        a.reason,
        a.started_at,
        a.finished_at,
        a.query_text,
        a.query_scope,
        a.verifier_version,
        a.artifact_sha256,
        a.contract_version,
        a.announced_total,
        a.api_record_count,
        a.api_distinct_count,
        a.api_duplicate_count,
        a.local_distinct_count,
        a.only_local_count,
        a.only_api_count,
        a.only_local_sample,
        a.only_api_sample,
        a.pages_fetched,
        a.http_attempts,
        a.api_keys_sha256,
        a.key_digest_recipe
    from tl_work.verification_attempt a
    join tl_work.capture c on c.capture_id = a.capture_id;

-- Extend the operational view with the latest attempt per capture. Existing
-- columns and the one-row-per-capture grain are unchanged; the lateral cannot
-- multiply or drop rows. coverage_verified stays what it always was -- the
-- current standing of the most recent attempt -- and the state column says which
-- kind of answer produced it.
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
        c.batch_size,
        latest.attempt_id  as verification_attempt_id,
        latest.state       as verification_state,
        latest.started_at  as verification_started_at,
        latest.finished_at as verification_finished_at
    from tl_work.capture c
    left join tl_work.published_capture p on p.capture_id = c.capture_id
    left join lateral (
        select v.attempt_id, v.state, v.started_at, v.finished_at
        from tl_work.verification_attempt v
        where v.capture_id = c.capture_id
        order by v.attempt_id desc
        limit 1
    ) latest on true;

grant select on tl_read.verification_attempt to tender_ledger_reader;
