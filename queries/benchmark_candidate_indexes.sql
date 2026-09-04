-- Candidate indexes evaluated by scripts/benchmark_workloads.py for the M3d
-- rehearsal. This file is not a migration: it is applied only to a disposable
-- benchmark database (never to `tender_ledger`, and never automatically by
-- `db upgrade`), and only after a baseline EXPLAIN showed a plan step these
-- columns could actually help -- a sequential or sort step over
-- tl_work.notice_capture keyed by canonical identity, or a filter on
-- tl_work.capture.acquisition_ordinal that the existing
-- capture_package_status_idx (source_package_id, status) cannot serve.
--
-- Workloads 1-4 all group, join or window-partition by the canonical
-- (publication_year, publication_number) identity; today the only index that
-- leads with either column is the notice_capture primary key, which leads
-- with capture_id instead. Workload 4 additionally filters
-- tl_work.capture.acquisition_ordinal, which has no index at all.
--
-- Whether either index is worth keeping is an empirical question the
-- benchmark answers by comparing median/p95 latency and EXPLAIN plans before
-- and after, on identical, checksum-verified results. Neither index is
-- adopted into a numbered migration here.

create index if not exists notice_capture_pub_identity_idx
    on tl_work.notice_capture (publication_year, publication_number, capture_id);

create index if not exists capture_acquisition_ordinal_idx
    on tl_work.capture (acquisition_ordinal);
