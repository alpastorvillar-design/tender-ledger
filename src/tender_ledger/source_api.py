"""Bounded enumeration of one OJ S issue from the TED Search API.

The verifier needs exactly one thing from the network: every canonical
publication identifier the API reports for one daily package, or a failure that
cannot be mistaken for a complete answer. Everything here exists to make the
second case impossible to confuse with the first.

Pagination is the whole difficulty. The observed API keeps returning a non-empty
``iterationNextToken`` on the page after the last record, and the final page of
data is short rather than absent, so neither "the token went away" nor "this page
was smaller than the limit" ends the walk. What ends it is the announced total
being fully received *and* a terminal page confirming it. Anything else --
a changed total, a repeated token, a duplicate identifier, an early stop, an
exhausted budget -- raises, and the caller records the attempt as unavailable.

Transport is a small protocol so tests drive the walk through controlled
responses and a local HTTP server without reaching TED. The shipped
implementation is ``urllib`` with a default TLS context, which validates the
certificate chain and the hostname for https URLs.

Interruption limit: ``urllib`` applies its timeout to individual socket
operations, not to a whole request. The walk checks its budget before every
request and before every sleep, but a request already in flight can still
overrun it by up to one operation timeout. This is a bounded budget, not a hard
deadline.
"""

import email.utils
import hashlib
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
VERIFIER_VERSION = "1"

#: How ``api_keys_sha256`` is built, recorded next to every digest so a stored
#: value stays comparable to one computed elsewhere.
KEY_DIGEST_RECIPE = 'sha256 of "<year>:<number>" lines, keys sorted ascending, joined by "\\n"'

_FIELDS = ("publication-number", "publication-date", "ojs-number")
_USER_AGENT = "tender-ledger/0.1 (+https://github.com/alpastorvillar-design/tender-ledger)"
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_DAILY_PACKAGE = re.compile(r"daily/(?P<year>[0-9]{4})(?P<ojs>[0-9]{5})")
_PUBLICATION_NUMBER = re.compile(r"(?P<number>[0-9]{1,12})-(?P<year>[0-9]{4})")


class SourceUnavailable(RuntimeError):
    """The source could not be enumerated reliably.

    Carries whatever the walk had observed when it failed, so a partial attempt
    can be recorded as evidence. Partial counts describe the attempt; they never
    describe the source's contents.
    """

    def __init__(self, message: str, *, observed: "Enumeration | None" = None):
        super().__init__(message)
        self.observed = observed


class TransientSourceError(SourceUnavailable):
    """A failure worth one more bounded attempt: connection, timeout, 408/429/5xx."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class UnsupportedPackage(ValueError):
    """This package identity has no supported Search API query."""


@dataclass(frozen=True)
class PackageQuery:
    """The one query a package identity is allowed to produce."""

    source_package_id: str
    ojs_number: str  # "220/2023", the form the API returns in ojs-number
    expression: str
    scope: str = "ALL"


def daily_package_query(source_package_id: str) -> PackageQuery:
    """Derive the Search API query for one daily package identity.

    ``daily/202300220`` is OJ S issue 220 of 2023 -- an issue ordinal, not the
    220th day of that year. Only this identity shape is supported, and the query
    is derived from it rather than accepted from a caller: a verification that
    could be narrowed by hand could certify a subset while reporting coverage of
    the whole issue.
    """
    match = _DAILY_PACKAGE.fullmatch(source_package_id)
    if match is None:
        raise UnsupportedPackage(
            f"{source_package_id!r} is not a canonical daily package identity"
            " (expected daily/YYYYNNNNN)"
        )
    year, ojs = int(match["year"]), int(match["ojs"])
    if not 1990 <= year <= 2099 or ojs < 1:
        raise UnsupportedPackage(
            f"{source_package_id!r} carries an implausible issue {ojs} of {year}"
        )
    return PackageQuery(
        source_package_id=source_package_id,
        ojs_number=f"{ojs}/{year}",
        expression=f"OJ = {ojs}/{year}",
    )


@dataclass(frozen=True)
class Budgets:
    """Hard limits on one verification. Exhausting any of them is a failure, not
    a result: the walk stops without a set it can compare."""

    max_notices: int = 10_000
    page_size: int = 250
    max_pages: int = 50
    max_page_attempts: int = 3
    max_response_bytes: int = 4 * 1024 * 1024
    total_seconds: float = 300.0
    operation_timeout: float = 20.0
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0


class Clock:
    """Injectable time so budgets, backoff and Retry-After are testable."""

    monotonic: Callable[[], float] = staticmethod(time.monotonic)
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)


@dataclass
class Enumeration:
    """What one walk observed. ``complete`` is what makes a comparison mean
    anything; without it these numbers only describe the attempt."""

    keys: set[tuple[int, int]] = field(default_factory=set)
    record_count: int = 0
    duplicate_count: int = 0
    announced_total: int | None = None
    pages_fetched: int = 0
    http_attempts: int = 0
    complete: bool = False


def keys_digest(keys: Iterable[tuple[int, int]]) -> str:
    """Digest a canonical key set following :data:`KEY_DIGEST_RECIPE`."""
    payload = "\n".join(f"{year}:{number}" for year, number in sorted(keys))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Response:
    """One HTTP response with a bounded body. ``headers`` keys are lower-cased."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def post_json(
        self, url: str, payload: dict, *, timeout: float, max_bytes: int
    ) -> Response: ...


