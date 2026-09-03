"""Fetch one bounded TED daily package into a controlled data directory.

The URL is derived from the package identity, never accepted from a caller: an
arbitrary URL could put any bytes under a package's name. Tests inject a local
origin through the ``url`` argument, which is not exposed on the command line.

What makes a download usable is not HTTP 200. The body is streamed to an
exclusive temporary file under fixed byte and time budgets, its declared length
is checked against what actually arrived, and the file is validated as a
complete gzip-tar package before it is renamed into place. JSON, HTML, a
truncated body or a corrupt archive therefore never reach the destination, and
a caller that finds the destination file can trust its bytes.

Interruption limit: like the Search API client, ``urllib`` applies its timeout
to individual socket operations rather than to a whole request. The budget is
checked between body reads, so a blocked socket operation can delay cancellation
by one operation timeout, and DNS resolution and header parsing have no hard
wall-clock deadline.

Resumption is deliberately absent. The observed endpoints advertise byte ranges
but carry no ETag or Last-Modified, so a range request cannot prove it is
continuing the same object. An interrupted download restarts from byte zero.
"""

import contextlib
import http.client
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .packages import Limits, PackageError, inspect_package
from .source_api import Clock, daily_package_query, parse_retry_after

PACKAGES_URL = "https://ted.europa.eu/packages"

#: Resource limits for the daily flow. The historical loader raises its own
#: limits for monthly archives; a daily package must not inherit those silently,
#: so inspection, validation and loading all use this one value.
DAILY_LIMITS = Limits()

_USER_AGENT = "tender-ledger/0.1 (+https://github.com/alpastorvillar-design/tender-ledger)"
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class DownloadError(RuntimeError):
    """The package could not be acquired. Nothing reached the destination."""


class TransientDownloadError(DownloadError):
    """A failure worth one more bounded attempt, restarted from byte zero."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class DownloadBudgets:
    """Hard limits on one acquisition. Exhausting any of them is a failure."""

    max_attempts: int = 3
    operation_timeout: float = 20.0
    total_seconds: float = 300.0
    max_artifact_bytes: int = 64 * 1024 * 1024
    #: Across every attempt of one acquisition, so a source that keeps
    #: truncating cannot be retried into an unbounded transfer.
    max_total_bytes: int = 192 * 1024 * 1024
    chunk_bytes: int = 1024 * 1024
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0


@dataclass(frozen=True)
class Artifact:
    """A validated package file at its final path."""

    path: Path
    sha256: str
    size_bytes: int
    notice_count: int
    http_attempts: int = 0
    downloaded_bytes: int = 0


def package_url(source_package_id: str) -> str:
    """The one URL a supported package identity is allowed to produce."""
    daily_package_query(source_package_id)  # raises UnsupportedPackage otherwise
    return f"{PACKAGES_URL}/{source_package_id}"


def artifact_destination(data_root: Path, source_package_id: str) -> Path:
    """Where a package's bytes live under the data root.

    The identity is validated first, so the path components are a fixed keyword
    and nine digits. Containment is asserted anyway: this is the only function
    that turns an identity into a filesystem path, so it is the right place to
    make traversal impossible rather than merely unlikely.
    """
    daily_package_query(source_package_id)
    kind, _, ordinal = source_package_id.partition("/")
    root = Path(data_root)
    destination = root / "packages" / kind / f"{ordinal}.tar.gz"
    if not destination.resolve().is_relative_to(root.resolve()):
        raise DownloadError(f"{source_package_id!r} resolves outside {root}")
    return destination


def validate_artifact(
    path: Path, *, expected_sha256: str | None = None, limits: Limits | None = None
) -> Artifact | None:
    """Describe a package file, or return None if it cannot be trusted.

    Used both after a download and when adopting a file a previous run left
    behind: a name and a database row are not evidence about content, so the
    bytes are hashed and the archive walked every time.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        summary = inspect_package(path, limits or DAILY_LIMITS)
    except (PackageError, OSError):
        return None
    if expected_sha256 is not None and summary["sha256"] != expected_sha256:
        return None
    return Artifact(
        path=path,
        sha256=summary["sha256"],
        size_bytes=summary["compressed_bytes"],
        notice_count=summary["notice_count"],
    )


