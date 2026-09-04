"""What a supported TED package identity is, and what it is allowed to cost.

Every other module reads its package numbers and derivations from here. That is
the point: before this existed the archive walker, the downloader and the Search
API client each carried their own ceilings, so the same archive was validated
against limits that differed by a factor of sixteen depending on which command
opened it, and a monthly package was refused by two of them and accepted by the
third.

This module deliberately imports nothing from the rest of the package. The
layers below build their own limit objects from these primitives, so the numbers
have one home without the identity contract depending on transport, storage or
projection code.

Two identity shapes are supported:

``daily/YYYYNNNNN``
    ``NNNNN`` is an OJ S issue ordinal, not a day of the year. The issue is a
    discrete identifier, so the source query is an equality.

``monthly/YYYY-MM``
    The month is always zero-padded, which matches TED's own ``{yyyy}_{mm}``
    archive filename convention and keeps one package from having two internal
    identities. The **derived TED package path** drops that zero
    (``monthly/2024-1``): the published download-direct page narrates a pattern
    containing ``/notice/`` that its own examples omit, and the endpoints that
    answer omit it too, so this is the observed endpoint shape rather than a
    documented one. A month has no discrete ordinal, so its query is the
    inclusive interval of its calendar days.
"""

import calendar
import dataclasses
import datetime as dt
import re
from dataclasses import dataclass

DAILY = "daily"
MONTHLY = "monthly"

_MIB = 1024 * 1024

_DAILY_ID = re.compile(r"daily/(?P<year>[0-9]{4})(?P<ojs>[0-9]{5})")
_MONTHLY_ID = re.compile(r"monthly/(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])")
_YEARS = range(1990, 2100)


class UnsupportedPackage(ValueError):
    """This package identity is not one this contract recognizes."""


@dataclass(frozen=True)
class ResourcePolicy:
    """The ceilings one kind of package may cost, in primitive values.

    These are safety ceilings, not targets: nothing approves a load by being
    under them. The time budgets are cooperative -- they are checked between
    operations, so with ``urllib`` the honest bound is a budget plus one
    operation timeout, and DNS resolution and header parsing have no hard
    wall-clock deadline.

    ``expanded_bytes`` is streamed and never written to disk, so it bounds work
    rather than storage.
    """

    # Archive
    compressed_bytes: int
    expanded_bytes: int
    member_bytes: int
    notices: int
    # Acquisition
    download_attempts: int
    download_total_bytes: int
    download_seconds: float
    chunk_bytes: int
    operation_timeout: float
    backoff_base_seconds: float
    backoff_max_seconds: float
    # Source enumeration
    api_page_size: int
    api_max_pages: int
    api_page_attempts: int
    api_response_bytes: int
    api_seconds: float

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            if getattr(self, field.name) <= 0:
                raise ValueError(f"{field.name} must be positive")
        # One acquisition may restart from byte zero, so the aggregate budget has
        # to cover every attempt or a source that keeps truncating exhausts it
        # before the attempts it is allowed.
        if self.download_total_bytes < self.download_attempts * self.compressed_bytes:
            raise ValueError(
                f"download_total_bytes {self.download_total_bytes} cannot fund"
                f" {self.download_attempts} attempts of {self.compressed_bytes} bytes"
            )
        # The walk ends on a terminal page that carries no records, so pagination
        # needs room for the notice ceiling plus that page. Without this an
        # archive could load and then be impossible to verify.
        if self.api_max_pages * self.api_page_size < self.notices + self.api_page_size:
            raise ValueError(
                f"{self.api_max_pages} pages of {self.api_page_size} cannot enumerate"
                f" {self.notices} notices and a terminal page"
            )
        if self.member_bytes > self.expanded_bytes:
            raise ValueError(
                f"member_bytes {self.member_bytes} exceeds the whole expanded archive"
            )


#: The daily flow's ceilings, unchanged: one OJ S issue is small and bounded, and
#: nothing about monthly support may relax it.
DAILY_POLICY = ResourcePolicy(
    compressed_bytes=64 * _MIB,
    expanded_bytes=512 * _MIB,
    member_bytes=8 * _MIB,
    notices=10_000,
    download_attempts=3,
    download_total_bytes=192 * _MIB,
    download_seconds=300.0,
    chunk_bytes=_MIB,
    operation_timeout=20.0,
    backoff_base_seconds=1.0,
    backoff_max_seconds=30.0,
    api_page_size=250,
    api_max_pages=50,
    api_page_attempts=3,
    api_response_bytes=4 * _MIB,
    api_seconds=300.0,
)

