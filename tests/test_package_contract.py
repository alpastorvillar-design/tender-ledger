"""Canonical package identities and the one resource policy each kind carries.

Nothing here touches the network, the filesystem or PostgreSQL: the module under
test is the contract every other layer reads its numbers and its derivations
from, so its own tests need no infrastructure.
"""

import dataclasses
import datetime as dt
import unittest

from tender_ledger import download, packages, source_api
from tender_ledger.package_contract import (
    DAILY,
    DAILY_POLICY,
    MONTHLY,
    MONTHLY_POLICY,
    UnsupportedPackage,
    package_identity,
    policy_for,
)


class DailyIdentityTests(unittest.TestCase):
    def test_a_daily_identity_keeps_its_issue_ordinal_semantics(self):
        identity = package_identity("daily/202300220")
        self.assertEqual(identity.kind, DAILY)
        self.assertEqual(identity.url_path, "daily/202300220")
        self.assertEqual(identity.destination_parts, ("packages", "daily", "202300220.tar.gz"))
        self.assertEqual(identity.ojs_number, "220/2023")
        self.assertEqual(identity.query_expression, "OJ = 220/2023")
        self.assertIsNone(identity.publication_interval)

    def test_the_smallest_and_largest_supported_issues_derive(self):
        self.assertEqual(package_identity("daily/199000001").query_expression, "OJ = 1/1990")
        self.assertEqual(
            package_identity("daily/209999999").query_expression, "OJ = 99999/2099"
        )

    def test_only_canonical_daily_identities_are_supported(self):
        for value in (
            "daily/2023220",       # the OJS number is five digits, not three
            "daily/2023002200",    # too long
            "daily/202300220 ",
            "daily/202300000",     # issue zero
            "daily/188800220",     # implausible year
            "notice/daily/202300220",
            "daily/../etc/passwd",
            "",
        ):
            with self.subTest(value=value), self.assertRaises(UnsupportedPackage):
                package_identity(value)


class MonthlyIdentityTests(unittest.TestCase):
    def test_the_url_segment_drops_the_leading_zero_and_the_destination_keeps_it(self):
        identity = package_identity("monthly/2024-01")
        self.assertEqual(identity.kind, MONTHLY)
        self.assertEqual(identity.url_path, "monthly/2024-1")
        self.assertEqual(identity.destination_parts, ("packages", "monthly", "2024-01.tar.gz"))
        self.assertIsNone(identity.ojs_number)

    def test_a_two_digit_month_keeps_both_digits_in_the_url(self):
        self.assertEqual(package_identity("monthly/2023-11").url_path, "monthly/2023-11")

    def test_the_interval_covers_every_calendar_day_of_the_month(self):
        cases = {
            "monthly/2020-01": (dt.date(2020, 1, 1), dt.date(2020, 1, 31)),
            "monthly/2023-02": (dt.date(2023, 2, 1), dt.date(2023, 2, 28)),  # not a leap year
            "monthly/2020-02": (dt.date(2020, 2, 1), dt.date(2020, 2, 29)),  # a leap year
            "monthly/2021-04": (dt.date(2021, 4, 1), dt.date(2021, 4, 30)),  # a 30-day month
            "monthly/2024-12": (dt.date(2024, 12, 1), dt.date(2024, 12, 31)),
        }
        for value, interval in cases.items():
            with self.subTest(value=value):
                self.assertEqual(package_identity(value).publication_interval, interval)

    def test_the_query_asks_for_the_whole_month_inclusively(self):
        self.assertEqual(
            package_identity("monthly/2020-02").query_expression,
            "PD>=20200201 AND PD<=20200229",
        )
        self.assertEqual(
            package_identity("monthly/2024-12").query_expression,
            "PD>=20241201 AND PD<=20241231",
        )

    def test_only_a_zero_padded_month_in_range_is_supported(self):
        for value in (
            "monthly/2020-1",      # unpadded: a second identity for one package
            "monthly/2020-00",
            "monthly/2020-13",
            "monthly/202001",
            "monthly/20-01",
            "monthly/1889-01",     # implausible year
            "monthly/2100-01",
            "monthly/2020-01 ",
            "monthly/2020-01/..",
            "monthly//2020-01",
            "monthly/2020-01.tar.gz",
            "notice/monthly/2020-01",
            "monthly\\2020-01",
        ):
            with self.subTest(value=value), self.assertRaises(UnsupportedPackage):
                package_identity(value)

    def test_the_destination_components_are_never_path_syntax(self):
        for value in ("monthly/../../etc", "monthly/2020-01/../../etc", "daily/../202300220"):
            with self.subTest(value=value), self.assertRaises(UnsupportedPackage):
                package_identity(value)


