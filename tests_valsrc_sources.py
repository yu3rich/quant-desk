# -*- coding: utf-8 -*-
"""估值多源：新增新浪/通达信价格兜底源的回归测试。

核心风险：价格源（comp=0）若被当成估值采纳源，会用没有 PE/PB/市值的
rows 覆盖掉带完整字段的估值文件。这里锁住「价格源只提示、不落盘」的语义。
"""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

import valsrc


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  OK", msg)


def test_meta_and_order():
    print("[1] 源清单与顺序")
    # 腾讯只给 PE(TTM)：_row 必须用 pe_ttm 兜底，否则 pe 整列为空
    r = valsrc._row("600519", date="20261125", price=1460, pe=None,
                    pe_ttm=22.2, pb=6.8, total_mv=18000)
    check(r.get("pe") == 22.2, "只有 pe_ttm 时 pe 用 TTM 兜底：%s" % r.get("pe"))
    check(r.get("pe_ttm") == 22.2, "pe_ttm 原值保留")
    r2 = valsrc._row("600519", date="20261125", price=1460, pe=21.0, pe_ttm=22.2)
    check(r2.get("pe") == 21.0, "有独立 pe 时不被 TTM 覆盖")

    for k in ("sina", "tdx"):
        check(k in valsrc.META, "%s 在 META 中登记" % k)
        check(valsrc.META[k]["cap"] == "price", "%s 是价格兜底类型（不参与估值采纳）" % k)
        check(k in valsrc.RUNNERS, "%s 已注册取数函数" % k)
    order = valsrc.DEFAULT_ORDER
    check(order.index("tx") == 0, "腾讯是出厂第一主源")
    check(order.index("tx") < order.index("sina") < order.index("ts"),
          "出厂顺序：腾讯 → 新浪/通达信 → Tushare（兜底源排在历史源前）")
    check(order.index("ts") < order.index("em") < order.index("bs"),
          "东财退到 Tushare 之后、补漏源之前，做低频备份")
    check(valsrc.META["em"].get("backup") is True
          and valsrc.META["em"].get("backoff_sec") == 6 * 3600,
          "东财标记为低频备份，失败冷却 6 小时")

    # 老配置（em 打头）迁移一次；用户自定义顺序不该被覆盖
    orig = valsrc.CFG_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            valsrc.CFG_PATH = Path(td) / "valsrc.json"
            valsrc.CFG_PATH.write_text(json.dumps({
                "order": ["em", "tx", "ts", "bs"],
                "enabled": {"em": True, "tx": True, "ts": True, "bs": False},
            }), "utf-8")
            cfg2 = valsrc.load_cfg()
            check(cfg2["order"] == valsrc.DEFAULT_ORDER,
                  "老出厂顺序迁移为 %s" % cfg2["order"])
            check(json.loads(valsrc.CFG_PATH.read_text("utf-8")).get("order_version") == 2,
                  "迁移结果已落盘，不会反复改用户顺序")
            custom = ["em", "ts", "tx", "sina", "tdx", "bs"]
            valsrc.save({"order": custom})
            check(valsrc.load_cfg()["order"] == custom, "用户自定义顺序保持不变")
            # 老配置里没列腾讯（曾关掉它）也不能在迁移时越界
            valsrc.CFG_PATH.write_text(json.dumps({"order": ["em", "ts", "bs"]}), "utf-8")
            check(valsrc.load_cfg()["order"] == valsrc.DEFAULT_ORDER,
                  "老配置缺腾讯时迁移不报错")
    finally:
        valsrc.CFG_PATH = orig


def test_sina_source():
    print("[2] 新浪源真实取数")
    r = valsrc.test("sina")
    check(r.get("ok"), "新浪单源测试可用：%s" % r.get("error", ""))
    check(int(r.get("count") or 0) > 4000, "取到全市场 %s 只" % r.get("count"))
    check(float(r.get("complete") or 0) == 0.0,
          "complete=0（无 PE/PB/市值），不会被误当估值源")
    check(r.get("date", "")[:2] == "20", "日期正常：%s" % r.get("date"))


def test_price_backup_not_written():
    print("[3] 主源全挂时价格兜底不污染估值文件")
    orig, orig_try = valsrc.CFG_PATH, valsrc._try
    # 盘中新浪只会返回「今天未收盘」，拿真实网络跑这段会随时间段时红时绿；
    # 这里只测采纳逻辑，用固定价格快照代替取数（真实取数由 [2] 覆盖）。
    def fake_try(key, t):
        if key == "sina":
            return {"ok": True, "date": t, "ms": 3,
                    "rows": [{"code": "000001", "price": 10.0}]}
        return {"ok": False, "error": "已关闭", "rows": []}

    try:
        valsrc._try = fake_try
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "valsrc.json"
            valsrc.CFG_PATH = p
            cfg = valsrc.load_cfg()
            cfg["enabled"] = {"em": False, "tx": False, "sina": True,
                              "tdx": False, "ts": False, "bs": False}
            # 假装之前已有全市场估值，被测的是「本次是否会覆盖它」
            cfg["fetched"] = {"date": "20261124", "count": 5600,
                              "source": "tx", "at": "2026-11-24 15:00:00"}
            valsrc.save(cfg)
            r = valsrc.fetch(force=True, date="20261124")
            check(len(r.get("rows") or []) == 0,
                  "价格源不返回 rows（不会覆盖估值文件）")
            check(bool(r.get("stale")), "如实标记 stale（沿用旧估值）")
            pb = r.get("price_backup") or []
            check(any(x.get("key") == "sina" for x in pb),
                  "price_backup 记录新浪可用：%s" % pb)
            check("价格通道仍可用" in str(r.get("message") or ""),
                  "消息里提示价格通道仍可用")
    finally:
        valsrc.CFG_PATH, valsrc._try = orig, orig_try


def test_backup_quiet_and_cooldown():
    print("[4] 东财备份：失败退避 6 小时、不写 warn")
    orig, orig_log = valsrc.CFG_PATH, valsrc._log
    logs = []
    try:
        with tempfile.TemporaryDirectory() as td:
            valsrc.CFG_PATH = Path(td) / "valsrc.json"
            valsrc.save_cfg(valsrc.load_cfg())
            valsrc._log = lambda msg, level="info", act="", detail="": logs.append((level, act))
            now = dt.datetime(2026, 11, 24, 16, 0, 0)
            valsrc._persist_last([
                {"key": "em", "ok": False, "error": "连接失败"},
                {"key": "tx", "ok": False, "error": "读取超时"},
            ], now)
            last = valsrc.load_cfg()["last"]
            check(last["em"]["next_try"] == "2026-11-24 22:00:00",
                  "东财失败后 6 小时才重试：%s" % last["em"]["next_try"])
            check(last["tx"]["next_try"] == "2026-11-24 16:01:30",
                  "普通源仍是 90 秒退避：%s" % last["tx"]["next_try"])
            check(("info", "em") in logs and ("warn", "em") not in logs,
                  "东财失败只记 info，普通源失败仍记 warn：%s" % logs)
    finally:
        valsrc.CFG_PATH, valsrc._log = orig, orig_log


if __name__ == "__main__":
    test_meta_and_order()
    test_sina_source()
    test_price_backup_not_written()
    test_backup_quiet_and_cooldown()
    print("\n全部通过")
