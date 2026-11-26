# -*- coding: utf-8 -*-
"""行情看图的数据源降级：腾讯新路径 → 腾讯备域名 → 新浪日线合成。

2026-11-26 腾讯把老路径 /appstock/app/fqkline/get 拦成 501，日周月图全挂。
这里锁住两件事：
  1. 任何一条源挂掉都不能让整个接口 500，要按顺序退到下一源；
  2. 新浪只有日线，周/月必须自己聚合，聚合口径（开=首、收=末、高低极值、量求和）
     一旦写错，图上会出现假的跳空。
"""
import unittest
from unittest import mock

import chartdata


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _sina_rows(n=10, start_day=1):
    """造 n 个连续交易日的日线（跳过周末，避免周聚合口径被自然日干扰）。"""
    import datetime as dt
    out, d = [], dt.date(2026, 11, start_day)
    while len(out) < n:
        if d.isoweekday() <= 5:
            i = len(out)
            out.append({"day": d.strftime("%Y-%m-%d"), "open": str(10 + i),
                        "high": str(11 + i), "low": str(9 + i), "close": str(10.5 + i),
                        "volume": str((100 + i) * 100)})   # 股 → 应除以 100 转手
        d += dt.timedelta(days=1)
    return out


class AggTest(unittest.TestCase):
    def test_week_aggregate(self):
        bars = [{"date": "2026-11-02", "open": 10, "close": 10.5, "high": 11, "low": 9.5, "volume": 100},
                {"date": "2026-11-03", "open": 10.5, "close": 12, "high": 12.5, "low": 10, "volume": 200},
                {"date": "2026-11-04", "open": 12, "close": 11, "high": 12.8, "low": 10.8, "volume": 300}]
        out = chartdata._agg_bars(bars, "week")
        self.assertEqual(len(out), 1)
        w = out[0]
        self.assertEqual(w["open"], 10)        # 首日开
        self.assertEqual(w["close"], 11)       # 末日收
        self.assertEqual(w["high"], 12.8)      # 区间最高
        self.assertEqual(w["low"], 9.5)        # 区间最低
        self.assertEqual(w["volume"], 600)     # 量求和
        self.assertEqual(w["date"], "2026-11-04")

    def test_month_split(self):
        bars = [{"date": "2026-10-30", "open": 1, "close": 2, "high": 3, "low": 0.5, "volume": 10},
                {"date": "2026-11-02", "open": 2, "close": 3, "high": 4, "low": 1.5, "volume": 20}]
        out = chartdata._agg_bars(bars, "month")
        self.assertEqual([x["date"] for x in out], ["2026-10-30", "2026-11-02"])
        self.assertEqual(out[0]["close"], 2)


class FallbackTest(unittest.TestCase):
    def setUp(self):
        chartdata.invalidate()

    def tearDown(self):
        chartdata.invalidate()

    def test_primary_ok(self):
        chartdata._tencent_kline = mock.Mock(return_value=(
            [{"date": "2026-11-25", "open": 1, "close": 2, "high": 3, "low": 0.5, "volume": 10},
             {"date": "2026-11-26", "open": 2, "close": 3, "high": 4, "low": 1.5, "volume": 20}],
            {"name": "测试股", "pre_close": 2.0}))
        r = chartdata.kline("300093", "day")
        self.assertEqual(r["source"], "腾讯")
        self.assertEqual(r["adjust"], "qfq")
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["pct"], 50.0)

    def test_both_tencent_down_uses_sina(self):
        chartdata._tencent_kline = mock.Mock(side_effect=RuntimeError("HTTP 501（被 WAF 拦截）"))
        rows = _sina_rows(10)
        with mock.patch.object(chartdata.requests, "get", return_value=_Resp(rows)):
            r = chartdata.kline("300093", "day")
        self.assertEqual(r["source"], "新浪")
        self.assertEqual(r["adjust"], "none")
        self.assertEqual(r["count"], 10)
        self.assertEqual(r["bars"][0]["volume"], 100.0)     # 10000 股 → 100 手
        self.assertIn("不复权", r["advice"])

    def test_sina_week_is_aggregated(self):
        chartdata._tencent_kline = mock.Mock(side_effect=RuntimeError("boom"))
        rows = _sina_rows(10)
        with mock.patch.object(chartdata.requests, "get", return_value=_Resp(rows)):
            r = chartdata.kline("300093", "week")
        self.assertEqual(r["source"], "新浪")
        self.assertLess(r["count"], 10)                    # 10 个交易日必然少于 10 周
        self.assertGreater(r["count"], 0)
        # 每周的收盘价必须是该周最后一个交易日的收盘价
        self.assertEqual(r["bars"][-1]["close"], 10.5 + 9)

    def test_all_down_raises_readable(self):
        chartdata._tencent_kline = mock.Mock(side_effect=RuntimeError("HTTP 501（被 WAF 拦截）"))
        with mock.patch.object(chartdata.requests, "get", side_effect=RuntimeError("timed out")):
            with self.assertRaises(RuntimeError) as ctx:
                chartdata.kline("300093", "day")
        msg = str(ctx.exception)
        self.assertIn("K 线接口都不可用", msg)
        self.assertIn("腾讯", msg)


if __name__ == "__main__":
    unittest.main()
