-- Workload 6: cross-package overlap audit.
--
-- Question: between two published packages, which canonical identities are
-- exclusive to one of them, and among the identities both hold, do their
-- published fields actually agree?
-- Grain: one row per finding -- an identity exclusive to A, an identity
-- exclusive to B, or a shared identity whose published content differs.
-- A package with no findings of a given kind contributes no row of that kind;
-- an exact, total overlap produces no rows at all.
-- Source and filters: tl_read.notice, restricted to source_package_id in
-- (:'package_a', :'package_b').
-- NULL/absent semantics: not applicable -- this workload reports differences,
-- not missing-value handling; a field genuinely absent on both sides (e.g.
-- both 'absent' buyer_country_status) is not a difference.
-- Order / tie-break: finding, then publication_ref -- the three sections
-- never overlap in identity, so no further tie-break is needed.
-- Parameters: :'package_a', :'package_b' -- two source_package_id values,
-- quoted. From psql: `-v package_a=daily/202300220 -v package_b=monthly/2023-11`.
-- From Python, as the tests do: replace the literal tokens `:'package_a'` and
-- `:'package_b'` with quoted identifiers before executing.
-- Does not let you claim: that :'package_a' is a subset of :'package_b', or
-- the reverse -- both directions are checked with NOT EXISTS, so a daily
-- package inside a monthly one is verified, never assumed. It also does not
-- claim the two packages describe the same real-world notice version; a
-- content difference here is a difference in what this system has stored for
-- that identity from each package, which is exactly what the daily/monthly
-- overlap hypothesis (see the M3 planning contract) needs to be checked, not
-- assumed.
with a as (
    select * from tl_read.notice where source_package_id = :'package_a'
),
b as (
    select * from tl_read.notice where source_package_id = :'package_b'
),
only_in_a as (
    select a.publication_ref
    from a
    where not exists (select 1 from b where b.publication_ref = a.publication_ref)
),
only_in_b as (
    select b.publication_ref
    from b
    where not exists (select 1 from a where a.publication_ref = b.publication_ref)
),
-- EXCEPT compares whole rows: a shared identity whose fields are byte-for-byte
-- identical on both sides produces the same row from a and from b, so it is
-- removed; any field difference leaves a's version of that row here.
content_differs as (
    select
        a.publication_ref, a.source_format, a.publication_date, a.dispatch_date,
        a.buyer_country_iso, a.buyer_country, a.primary_cpv, a.additional_cpv,
        a.change_reference_status
    from a
    where a.publication_ref in (select publication_ref from b)
    except
    select
        b.publication_ref, b.source_format, b.publication_date, b.dispatch_date,
        b.buyer_country_iso, b.buyer_country, b.primary_cpv, b.additional_cpv,
        b.change_reference_status
    from b
    where b.publication_ref in (select publication_ref from a)
)
select 'only_in_a' as finding, publication_ref, null as detail
from only_in_a
union all
select 'only_in_b', publication_ref, null
from only_in_b
union all
select
    'content_differs',
    d.publication_ref,
    format(
        'a: format=%s date=%s country=%s cpv=%s refs=%s',
        d.source_format, d.publication_date,
        coalesce(d.buyer_country_iso, d.buyer_country), d.primary_cpv,
        d.change_reference_status
    )
from content_differs d
order by finding, publication_ref;
