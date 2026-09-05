# Measured rehearsal (M3d)

Status: a real rehearsal ran against a disposable database and produced
measured results, but it did **not** complete as planned. Two of the five
`manifests/m3-pilot.json` identities are structurally incompatible with the
current archive walker, and a third fails on an eForms notice subtype the
projection does not recognize. Only one monthly package and the already-known
daily package actually loaded. The 100,000-notice rehearsal gate and the
million-notice minimum in [scale-and-sql.md](scale-and-sql.md) remain unmet.

Commit at the start of this rehearsal: `04513b322e8a932369be783fc33196403b3566a9`.
Branch: `feat/measured-rehearsal`. Database: an exclusive `tender_ledger_m3_rehearsal`,
created from `template0` and migrated to `0005`; the development database was
never touched (verified identical before and after: 1 capture, 2,967 notices,
1 run, 1 checkpoint).

## Environment

Windows 11 Pro, AMD Ryzen 7 7800X3D (16 logical processors host-side), 31.1 GiB
RAM. PostgreSQL 17.11 in the project's own Compose stack, limited to 2 CPU /
2 GiB as configured. Python 3.14.3. Host free disk stayed above 636 GiB
throughout (gate: at least 100 GiB); the PostgreSQL data directory and the
Docker WSL virtual disk each grew by under 100 MiB -- two overlapping views
of local storage, both nowhere near the 150 GiB ceiling.

## Finding: two real monthly-package shapes, neither fully anticipated

`monthly/2020-01` transferred completely but failed archive validation.
A bounded 4 MiB ranged read of `monthly/2024-01` established the same outer
layout without claiming full-file validation. Each has a top-level entry for
a nested `.tar.gz` file, one per publication day (for example
`01/20200131_2020022.tar.gz`; the sampled 2020 member had a gzip header).
The current walker (`packages.py`) only recognizes flat XML
members; a member that is not a regular XML file is an unconditional,
archive-level rejection, by design (the same defense that rejects a symlink or
an oversized member also rejects a nested archive it cannot recurse into).

`monthly/2020-02` downloaded and loaded successfully: its members are flat
XML files inside a per-day *directory* (`20200206_026/058164_2020.xml`), which
the walker accepts because it only inspects the filename leaf
(`PurePosixPath(member_name).name`), not the path depth.

`monthly/2023-11` downloaded but failed at the member level: it contains at
least one eForms notice whose root,
`{http://data.europa.eu/p27/eforms-business-registration-information-notice/1}BusinessRegistrationInformationNotice`,
is not in the three roots `EFORMS_ROOTS` recognizes (`ContractNotice`,
`ContractAwardNotice`, `PriorInformationNotice`). This is a data-shape gap
independent of the nested-archive one above.

`daily/202300220` is unaffected -- flat XML, as in every prior milestone.

| Identity | Real shape observed | Loadable today |
| --- | --- | --- |
| `monthly/2020-01` | nested `.tar.gz` per day | No -- archive-level |
| `monthly/2020-02` | flat XML in a per-day directory | Yes |
| `monthly/2023-11` | flat XML, includes an unrecognized eForms root | No -- member-level |
| `monthly/2024-01` | outer `.tar.gz` member per day observed via a 4 MiB ranged read | No -- archive-level |
| `daily/202300220` | flat XML | Yes (already known) |

Neither failure corrupts capture state: `download_package` never persists
bytes it cannot validate, so nothing reached `data/` for the three
incompatible identities and no capture or checkpoint exists for them. The
failed manifest attempt did persist one resumable `monthly/2020-01` run at
`phase = 'starting'`, including its bounded error; the other two identities
have no run.

Running `ingest-manifest` against the unmodified pilot manifest reproduces
this cleanly and stops at the first entry, `monthly/2020-01`, exactly as
designed: `status: "failed"`, zero captures, zero checkpoints, the run left
resumable at `phase: "starting"`. Because the manifest's recovery model is a
hard stop at the first non-current package, and the fixed order puts an
archive-incompatible identity first, packages 2-5 -- including the two
identities that *do* load -- are never attempted through the manifest.

## Real ingest: interrupt, resume, full replay

