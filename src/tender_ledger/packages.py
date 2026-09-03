"""Inspect bounded TED archives without extracting their members to disk."""

import gzip
import hashlib
import re
import tarfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .package_contract import DAILY_POLICY, ResourcePolicy, policy_for


class PackageError(ValueError):
    """An archive violates the supported source contract or resource limits."""


@dataclass(frozen=True, order=True)
class NoticeKey:
    year: int
    number: int

    @classmethod
    def parse(cls, value: str) -> "NoticeKey":
        match = re.fullmatch(r"([0-9]{1,8})[-_]([1-9][0-9]{3})(?:\.xml)?", value)
        if match is None or int(match[1]) == 0:
            raise PackageError(f"Invalid publication reference: {value!r}")
        return cls(year=int(match[2]), number=int(match[1]))


@dataclass(frozen=True)
class Limits:
    """This layer's view of a package's resource policy.

    The production values come from :mod:`package_contract`, so a daily and a
    monthly archive are validated against ceilings that are decided in one place.
    Tests construct small limits explicitly.
    """

    compressed_bytes: int = DAILY_POLICY.compressed_bytes
    expanded_bytes: int = DAILY_POLICY.expanded_bytes
    member_bytes: int = DAILY_POLICY.member_bytes
    notices: int = DAILY_POLICY.notices

    def __post_init__(self) -> None:
        values = (self.compressed_bytes, self.expanded_bytes, self.member_bytes, self.notices)
        if min(values) <= 0:
            raise ValueError("All archive limits must be positive")


def limits_from(policy: ResourcePolicy) -> Limits:
    return Limits(
        compressed_bytes=policy.compressed_bytes,
        expanded_bytes=policy.expanded_bytes,
        member_bytes=policy.member_bytes,
        notices=policy.notices,
    )


def limits_for(source_package_id: str) -> Limits:
    """The archive limits this package identity is allowed to cost."""
    return limits_from(policy_for(source_package_id))


LEGACY_ROOTS = {
    f"{{http://publications.europa.eu/resource/schema/ted/{version}/publication}}TED_EXPORT"
    for version in ("R2.0.8", "R2.0.9")
}
EFORMS_ROOTS = {
    f"{{urn:oasis:names:specification:ubl:schema:xsd:{name}-2}}{name}"
    for name in ("ContractNotice", "ContractAwardNotice", "PriorInformationNotice")
}
CBC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}"


