"""Search API query derivation, pagination, retries and the HTTP transport.

These run without PostgreSQL and without touching TED: pagination is driven
through a scripted transport, and the shipped `urllib` client is exercised
against a local HTTP server.
"""

import json
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ted_fixtures import DEFAULT_OJS, FakeTransport, api_page, raw_page, search_body
from tender_ledger.source_api import (
    Budgets,
    Clock,
    SourceUnavailable,
    TransientSourceError,
    UnsupportedPackage,
    UrllibTransport,
    daily_package_query,
    enumerate_publication_keys,
    keys_digest,
    parse_retry_after,
)

QUERY = daily_package_query("daily/202300220")


class FakeClock(Clock):
    """Monotonic time that only moves when the test says so."""

    def __init__(self, wall=datetime(2026, 9, 3, 12, 0, tzinfo=UTC)):
        self.elapsed = 0.0
        self.slept: list[float] = []
        self.wall = wall

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.elapsed += seconds

    def now(self):
        return self.wall


def walk(script, *, budgets=None, clock=None, before_request=None):
    transport = FakeTransport(script, before_request=before_request)
    result = enumerate_publication_keys(
        transport,
        QUERY,
        budgets=budgets or Budgets(page_size=2, backoff_base_seconds=1.0),
        clock=clock or FakeClock(),
    )
    return result, transport


def fails(script, **kwargs):
    transport = FakeTransport(script, before_request=kwargs.pop("before_request", None))
    budgets = kwargs.pop("budgets", None) or Budgets(page_size=2, backoff_base_seconds=1.0)
    clock = kwargs.pop("clock", None) or FakeClock()
    try:
        enumerate_publication_keys(transport, QUERY, budgets=budgets, clock=clock)
    except SourceUnavailable as exc:
        return exc, transport
    raise AssertionError("the enumeration was expected to fail")


class PackageQueryTests(unittest.TestCase):
    def test_daily_identity_is_an_issue_ordinal_not_a_day(self):
        query = daily_package_query("daily/202300220")
        self.assertEqual(query.ojs_number, "220/2023")
        self.assertEqual(query.expression, "OJ = 220/2023")
        self.assertEqual(query.scope, "ALL")
        self.assertEqual(daily_package_query("daily/202000001").expression, "OJ = 1/2020")

    def test_only_canonical_daily_identities_are_supported(self):
        for value in (
            "daily/2023220",       # the OJS number is five digits, not three
            "daily/2023002200",    # too long
            "monthly/202311",
            "daily/202300220 ",
            "notice/daily/202300220",
            "daily/202300000",     # issue zero
            "daily/188800220",     # implausible year
            "",
        ):
            with self.subTest(value=value), self.assertRaises(UnsupportedPackage):
                daily_package_query(value)


