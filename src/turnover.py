"""Tencent minute turnover, aligned by trading date and completed minute.

Amounts are cumulative CNY (not volume, and not per-minute increments).
The live endpoint's last point can be labelled with the *next* minute.
Only points at or before its quote timestamp are eligible for comparison.
"""

import json
import logging
import os
import shutil
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

MARKET_CODES = ("sh000001", "sz399001")
COMPARE_INTERVALS = (1, 5, 15, 30)
YUAN_PER_YI = Decimal("100000000")
CHINA_TZ = timezone(timedelta(hours=8))
SOURCE = "tencent"


def market_now():
    return datetime.now(CHINA_TZ).replace(tzinfo=None)


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("成交额格式无效") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("成交额必须是非负有限数值")
    return result


def format_yi(yuan, signed=False):
    rounded = (Decimal(str(yuan)) / YUAN_PER_YI).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    if rounded == 0:
        rounded = abs(rounded)
    return (f"{rounded:+.0f}" if signed else f"{rounded:.0f}") + "亿"


def request_json(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_minutes(payload, code, history):
    if payload.get("code") != 0:
        raise ValueError("分时接口返回错误")
    try:
        body = payload["data"][code]
        quote_time = datetime.strptime(body["qt"][code][30], "%Y%m%d%H%M%S")
        rows = body["data"] if history else [body["data"]]
        days = {}
        for row in rows:
            date = row["date"]
            datetime.strptime(date, "%Y%m%d")
            points = {}
            previous = Decimal("0")
            for line in row["data"]:
                fields = line.split()
                if len(fields) != 4:
                    raise ValueError("分时数据缺少成交额")
                minute = fields[0]
                if len(minute) != 4:
                    raise ValueError("分时时间无效")
                datetime.strptime(minute, "%H%M")
                if not ("0930" <= minute <= "1130" or "1300" <= minute <= "1500"):
                    raise ValueError("分时时间不在交易时段")
                value = amount(fields[3])
                if value < previous:
                    raise ValueError("累计成交额出现倒退")
                previous = value
                points[minute] = str(value)
            if points:
                days[date] = points
        if quote_time.strftime("%Y%m%d") not in days:
            raise ValueError("行情日期与分时日期不一致")
        return {"timestamp": quote_time, "days": days}
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("分时接口数据不完整") from exc


@dataclass(frozen=True)
class Comparison:
    trading_date: str
    previous_date: str
    minute: str
    current_yuan: Decimal
    previous_yuan: Decimal

    @property
    def difference_yuan(self):
        return self.current_yuan - self.previous_yuan


class MinuteCache:
    """Bounded, date-separated cache. Legacy data is backed up before replacement."""

    def __init__(self, path):
        self.path = Path(path)
        self.days = {}
        self.previous_dates = {}
        self._legacy = False
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (data.get("version"), data.get("source"), data.get("unit")) != (2, SOURCE, "CNY"):
                raise ValueError("旧版缓存")
            for date, markets in data["days"].items():
                datetime.strptime(date, "%Y%m%d")
                for code, points in markets.items():
                    if code not in MARKET_CODES:
                        raise ValueError("未知市场")
                    for minute, value in points.items():
                        datetime.strptime(minute, "%H%M")
                        amount(value)
            for date, previous in data["previous_dates"].items():
                datetime.strptime(date, "%Y%m%d")
                datetime.strptime(previous, "%Y%m%d")
                if previous >= date:
                    raise ValueError("比较日期无效")
            self.days = data["days"]
            self.previous_dates = data["previous_dates"]
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            self._legacy = True
            logging.info("旧版或损坏的成交额缓存将在成功取数后备份并重建")
        except OSError:
            logging.exception("读取成交额缓存失败")
            self._legacy = True

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._legacy and self.path.exists():
            stamp = market_now().strftime("%Y%m%d-%H%M%S-%f")
            backup = self.path.with_name(self.path.name + f".legacy-{stamp}.bak")
            shutil.copy2(self.path, backup)
            self._legacy = False
            logging.info("旧成交额缓存已备份：%s", backup)
        self.days = {date: self.days[date] for date in sorted(self.days)[-5:]}
        self.previous_dates = {
            date: previous for date, previous in self.previous_dates.items()
            if date in self.days and previous in self.days
        }
        data = {"version": 2, "source": SOURCE, "unit": "CNY",
                "days": self.days, "previous_dates": self.previous_dates}
        temporary = self.path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


class TurnoverService:
    """Used serially by the background worker; no Qt or UI dependencies."""

    def __init__(self, cache_path, fetch=request_json, clock=market_now):
        self.cache = MinuteCache(cache_path)
        self.fetch = fetch
        self.clock = clock
        self.history_checked_on = None

    def _fetch_pair(self, history, cancelled):
        endpoint = "day" if history else "minute"
        result = {}
        for code in MARKET_CODES:
            if cancelled():
                raise InterruptedError()
            url = f"https://web.ifzq.gtimg.cn/appstock/app/{endpoint}/query?code={code}"
            result[code] = parse_minutes(self.fetch(url), code, history)
        dates = {r["timestamp"].strftime("%Y%m%d") for r in result.values()}
        if len(dates) != 1:
            raise ValueError("沪深两市行情日期不一致")
        return result

    def refresh(self, cancelled=lambda: False):
        now = self.clock()
        history = self.history_checked_on != now.date()
        try:
            pair = self._fetch_pair(history, cancelled)
        except InterruptedError:
            raise
        except (OSError, ValueError):
            # A cached, previously verified baseline may survive a history endpoint outage.
            if not history or not self.cache.previous_dates:
                raise
            pair = self._fetch_pair(False, cancelled)
            history = False
        date = pair[MARKET_CODES[0]]["timestamp"].strftime("%Y%m%d")
        if not history and date not in self.cache.previous_dates:
            pair = self._fetch_pair(True, cancelled)
            history = True
            date = pair[MARKET_CODES[0]]["timestamp"].strftime("%Y%m%d")

        if history:
            # Each market must agree on the immediately preceding trading date.
            previous = []
            for code in MARKET_CODES:
                dates = sorted(d for d in pair[code]["days"] if d < date)
                if not dates:
                    raise ValueError("缺少上一交易日分时数据")
                previous.append(dates[-1])
            if len(set(previous)) != 1:
                raise ValueError("沪深两市上一交易日不一致")
            self.cache.previous_dates[date] = previous[0]

        # Freeze only completed points. Do not let today's partial point enter tomorrow's cache.
        completed = {}
        for code in MARKET_CODES:
            quote_time = pair[code]["timestamp"]
            if quote_time > now + timedelta(minutes=1):
                raise ValueError("行情时间晚于当前时间")
            active_session = "0930" <= now.strftime("%H%M") <= "1130" or "1300" <= now.strftime("%H%M") <= "1500"
            if quote_time.date() == now.date() and active_session and (now - quote_time).total_seconds() > 180:
                raise ValueError("分时行情已超过三分钟未更新")
            for day, points in pair[code]["days"].items():
                if day > date:
                    continue
                eligible = {
                    minute: value for minute, value in points.items()
                    if datetime.strptime(day + minute, "%Y%m%d%H%M") <= min(quote_time, now)
                }
                self.cache.days.setdefault(day, {})[code] = eligible
                if day == date:
                    completed[code] = eligible
        if history:
            self.history_checked_on = now.date()
        try:
            self.cache.save()
        except OSError:
            logging.exception("保存分时缓存失败；本次仍使用在线数据")

        common = set(completed[MARKET_CODES[0]]) & set(completed[MARKET_CODES[1]])
        if not common:
            raise ValueError("等待沪深两市首个已完成分钟")
        minute = max(common)
        # A missing latest point must not silently turn a live comparison into an old one.
        source_minute = min(r["timestamp"] for r in pair.values()).strftime("%H%M")
        expected = min(source_minute, "1500")
        if "1130" < expected < "1300":
            expected = "1130"
        if minute != expected:
            raise ValueError("沪深两市最新已完成分钟不完整")
        previous_date = self.cache.previous_dates[date]
        try:
            current = sum(amount(completed[c][minute]) for c in MARKET_CODES)
            previous = sum(amount(self.cache.days[previous_date][c][minute]) for c in MARKET_CODES)
        except KeyError as exc:
            self.history_checked_on = None
            raise ValueError("上一交易日同一分钟数据不完整") from exc
        return Comparison(date, previous_date, minute, current, previous)
