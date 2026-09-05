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

from .package_contract import (
    DAILY_POLICY,
    ResourcePolicy,
    package_identity,
)


class PackageError(ValueError):
    """An archive violates the supported source contract or resource limits.

    ``code`` names the rejection so the survey can count it without parsing
    prose, and ``detail`` carries the one bounded value worth reporting for that
    code -- today, the XML root a member turned out to have.
    """

    def __init__(self, message: str, *, code: str = "archive", detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, order=True)
class NoticeKey:
    year: int
    number: int

    @classmethod
    def parse(cls, value: str) -> "NoticeKey":
        match = re.fullmatch(r"([0-9]{1,8})[-_]([1-9][0-9]{3})(?:\.xml)?", value)
        if match is None or int(match[1]) == 0:
            raise PackageError(
                f"Invalid publication reference: {value!r}", code="invalid_identity"
            )
        return cls(year=int(match[2]), number=int(match[1]))


@dataclass(frozen=True)
class Limits:
    """This layer's view of a package's resource policy.

    The production values come from :mod:`package_contract`, so a daily and a
    monthly archive are validated against ceilings that are decided in one place.
    Tests construct small limits explicitly.

    The defaults are the daily ones, which is the conservative choice: they admit
    no nested container at all, so a caller that forgets to name a policy cannot
    accidentally open a layout the package identity never approved.
    """

    compressed_bytes: int = DAILY_POLICY.compressed_bytes
    expanded_bytes: int = DAILY_POLICY.expanded_bytes
    member_bytes: int = DAILY_POLICY.member_bytes
    notices: int = DAILY_POLICY.notices
    container_count: int = DAILY_POLICY.container_count
    container_bytes: int = DAILY_POLICY.container_bytes

    def __post_init__(self) -> None:
        values = (self.compressed_bytes, self.expanded_bytes, self.member_bytes,
                  self.notices, self.container_bytes)
        if min(values) <= 0:
            raise ValueError("All archive limits must be positive")
        if self.container_count < 0:
            raise ValueError("container_count must not be negative")


def limits_from(policy: ResourcePolicy) -> Limits:
    return Limits(
        compressed_bytes=policy.compressed_bytes,
        expanded_bytes=policy.expanded_bytes,
        member_bytes=policy.member_bytes,
        notices=policy.notices,
        container_count=policy.container_count,
        container_bytes=policy.container_bytes,
    )


def limits_for(source_package_id: str) -> Limits:
    """The archive limits this package identity is allowed to cost."""
    return limits_from(package_identity(source_package_id).policy)


