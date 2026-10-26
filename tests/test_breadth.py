"""Market coverage, counts, and exchange timestamps for the breadth snapshot."""

import copy
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from breadth import parse_breadth
from turnover import CHINA_TZ


class BreadthTests(unittest.TestCase):
    def setUp(self):
        self.stamp = int(datetime(2026, 10, 26, 10, 20, 30, tzinfo=CHINA_TZ).timestamp())
        self.payload = {"rc": 0, "data": {"diff": [
            {"f12": "000002", "f13": 1, "f104": 800, "f105": 1400, "f124": self.stamp},
            {"f12": "399107", "f13": 0, "f104": 700, "f105": 2100, "f124": self.stamp - 3},
        ]}}

    def test_combines_a_share_markets_using_earliest_china_timestamp(self):
        result = parse_breadth(self.payload)
        self.assertEqual((result.advancing, result.declining), (1500, 3500))
        self.assertEqual(result.timestamp, datetime(2026, 10, 26, 10, 20, 27))

    def test_zero_advances_are_valid(self):
        for row in self.payload["data"]["diff"]:
            row["f104"] = 0
        self.assertEqual(parse_breadth(self.payload).advancing, 0)

    def test_missing_duplicate_or_wrong_market_never_gives_a_partial_total(self):
        rows = self.payload["data"]["diff"]
        for invalid_rows in (rows[:1], rows[:1] * 2, rows + [rows[0]],
                             [rows[0], {**rows[1], "f12": "399001"}]):
            with self.subTest(rows=invalid_rows), self.assertRaises(ValueError):
                parse_breadth({"rc": 0, "data": {"diff": invalid_rows}})

    def test_invalid_count_or_timestamp_is_unavailable(self):
        for field, values in (("f104", (None, "-", -1, 1.5, True)),
                              ("f105", (None, "-", -1)),
                              ("f124", (0, None, "-", self.stamp - 86400))):
            for value in values:
                payload = copy.deepcopy(self.payload)
                payload["data"]["diff"][0][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    parse_breadth(payload)

    def test_error_or_missing_payload_is_unavailable(self):
        for payload in ({"rc": 1}, {"rc": 0, "data": None},
                        {"rc": 0, "data": {"diff": []}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_breadth(payload)


if __name__ == "__main__":
    unittest.main()
