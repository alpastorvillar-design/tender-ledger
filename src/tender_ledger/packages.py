"""Inspect bounded TED archives without extracting their members to disk."""

from collections import Counter
from dataclasses import dataclass
import gzip
import hashlib
from pathlib import Path, PurePosixPath
import re
import tarfile
from typing import BinaryIO
import xml.etree.ElementTree as ET


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
    compressed_bytes: int = 64 * 1024 * 1024
    expanded_bytes: int = 512 * 1024 * 1024
    member_bytes: int = 8 * 1024 * 1024
    notices: int = 10_000

    def __post_init__(self) -> None:
        values = (self.compressed_bytes, self.expanded_bytes, self.member_bytes, self.notices)
        if min(values) <= 0:
            raise ValueError("All archive limits must be positive")


LEGACY_ROOTS = {
    f"{{http://publications.europa.eu/resource/schema/ted/{version}/publication}}TED_EXPORT"
    for version in ("R2.0.8", "R2.0.9")
}
EFORMS_ROOTS = {
    f"{{urn:oasis:names:specification:ubl:schema:xsd:{name}-2}}{name}"
    for name in ("ContractNotice", "ContractAwardNotice", "PriorInformationNotice")
}
CBC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}"


def inspect_notice(member_name: str, xml: bytes) -> tuple[NoticeKey, str, str]:
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
        return key, "legacy", version
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
        return key, "eforms", version
    raise PackageError(f"Unsupported XML root in {member_name}: {root.tag}")


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


def inspect_package(path: Path, limits: Limits = Limits()) -> dict:
    keys: set[NoticeKey] = set()
    formats: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    member_bytes = 0
    try:
        with path.open("rb") as source:
            compressed_bytes = source.seek(0, 2)
            if compressed_bytes > limits.compressed_bytes:
                raise PackageError("Compressed archive exceeds its byte limit")
            source.seek(0)
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
            source.seek(0)
            with gzip.GzipFile(fileobj=source, mode="rb") as expanded:
                reader = _ExpandedReader(expanded, limits.expanded_bytes)
                # One header block prevents read-ahead from hiding bytes after
                # the end marker from the explicit trailer check below.
                with tarfile.open(
                    fileobj=reader, mode="r|", stream=True, bufsize=512
                ) as archive:
                    for member in archive:
                        parts = PurePosixPath(member.name).parts
                        if (
                            not parts
                            or member.name.startswith("/")
                            or ".." in parts
                            or "\\" in member.name
                            or ":" in member.name
                        ):
                            raise PackageError(f"Unsafe member path: {member.name!r}")
                        if member.isdir():
                            continue
                        if (
                            not member.isfile()
                            or member.issparse()
                            or not member.name.endswith(".xml")
                        ):
                            raise PackageError(f"Unsupported archive member: {member.name!r}")
                        if member.size > limits.member_bytes:
                            raise PackageError(f"Member exceeds its byte limit: {member.name}")
                        if len(keys) >= limits.notices:
                            raise PackageError("Archive exceeds its notice limit")
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise PackageError(f"Unreadable member: {member.name}")
                        with stream:
                            xml = stream.read(limits.member_bytes + 1)
                        if len(xml) != member.size:
                            raise PackageError(f"Incomplete member: {member.name}")
                        key, form, version = inspect_notice(member.name, xml)
                        if key in keys:
                            raise PackageError(f"Duplicate publication: {key.number}-{key.year}")
                        keys.add(key)
                        formats[form] += 1
                        versions[version] += 1
                        member_bytes += member.size
                # tar iteration can stop before gzip's trailer. Consume the rest
                # to check CRC/length and reject non-padding data after the tar.
                while remainder := reader.read(64 * 1024):
                    if any(remainder):
                        raise PackageError("Unexpected data after the tar end marker")
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise PackageError(f"Unreadable or corrupt archive: {path.name}") from exc
    return {
        "sha256": checksum,
        "compressed_bytes": compressed_bytes,
        "expanded_bytes": reader.count,
        "xml_member_bytes": member_bytes,
        "notice_count": len(keys),
        "formats": dict(sorted(formats.items())),
        "schema_versions": dict(sorted(versions.items())),
        "source_coverage_verified": False,
    }