class PaginationTests(unittest.TestCase):
    def test_a_terminal_empty_page_that_still_carries_a_token_ends_the_walk(self):
        result, transport = walk([
            api_page([1, 2], total=3, token="page-2"),
            api_page([3], total=3, token="page-3"),
            api_page([], total=3, token="still-here"),
        ])
        self.assertTrue(result.complete)
        self.assertEqual(result.keys, {(2023, 1), (2023, 2), (2023, 3)})
        self.assertEqual((result.record_count, result.duplicate_count), (3, 0))
        self.assertEqual((result.announced_total, result.pages_fetched), (3, 3))
        self.assertEqual(len(transport.requests), 3)

    def test_the_first_request_carries_the_derived_query_and_no_token(self):
        _, transport = walk([api_page([1], total=1), api_page([], total=1)])
        payload = transport.requests[0]["payload"]
        self.assertEqual(payload["query"], "OJ = 220/2023")
        self.assertEqual(payload["scope"], "ALL")
        self.assertEqual(payload["paginationMode"], "ITERATION")
        self.assertIs(payload["checkQuerySyntax"], False)
        self.assertEqual(
            payload["fields"], ["publication-number", "publication-date", "ojs-number"]
        )
        self.assertNotIn("iterationNextToken", payload)
        self.assertEqual(transport.requests[1]["payload"]["iterationNextToken"], "next-token")

    def test_a_missing_token_after_every_record_arrived_also_ends_the_walk(self):
        result, transport = walk([
            api_page([1, 2], total=3, token="page-2"),
            api_page([3], total=3, token=None),
        ])
        self.assertTrue(result.complete)
        self.assertEqual(result.record_count, 3)
        self.assertEqual(len(transport.requests), 2)

    def test_zero_results_enumerate_completely_and_announce_zero(self):
        result, _ = walk([api_page([], total=0)])
        self.assertTrue(result.complete)
        self.assertEqual((result.announced_total, result.record_count), (0, 0))
        self.assertEqual(result.keys, set())

    def test_a_short_final_page_is_not_treated_as_the_end(self):
        # 2 of a 3-record total: the page being smaller than the limit proves
        # nothing, so the walk must ask again rather than stop here.
        exc, transport = fails([
            api_page([1, 2], total=3, token=None),
        ])
        self.assertIn("ended without a token", str(exc))
        self.assertEqual(len(transport.requests), 1)

    def test_an_early_empty_page_is_incomplete_not_finished(self):
        exc, _ = fails([api_page([1], total=5), api_page([], total=5)])
        self.assertIn("stopped after 1 records", str(exc))
        self.assertEqual(exc.observed.record_count, 1)
        self.assertEqual(exc.observed.announced_total, 5)
        self.assertFalse(exc.observed.complete)

    def test_a_repeated_page_is_counted_as_duplicates_and_cannot_finish(self):
        exc, _ = fails([
            api_page([1, 2], total=4, token="a"),
            api_page([1, 2], total=4, token="b"),
            api_page([], total=4, token="c"),
        ])
        self.assertEqual(exc.observed.duplicate_count, 2)
        self.assertEqual(exc.observed.record_count, 4)
        self.assertEqual(len(exc.observed.keys), 2)
        self.assertIn("2 distinct", str(exc))

    def test_a_repeated_token_stops_the_walk(self):
        exc, _ = fails([
            api_page([1, 2], total=6, token="loop"),
            api_page([3, 4], total=6, token="loop"),
        ])
        self.assertIn("repeated an iteration token", str(exc))

    def test_a_changing_total_stops_the_walk(self):
        exc, _ = fails([api_page([1, 2], total=4), api_page([3], total=9)])
        self.assertIn("total changed from 4 to 9", str(exc))

    def test_more_records_than_announced_stops_the_walk(self):
        exc, _ = fails([api_page([1, 2], total=3), api_page([3, 4], total=3)])
        self.assertIn("received 4 records", str(exc))

    def test_pagination_cannot_exceed_the_page_budget(self):
        pages = [api_page([n], total=99, token=f"t{n}") for n in range(1, 6)]
        exc, transport = fails(pages, budgets=Budgets(page_size=1, max_pages=3))
        self.assertIn("exceeded 3 pages", str(exc))
        self.assertEqual(len(transport.requests), 3)

    def test_a_total_over_the_notice_budget_stops_before_paging(self):
        exc, transport = fails(
            [api_page([1], total=50_000)], budgets=Budgets(page_size=1, max_notices=10)
        )
        self.assertIn("over the 10 budget", str(exc))
        self.assertEqual(len(transport.requests), 1)


class ContractTests(unittest.TestCase):
    def bad(self, body, *, message):
        exc, transport = fails([raw_page(json.dumps(body).encode())])
        self.assertIn(message, str(exc))
        self.assertEqual(len(transport.requests), 1, "a contract failure must not be retried")

    def test_a_timed_out_page_is_never_accepted(self):
        self.bad(search_body([1], total=1, timed_out=True), message="timedOut")

    def test_a_boolean_is_not_a_total(self):
        body = search_body([], total=0)
        body["totalNoticeCount"] = True
        self.bad(body, message="not a count")

    def test_a_negative_total_is_rejected(self):
        self.bad(search_body([], total=-1), message="not a count")

    def test_a_missing_notices_list_is_rejected(self):
        body = search_body([], total=0)
        del body["notices"]
        self.bad(body, message="no notices list")

    def test_a_notice_from_another_issue_is_rejected(self):
        body = search_body([1], total=1, ojs="221/2023")
        self.bad(body, message="not 220/2023")

    def test_a_missing_requested_field_is_rejected(self):
        body = search_body([1], total=1)
        del body["notices"][0]["publication-date"]
        self.bad(body, message="missing publication-date")

    def test_a_publication_date_that_is_not_a_calendar_date_is_rejected(self):
        self.bad(
            search_body([1], total=1, publication_date="last Tuesday"),
            message="not a calendar date",
        )

    def test_a_non_canonical_publication_number_is_rejected(self):
        body = search_body([1], total=1)
        body["notices"][0]["publication-number"] = "0-2023"
        self.bad(body, message="not canonical")

    def test_an_empty_iteration_token_is_rejected(self):
        self.bad(search_body([1], total=2, token=""), message="not a non-empty string")

    def test_html_instead_of_json_is_not_retried(self):
        exc, transport = fails([raw_page(b"<html>maintenance</html>")])
        self.assertIn("not JSON", str(exc))
        self.assertEqual(len(transport.requests), 1)

    def test_a_json_array_is_not_a_response(self):
        exc, _ = fails([raw_page(b"[]")])
        self.assertIn("not a JSON object", str(exc))

    def test_dates_with_different_offsets_are_the_same_calendar_date(self):
        result, _ = walk([
            api_page([1], total=2, publication_date="2023-11-15+01:00", token="page-2"),
            api_page([2], total=2, publication_date="2023-11-15Z", token=None),
        ])
        self.assertTrue(result.complete)


