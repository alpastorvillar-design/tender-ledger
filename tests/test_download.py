"""The package downloader against a local HTTP server. TED is never contacted.

Every case here drives the shipped client over a real socket: the product URL is
derived from the package identity and only the tests inject a local one, so a
downloader that quietly accepted an arbitrary URL would fail these tests.
"""

import gzip
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from ted_fixtures import legacy_member, package_bytes
from tender_ledger.download import (
    DownloadBudgets,
    DownloadError,
    TransientDownloadError,
    artifact_destination,
    download_package,
    package_url,
)
from tender_ledger.source_api import UnsupportedPackage

PACKAGE = "daily/202300220"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # BaseHTTPRequestHandler's required naming
        self.server.received.append(dict(self.headers))
        self.server.reply(self)

    def log_message(self, *args):
        pass


def body_reply(payload, *, status=200, headers=None, declare_length=True):
    def reply(handler):
        handler.send_response(status)
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        if declare_length:
            handler.send_header("Content-Length", str(len(payload)))
        else:
            handler.send_header("Connection", "close")
            handler.close_connection = True
        handler.end_headers()
        handler.wfile.write(payload)

    return reply


class DownloadTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = package_bytes([legacy_member(1001), legacy_member(1002)])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        # A client that hangs up mid-body is the point of several tests; the
        # resulting broken pipe is expected and should not print a traceback.
        self.server.handle_error = lambda request, address: None
        self.server.received = []
        self.server.reply = body_reply(self.archive)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/packages/daily/202300220"

    def destination(self, package=PACKAGE):
        return artifact_destination(self.root, package)

    def download(self, package=PACKAGE, **kwargs):
        kwargs.setdefault("url", self.url)
        return download_package(package, self.destination(package), **kwargs)

    def part_files(self):
        return sorted(p.name for p in self.destination().parent.glob("*.part-*"))


class UrlDerivationTests(unittest.TestCase):
    def test_the_url_comes_from_the_package_identity(self):
        self.assertEqual(
            package_url(PACKAGE), "https://ted.europa.eu/packages/daily/202300220"
        )

    def test_an_unsupported_identity_has_no_url_and_no_path(self):
        for bad in ("monthly/202300", "daily/2023", "daily/../etc", "notices/1"):
            with self.subTest(package=bad):
                with self.assertRaises(UnsupportedPackage):
                    package_url(bad)
                with self.assertRaises(UnsupportedPackage):
                    artifact_destination(Path("data"), bad)

    def test_the_destination_stays_inside_the_data_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = artifact_destination(root, PACKAGE)
            self.assertTrue(destination.resolve().is_relative_to(root.resolve()))
            self.assertEqual(destination.name, "202300220.tar.gz")


class HappyPathTests(DownloadTestCase):
    def test_a_complete_archive_lands_at_its_destination_and_is_described(self):
        result = self.download()
        self.assertEqual(result.path, self.destination())
        self.assertEqual(result.size_bytes, len(self.archive))
        self.assertEqual(self.destination().read_bytes(), self.archive)
        self.assertEqual(result.notice_count, 2)
        self.assertEqual(result.http_attempts, 1)
        self.assertEqual(self.part_files(), [])

    def test_the_request_asks_for_an_untransformed_body(self):
        self.download()
        self.assertEqual(self.server.received[0]["Accept-Encoding"], "identity")

    def test_a_content_disposition_filename_never_decides_the_path(self):
        self.server.reply = body_reply(
            self.archive,
            headers={"Content-Disposition": 'attachment; filename="../../escape.tar.gz"'},
        )
        result = self.download()
        self.assertEqual(result.path, self.destination())
        self.assertEqual([p.name for p in self.root.rglob("*.tar.gz")], ["202300220.tar.gz"])


class RetryTests(DownloadTestCase):
    def replies(self, sequence):
        """Serve one reply per request, in order."""
        def reply(handler):
            sequence[min(len(self.server.received) - 1, len(sequence) - 1)](handler)

        self.server.reply = reply

    def test_a_503_is_retried_and_then_succeeds(self):
        self.replies([body_reply(b"busy", status=503), body_reply(self.archive)])
        result = self.download(budgets=DownloadBudgets(backoff_base_seconds=0.01))
        self.assertEqual(result.http_attempts, 2)
        self.assertEqual(result.size_bytes, len(self.archive))

    def test_the_attempt_limit_stops_a_persistently_failing_source(self):
        self.replies([body_reply(b"busy", status=503)])
        with self.assertRaises(DownloadError) as caught:
            self.download(budgets=DownloadBudgets(max_attempts=3, backoff_base_seconds=0.01))
        self.assertIn("503", str(caught.exception))
        self.assertEqual(len(self.server.received), 3)
        self.assertFalse(self.destination().exists())

    def test_a_retry_after_longer_than_the_budget_stops_instead_of_hitting_the_source(self):
        self.replies([body_reply(b"busy", status=429, headers={"Retry-After": "600"})])
        with self.assertRaises(DownloadError) as caught:
            self.download(budgets=DownloadBudgets(total_seconds=5.0))
        self.assertIn("exceeds the remaining budget", str(caught.exception))
        self.assertEqual(len(self.server.received), 1)

    def test_a_truncated_body_is_retried_from_the_first_byte(self):
        def short(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(self.archive) + 500))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(self.archive[:200])
            handler.close_connection = True

        self.replies([short, body_reply(self.archive)])
        result = self.download(budgets=DownloadBudgets(backoff_base_seconds=0.01))
        self.assertEqual(result.http_attempts, 2)
        self.assertEqual(self.destination().read_bytes(), self.archive)
        self.assertEqual(self.part_files(), [])

    def test_a_404_is_not_a_valid_empty_window_and_is_never_retried(self):
        self.replies([body_reply(b"not found", status=404)])
        with self.assertRaises(DownloadError) as caught:
            self.download()
        self.assertNotIsInstance(caught.exception, TransientDownloadError)
        self.assertIn("404", str(caught.exception))
        self.assertEqual(len(self.server.received), 1)