LEGACY_ROOTS = {
    f"{{http://publications.europa.eu/resource/schema/ted/{version}/publication}}TED_EXPORT"
    for version in ("R2.0.8", "R2.0.9")
}
EFORMS_ROOTS = {
    f"{{urn:oasis:names:specification:ubl:schema:xsd:{name}-2}}{name}"
    for name in ("ContractNotice", "ContractAwardNotice", "PriorInformationNotice")
} | {
    # The SDK's fourth notice document, which is not a UBL procurement document
    # and so carries its own namespace. Added by exact name after a bounded
    # structural read found one in a real monthly package; every other root,
    # including a later version of this namespace, stays unsupported.
    "{http://data.europa.eu/p27/eforms-business-registration-information-notice/1}"
    "BusinessRegistrationInformationNotice",
}
CBC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}"
MAX_SCHEMA_VERSION_CHARS = 128


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
        raise PackageError(
            f"Unsupported XML encoding in {member_name}", code="unsupported_encoding"
        ) from exc
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise PackageError(
            f"Unsupported XML declaration in {member_name}", code="unsupported_declaration"
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PackageError(f"Malformed XML in {member_name}", code="malformed_xml") from exc
    if root.tag in LEGACY_ROOTS:
        if NoticeKey.parse(root.get("DOC_ID", "")) != key:
            raise PackageError(
                f"Filename and DOC_ID disagree in {member_name}", code="identity_mismatch"
            )
        # Some published legacy members omit VERSION; their namespace still
        # identifies the supported schema family.
        version = root.get("VERSION") or root.tag.split("/")[-2]
        if len(version) > MAX_SCHEMA_VERSION_CHARS:
            raise PackageError(
                f"Unsupported schema version in {member_name}",
                code="unsupported_version",
            )
        return root, key, "legacy", version
    if root.tag in EFORMS_ROOTS:
        version = root.findtext(f"{CBC}CustomizationID")
        identifier = root.find(f"{CBC}ID")
        if (
            not version
            or not version.startswith("eforms-sdk-")
            or len(version) > MAX_SCHEMA_VERSION_CHARS
        ):
            raise PackageError(
                f"Missing or unsupported CustomizationID in {member_name}",
                code="unsupported_customization",
            )
        if (
            identifier is None
            or identifier.get("schemeName") != "notice-id"
            or not identifier.text
        ):
            raise PackageError(
                f"Missing eForms notice identifier in {member_name}",
                code="missing_notice_identifier",
            )
        return root, key, "eforms", version
    raise PackageError(
        f"Unsupported XML root in {member_name}: {root.tag}",
        code="unsupported_root",
        detail=root.tag,
    )


def inspect_notice(member_name: str, xml: bytes) -> tuple[NoticeKey, str, str]:
    _, key, form, version = parse_notice(member_name, xml)
    return key, form, version


class _ExpandedBudget:
    """Bytes one archive is allowed to expand to, outer and nested together.

    A nested container's expansion is spent from the same budget as the outer
    archive's, so a container cannot use its own compression to buy work the
    package identity's ceiling forbids.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.count = 0

    def remaining(self) -> int:
        return self.limit - self.count

    def spend(self, count: int) -> None:
        self.count += count
        if self.count > self.limit:
            raise PackageError("Expanded archive exceeds its byte limit")


class _ExpandedReader:
    """Read from one decompressed stream, charging every byte to ``budget``."""

    def __init__(self, stream: BinaryIO, budget: _ExpandedBudget):
        self.stream = stream
        self.budget = budget

    def read(self, size: int = -1) -> bytes:
        # One byte past the budget is read deliberately: it is what turns "the
        # limit is reached" into "the limit is exceeded".
        allowed = self.budget.remaining() + 1
        amount = allowed if size < 0 else min(size, allowed)
        data = self.stream.read(amount)
        self.budget.spend(len(data))
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


#: TED publishes some months as one nested archive per publication day. Only
#: this suffix is treated as a container; anything else that is not XML stays an
#: ordinary unsupported member.
CONTAINER_SUFFIX = ".tar.gz"

FLAT_LAYOUT = "flat"
NESTED_LAYOUT = "nested"
EMPTY_LAYOUT = "empty"


class _PackageArchive:
    """Walk a gzip-tar TED package one member at a time under fixed resource limits.

    The summarizing inspector, the streaming loader and the compatibility survey
    all read packages through this class, so archive-integrity behavior (gzip
    CRC, tar trailer, safe paths, member types, byte limits) is defined once and
    cannot drift between a survey and the load it is supposed to predict.

    Two kinds of rejection are deliberately different. An archive-level fault --
    an unsafe path, a member that is not a regular file, an exhausted limit,
    truncation, corruption -- makes further reading unsafe or meaningless and
    always raises. A member-level fault is about one member's content; callers
    that pass ``on_rejected`` get it reported and the walk continues, which is
    what turns a first-failure stop into an inventory. The loader passes nothing,
    so a load stays all-or-nothing.

    Two layouts are supported, and never both in the same archive: XML members
    under directories, or exactly one level of nested daily ``.tar.gz``
    containers whose own members are XML. Nesting is opened by the package
    policy (``container_count``), so a daily package refuses it whatever the
    archive contains, and a container inside a container is refused everywhere.
    Containers are streamed like every other member: nothing is extracted to
    disk and no whole day is held in memory.
    """

    def __init__(self, path: Path, limits: Limits):
        self.path = path
        self.limits = limits
        self.compressed_bytes = 0
        self.expanded_bytes = 0
        self.xml_member_bytes = 0
        self.member_count = 0
        self.xml_member_count = 0
        self.container_count = 0
        self.layout = EMPTY_LAYOUT

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
        self._budget = _ExpandedBudget(self.limits.expanded_bytes)
        self._reader = _ExpandedReader(self._gzip, self._budget)
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

    def members(self, *, on_rejected=None):
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
                self._check_regular(member)
                if self._is_container(member):
                    yield from self._container(member, on_rejected)
                    continue
                yield from self._notice(self._tar, member, member.name, on_rejected)
            self._drain(self._reader, self.path.name)
            self.expanded_bytes = self._budget.count
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise PackageError(f"Unreadable or corrupt archive: {self.path.name}") from exc

    def _is_container(self, member: tarfile.TarInfo) -> bool:
        """Whether this outer member is a nested daily archive to walk into.

        The layout is decided by the archive, but only ever within what the
        policy already allows: with ``container_count`` at zero the answer is
        always no, and the member falls through to the ordinary rejection path.
        """
        if self.limits.container_count <= 0 or not member.name.endswith(CONTAINER_SUFFIX):
            return False
        self._claim_layout(NESTED_LAYOUT, member.name)
        return True

    def _claim_layout(self, layout: str, member_name: str) -> None:
        if self.layout not in (EMPTY_LAYOUT, layout):
            raise PackageError(
                f"Archive mixes flat and nested members: {member_name!r}"
            )
        self.layout = layout

    def _container(self, member: tarfile.TarInfo, on_rejected):
        """Walk one nested daily archive, streaming, without recursing further."""
        if self.container_count >= self.limits.container_count:
            raise PackageError("Archive exceeds its nested container limit")
        if member.size > self.limits.container_bytes:
            raise PackageError(f"Nested container exceeds its byte limit: {member.name}")
        self.container_count += 1

        stream = self._tar.extractfile(member)
        if stream is None:
            raise PackageError(f"Unreadable member: {member.name}")
        with stream:
            inner_gzip = gzip.GzipFile(fileobj=stream, mode="rb")
            try:
                # The same budget as the outer archive: a container's expansion
                # is work this package is spending, not work it hides.
                reader = _ExpandedReader(inner_gzip, self._budget)
                inner = tarfile.open(  # noqa: SIM115 -- closed in the finally below
                    fileobj=reader, mode="r|", stream=True, bufsize=512
                )
                try:
                    for nested in inner:
                        _check_member_path(nested)
                        if nested.isdir():
                            continue
                        self._check_regular(nested)
                        if nested.name.endswith(CONTAINER_SUFFIX):
                            raise PackageError(
                                f"Nested archive inside a container: {nested.name!r}"
                            )
                        label = f"{member.name}/{nested.name}"
                        yield from self._notice(inner, nested, label, on_rejected)
                finally:
                    inner.close()
                self._drain(reader, member.name)
            finally:
                inner_gzip.close()

    def _notice(self, tar: tarfile.TarFile, member: tarfile.TarInfo, label: str, on_rejected):
        """Read one candidate XML member and yield it, or report its rejection.

        ``label`` names the member for diagnostics -- for a nested member, the
        container it came from and its own name. It is never a filesystem path.
        """
        if member.size > self.limits.member_bytes:
            raise PackageError(f"Member exceeds its byte limit: {label}")
        if self.member_count >= self.limits.notices:
            raise PackageError("Archive exceeds its notice limit")
        stream = tar.extractfile(member)
        if stream is None:
            raise PackageError(f"Unreadable member: {label}")
        with stream:
            xml = stream.read(self.limits.member_bytes + 1)
        if len(xml) != member.size:
            raise PackageError(f"Incomplete member: {label}")
        self.member_count += 1
        is_xml = member.name.endswith(".xml")
        # Which layout this archive is committed to is an archive-level fact, so
        # it is settled before the member-level rejections a survey may absorb.
        if is_xml and tar is self._tar:
            self._claim_layout(FLAT_LAYOUT, member.name)
        try:
            if not is_xml:
                raise PackageError(
                    f"Unsupported archive member: {label!r}",
                    code="nested_container_not_allowed"
                    if member.name.endswith(CONTAINER_SUFFIX)
                    else "unsupported_member_name",
                )
            self.xml_member_count += 1
            self.xml_member_bytes += member.size
            parsed = parse_notice(label, xml)
        except PackageError as exc:
            if on_rejected is None:
                raise
            on_rejected(label, exc)
            return
        yield NoticeMember(*parsed, label)

    @staticmethod
    def _check_regular(member: tarfile.TarInfo) -> None:
        if not member.isfile() or member.issparse():
            raise PackageError(f"Unsupported archive member: {member.name!r}")

    @staticmethod
    def _drain(reader: _ExpandedReader, name: str) -> None:
        """Finish a gzip stream: tar iteration can stop before its trailer.

        Reading to the end checks the CRC and length, and rejects anything but
        padding after the tar's own end marker.
        """
        while remainder := reader.read(64 * 1024):
            if any(remainder):
                raise PackageError(f"Unexpected data after the tar end marker in {name}")


def stream_notices(path: Path, limits: Limits = Limits()):
    """Yield a NoticeMember for each XML member, enforcing the same archive
    integrity and resource limits as inspect_package. The stream holds one member
    at a time; duplicate-identity enforcement is left to the caller (for the
    loader, the database primary key)."""
    with _PackageArchive(path, limits) as archive:
        yield from archive.members()


#: Counts in a survey are unbounded; the illustrations beside them are not.
SURVEY_SAMPLE_LIMIT = 20


def survey_package(
    path: Path, limits: Limits = Limits(), *, source_package_id: str | None = None
) -> dict:
    """Inventory one local archive's compatibility without loading anything.

    A load is all-or-nothing: the first member this contract cannot project
    condemns the whole capture. That is the right behaviour for a load and a bad
    way to find out what is inside a period nobody has opened before, which is
    what this is for -- a monthly archive from an era whose schema versions have
    never been seen should be surveyed before it is downloaded into a capture.

    The walk uses the same archive defenses and the same resource limits as a
    load, so an archive-level fault still stops it and nothing here can approve
    bytes a load would refuse. Member-level faults are counted by reason instead,
    and the result says plainly whether the archive is loadable.

    Each member is also projected, and discarded. ``compatible_for_load`` is a
    claim about what a load would do, and a load projects: a real monthly package
    carries a legacy notice with no ``CODED_DATA_SECTION``, which parses as a
    supported root and then condemns the capture. Counting only what the walker
    accepts would call that archive loadable and be wrong about the one question
    this exists to answer.

    Nothing is written: no capture, no rows, no coverage claim. The output
    carries counts, schema provenance and sanitized member names -- never XML
    content, notice fields or filesystem paths.
    """
    # Local import: the projection is built on this module, so importing it at
    # module scope would be circular. The survey is the one reader here that
    # needs it.
    from .projection import project_member

    if source_package_id is not None:
        package_identity(source_package_id)

    keys: set[NoticeKey] = set()
    formats: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    roots: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    unsupported_roots: Counter[str] = Counter()
    duplicates: list[str] = []
    rejected: list[dict[str, str]] = []
    rejected_count = 0
    duplicate_count = 0

    def reject(member_name: str, exc: PackageError) -> None:
        nonlocal rejected_count
        rejected_count += 1
        reasons[exc.code] += 1
        if exc.code == "unsupported_root" and exc.detail is not None:
            unsupported_roots[exc.detail[:200]] += 1
        if len(rejected) < SURVEY_SAMPLE_LIMIT:
            rejected.append({"member": _member_label(member_name), "reason": exc.code})

    checksum = _digest(path)
    with _PackageArchive(path, limits) as archive:
        for member in archive.members(on_rejected=reject):
            # These describe XML members, not unique notice identities. Keep a
            # repeated identity in the schema inventory even though the package
            # remains incompatible for loading.
            formats[member.source_format] += 1
            versions[member.schema_version] += 1
            roots[member.root.tag] += 1
            try:
                project_member(member)
            except PackageError as exc:
                reject(member.member_name, exc)
                continue
            if member.key in keys:
                # A load fails on this through the primary key. Here it is one
                # more finding, so the rest of the archive still gets counted.
                duplicate_count += 1
                if len(duplicates) < SURVEY_SAMPLE_LIMIT:
                    duplicates.append(f"{member.key.number}-{member.key.year}")
                continue
            keys.add(member.key)
        return {
            "source_package_id": source_package_id,
            "sha256": checksum,
            "compressed_bytes": archive.compressed_bytes,
            "expanded_bytes": archive.expanded_bytes,
            "xml_member_bytes": archive.xml_member_bytes,
            "layout": archive.layout,
            "container_count": archive.container_count,
            "member_count": archive.member_count,
            "xml_member_count": archive.xml_member_count,
            "notice_count": len(keys),
            "formats": dict(sorted(formats.items())),
            "schema_versions": _bounded_counts(versions),
            "schema_version_kinds": len(versions),
            "roots": dict(sorted(roots.items())),
            "duplicate_identity_count": duplicate_count,
            "duplicate_identity_sample": duplicates,
            "incompatible_member_count": rejected_count,
            "incompatible_reasons": dict(sorted(reasons.items())),
            "unsupported_roots": _bounded_counts(unsupported_roots),
            "unsupported_root_kinds": len(unsupported_roots),
            "incompatible_sample": rejected,
            "compatible_for_load": rejected_count == 0 and duplicate_count == 0,
        }


def _member_label(member_name: str) -> str:
    """A member's own name, without its path and bounded in length."""
    return PurePosixPath(member_name).name[:120]


def _bounded_counts(values: Counter[str]) -> dict[str, int]:
    """Return the most frequent categories with deterministic tie-breaking."""
    ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    return dict(ordered[:SURVEY_SAMPLE_LIMIT])


def _digest(path: Path) -> str:
    try:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as exc:
        raise PackageError(f"Unreadable or corrupt archive: {path.name}") from exc


def inspect_package(path: Path, limits: Limits = Limits()) -> dict:
    keys: set[NoticeKey] = set()
    formats: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    checksum = _digest(path)
    with _PackageArchive(path, limits) as archive:
        for member in archive.members():
            if member.key in keys:
                raise PackageError(
                    f"Duplicate publication: {member.key.number}-{member.key.year}",
                    code="duplicate_identity",
                )
            keys.add(member.key)
            formats[member.source_format] += 1
            versions[member.schema_version] += 1
        return {
            "sha256": checksum,
            "compressed_bytes": archive.compressed_bytes,
            "expanded_bytes": archive.expanded_bytes,
            "xml_member_bytes": archive.xml_member_bytes,
            "layout": archive.layout,
            "container_count": archive.container_count,
            "notice_count": len(keys),
            "formats": dict(sorted(formats.items())),
            "schema_versions": dict(sorted(versions.items())),
            "source_coverage_verified": False,
        }