def _read_capped(response, max_bytes: int) -> bytes:
    """Read at most ``max_bytes``, refusing an oversized body before buffering it."""
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.strip().isdigit() and int(declared) > max_bytes:
        raise SourceUnavailable(
            f"the response advertises {int(declared)} bytes, over the {max_bytes} byte budget"
        )
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise SourceUnavailable(f"the response body exceeds the {max_bytes} byte budget")
    return body


class UrllibTransport:
    """POST JSON with the standard library under a default (validating) TLS context."""

    def __init__(self, *, user_agent: str = _USER_AGENT):
        self.context = ssl.create_default_context()
        self._user_agent = user_agent

    def post_json(
        self, url: str, payload: dict, *, timeout: float, max_bytes: int
    ) -> Response:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self._user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.context) as reply:
                return _response(reply, max_bytes)
        except urllib.error.HTTPError as exc:
            # An error status still carries headers worth honouring (Retry-After).
            with exc:
                return _response(exc, max_bytes)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                # A rejected certificate is not a hiccup to retry through.
                raise SourceUnavailable(f"TLS verification failed: {exc.reason}") from exc
            raise TransientSourceError(f"connection failed: {exc.reason}") from exc
        except (TimeoutError, http.client.HTTPException, OSError) as exc:
            raise TransientSourceError(
                f"connection failed: {type(exc).__name__}: {exc}"
            ) from exc