class RetryTests(unittest.TestCase):
    def test_a_transient_status_is_retried_with_bounded_backoff(self):
        clock = FakeClock()
        result, transport = walk(
            [
                raw_page(b"", status=503),
                raw_page(b"", status=503),
                api_page([1], total=1),
                api_page([], total=1),
            ],
            clock=clock,
        )
        self.assertTrue(result.complete)
        self.assertEqual(clock.slept, [1.0, 2.0])
        self.assertEqual(result.http_attempts, 4)
        self.assertEqual(len(transport.requests), 4)

    def test_a_transient_status_gives_up_after_the_attempt_budget(self):
        clock = FakeClock()
        exc, transport = fails([raw_page(b"", status=503)] * 3, clock=clock)
        self.assertIn("HTTP 503 after 3 attempts", str(exc))
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(exc.observed.http_attempts, 3)

    def test_retry_after_in_seconds_is_respected(self):
        clock = FakeClock()
        walk(
            [
                raw_page(b"", status=429, headers={"retry-after": "5"}),
                api_page([1], total=1),
                api_page([], total=1),
            ],
            clock=clock,
        )
        self.assertEqual(clock.slept, [5.0])

    def test_retry_after_as_an_http_date_is_respected(self):
        clock = FakeClock()
        when = (clock.wall + timedelta(seconds=7)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        walk(
            [
                raw_page(b"", status=503, headers={"retry-after": when}),
                api_page([1], total=1),
                api_page([], total=1),
            ],
            clock=clock,
        )
        self.assertEqual(clock.slept, [7.0])

    def test_a_retry_after_beyond_the_budget_stops_instead_of_sleeping_less(self):
        clock = FakeClock()
        exc, transport = fails(
            [raw_page(b"", status=503, headers={"retry-after": "120"})],
            budgets=Budgets(page_size=2, total_seconds=30.0),
            clock=clock,
        )
        self.assertIn("exceeds the remaining budget", str(exc))
        self.assertEqual(clock.slept, [])
        self.assertEqual(len(transport.requests), 1)

    def test_a_connection_failure_is_retried_and_can_recover(self):
        clock = FakeClock()
        result, transport = walk(
            [
                TransientSourceError("connection failed: reset"),
                api_page([1], total=1),
                api_page([], total=1),
            ],
            clock=clock,
        )
        self.assertTrue(result.complete)
        self.assertEqual(clock.slept, [1.0])
        self.assertEqual(len(transport.requests), 3)

    def test_terminal_statuses_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                exc, transport = fails([raw_page(b"", status=status)])
                self.assertEqual(str(exc), f"HTTP {status}")
                self.assertEqual(len(transport.requests), 1)

    def test_an_exhausted_time_budget_stops_the_walk(self):
        clock = FakeClock()

        def spend(request_number):
            clock.elapsed += 20.0

        exc, transport = fails(
            [api_page([n], total=9, token=f"t{n}") for n in range(1, 5)],
            budgets=Budgets(page_size=1, total_seconds=30.0),
            clock=clock,
            before_request=spend,
        )
        self.assertIn("time budget is exhausted", str(exc))
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(exc.observed.pages_fetched, 2)

    def test_the_operation_timeout_never_exceeds_the_remaining_budget(self):
        clock = FakeClock()
        transport = FakeTransport([api_page([1], total=1), api_page([], total=1)])
        enumerate_publication_keys(
            transport,
            QUERY,
            budgets=Budgets(page_size=1, total_seconds=5.0, operation_timeout=20.0),
            clock=clock,
        )
        self.assertEqual(transport.requests[0]["timeout"], 5.0)


class RetryAfterParsingTests(unittest.TestCase):
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    def test_values(self):
        self.assertIsNone(parse_retry_after(None, self.now))
        self.assertIsNone(parse_retry_after("soon", self.now))
        self.assertEqual(parse_retry_after(" 12 ", self.now), 12.0)
        past = (self.now - timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertEqual(parse_retry_after(past, self.now), 0.0)


class DigestTests(unittest.TestCase):
    def test_the_digest_is_order_independent_and_matches_the_documented_recipe(self):
        keys = [(2023, 12), (2023, 3)]
        self.assertEqual(keys_digest(keys), keys_digest(reversed(keys)))
        self.assertEqual(keys_digest([(2023, 1)]), keys_digest({(2023, 1)}))
        self.assertNotEqual(keys_digest([(2023, 1)]), keys_digest([(2023, 10)]))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # BaseHTTPRequestHandler's required naming
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received.append(json.loads(self.rfile.read(length)))
        status, body, headers = self.server.reply(self)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TransportTests(unittest.TestCase):
    """The shipped client against a real socket. Plain HTTP keeps the test local;
    TLS validation is a property of the context, asserted separately."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        # A client that hangs up on a timeout is the point of one of these tests;
        # its broken pipe is not a test failure and should not print a traceback.
        self.server.handle_error = lambda request, address: None
        self.server.received = []
        self.server.reply = lambda handler: (200, b"{}", {})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/search"
        self.transport = UrllibTransport()

    def post(self, **kwargs):
        return self.transport.post_json(
            self.url, {"query": "OJ = 220/2023"},
            timeout=kwargs.pop("timeout", 5.0),
            max_bytes=kwargs.pop("max_bytes", 64 * 1024),
        )

    def test_it_posts_json_and_returns_the_body(self):
        payload = json.dumps(search_body([1], total=1, ojs=DEFAULT_OJS)).encode()
        self.server.reply = lambda handler: (200, payload, {})
        response = self.post()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["totalNoticeCount"], 1)
        self.assertEqual(self.server.received, [{"query": "OJ = 220/2023"}])

    def test_an_error_status_still_returns_its_headers(self):
        self.server.reply = lambda handler: (503, b"busy", {"Retry-After": "9"})
        response = self.post()
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["retry-after"], "9")

    def test_an_advertised_oversized_body_is_refused_before_reading_it(self):
        self.server.reply = lambda handler: (200, b"x" * 5000, {})
        with self.assertRaises(SourceUnavailable) as caught:
            self.post(max_bytes=100)
        self.assertIn("advertises 5000 bytes", str(caught.exception))

    def test_an_undeclared_oversized_body_is_refused_while_reading(self):
        big = b"x" * 5000
        self.server.reply = lambda handler: (200, big, {})
        original = _Handler.do_POST

        def chunked(handler):  # send without a usable Content-Length
            handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
            handler.send_response(200)
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            handler.wfile.write(b"1388\r\n" + big + b"\r\n0\r\n\r\n")

        _Handler.do_POST = chunked
        self.addCleanup(setattr, _Handler, "do_POST", original)
        with self.assertRaises(SourceUnavailable) as caught:
            self.post(max_bytes=100)
        self.assertIn("exceeds the 100 byte budget", str(caught.exception))

    def test_a_slow_response_times_out_as_a_transient_failure(self):
        import time

        def slow(handler):
            time.sleep(1.5)
            return 200, b"{}", {}

        self.server.reply = slow
        with self.assertRaises(TransientSourceError):
            self.post(timeout=0.2)

    def test_a_refused_connection_is_a_transient_failure(self):
        self.server.shutdown()
        self.server.server_close()
        self.addCleanup(setattr, self.server, "server_close", lambda: None)
        self.addCleanup(setattr, self.server, "shutdown", lambda: None)
        with self.assertRaises(TransientSourceError):
            self.post(timeout=1.0)

    def test_the_tls_context_validates_certificates_and_hostnames(self):
        import ssl

        self.assertTrue(self.transport.context.check_hostname)
        self.assertEqual(self.transport.context.verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