Given the above, the recovery demonstration ran against `monthly/2020-02`
directly (`ingest`, not `ingest-manifest`) rather than the fixed five-package
order, since no ordering of the pilot manifest lets a compatible package be
reached before the first incompatible one.

1. **Interrupted**: after 10 confirmed batches (5,000 of 50,522 notices),
   another connection ran `pg_terminate_backend` against the exact backend
   PID correlated by a distinct `application_name` set only for this
   process's connection. The capture stayed `loading` (never `published`),
   the run stayed at `artifact_ready` (resumable), and no checkpoint existed.
2. **Resumed**: re-running the same command reused the same run and capture,
   skipped batches 0-9 (their `committed_at` timestamps predate the resume by
   about a minute), loaded the remaining 92 batches, verified all 50,522
   notices against the live TED Search API (204 pages, zero differences,
   zero duplicates), and sealed the checkpoint.
3. **Replayed**: a third run made zero HTTP requests of any kind and created
   zero new captures, batches, attempts or checkpoints. It spent about 26 s
   locally re-hashing and walking the complete 139 MB archive before trusting
   the stored checkpoint.

`daily/202300220` loaded, verified and checkpointed the same way, for
completeness. The two loaded packages cover non-overlapping periods (a
November 2023 issue and February 2020), so a cross-package overlap audit
between them correctly reports zero shared identities -- a negative control,
not the positive-overlap case the original `daily/202300220` +
`monthly/2023-11` pairing was meant to exercise, since 2023-11 could not load.

## Benchmark

Ran against the two loaded packages: **53,489 real notices** (50,522 +
2,967) -- far below the ~228,000 the five-package plan intended, and well
below the 1,000,000-notice minimum gate. `effective_cache_size` was fixed to
1536 MB in every session; `VACUUM (ANALYZE)` ran before the baseline and
`ANALYZE` again after adding the candidate indexes in
[`queries/benchmark_candidate_indexes.sql`](../queries/benchmark_candidate_indexes.sql).
Every workload's checksum and row count are identical before and after.

| Workload | Baseline median (ms) | Candidate-index median (ms) | Plan changed? |
| --- | ---: | ---: | --- |
| monthly_notice_counts | 53.5 | 43.3 | Seq Scan → Index Scan |
| latest_capture_per_publication | 158.8 | 162.3 | Seq Scan → Index Scan (no net gain) |
| official_change_references | 186.5 | 135.2 | Seq Scan → Index Only Scan |
| acquisition_cutoff_state | 143.2 | 139.3 | unchanged |
| monthly_coverage_calendar | 0.7 | 0.7 | unchanged |
| cross_package_overlap_audit | 156.5 | 158.9 | unchanged |

Two workloads improved by 19% and 28% after a plan change. A third plan also
switched to an index scan while its median became 2% slower; the other three
medians moved by less than 4%. At 53k rows the whole working set sits
comfortably inside 1536 MB of cache, so these measurements do not justify an
index decision and say little about behavior at the 1,000,000-row minimum
gate. The protocol must be repeated there, exactly as
[scale-and-sql.md](scale-and-sql.md) anticipated. Annual
partitioning was not evaluated this round: real data covers only two
populated months, too few to say anything honest about partition pruning.

Reusable, portable, fixture-tested harness:
[`scripts/benchmark_workloads.py`](../scripts/benchmark_workloads.py).

## What this rehearsal does not establish

- The 100,000-notice engineering gate and the 1,000,000-notice minimum gate
  in [scale-and-sql.md](scale-and-sql.md) -- 53,489 real notices loaded, not
  100,000 or more.
- That the pilot manifest can be run end to end -- it cannot, against three
  of its five identities, without a code change this rehearsal did not make.
- Benchmark conclusions at the scale the minimum gate requires.
- A decision on the 2020-2025 historical target -- that decision needs a
  monthly-package walker that handles nested day-archives, a wider eForms
  root allow-list, and a fresh survey of how common each real shape is across
  the archive, none of which this rehearsal implements.

The top-level [README](../README.md) and
[scale-and-sql.md](scale-and-sql.md) link this incomplete rehearsal while
retaining the unmet scale gates.
