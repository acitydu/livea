"""Deterministic coverage of money units, minute boundaries and trading dates."""

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from turnover import MARKET_CODES, MinuteCache, TurnoverService, format_yi, parse_minutes


def payload(code, stamp="20261014102027", today="20261014", previous="20261013"):
    quote = [""] * 31
    quote[30] = stamp
    return {"code": 0, "data": {code: {
        "qt": {code: quote}, "data": [
            {"date": today, "data": ["1000 3800 10 10000000000",
                                      "1020 3800 20 20000000000",
                                      "1021 3800 21 21000000000"]},
            {"date": previous, "data": ["1000 3800 12 12000000000",
                                         "1020 3800 25 25000000000",
                                         "1021 3800 26 26000000000"]},
        ],
    }}}


class TurnoverTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "minute_cache.json"
        self.now = datetime(2026, 10, 14, 10, 20, 30)
        self.data = {c: payload(c) for c in MARKET_CODES}
        self.urls = []
        self.history_error = False
        self.market_error = None
        self.service = TurnoverService(self.path, self.fetch, lambda: self.now)

    def fetch(self, url):
        self.urls.append(url)
        code = parse_qs(urlparse(url).query)["code"][0]
        if code == self.market_error or (self.history_error and "/day/" in url):
            raise OSError("offline")
        result = copy.deepcopy(self.data[code])
        if "/minute/" in url:
            result["data"][code]["data"] = result["data"][code]["data"][0]
        return result

    def test_completed_minute_not_mutating_last_point(self):
        result = self.service.refresh()
        self.assertEqual(result.minute, "1020")
        self.assertEqual(result.current_yuan, Decimal("40000000000"))
        self.assertEqual(result.difference_yuan, Decimal("-10000000000"))
        self.assertEqual(format_yi(result.difference_yuan, True), "-100亿")
        saved = json.loads(self.path.read_text())
        self.assertNotIn("1021", saved["days"]["20261014"][MARKET_CODES[0]])

    def test_history_only_once_then_today_endpoint(self):
        self.service.refresh()
        self.service.refresh()
        self.assertEqual(sum("/day/" in url for url in self.urls), 2)
        self.assertEqual(sum("/minute/" in url for url in self.urls), 2)

    def test_monday_compares_friday_without_calendar_guess(self):
        self.now = datetime(2026, 10, 12, 10, 20, 30)
        self.data = {c: payload(c, "20261012102027", "20261012", "20261009") for c in MARKET_CODES}
        self.assertEqual(self.service.refresh().previous_date, "20261009")

    def test_holiday_gap_uses_actual_previous_trading_date(self):
        self.now = datetime(2026, 10, 8, 10, 20, 30)
        self.data = {c: payload(c, "20261008102027", "20261008", "20260930") for c in MARKET_CODES}
        self.assertEqual(self.service.refresh().previous_date, "20260930")

    def test_lunch_and_close_use_final_available_minute(self):
        for stamp, minute, walltime in [("20261014113003", "1130", (12, 10)),
                                       ("20261014150003", "1500", (18, 0))]:
            with self.subTest(minute=minute):
                self.now = datetime(2026, 10, 14, *walltime)
                for code in MARKET_CODES:
                    self.data[code] = payload(code, stamp)
                    for day in self.data[code]["data"][code]["data"]:
                        day["data"] = [f"{minute} 3800 20 20000000000"]
                self.service = TurnoverService(self.path, self.fetch, lambda: self.now)
                self.assertEqual(self.service.refresh().minute, minute)

    def test_weekend_keeps_actual_trade_date(self):
        self.now = datetime(2026, 10, 17, 12)
        self.data = {c: payload(c, "20261016150003", "20261016", "20261015") for c in MARKET_CODES}
        for code in MARKET_CODES:
            for day in self.data[code]["data"][code]["data"]:
                day["data"] = ["1500 3800 20 20000000000"]
        self.assertEqual(self.service.refresh().trading_date, "20261016")

    def test_one_market_failure_never_creates_half_total(self):
        self.market_error = MARKET_CODES[1]
        with self.assertRaises(OSError):
            self.service.refresh()
        self.assertFalse(self.path.exists())

    def test_missing_previous_minute_does_not_use_nearest(self):
        self.data[MARKET_CODES[1]]["data"][MARKET_CODES[1]]["data"][1]["data"].pop(1)
        with self.assertRaisesRegex(ValueError, "同一分钟"):
            self.service.refresh()

    def test_missing_current_minute_does_not_show_old_comparison(self):
        self.data[MARKET_CODES[1]]["data"][MARKET_CODES[1]]["data"][0]["data"].pop(1)
        with self.assertRaisesRegex(ValueError, "最新已完成分钟"):
            self.service.refresh()

    def test_mismatched_market_dates_rejected(self):
        code = MARKET_CODES[1]
        self.data[code] = payload(code, "20261013102027", "20261013", "20261012")
        with self.assertRaisesRegex(ValueError, "行情日期"):
            self.service.refresh()

    def test_mismatched_previous_dates_rejected(self):
        code = MARKET_CODES[1]
        self.data[code] = payload(code, previous="20261012")
        with self.assertRaisesRegex(ValueError, "上一交易日不一致"):
            self.service.refresh()

    def test_stale_market_rejected(self):
        self.now = datetime(2026, 10, 14, 10, 30)
        with self.assertRaisesRegex(ValueError, "三分钟"):
            self.service.refresh()

    def test_restart_uses_cache_if_history_endpoint_fails(self):
        first = self.service.refresh()
        self.history_error = True
        restarted = TurnoverService(self.path, self.fetch, lambda: self.now)
        self.assertEqual(first, restarted.refresh())

    def test_cross_day_refresh_loads_new_baseline(self):
        self.service.refresh()
        self.now = datetime(2026, 10, 15, 10, 20, 30)
        self.data = {c: payload(c, "20261015102027", "20261015", "20261014") for c in MARKET_CODES}
        result = self.service.refresh()
        self.assertEqual(result.previous_date, "20261014")
        self.assertEqual(sum("/day/" in url for url in self.urls), 4)

    def test_legacy_cache_is_backed_up_exactly_before_rebuild(self):
        old = '{"date":"20261013","amounts":{"date":"20261012","amounts":{}}}'
        self.path.write_text(old)
        self.service = TurnoverService(self.path, self.fetch, lambda: self.now)
        self.service.refresh()
        backups = list(self.path.parent.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), old)
        self.assertEqual(json.loads(self.path.read_text())["version"], 2)
        self.service.refresh()
        self.assertEqual(len(list(self.path.parent.glob("*.bak"))), 1)

    def test_corrupt_cache_can_recover(self):
        self.path.write_text('{broken')
        self.service = TurnoverService(self.path, self.fetch, lambda: self.now)
        self.assertEqual(self.service.refresh().minute, "1020")

    def test_cache_retains_only_five_dates(self):
        cache = MinuteCache(self.path)
        cache.days = {f"202610{d:02}": {} for d in range(1, 10)}
        cache.save()
        self.assertEqual(list(cache.days), [f"202610{d:02}" for d in range(5, 10)])

    def test_invalid_amounts_fail_closed(self):
        for value in ["NaN", "Infinity", "-1", "bad"]:
            with self.subTest(value=value):
                code = MARKET_CODES[0]
                data = payload(code)
                data["data"][code]["data"][0]["data"] = [f"1020 3800 20 {value}"]
                with self.assertRaises(ValueError):
                    parse_minutes(data, code, True)

    def test_rounding_positive_negative_and_zero(self):
        self.assertEqual(format_yi(150000000, True), "+2亿")
        self.assertEqual(format_yi(-150000000, True), "-2亿")
        self.assertEqual(format_yi(-10000, True), "+0亿")

    def test_cancellation_does_not_retry_network(self):
        with self.assertRaises(InterruptedError):
            self.service.refresh(lambda: True)
        self.assertEqual(self.urls, [])


if __name__ == "__main__":
    unittest.main()
