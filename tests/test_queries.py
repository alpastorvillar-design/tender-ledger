"""The shipped SQL query has a correctness fixture and a stated grain."""

import tempfile
import unittest
from pathlib import Path

import psycopg

from ted_fixtures import (
    TEST_DB,
    eforms_member,
    ensure_test_database,
    legacy_member,
    truncate_all,
    write_package,
)
from tender_ledger import db
from tender_ledger.config import load_config
from tender_ledger.loader import load_package

_QUERY = (Path(__file__).resolve().parents[1] / "queries" / "monthly_notice_counts.sql").read_text()


def setUpModule():
    ensure_test_database()


class MonthlyNoticeCountsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.conn = db.connect(load_config(dbname=TEST_DB))
        except psycopg.OperationalError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.conn.close)
        truncate_all(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_grain_is_month_country_cpv_division_over_distinct_notices(self):
        november = [
            legacy_member(1, date_pub="20231115", country="PL", cpv=("79000000",)),
            legacy_member(2, date_pub="20231120", country="PL", cpv=("79500000", "79600000")),
            eforms_member(3, pub_date="2023-11-15Z", buyer_country="POL", main_cpv="72000000"),
            legacy_member(4, date_pub="20231122", country="", cpv=()),  # absent country + cpv
        ]
        december = [legacy_member(5, date_pub="20231205", country="ES", cpv=("45000000",))]
        load_package(self.conn, write_package(self.dir / "nov", november), "daily/nov")
        load_package(self.conn, write_package(self.dir / "dec", december), "daily/dec")

        rows = self.conn.execute(_QUERY).fetchall()
        as_dict = {(str(m), c, cpv): n for m, c, cpv, n in rows}

        self.assertEqual(as_dict[("2023-11-01", "PL", "79000000")], 2)
        self.assertEqual(as_dict[("2023-11-01", "PL", "72000000")], 1)
        self.assertEqual(as_dict[("2023-11-01", "(absent)", "(absent)")], 1)
        self.assertEqual(as_dict[("2023-12-01", "ES", "45000000")], 1)
        self.assertEqual(sum(as_dict.values()), 5)

    def test_overlapping_packages_do_not_double_count(self):
        daily = [legacy_member(10, date_pub="20231115", country="DE", cpv=("30000000",))]
        monthly = [legacy_member(10, date_pub="20231115", country="DE", cpv=("30000000",))]
        load_package(self.conn, write_package(self.dir / "d", daily), "daily/2023220")
        load_package(self.conn, write_package(self.dir / "m", monthly), "monthly/202311")

        rows = self.conn.execute(_QUERY).fetchall()
        self.assertEqual(sum(n for *_, n in rows), 1)


if __name__ == "__main__":
    unittest.main()
