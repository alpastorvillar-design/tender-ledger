"""A versioned manifest of packages a sequential backfill run will process.

The document is validated strictly, and every check here runs before any
network or database access: an invalid manifest must never open a connection
or start a download. Strict schemas -- no unknown top-level or per-entry key
is tolerated -- are what keeps a manifest from ever carrying a URL, an output
path or a credential: those are not fields this format has, so one present in
a document is rejected the same way an unknown key would be.

The per-entry counts, bytes, observation date and purpose are informative
planning metadata only. Nothing in this module or in the runner that consumes
a ``Manifest`` uses them to approve, reject, end or skip a package: the
runner's decisions come only from what ``ingest_package`` reports about the
real artifact, capture, verification and checkpoint.
"""

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from .package_contract import UnsupportedPackage, package_identity

#: The only manifest schema this runner accepts. A document naming any other
#: value is refused rather than guessed at.
MANIFEST_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({"manifest_version", "packages"})
_ENTRY_KEYS = frozenset({
    "order",
    "source_package_id",
    "notice_count_observed",
    "compressed_bytes_observed",
    "observed_at",
    "purpose",
})


class ManifestError(ValueError):
    """The manifest is not a document the runner may execute."""


@dataclass(frozen=True)
class ManifestEntry:
    """One package the manifest names, plus the planning metadata beside it.

    ``notice_count_observed``, ``compressed_bytes_observed``, ``observed_at``
    and ``purpose`` are carried through only to be echoed in a report; they are
    never compared against what a run actually observes.
    """

    order: int
    source_package_id: str
    notice_count_observed: int
    compressed_bytes_observed: int
    observed_at: dt.date
    purpose: str


@dataclass(frozen=True)
class Manifest:
    manifest_version: int
    entries: tuple[ManifestEntry, ...]


def load_manifest(path: Path) -> Manifest:
    """Read and validate a manifest file. Raises before any HTTP or database use."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"Unreadable manifest {path}: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Invalid JSON in manifest {path}: {exc}") from exc
    return parse_manifest(document)


def parse_manifest(document: object) -> Manifest:
    """Validate an already-decoded manifest document.

    Every rejection happens here, before a caller ever reaches network or
    database code: an unknown version, an extra or missing key at either
    level, an empty or non-list ``packages``, a non-canonical identity, a
    duplicate identity, an ``order`` that does not match its position, and a
    wrong type anywhere are all refused.
    """
    if not isinstance(document, dict):
        raise ManifestError("A manifest must be a JSON object")
    _reject_unknown_keys(document, _TOP_LEVEL_KEYS, "manifest")

    version = document["manifest_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != MANIFEST_VERSION:
        raise ManifestError(
            f"Unsupported manifest_version {version!r}; expected {MANIFEST_VERSION}"
        )

    packages = document["packages"]
    if not isinstance(packages, list) or len(packages) == 0:
        raise ManifestError("packages must be a non-empty array")

    entries = tuple(
        _parse_entry(position, item) for position, item in enumerate(packages, start=1)
    )
    seen: set[str] = set()
    for entry in entries:
        if entry.source_package_id in seen:
            raise ManifestError(f"Duplicate package identity: {entry.source_package_id!r}")
        seen.add(entry.source_package_id)

    return Manifest(manifest_version=version, entries=entries)


def _parse_entry(position: int, item: object) -> ManifestEntry:
    if not isinstance(item, dict):
        raise ManifestError(f"Entry {position} must be a JSON object")
    _reject_unknown_keys(item, _ENTRY_KEYS, f"entry {position}")

    order = item["order"]
    if not isinstance(order, int) or isinstance(order, bool) or order != position:
        raise ManifestError(f"Entry {position} has order {order!r}, expected {position}")

    source_package_id = item["source_package_id"]
    if not isinstance(source_package_id, str):
        raise ManifestError(f"Entry {position} source_package_id must be a string")
    try:
        package_identity(source_package_id)
    except UnsupportedPackage as exc:
        raise ManifestError(f"Entry {position}: {exc}") from exc

    notice_count = _non_negative_int(
        item["notice_count_observed"], position, "notice_count_observed"
    )
    compressed_bytes = _non_negative_int(
        item["compressed_bytes_observed"], position, "compressed_bytes_observed"
    )

    observed_at = item["observed_at"]
    if not isinstance(observed_at, str):
        raise ManifestError(f"Entry {position} observed_at must be a string")
    try:
        observed_date = dt.date.fromisoformat(observed_at)
    except ValueError as exc:
        raise ManifestError(
            f"Entry {position} observed_at is not an ISO date: {observed_at!r}"
        ) from exc

    purpose = item["purpose"]
    if not isinstance(purpose, str) or not purpose.strip():
        raise ManifestError(f"Entry {position} purpose must be a non-empty string")

    return ManifestEntry(
        order=order,
        source_package_id=source_package_id,
        notice_count_observed=notice_count,
        compressed_bytes_observed=compressed_bytes,
        observed_at=observed_date,
        purpose=purpose,
    )


def _non_negative_int(value: object, position: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestError(f"Entry {position} {field} must be a non-negative integer")
    return value


def _reject_unknown_keys(document: dict, allowed: frozenset[str], label: str) -> None:
    extra = sorted(set(document) - allowed)
    missing = sorted(allowed - set(document))
    if extra or missing:
        raise ManifestError(f"{label} keys: extra={extra} missing={missing}")