def download_package(
    source_package_id: str,
    destination: Path,
    *,
    url: str | None = None,
    budgets: DownloadBudgets | None = None,
    clock: Clock | None = None,
    limits: Limits | None = None,
) -> Artifact:
    """Acquire one package and leave it validated at ``destination``.

    Every failure path removes this call's own temporary file and leaves any
    existing destination untouched; the rename is the only thing that changes
    what a reader of the data directory sees.
    """
    budgets = budgets or DownloadBudgets()
    clock = clock or Clock()
    limits = limits or DAILY_LIMITS
    url = url or package_url(source_package_id)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    deadline = _Deadline(budgets.total_seconds, clock)
    spent = _ByteBudget(budgets.max_total_bytes)
    opener = _build_opener()
    last: TransientDownloadError | None = None

    for attempt in range(1, budgets.max_attempts + 1):
        deadline.check()
        temporary = destination.parent / f".{destination.name}.part-{uuid.uuid4().hex[:12]}"
        try:
            written = _fetch(url, temporary, opener, budgets, deadline, spent, clock)
            artifact = validate_artifact(temporary, limits=limits)
            if artifact is None:
                raise DownloadError(
                    f"the response is not a usable TED package:"
                    f" {_describe_rejection(temporary, limits)}"
                )
            os.replace(temporary, destination)
            _sync_directory(destination.parent)
            return Artifact(
                path=destination,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                notice_count=artifact.notice_count,
                http_attempts=attempt,
                downloaded_bytes=written,
            )
        except TransientDownloadError as exc:
            _discard(temporary)
            last = exc
            if attempt == budgets.max_attempts:
                break
            _wait(exc, attempt, budgets, deadline, clock)
        except BaseException:
            _discard(temporary)
            raise
    raise DownloadError(f"{last} after {budgets.max_attempts} attempts")


def _describe_rejection(path: Path, limits: Limits) -> str:
    """Why a downloaded body was rejected, in the inspector's own words."""
    try:
        inspect_package(path, limits)
    except (PackageError, OSError) as exc:
        return str(exc)
    return "the checksum did not match what was requested"


def _wait(
    exc: TransientDownloadError,
    attempt: int,
    budgets: DownloadBudgets,
    deadline: "_Deadline",
    clock: Clock,
) -> None:
    wait = exc.retry_after
    if wait is None:
        wait = min(
            budgets.backoff_base_seconds * 2 ** (attempt - 1), budgets.backoff_max_seconds
        )
    if wait >= deadline.remaining():
        # Sleeping less than the source asked for and hitting it again early is
        # worse than stopping, so stop.
        raise DownloadError(
            f"{exc}; the required wait of {wait:.1f}s exceeds the remaining budget"
        ) from exc
    clock.sleep(wait)


class _Deadline:
    def __init__(self, seconds: float, clock: Clock):
        self._clock = clock
        self._end = clock.monotonic() + seconds

    def remaining(self) -> float:
        return self._end - self._clock.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise DownloadError("the download time budget is exhausted")