def parse_notice(member_name: str, xml: bytes) -> tuple[ET.Element, NoticeKey, str, str]:
    """Return the parsed root plus (key, format, schema version) for one member.

    Enforces the supported encoding, the DTD/entity restriction, and the root
    contract. Callers that only need the metadata use ``inspect_notice``.
    """
    key = NoticeKey.parse(PurePosixPath(member_name).name)
    # The supported source encoding is UTF-8. Decode before screening declarations
    # so an alternative byte encoding cannot bypass the DTD/entity restriction.
    try:
        text = xml.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PackageError(f"Unsupported XML encoding in {member_name}") from exc
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise PackageError(f"Unsupported XML declaration in {member_name}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PackageError(f"Malformed XML in {member_name}") from exc
    if root.tag in LEGACY_ROOTS:
        if NoticeKey.parse(root.get("DOC_ID", "")) != key:
            raise PackageError(f"Filename and DOC_ID disagree in {member_name}")
        # Some published legacy members omit VERSION; their namespace still
        # identifies the supported schema family.
        version = root.get("VERSION") or root.tag.split("/")[-2]
        return root, key, "legacy", version
    if root.tag in EFORMS_ROOTS:
        version = root.findtext(f"{CBC}CustomizationID")
        identifier = root.find(f"{CBC}ID")
        if not version or not version.startswith("eforms-sdk-"):
            raise PackageError(f"Missing or unsupported CustomizationID in {member_name}")
        if (
            identifier is None
            or identifier.get("schemeName") != "notice-id"
            or not identifier.text
        ):
            raise PackageError(f"Missing eForms notice identifier in {member_name}")
        return root, key, "eforms", version
    raise PackageError(f"Unsupported XML root in {member_name}: {root.tag}")


def inspect_notice(member_name: str, xml: bytes) -> tuple[NoticeKey, str, str]:
    _, key, form, version = parse_notice(member_name, xml)
    return key, form, version


class _ExpandedReader:
    def __init__(self, stream: BinaryIO, limit: int):
        self.stream = stream
        self.limit = limit
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.count
        amount = remaining + 1 if size < 0 else min(size, remaining + 1)
        data = self.stream.read(amount)
        self.count += len(data)
        if self.count > self.limit:
            raise PackageError("Expanded archive exceeds its byte limit")
        return data


@dataclass(frozen=True)
class NoticeMember:
    """One parsed XML member of a package archive."""

    root: ET.Element
    key: NoticeKey
    source_format: str
    schema_version: str
    member_name: str


def _check_member_path(member: tarfile.TarInfo) -> None:
    parts = PurePosixPath(member.name).parts
    if (
        not parts
        or member.name.startswith("/")
        or ".." in parts
        or "\\" in member.name
        or ":" in member.name
    ):
        raise PackageError(f"Unsafe member path: {member.name!r}")


class _PackageArchive:
    """Walk a gzip-tar TED package one member at a time under fixed resource limits.

    Both the summarizing inspector and the streaming loader read packages through
    this class so archive-integrity behavior (gzip CRC, tar trailer, member
    validation, byte limits) is defined once.
    """

    def __init__(self, path: Path, limits: Limits):
        self.path = path
        self.limits = limits
        self.compressed_bytes = 0
        self.expanded_bytes = 0
        self.xml_member_bytes = 0
        self.member_count = 0

    def __enter__(self) -> "_PackageArchive":
        try:
            self._source = self.path.open("rb")
            self.compressed_bytes = self._source.seek(0, 2)
            self._source.seek(0)
        except OSError as exc:
            raise PackageError(f"Unreadable or corrupt archive: {self.path.name}") from exc
        if self.compressed_bytes > self.limits.compressed_bytes:
            self._source.close()
            raise PackageError("Compressed archive exceeds its byte limit")
        self._gzip = gzip.GzipFile(fileobj=self._source, mode="rb")
        self._reader = _ExpandedReader(self._gzip, self.limits.expanded_bytes)
        self._tar: tarfile.TarFile | None = None
        return self

    def __exit__(self, *exc: object) -> None:
        for closeable in (getattr(self, "_tar", None), getattr(self, "_gzip", None),
                          getattr(self, "_source", None)):
            try:
                if closeable is not None:
                    closeable.close()
            except OSError:
                pass

    def members(self):
        try:
            # One header block prevents read-ahead from hiding bytes after the
            # end marker from the explicit trailer check below. Opening here keeps
            # a corrupt gzip header on the same error path as a corrupt body.
            # __exit__ closes it; this class is itself the context manager.
            self._tar = tarfile.open(  # noqa: SIM115
                fileobj=self._reader, mode="r|", stream=True, bufsize=512
            )
            for member in self._tar:
                _check_member_path(member)
                if member.isdir():
                    continue
                if (
                    not member.isfile()
                    or member.issparse()
                    or not member.name.endswith(".xml")
                ):
                    raise PackageError(f"Unsupported archive member: {member.name!r}")
                if member.size > self.limits.member_bytes:
                    raise PackageError(f"Member exceeds its byte limit: {member.name}")
                if self.member_count >= self.limits.notices:
                    raise PackageError("Archive exceeds its notice limit")
                stream = self._tar.extractfile(member)
                if stream is None:
                    raise PackageError(f"Unreadable member: {member.name}")
                with stream:
                    xml = stream.read(self.limits.member_bytes + 1)
                if len(xml) != member.size:
                    raise PackageError(f"Incomplete member: {member.name}")
                root, key, form, version = parse_notice(member.name, xml)
                self.member_count += 1
                self.xml_member_bytes += member.size
                yield NoticeMember(root, key, form, version, member.name)
            # tar iteration can stop before gzip's trailer. Consume the rest to
            # check CRC/length and reject non-padding data after the tar.
            while remainder := self._reader.read(64 * 1024):
                if any(remainder):
                    raise PackageError("Unexpected data after the tar end marker")
            self.expanded_bytes = self._reader.count
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise PackageError(f"Unreadable or corrupt archive: {self.path.name}") from exc


def stream_notices(path: Path, limits: Limits = Limits()):
    """Yield a NoticeMember for each XML member, enforcing the same archive
    integrity and resource limits as inspect_package. The stream holds one member
    at a time; duplicate-identity enforcement is left to the caller (for the
    loader, the database primary key)."""
    with _PackageArchive(path, limits) as archive:
        yield from archive.members()


def inspect_package(path: Path, limits: Limits = Limits()) -> dict:
    keys: set[NoticeKey] = set()
    formats: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    try:
        with path.open("rb") as source:
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as exc:
        raise PackageError(f"Unreadable or corrupt archive: {path.name}") from exc
    with _PackageArchive(path, limits) as archive:
        for member in archive.members():
            if member.key in keys:
                raise PackageError(
                    f"Duplicate publication: {member.key.number}-{member.key.year}"
                )
            keys.add(member.key)
            formats[member.source_format] += 1
            versions[member.schema_version] += 1
        return {
            "sha256": checksum,
            "compressed_bytes": archive.compressed_bytes,
            "expanded_bytes": archive.expanded_bytes,
            "xml_member_bytes": archive.xml_member_bytes,
            "notice_count": len(keys),
            "formats": dict(sorted(formats.items())),
            "schema_versions": dict(sorted(versions.items())),
            "source_coverage_verified": False,
        }
