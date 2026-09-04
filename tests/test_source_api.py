"""Search API query derivation, pagination, retries and the HTTP transport.

These run without PostgreSQL and without touching TED: pagination is driven
through a scripted transport, and the shipped `urllib` client is exercised
against a local HTTP server.
"""

import json
import threading
import time
import unittest
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ted_fixtures import DEFAULT_OJS, FakeTransport, api_page, raw_page, search_body
from tender_ledger.package_contract import UnsupportedPackage
from tender_ledger.source_api import (
    Budgets,
    Clock,
    PackageQuery,
    SourceUnavailable,
    TransientSourceError,
    UrllibTransport,
    budgets_for,
    enumerate_publication_keys,
    keys_digest,
    package_query,
    parse_retry_after,
)

QUERY = package_query("daily/202300220")


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
        query = package_query("daily/202300220")
        self.assertEqual(query.ojs_number, "220/2023")
        self.assertEqual(query.expression, "OJ = 220/2023")
        self.assertEqual(query.scope, "ALL")
        self.assertEqual(package_query("daily/202000001").expression, "OJ = 1/2020")

    def test_only_canonical_package_identities_are_supported(self):
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
                package_query(value)


class MonthlyQueryTests(unittest.TestCase):
    def test_a_monthly_identity_asks_for_its_whole_inclusive_interval(self):
        query = package_query("monthly/2020-02")
        self.assertEqual(query.expression, "PD>=20200201 AND PD<=20200229")
        self.assertEqual(query.scope, "ALL")
        self.assertIsNone(query.ojs_number)
        self.assertEqual(
            query.publication_interval, (date(2020, 2, 1), date(2020, 2, 29))
        )

    def test_a_query_without_a_per_record_rule_cannot_be_built(self):
        # Set equality proves two sets match, not that the source enumerated the
        # window that was asked for. A query with no membership rule would make
        # that the only evidence, so it is refused at construction.
        with self.assertRaisesRegex(ValueError, "membership"):
            PackageQuery(source_package_id="daily/202300220", expression="OJ = 220/2023")
        with self.assertRaisesRegex(ValueError, "membership"):
            PackageQuery(
                source_package_id="daily/202300220", expression="OJ = 220/2023",
                ojs_number="220/2023",
                publication_interval=(date(2023, 11, 1), date(2023, 11, 30)),
            )

    def test_membership_is_decided_by_the_identity_not_by_the_echoed_expression(self):
        query = package_query("monthly/2023-11")
        self.assertIsNone(query.membership_error("999/2023", date(2023, 11, 1)))
        self.assertIsNone(query.membership_error("1/2023", date(2023, 11, 30)))
        for outside in (date(2023, 10, 31), date(2023, 12, 1)):
            with self.subTest(outside=outside):
                self.assertIn(
                    "outside", query.membership_error("220/2023", outside)
                )

    def test_a_daily_query_still_decides_membership_by_the_issue_ordinal(self):
        query = package_query("daily/202300220")
        self.assertIsNone(query.membership_error(DEFAULT_OJS, date(1999, 1, 1)))
        self.assertIn("not 220/2023", query.membership_error("221/2023", date(2023, 11, 15)))


