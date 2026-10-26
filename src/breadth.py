"""Shanghai and Shenzhen A-share advances/declines from Eastmoney snapshots."""

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from turnover import CHINA_TZ


# A-share composites cover both markets without counting sub-indices twice.
BREADTH_MARKETS = {(1, "000002"), (0, "399107")}
BREADTH_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get"
    "?secids=1.000002,0.399107&fields=f12,f13,f104,f105,f124"
)


@dataclass(frozen=True)
class Breadth:
    advancing: int
    declining: int
    timestamp: datetime


def parse_breadth(payload):
    try:
        if payload["rc"] != 0:
            raise ValueError("涨跌家数接口返回错误")
        rows = payload["data"]["diff"]
        markets = {}
        for row in rows:
            key = (row["f13"], row["f12"])
            if key not in BREADTH_MARKETS or key in markets:
                raise ValueError("涨跌家数市场范围不完整")
            counts = [row[field] for field in ("f104", "f105")]
            if any(type(value) is not int or value < 0 for value in counts):
                raise ValueError("涨跌家数格式无效")
            stamp = row["f124"]
            if type(stamp) is not int or stamp <= 0:
                raise ValueError("涨跌家数缺少行情时间")
            timestamp = datetime.fromtimestamp(stamp, CHINA_TZ).replace(tzinfo=None)
            markets[key] = (*counts, timestamp)
        if markets.keys() != BREADTH_MARKETS:
            raise ValueError("涨跌家数缺少沪深市场数据")
        if len({data[2].date() for data in markets.values()}) != 1:
            raise ValueError("涨跌家数行情日期不一致")
        return Breadth(
            sum(data[0] for data in markets.values()),
            sum(data[1] for data in markets.values()),
            min(data[2] for data in markets.values()),
        )
    except (KeyError, TypeError, OverflowError, OSError) as exc:
        raise ValueError("涨跌家数数据不完整") from exc


def request_breadth():
    request = urllib.request.Request(BREADTH_URL, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        return parse_breadth(json.loads(response.read().decode("utf-8")))