#: Monthly packages widen exactly eight ceilings and nothing else. The numbers
#: come from the 2020-2025 inventory that has actually been measured: 512 MiB is
#: 1.23x the largest of the 72 observed monthly headers (416,604,969 B), 150,000
#: notices is roughly 1.6x the largest month estimated from its compressed
#: bytes-per-notice rate, and 8 GiB is about 3x the largest month's expansion at
#: the only ratio ever measured (6.66x, on one mixed daily package). They must be
#: re-derived if the historical target ever grows past 2025.
MONTHLY_POLICY = dataclasses.replace(
    DAILY_POLICY,
    compressed_bytes=512 * _MIB,
    expanded_bytes=8 * 1024 * _MIB,
    member_bytes=32 * _MIB,
    notices=150_000,
    download_total_bytes=1_536 * _MIB,
    download_seconds=1_800.0,
    api_max_pages=620,
    api_seconds=1_800.0,
)


@dataclass(frozen=True)
class PackageIdentity:
    """One supported package, and everything derivable from its identity alone.

    ``url_path`` and ``destination_parts`` are built from a fullmatch of a strict
    pattern, so neither can carry separators, traversal or whitespace.
    """

    source_package_id: str
    kind: str
    url_path: str
    destination_parts: tuple[str, ...]
    query_expression: str
    policy: ResourcePolicy
    #: "220/2023" for a daily package: the value its records must carry.
    ojs_number: str | None = None
    #: Inclusive first and last calendar day, for a monthly package.
    publication_interval: tuple[dt.date, dt.date] | None = None


def package_identity(source_package_id: str) -> PackageIdentity:
    """Resolve one canonical identity, or refuse it.

    Refusing is the whole job: an arbitrary string here would become an arbitrary
    URL, an arbitrary path and an unbounded query.
    """
    daily = _DAILY_ID.fullmatch(source_package_id)
    if daily is not None:
        return _daily_identity(source_package_id, int(daily["year"]), int(daily["ojs"]))
    monthly = _MONTHLY_ID.fullmatch(source_package_id)
    if monthly is not None:
        return _monthly_identity(
            source_package_id, int(monthly["year"]), int(monthly["month"])
        )
    raise UnsupportedPackage(
        f"{source_package_id!r} is not a canonical package identity"
        " (expected daily/YYYYNNNNN or monthly/YYYY-MM)"
    )


def policy_for(source_package_id: str) -> ResourcePolicy:
    """Return conservative ceilings for a lower-level package label.

    User-facing operations validate canonical identities before they reach this
    helper. Repository-level operations may still encounter an older or manually
    created capture label; it gets the narrowest policy rather than a permissive
    one, so a wrong guess can only refuse work.
    """
    try:
        return package_identity(source_package_id).policy
    except UnsupportedPackage:
        return DAILY_POLICY


def _daily_identity(source_package_id: str, year: int, ojs: int) -> PackageIdentity:
    if year not in _YEARS or ojs < 1:
        raise UnsupportedPackage(
            f"{source_package_id!r} carries an implausible issue {ojs} of {year}"
        )
    ordinal = source_package_id.partition("/")[2]
    return PackageIdentity(
        source_package_id=source_package_id,
        kind=DAILY,
        url_path=f"{DAILY}/{ordinal}",
        destination_parts=("packages", DAILY, f"{ordinal}.tar.gz"),
        query_expression=f"OJ = {ojs}/{year}",
        policy=DAILY_POLICY,
        ojs_number=f"{ojs}/{year}",
    )


def _monthly_identity(source_package_id: str, year: int, month: int) -> PackageIdentity:
    if year not in _YEARS:
        raise UnsupportedPackage(
            f"{source_package_id!r} carries an implausible month {month} of {year}"
        )
    first = dt.date(year, month, 1)
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    return PackageIdentity(
        source_package_id=source_package_id,
        kind=MONTHLY,
        # The observed endpoint drops the month's leading zero; the internal
        # identity and the local filename keep it.
        url_path=f"{MONTHLY}/{year}-{month}",
        destination_parts=("packages", MONTHLY, f"{year}-{month:02d}.tar.gz"),
        query_expression=f"PD>={first:%Y%m%d} AND PD<={last:%Y%m%d}",
        policy=MONTHLY_POLICY,
        publication_interval=(first, last),
    )