class LimitTests(DownloadTestCase):
    def test_an_advertised_oversized_body_is_refused_before_it_is_read(self):
        self.server.reply = body_reply(b"x" * 5000)
        with self.assertRaises(DownloadError) as caught:
            self.download(budgets=DownloadBudgets(max_artifact_bytes=1000))
        self.assertIn("advertises 5000 bytes", str(caught.exception))
        self.assertEqual(self.part_files(), [])

    def test_an_undeclared_oversized_body_is_stopped_while_reading(self):
        payload = b"x" * 5000
        self.server.reply = body_reply(payload, declare_length=False)
        with self.assertRaises(DownloadError) as caught:
            self.download(budgets=DownloadBudgets(max_artifact_bytes=1000, max_attempts=1))
        self.assertIn("exceeds", str(caught.exception))
        self.assertEqual(self.part_files(), [])

    def test_the_aggregate_byte_budget_stops_repeated_attempts(self):
        # Each attempt is truncated, so each one is retriable and each one costs
        # bytes. Without an aggregate budget the retries would transfer forever.
        def truncated(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "8000")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b"x" * 4000)
            handler.close_connection = True

        self.server.reply = truncated
        with self.assertRaises(DownloadError) as caught:
            self.download(budgets=DownloadBudgets(
                max_artifact_bytes=10_000, max_total_bytes=6_000,
                max_attempts=5, backoff_base_seconds=0.01,
            ))
        self.assertIn("byte budget", str(caught.exception))
        self.assertEqual(len(self.server.received), 2)
        self.assertEqual(self.part_files(), [])

    def test_a_slow_body_is_stopped_by_the_time_budget(self):
        stop = threading.Event()
        self.addCleanup(stop.set)

        def trickle(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(self.archive) + 4000))
            handler.end_headers()
            for _ in range(200):
                if stop.is_set():
                    return
                handler.wfile.write(b" " * 32)
                handler.wfile.flush()
                time.sleep(0.04)

        self.server.reply = trickle
        started = time.monotonic()
        with self.assertRaises(DownloadError) as caught:
            self.download(budgets=DownloadBudgets(
                total_seconds=0.3, operation_timeout=0.2, max_attempts=1,
            ))
        self.assertIn("time budget", str(caught.exception))
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(self.part_files(), [])


class ContentTests(DownloadTestCase):
    def test_html_with_a_200_is_not_an_archive(self):
        self.server.reply = body_reply(
            b"<html><body>maintenance</body></html>",
            headers={"Content-Type": "text/html"},
        )
        with self.assertRaises(DownloadError) as caught:
            self.download()
        self.assertIn("corrupt archive", str(caught.exception))
        self.assertFalse(self.destination().exists())
        self.assertEqual(self.part_files(), [])

    def test_json_with_a_200_is_not_an_archive(self):
        self.server.reply = body_reply(json.dumps({"error": "nope"}).encode())
        with self.assertRaises(DownloadError):
            self.download()
        self.assertFalse(self.destination().exists())

    def test_a_corrupt_gzip_never_reaches_the_destination(self):
        broken = bytearray(self.archive)
        broken[-3] ^= 0xFF  # break the gzip CRC
        self.server.reply = body_reply(bytes(broken))
        with self.assertRaises(DownloadError):
            self.download()
        self.assertFalse(self.destination().exists())
        self.assertEqual(self.part_files(), [])

    def test_a_transformed_body_is_refused(self):
        self.server.reply = body_reply(
            gzip.compress(self.archive), headers={"Content-Encoding": "gzip"}
        )
        with self.assertRaises(DownloadError) as caught:
            self.download()
        self.assertIn("Content-Encoding", str(caught.exception))

    def test_an_unrequested_partial_response_is_refused(self):
        self.server.reply = body_reply(
            self.archive[:100], status=206,
            headers={"Content-Range": f"bytes 0-99/{len(self.archive)}"},
        )
        with self.assertRaises(DownloadError) as caught:
            self.download()
        self.assertIn("206", str(caught.exception))

    def test_a_redirect_away_from_the_requested_origin_is_not_followed(self):
        self.server.reply = body_reply(
            b"", status=302, headers={"Location": "http://example.invalid/elsewhere"}
        )
        with self.assertRaises(DownloadError) as caught:
            self.download()
        self.assertIn("redirect", str(caught.exception).lower())
        self.assertEqual(len(self.server.received), 1)


class ReuseTests(DownloadTestCase):
    def test_a_second_download_replaces_the_destination_atomically(self):
        first = self.download()
        second = self.download()
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(self.destination().read_bytes(), self.archive)
        self.assertEqual(self.part_files(), [])

    def test_a_failed_download_leaves_an_existing_artifact_untouched(self):
        self.download()
        self.server.reply = body_reply(b"gone", status=500)
        with self.assertRaises(DownloadError):
            self.download(budgets=DownloadBudgets(max_attempts=1))
        self.assertEqual(self.destination().read_bytes(), self.archive)
        self.assertEqual(self.part_files(), [])


if __name__ == "__main__":
    unittest.main()
