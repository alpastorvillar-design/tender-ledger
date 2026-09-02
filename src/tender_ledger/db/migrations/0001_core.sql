-- Tender Ledger core schema: transactional package loading with a clean
-- separation between internal working tables and the consumption surface.
--
-- tl_work  : capture / batch / notice / published-pointer tables. Internal.
-- tl_read  : views only. The reader role can touch nothing else.
--
-- Baseline is an unpartitioned notice table. Publication year stays in the
-- natural key so a later annual-partitioned table keeps the same identity and
-- its uniqueness can be proven equivalent before any migration.

create schema if not exists tl_work;
create schema if not exists tl_read;

revoke all on schema tl_work from public;

create table tl_work.schema_migrations (
    version    text primary key,
    applied_at timestamptz not null default now()
);

-- Deterministic, source-independent acquisition order. A capture's ordinal is
-- assigned when its identity is first persisted, before any notice row is
-- loaded, and never reflects official publication or version order.
create sequence tl_work.acquisition_seq;

create table tl_work.capture (
    capture_id            bigint generated always as identity primary key,
    source_package_id     text    not null,
    acquisition_ordinal   bigint  not null default nextval('tl_work.acquisition_seq'),
    artifact_sha256       text    not null,
    artifact_bytes        bigint  not null check (artifact_bytes >= 0),
    contract_version      text    not null,
    member_count          integer check (member_count >= 0),
    distinct_notice_count integer check (distinct_notice_count >= 0),
    loaded_row_count      integer check (loaded_row_count >= 0),
    status                text    not null default 'acquiring'
        check (status in ('acquiring', 'loading', 'loaded', 'published', 'superseded', 'failed')),
    failure_reason        text,
    -- "artifact fully loaded" is not "coverage verified against TED". The API
    -- verifier is a later slice; until it runs this stays false.
    coverage_verified     boolean not null default false,
    acquired_at           timestamptz not null default now(),
    published_at          timestamptz,
    unique (source_package_id, acquisition_ordinal)
);

create index capture_package_status_idx on tl_work.capture (source_package_id, status);

-- Only committed batches have a row here: a batch that fails rolls back its rows
-- and this record together.
create table tl_work.capture_batch (
    capture_id    bigint  not null references tl_work.capture (capture_id),
    batch_ordinal integer not null check (batch_ordinal >= 0),
    member_lo     text    not null,
    member_hi     text    not null,
    row_count     integer not null check (row_count >= 0),
    committed_at  timestamptz not null default now(),
    primary key (capture_id, batch_ordinal)
);

create table tl_work.notice_capture (
    capture_id           bigint  not null references tl_work.capture (capture_id),
    publication_year     integer not null,
    publication_number   bigint  not null check (publication_number > 0),
    batch_ordinal        integer not null,
    source_format        text    not null check (source_format in ('legacy', 'eforms')),
    schema_version       text    not null,
    source_filename      text    not null,
    notice_uuid          text,
    notice_version       text,
    publication_date     date    not null,
    publication_date_raw text    not null,
    dispatch_date        date,
    dispatch_date_raw    text,
    buyer_country        text,
    buyer_country_iso    text,
    buyer_country_status text    not null check (buyer_country_status in ('present', 'absent', 'not_applicable')),
    primary_cpv          text,
    primary_cpv_status   text    not null check (primary_cpv_status in ('present', 'absent', 'not_applicable')),
    additional_cpv       text[]  not null default '{}',
    -- Canonical identity is unique within a capture. A package that repeats a
    -- publication number makes its batch violate this and the capture fails.
    primary key (capture_id, publication_year, publication_number)
);

create index notice_capture_pubdate_idx on tl_work.notice_capture (publication_date);
create index notice_capture_country_idx on tl_work.notice_capture (buyer_country_iso);

-- The one row that makes a capture visible for a source package. Publishing
-- swaps capture_id in a single transaction, so readers never see a half-built
-- capture and always keep the previous complete one until the swap commits.
create table tl_work.published_capture (
    source_package_id text    primary key,
    capture_id        bigint  not null references tl_work.capture (capture_id),
    published_at      timestamptz not null default now()
);

-- One row per published notice, per source package. Daily and monthly packages
-- overlap, so this view can repeat a canonical identity across packages;
-- tl_read.distinct_notice collapses that.
create view tl_read.notice as
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
        c.published_at
    from tl_work.published_capture p
    join tl_work.capture c        on c.capture_id = p.capture_id
    join tl_work.notice_capture n on n.capture_id = p.capture_id;

-- One row per canonical identity across every published package, resolved to the
-- most recently acquired capture. This is the honest distinct-notice surface.
create view tl_read.distinct_notice as
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
        published_at
    from tl_read.notice
    order by publication_year, publication_number, acquisition_ordinal desc;

-- Operational surface: every capture and whether it is the published one. It
-- exposes no notice rows, so a partial capture's contents stay internal.
create view tl_read.capture_status as
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
        c.published_at
    from tl_work.capture c
    left join tl_work.published_capture p on p.capture_id = c.capture_id;

-- Read-only role. Membership in views only; no rights on tl_work. Views run with
-- the owner's privileges (security_invoker defaults off), so the reader can see
-- published notices through the view but cannot select the base tables.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'tender_ledger_reader') then
        create role tender_ledger_reader nologin;
    end if;
end
$$;

revoke all on schema tl_work from tender_ledger_reader;
grant usage on schema tl_read to tender_ledger_reader;
grant select on tl_read.notice, tl_read.distinct_notice, tl_read.capture_status
    to tender_ledger_reader;