class _ByteBudget:
    """Bytes received across every attempt of one acquisition."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def spend(self, count: int) -> None:
        self.used += count
        if self.used > self.limit:
            raise DownloadError(
                f"the acquisition received {self.used} bytes, over the"
                f" {self.limit} byte budget for one package"
            )


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only while it stays on the origin that was requested.

    The product only ever requests the derived https TED URL, so this is what
    keeps a redirect from moving the download to another host or down to plain
    HTTP. Returning None leaves urllib to raise the 3xx as an error, which the
    caller reports rather than retries.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _build_opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()  # validates the chain and the hostname
    return urllib.request.build_opener(
        _SameOriginRedirect(), urllib.request.HTTPSHandler(context=context)
    )


def _fetch(
    url: str,
    temporary: Path,
    opener: urllib.request.OpenerDirector,
    budgets: DownloadBudgets,
    deadline: _Deadline,
    spent: _ByteBudget,
    clock: Clock,
) -> int:
    """One attempt: stream the body to ``temporary`` and return its byte count."""
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/gzip, application/octet-stream",
            # An entity that is already gzip must not also be transfer-encoded:
            # a decoded body would no longer be the bytes the checksum names.
            "Accept-Encoding": "identity",
            "User-Agent": _USER_AGENT,
        },
    )
    deadline.check()
    timeout = min(budgets.operation_timeout, max(deadline.remaining(), 0.001))
    try:
        reply = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        with exc:
            status, headers = exc.status, exc.headers
            retry_after = parse_retry_after(headers.get("Retry-After"), clock.now())
        if 300 <= status < 400:
            raise DownloadError(
                f"HTTP {status}: refusing a redirect to"
                f" {headers.get('Location')!r}; only the requested origin is followed"
            ) from exc
        if status in _RETRYABLE_STATUS:
            raise TransientDownloadError(f"HTTP {status}", retry_after=retry_after) from exc
        raise DownloadError(f"HTTP {status}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise DownloadError(f"TLS verification failed: {exc.reason}") from exc
        raise TransientDownloadError(f"connection failed: {exc.reason}") from exc
    except (TimeoutError, http.client.HTTPException, OSError) as exc:
        raise TransientDownloadError(
            f"connection failed: {type(exc).__name__}: {exc}"
        ) from exc

    with reply:
        if reply.status != 200:
            # 206 included: no range was requested, so a partial body here is a
            # protocol violation rather than a transfer this code can complete.
            raise DownloadError(f"HTTP {reply.status}: only a complete 200 response is used")
        encoding = (reply.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding != "identity":
            raise DownloadError(f"unsupported Content-Encoding {encoding!r}")
        declared = _declared_length(reply.headers.get("Content-Length"), budgets)
        written = _stream(reply, temporary, budgets, deadline, spent)

    if declared is not None and written != declared:
        raise TransientDownloadError(
            f"incomplete HTTP body: received {written} of {declared} declared bytes"
        )
    return written


def _declared_length(value: str | None, budgets: DownloadBudgets) -> int | None:
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]+", value.strip()):
        raise DownloadError("the response has an invalid Content-Length")
    declared = int(value)
    if declared > budgets.max_artifact_bytes:
        raise DownloadError(
            f"the response advertises {declared} bytes, over the"
            f" {budgets.max_artifact_bytes} byte budget"
        )
    return declared


def _stream(
    reply, temporary: Path, budgets: DownloadBudgets, deadline: _Deadline, spent: _ByteBudget
) -> int:
    """Copy the body out under both byte budgets, checking the clock between reads.

    read1 returns what has arrived instead of waiting for a full buffer, so a
    slow sender is interrupted by the deadline rather than by a socket timeout.
    The limit is enforced on bytes actually received, so a missing or lying
    Content-Length changes nothing.
    """
    written = 0
    with temporary.open("wb") as handle:
        try:
            while True:
                deadline.check()
                chunk = reply.read1(budgets.chunk_bytes)
                deadline.check()
                if not chunk:
                    break
                spent.spend(len(chunk))
                written += len(chunk)
                if written > budgets.max_artifact_bytes:
                    raise DownloadError(
                        f"the response body exceeds the {budgets.max_artifact_bytes}"
                        " byte budget"
                    )
                handle.write(chunk)
        except (TimeoutError, http.client.HTTPException, OSError) as exc:
            raise TransientDownloadError(
                f"the body was interrupted after {written} bytes:"
                f" {type(exc).__name__}: {exc}"
            ) from exc
        handle.flush()
        os.fsync(handle.fileno())
    return written


def _sync_directory(path: Path) -> None:
    """Make the rename itself durable where the platform supports it.

    Windows has no directory file descriptor to sync; the file's own fsync
    already happened, so this is a durability improvement on POSIX rather than a
    correctness requirement of the flow.
    """
    with contextlib.suppress(OSError, AttributeError):
        fd = os.open(path, getattr(os, "O_DIRECTORY", os.O_RDONLY))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _discard(temporary: Path) -> None:
    """Remove only this call's own partial file. Nothing else is swept."""
    with contextlib.suppress(OSError):
        temporary.unlink(missing_ok=True)