class PolicyTests(unittest.TestCase):
    MIB = 1024 * 1024

    def test_the_daily_policy_is_exactly_what_the_daily_flow_already_enforced(self):
        policy = policy_for("daily/202300220")
        self.assertEqual(policy.compressed_bytes, 64 * self.MIB)
        self.assertEqual(policy.expanded_bytes, 512 * self.MIB)
        self.assertEqual(policy.member_bytes, 8 * self.MIB)
        self.assertEqual(policy.notices, 10_000)
        self.assertEqual(policy.download_total_bytes, 192 * self.MIB)
        self.assertEqual(policy.download_seconds, 300.0)
        self.assertEqual(policy.api_max_pages, 50)
        self.assertEqual(policy.api_seconds, 300.0)

    def test_the_monthly_policy_is_the_accepted_contract(self):
        policy = policy_for("monthly/2024-01")
        self.assertEqual(policy.compressed_bytes, 512 * self.MIB)
        self.assertEqual(policy.expanded_bytes, 8 * 1024 * self.MIB)
        self.assertEqual(policy.member_bytes, 32 * self.MIB)
        self.assertEqual(policy.notices, 150_000)
        self.assertEqual(policy.download_total_bytes, 1_536 * self.MIB)
        self.assertEqual(policy.download_seconds, 1_800.0)
        self.assertEqual(policy.api_max_pages, 620)
        self.assertEqual(policy.api_page_size, 250)
        self.assertEqual(policy.api_seconds, 1_800.0)

    def test_per_operation_timeouts_retries_and_backoff_do_not_vary_by_kind(self):
        daily, monthly = policy_for("daily/202300220"), policy_for("monthly/2024-01")
        for field in ("download_attempts", "operation_timeout", "api_page_size",
                      "api_page_attempts", "api_response_bytes", "backoff_base_seconds",
                      "backoff_max_seconds", "chunk_bytes"):
            with self.subTest(field=field):
                self.assertEqual(getattr(daily, field), getattr(monthly, field))

    def test_a_monthly_package_never_lowers_a_daily_ceiling(self):
        daily, monthly = policy_for("daily/202300220"), policy_for("monthly/2024-01")
        for field in ("compressed_bytes", "expanded_bytes", "member_bytes", "notices",
                      "download_total_bytes", "download_seconds", "api_max_pages",
                      "api_seconds"):
            with self.subTest(field=field):
                self.assertGreaterEqual(getattr(monthly, field), getattr(daily, field))

    def test_every_shipped_policy_holds_its_invariants(self):
        for value in ("daily/202300220", "monthly/2020-01"):
            policy = policy_for(value)
            with self.subTest(value=value):
                self.assertGreater(min(policy.compressed_bytes, policy.member_bytes,
                                        policy.notices, policy.api_max_pages), 0)
                self.assertGreaterEqual(
                    policy.download_total_bytes,
                    policy.download_attempts * policy.compressed_bytes,
                )
                self.assertGreaterEqual(
                    policy.api_max_pages * policy.api_page_size,
                    policy.notices + policy.api_page_size,
                )
                self.assertLessEqual(policy.member_bytes, policy.expanded_bytes)

    def test_a_non_positive_ceiling_is_refused_at_construction(self):
        for field in ("compressed_bytes", "notices", "download_seconds", "api_max_pages"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                dataclasses.replace(DAILY_POLICY, **{field: 0})

    def test_retrying_more_than_the_aggregate_budget_allows_is_refused(self):
        with self.assertRaisesRegex(ValueError, "attempts"):
            dataclasses.replace(
                DAILY_POLICY, download_total_bytes=DAILY_POLICY.compressed_bytes
            )

    def test_pagination_that_cannot_reach_the_notice_ceiling_is_refused(self):
        with self.assertRaisesRegex(ValueError, "pages"):
            dataclasses.replace(MONTHLY_POLICY, api_max_pages=100)

    def test_a_member_larger_than_the_whole_expansion_is_refused(self):
        with self.assertRaisesRegex(ValueError, "member"):
            dataclasses.replace(DAILY_POLICY, member_bytes=DAILY_POLICY.expanded_bytes * 2)

    def test_the_policy_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            DAILY_POLICY.notices = 1


class PolicyAdapterTests(unittest.TestCase):
    """Each layer keeps its own limit object; none of them keeps its own numbers."""

    def test_the_three_layers_read_one_policy_per_kind(self):
        for value in ("daily/202300220", "monthly/2020-01"):
            policy = policy_for(value)
            with self.subTest(value=value):
                limits = packages.limits_for(value)
                self.assertEqual(
                    (limits.compressed_bytes, limits.expanded_bytes,
                     limits.member_bytes, limits.notices),
                    (policy.compressed_bytes, policy.expanded_bytes,
                     policy.member_bytes, policy.notices),
                )
                budgets = download.budgets_for(value)
                self.assertEqual(budgets.max_artifact_bytes, policy.compressed_bytes)
                self.assertEqual(budgets.max_total_bytes, policy.download_total_bytes)
                self.assertEqual(budgets.max_attempts, policy.download_attempts)
                self.assertEqual(budgets.total_seconds, policy.download_seconds)
                api = source_api.budgets_for(value)
                self.assertEqual(api.max_notices, policy.notices)
                self.assertEqual(api.max_pages, policy.api_max_pages)
                self.assertEqual(api.page_size, policy.api_page_size)
                self.assertEqual(api.total_seconds, policy.api_seconds)

    def test_the_archive_and_the_source_agree_on_the_same_notice_ceiling(self):
        # Divergence here is the failure that lets an archive load and then be
        # impossible to verify.
        for value in ("daily/202300220", "monthly/2020-01"):
            with self.subTest(value=value):
                self.assertEqual(
                    packages.limits_for(value).notices,
                    source_api.budgets_for(value).max_notices,
                )

    def test_the_downloader_can_never_accept_more_than_the_archive_walker(self):
        for value in ("daily/202300220", "monthly/2020-01"):
            with self.subTest(value=value):
                self.assertEqual(
                    download.budgets_for(value).max_artifact_bytes,
                    packages.limits_for(value).compressed_bytes,
                )


class UnrecognizedIdentityTests(unittest.TestCase):
    def test_a_lower_level_unknown_label_gets_the_narrowest_policy(self):
        # Repository-level recovery can encounter an old manual label. It must
        # never widen a ceiling, even though operator-facing adapters reject it.
        self.assertEqual(policy_for("daily/scratch"), DAILY_POLICY)
        self.assertEqual(policy_for("monthly/2020-1"), DAILY_POLICY)
        self.assertEqual(policy_for(""), DAILY_POLICY)

    def test_operator_facing_adapters_reject_an_unrecognized_identity(self):
        for resolve in (
            package_identity,
            packages.limits_for,
            download.budgets_for,
            source_api.budgets_for,
        ):
            with self.subTest(resolve=resolve.__module__), self.assertRaises(
                UnsupportedPackage
            ):
                resolve("daily/scratch")


if __name__ == "__main__":
    unittest.main()