class NoticeBudgetTests(unittest.TestCase):
    """The announced total of a real month has to fit the monthly budget only.

    Both counts are ones the API actually reported for the rehearsal packages, so
    this fixes the ceiling against observed volume rather than a round number.
    """

    OBSERVED = {"monthly/2020-01": 50_123, "monthly/2024-01": 65_708}

    def page(self, total, *, publication_date):
        return api_page(
            [1], total=total, token="page-2", publication_date=publication_date, ojs="1/2024"
        )

    def test_a_daily_budget_refuses_a_month_sized_total_on_the_first_page(self):
        for package, total in self.OBSERVED.items():
            with self.subTest(package=package):
                transport = FakeTransport([self.page(total, publication_date="2024-01-02Z")])
                with self.assertRaises(SourceUnavailable) as caught:
                    enumerate_publication_keys(
                        transport, package_query("monthly/2024-01"),
                        budgets=budgets_for("daily/202300220"), clock=FakeClock(),
                    )
                self.assertIn(f"the source reports {total} notices", str(caught.exception))
                self.assertIn("over the 10000 budget", str(caught.exception))
                self.assertEqual(len(transport.requests), 1)

    def test_the_monthly_budget_accepts_the_same_totals_and_keeps_walking(self):
        for package, total in self.OBSERVED.items():
            with self.subTest(package=package):
                transport = FakeTransport([
                    self.page(total, publication_date="2024-01-02Z"),
                    api_page([], total=total, token="still-here"),
                ])
                with self.assertRaises(SourceUnavailable) as caught:
                    enumerate_publication_keys(
                        transport, package_query("monthly/2024-01"),
                        budgets=budgets_for(package), clock=FakeClock(),
                    )
                # The walk got past the ceiling and stopped only because the
                # scripted source delivered one record of that total, not
                # because of a budget.
                self.assertNotIn("budget", str(caught.exception))
                self.assertIn(
                    f"stopped after 1 records (1 distinct) for a reported total of {total}",
                    str(caught.exception),
                )
                self.assertEqual(len(transport.requests), 2)

    def test_the_monthly_page_ceiling_covers_the_monthly_notice_ceiling(self):
        budgets = budgets_for("monthly/2020-01")
        self.assertGreaterEqual(
            budgets.max_pages * budgets.page_size, budgets.max_notices + budgets.page_size
        )