def _response(reply, max_bytes: int) -> Response:
    return Response(
        status=reply.status,
        headers={key.lower(): value for key, value in reply.headers.items()},
        body=_read_capped(reply, max_bytes),
    )


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds to wait from a ``Retry-After`` value: delta-seconds or an HTTP-date."""
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - now).total_seconds())


class _Deadline:
    def __init__(self, seconds: float, clock: Clock):
        self._clock = clock
        self._end = clock.monotonic() + seconds

    def remaining(self) -> float:
        return self._end - self._clock.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise SourceUnavailable("the verification time budget is exhausted")


def _decode(body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceUnavailable(f"the response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceUnavailable("the response is not a JSON object")
    return data


def _page_keys(data: dict, expected_ojs: str) -> tuple[list[tuple[int, int]], int]:
    """Validate one page against the documented contract and return its keys and total."""
    if data.get("timedOut") is not False:
        raise SourceUnavailable(f"the source reported timedOut={data.get('timedOut')!r}")
    total = data.get("totalNoticeCount")
    # bool is an int subclass; True must not pass for a count.
    if type(total) is not int or total < 0:
        raise SourceUnavailable(f"totalNoticeCount is not a count: {total!r}")
    notices = data.get("notices")
    if not isinstance(notices, list):
        raise SourceUnavailable("the response carries no notices list")

    keys: list[tuple[int, int]] = []
    for notice in notices:
        if not isinstance(notice, dict):
            raise SourceUnavailable("a notice entry is not an object")
        missing = [name for name in _FIELDS if not isinstance(notice.get(name), str)]
        if missing:
            raise SourceUnavailable(f"a notice entry is missing {', '.join(missing)}")
        if notice["ojs-number"] != expected_ojs:
            raise SourceUnavailable(
                f"a notice belongs to OJ S {notice['ojs-number']}, not {expected_ojs}"
            )
        try:
            # The suffix ("+01:00", "Z") does not change the calendar date.
            date.fromisoformat(notice["publication-date"][:10])
        except ValueError as exc:
            raise SourceUnavailable(
                f"publication-date {notice['publication-date']!r} is not a calendar date"
            ) from exc
        match = _PUBLICATION_NUMBER.fullmatch(notice["publication-number"])
        if match is None or int(match["number"]) == 0:
            raise SourceUnavailable(
                f"publication-number {notice['publication-number']!r} is not canonical"
            )
        keys.append((int(match["year"]), int(match["number"])))
    return keys, total


def _fetch_page(
    transport: Transport,
    url: str,
    payload: dict,
    budgets: Budgets,
    deadline: _Deadline,
    clock: Clock,
    observed: Enumeration,
) -> dict:
    """One page, with bounded retries for transient failures only."""
    last: TransientSourceError | None = None
    for attempt in range(1, budgets.max_page_attempts + 1):
        deadline.check()
        observed.http_attempts += 1
        try:
            response = transport.post_json(
                url,
                payload,
                timeout=min(budgets.operation_timeout, max(deadline.remaining(), 0.001)),
                max_bytes=budgets.max_response_bytes,
            )
            if response.status in _RETRYABLE_STATUS:
                raise TransientSourceError(
                    f"HTTP {response.status}",
                    retry_after=parse_retry_after(
                        response.headers.get("retry-after"), clock.now()
                    ),
                )
            if response.status != 200:
                raise SourceUnavailable(f"HTTP {response.status}")
            return _decode(response.body)
        except TransientSourceError as exc:
            last = exc
            if attempt == budgets.max_page_attempts:
                break
            wait = exc.retry_after
            if wait is None:
                wait = min(
                    budgets.backoff_base_seconds * 2 ** (attempt - 1),
                    budgets.backoff_max_seconds,
                )
            if wait >= deadline.remaining():
                # Sleeping less than asked and hitting the source again early is
                # worse than stopping, so stop.
                raise SourceUnavailable(
                    f"{exc}; the required wait of {wait:.1f}s exceeds the remaining budget"
                ) from exc
            clock.sleep(wait)
    raise SourceUnavailable(f"{last} after {budgets.max_page_attempts} attempts")


def enumerate_publication_keys(
    transport: Transport,
    query: PackageQuery,
    *,
    budgets: Budgets = Budgets(),
    clock: Clock | None = None,
    url: str = SEARCH_URL,
) -> Enumeration:
    """Walk every page of one OJ S issue, or raise :class:`SourceUnavailable`.

    A returned enumeration is complete by construction: the announced total was
    stable, every record arrived exactly once, and a terminal page confirmed
    there were no more.
    """
    clock = clock or Clock()
    observed = Enumeration()
    try:
        _walk(transport, query, budgets, clock, url, observed)
    except SourceUnavailable as exc:
        if exc.observed is None:
            exc.observed = observed
        raise
    observed.complete = True
    return observed


def _walk(
    transport: Transport,
    query: PackageQuery,
    budgets: Budgets,
    clock: Clock,
    url: str,
    observed: Enumeration,
) -> None:
    deadline = _Deadline(budgets.total_seconds, clock)
    token: str | None = None
    seen_tokens: set[str] = set()

    while True:
        if observed.pages_fetched >= budgets.max_pages:
            raise SourceUnavailable(f"pagination exceeded {budgets.max_pages} pages")
        payload = {
            "query": query.expression,
            "scope": query.scope,
            "limit": budgets.page_size,
            "paginationMode": "ITERATION",
            "checkQuerySyntax": False,
            "fields": list(_FIELDS),
        }
        if token is not None:
            payload["iterationNextToken"] = token

        data = _fetch_page(transport, url, payload, budgets, deadline, clock, observed)
        observed.pages_fetched += 1
        keys, total = _page_keys(data, query.ojs_number)

        if observed.announced_total is None:
            observed.announced_total = total
            if total > budgets.max_notices:
                raise SourceUnavailable(
                    f"the source reports {total} notices, over the {budgets.max_notices} budget"
                )
        elif total != observed.announced_total:
            raise SourceUnavailable(
                f"the reported total changed from {observed.announced_total} to {total}"
                " during pagination"
            )

        new = [key for key in keys if key not in observed.keys]
        observed.duplicate_count += len(keys) - len(new)
        observed.keys.update(new)
        observed.record_count += len(keys)
        if observed.record_count > total:
            raise SourceUnavailable(
                f"received {observed.record_count} records for a reported total of {total}"
            )

        next_token = data.get("iterationNextToken")
        if next_token is not None and (not isinstance(next_token, str) or not next_token):
            raise SourceUnavailable("iterationNextToken is not a non-empty string")

        if not keys:
            # The terminal page. The observed API still hands back a token here,
            # so the count is the signal, not the token.
            if _received_everything(observed, total):
                return
            raise SourceUnavailable(
                f"the source stopped after {observed.record_count} records"
                f" ({len(observed.keys)} distinct) for a reported total of {total}"
            )
        if next_token is None:
            if _received_everything(observed, total):
                return
            raise SourceUnavailable(
                f"pagination ended without a token after {observed.record_count} records"
                f" ({len(observed.keys)} distinct) for a reported total of {total}"
            )
        if next_token in seen_tokens:
            raise SourceUnavailable("pagination repeated an iteration token without progress")
        seen_tokens.add(next_token)
        token = next_token


def _received_everything(observed: Enumeration, total: int) -> bool:
    """Records, distinct keys and the announced total all agree. Any duplicate
    breaks the middle equality, so cardinality can never be approved past one."""
    return observed.record_count == len(observed.keys) == total