class MonthlyMembershipTests(unittest.TestCase):
    """Every record of a monthly enumeration has to belong to the month asked for."""

    def walk(self, script, *, package="monthly/2023-11"):
        transport = FakeTransport(script)
        return enumerate_publication_keys(
            transport, package_query(package),
            budgets=Budgets(page_size=2, backoff_base_seconds=1.0), clock=FakeClock(),
        )

    def fails(self, script, **kwargs):
        try:
            self.walk(script, **kwargs)
        except SourceUnavailable as exc:
            return exc
        raise AssertionError("the enumeration was expected to fail")

    def test_both_edges_of_the_month_are_inside_it(self):
        result = self.walk([
            api_page([1], total=2, token="p2", publication_date="2023-11-01+01:00"),
            api_page([2], total=2, token="p3", publication_date="2023-11-30Z"),
            api_page([], total=2, token="still-here"),
        ])
        self.assertTrue(result.complete)
        self.assertEqual(result.keys, {(2023, 1), (2023, 2)})

    def test_one_day_outside_the_month_on_either_side_never_verifies(self):
        for outside in ("2023-10-31Z", "2023-12-01Z"):
            with self.subTest(outside=outside):
                exc = self.fails([api_page([1], total=1, publication_date=outside)])
                self.assertIn("outside 2023-11-01..2023-11-30", str(exc))

    def test_a_missing_or_unparseable_publication_date_never_verifies(self):
        exc = self.fails([api_page([1], total=1, publication_date="2023-11-32Z")])
        self.assertIn("is not a calendar date", str(exc))
        for malformed in ("2023-11-15garbage", "2023-11-15+25:00"):
            with self.subTest(malformed=malformed):
                exc = self.fails([
                    api_page([1], total=1, publication_date=malformed)
                ])
                self.assertIn("is not a calendar date", str(exc))
        exc = self.fails([raw_page(json.dumps({
            "notices": [{"publication-number": "1-2023", "ojs-number": "220/2023"}],
            "totalNoticeCount": 1, "timedOut": False,
        }).encode())])
        self.assertIn("missing publication-date", str(exc))

    def test_a_monthly_walk_ignores_the_issue_a_record_came_from(self):
        # A month spans many OJ S issues, so ojs-number carries no membership
        # information here and must not be turned into one.
        result = self.walk([
            api_page([1], total=2, token="p2", ojs="211/2023",
                     publication_date="2023-11-02Z"),
            api_page([2], total=2, token="p3", ojs="230/2023",
                     publication_date="2023-11-29Z"),
            api_page([], total=2, token="still-here"),
        ])
        self.assertTrue(result.complete)

    def test_the_safe_endings_still_apply_to_a_monthly_walk(self):
        cases = {
            "duplicate": ([api_page([1, 1], total=2, publication_date="2023-11-02Z"),
                           api_page([], total=2)], "stopped after 2 records"),
            "changing total": ([api_page([1], total=2, token="p2",
                                         publication_date="2023-11-02Z"),
                               api_page([2], total=3, publication_date="2023-11-03Z")],
                               "total changed"),
            "repeated token": ([api_page([1], total=3, token="same",
                                         publication_date="2023-11-02Z"),
                               api_page([2], total=3, token="same",
                                        publication_date="2023-11-03Z")],
                               "repeated an iteration token"),
            "premature end": ([api_page([1], total=9, token=None,
                                        publication_date="2023-11-02Z")],
                              "ended without a token"),
        }
        for name, (script, message) in cases.items():
            with self.subTest(case=name):
                self.assertIn(message, str(self.fails(script)))

    def test_an_empty_month_is_reported_as_such_and_never_as_coverage(self):
        result = self.walk([api_page([], total=0)])
        self.assertTrue(result.complete)
        self.assertEqual((result.announced_total, result.keys), (0, set()))


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

    def test_duplicates_within_a_page_and_across_pages_are_counted(self):
        exc, _ = fails([
            api_page([1, "000001"], total=4, token="a"),
            api_page([1, 2], total=4, token="b"),
            api_page([], total=4),
        ])
        self.assertEqual(exc.observed.record_count, 4)
        self.assertEqual(len(exc.observed.keys), 2)
        self.assertEqual(exc.observed.duplicate_count, 2)

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
        self.assertEqual(exc.observed.pages_fetched, 1)  # the late response was rejected

    def test_a_terminal_response_after_the_deadline_cannot_complete(self):
        for terminal in ("no_token", "empty_page"):
            with self.subTest(terminal=terminal):
                clock = FakeClock()
                script = [api_page([1], total=1, token=None)]
                late_request = 1
                if terminal == "empty_page":
                    script = [api_page([1], total=1), api_page([], total=1)]
                    late_request = 2

                def spend(number, expected=late_request, current_clock=clock):
                    if number == expected:
                        current_clock.elapsed = 2.0

                exc, _ = fails(script, budgets=Budgets(total_seconds=1.0),
                               clock=clock, before_request=spend)
                self.assertIn("time budget is exhausted", str(exc))
                self.assertFalse(exc.observed.complete)

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

    def test_valid_json_does_not_hide_a_truncated_http_message(self):
        body = json.dumps(search_body([1], total=1, token=None)).encode()

        def truncated(handler):
            handler.rfile.read(int(handler.headers["Content-Length"]))
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body) + 100))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
            handler.close_connection = True

        original = _Handler.do_POST
        _Handler.do_POST = truncated
        self.addCleanup(setattr, _Handler, "do_POST", original)
        with self.assertRaises(TransientSourceError) as caught:
            self.post()
        self.assertIn("incomplete HTTP body", str(caught.exception))

    def test_a_trickling_body_is_stopped_by_the_verification_budget(self):
        body = json.dumps(search_body([1], total=1, token=None)).encode() + b" " * 3000
        stop = threading.Event()

        def trickle(handler):
            handler.rfile.read(int(handler.headers["Content-Length"]))
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            for offset in range(0, len(body), 32):
                if stop.is_set():
                    return
                handler.wfile.write(body[offset:offset + 32])
                handler.wfile.flush()
                time.sleep(0.04)  # progress prevents a socket inactivity timeout

        original = _Handler.do_POST
        _Handler.do_POST = trickle
        self.addCleanup(setattr, _Handler, "do_POST", original)
        self.addCleanup(stop.set)
        started = time.monotonic()
        with self.assertRaises(SourceUnavailable) as caught:
            enumerate_publication_keys(
                self.transport, QUERY, url=self.url,
                budgets=Budgets(total_seconds=0.25, operation_timeout=0.2),
            )
        self.assertIn("time budget is exhausted", str(caught.exception))
        self.assertLess(time.monotonic() - started, 1.5)

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
