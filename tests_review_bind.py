# -*- coding: utf-8 -*-
"""实盘复盘结合项回归：风险预算日表、跌破参考位、计划-执行偏差、
成本 vs 同行估值分位、Kelly 滚动窗口。"""
import emtai
import emtbook
import portfolio
import riskview
import store

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def test_risk_daily():
    print("\n[风险预算日表]")
    day = "1999-01-02"
    try:
        store.risk_daily_put({"date": day, "total": 3.5, "budget": 1.0, "usage": 350,
                              "top_code": "600519", "top_name": "贵州茅台",
                              "top_risk": 2.0, "exposure": 60, "n_pos": 5})
        store.risk_daily_put({"date": day, "total": 4.0, "budget": 1.0, "usage": 400,
                              "top_code": "600519", "top_name": "贵州茅台",
                              "top_risk": 2.4, "exposure": 62, "n_pos": 5})
        rows = [x for x in store.risk_daily_list(400) if x["date"] == day]
        check(len(rows) == 1, "同一天重复写入只有一行（upsert）")
        check(rows and abs(rows[0]["usage"] - 400) < 1e-6, "同日数值被更新为最新值")
        days = [x["date"] for x in store.risk_daily_list(400)]
        check(days == sorted(days), "趋势按日期升序返回")
    finally:
        store.conn().execute("DELETE FROM risk_daily WHERE date=?", (day,))
        store.conn().commit()


def test_risk_summary():
    print("\n[风险汇总：预算使用率与跌破参考位]")
    pos = {"items": [{"code": "600519", "name": "贵州茅台",
                      "mv_share": 30, "cost": 100.0, "price": 80.0}]}
    risk = {"600519": {"atr_pct": 5.0}}
    r = emtbook._risk_summary(pos, risk, {"atr_mult": 2.0, "risk_pct": 0.01})
    check(abs((r.get("total") or 0) - 3.0) < 1e-6, "合计风险 30%×5%×2=3%")
    check(abs((r.get("usage") or 0) - 300) < 1e-6, "预算使用率 3%/1%=300%")
    below = r.get("below_ref") or []
    check(len(below) == 1, "成本 100、ATR 5%% 时参考位 90，现价 80 判定跌破")
    if below:
        check(abs(below[0]["ref"] - 90) < 1e-6, "参考位按成本×(1-2×ATR) 计算")
    ok_pos = {"items": [{"code": "600519", "name": "贵州茅台",
                         "mv_share": 10, "cost": 100.0, "price": 120.0}]}
    check(not (emtbook._risk_summary(ok_pos, risk, {"atr_mult": 2.0, "risk_pct": 0.01})
               .get("below_ref")), "现价高于参考位时不进列表")


def test_plan_deviation():
    print("\n[计划-执行偏差]")
    orig_plan, orig_fills = store.plan_latest, emtbook._all_fills
    try:
        store.plan_latest = lambda: {
            "id": 1, "at": "2026-11-24 09:00:00", "as_of": "2026-11-24",
            "payload": {"date": "2026-11-24", "mode": "equal", "topk": 10, "n_drop": 2,
                        "buys": [{"code": "600519", "name": "贵州茅台"}],
                        "sells": [{"code": "000001", "name": "平安银行"},
                                  {"code": "000002", "name": "万科A"}]}}
        emtbook._all_fills = lambda led: [
            {"date": "2026-11-25", "code": "600519", "name": "贵州茅台",
             "side": "买入", "qty": 100, "price": 1500},
            {"date": "2026-11-25", "code": "000858", "name": "五粮液",
             "side": "买入", "qty": 200, "price": 150},
            {"date": "2026-11-25", "code": "000001", "name": "平安银行",
             "side": "卖出", "qty": 1000, "price": 10},
        ]
        d = emtbook._plan_deviation([{"date": "2026-11-25"}])
        check(d.get("since") == "2026-11-24", "计划日取自存档")
        check(d.get("done_buy") == 1 and d.get("done_sell") == 1,
              "计划内买入/卖出各执行 1 只")
        check([x["name"] for x in (d.get("extra_buy") or [])] == ["五粮液"],
              "五粮液识别为计划外买入")
        check(abs((d.get("extra_ratio") or 0) - 15.8) < 0.2,
              "计划外金额占比 30000/190000≈15.8%%（实际 %s）" % d.get("extra_ratio"))
        check((d.get("planned_sell") or 0) - (d.get("done_sell") or 0) == 1,
              "计划卖出 2 只、实际 1 只，差额 1 可用于「该卖没卖」提示")

        # 计划日之后没有成交
        emtbook._all_fills = lambda led: [
            {"date": "2026-11-20", "code": "600519", "name": "贵州茅台",
             "side": "买入", "qty": 100, "price": 1500}]
        d2 = emtbook._plan_deviation([{"date": "2026-11-20"}])
        check(d2.get("empty") is True, "计划后无成交时如实标记 empty")
    finally:
        store.plan_latest, emtbook._all_fills = orig_plan, orig_fills


def test_cost_vs_peers():
    print("\n[成本 vs 同行 PE 75 分位]")
    orig = riskview.valuation_batch
    try:
        riskview.valuation_batch = lambda pairs: {
            str(c).zfill(6): {"price_band_pe": [10, 12, 15], "industry": "白酒", "peers": 30}
            for c, _p in pairs}
        diag = emtbook._diagnose(
            {"overview": {"total": 1000000, "avail": 200000}},
            {"items": [{"code": "600519", "name": "贵州茅台", "mv_share": 20,
                        "cost": 20.0, "price": 18.0, "mv": 200000, "pnl": -1}],
             "top3": 20, "eff_n": 1, "count": 1},
            {"turnover_rate": 0}, {"mdd": 0}, True, {}, {}, risk={})
        hit = [x for x in diag if "75 分位" in (x.get("title") or "")]
        check(bool(hit), "成本 20 > 75 分位 15 时触发提示")
        check(hit and "不是卖出信号" in (hit[0].get("detail") or ""),
              "措辞明确是相对估值位置，不是卖出信号")
        riskview.valuation_batch = lambda pairs: {
            str(c).zfill(6): {"price_band_pe": [10, 12, 25], "industry": "白酒", "peers": 30}
            for c, _p in pairs}
        diag2 = emtbook._diagnose(
            {"overview": {"total": 1000000, "avail": 200000}},
            {"items": [{"code": "600519", "name": "贵州茅台", "mv_share": 20,
                        "cost": 20.0, "price": 18.0, "mv": 200000, "pnl": -1}],
             "top3": 20, "eff_n": 1, "count": 1},
            {"turnover_rate": 0}, {"mdd": 0}, True, {}, {}, risk={})
        check(not [x for x in diag2 if "75 分位" in (x.get("title") or "")],
              "成本低于 75 分位时不触发")
    finally:
        riskview.valuation_batch = orig


def test_kelly_rolling():
    print("\n[Kelly 滚动窗口]")
    orig_cfg, orig_board = portfolio.cfg, emtbook.board
    try:
        portfolio.cfg = lambda: {"kelly_win": 0, "kelly_pl": 0, "kelly_window": 10}
        recent = ([{"pnl": 100.0}] * 6 + [{"pnl": -50.0}] * 4)
        older = [{"pnl": -1000.0}] * 10
        emtbook.board = lambda days=180: {"trades": {
            "closed": 20, "win_rate": 30.0, "pl_ratio": 0.5,
            "closed_list": recent + older}}
        wr, pl, src = portfolio._kelly_params()
        check(src == "emtbook:10", "只取最近 10 笔（%s）" % src)
        check(abs(wr - 60) < 1e-6, "滚动胜率 6/10=60%%（实际 %s）" % wr)
        check(abs(pl - 2.0) < 1e-6, "滚动盈亏比 100/50=2.0（实际 %s）" % pl)

        portfolio.cfg = lambda: {"kelly_win": 55, "kelly_pl": 1.8, "kelly_window": 10}
        wr2, pl2, src2 = portfolio._kelly_params()
        check(src2 == "config" and wr2 == 55 and pl2 == 1.8,
              "配置了显式胜率时优先于账本")
    finally:
        portfolio.cfg, emtbook.board = orig_cfg, orig_board


def test_journal_checks():
    print("\n[日记明日检查]")
    board = {
        "overview": {"total": 100000, "day_pnl": -100, "mv": 50000, "avail": 50000},
        "positions": {"items": [{"code": "600519", "name": "贵州茅台",
                                 "mv": 50000, "mv_share": 50, "pnl": -100}],
                      "count": 1, "top3": 50, "eff_n": 1},
        "trades": {}, "curve": {}, "attribution": {},
        "diagnose": [{"level": "warn", "title": "单票超限：贵州茅台 占总资产 50%"}],
        "risk": {"items": [{"name": "贵州茅台", "risk_pct": 2.0, "mv_share": 50,
                            "atr_pct": 2.0}],
                 "risk_pct": 1.0, "atr_mult": 2.0, "total": 2.0, "usage": 200.0,
                 "below_ref": [{"name": "贵州茅台", "cost": 1500.0, "price": 1300.0,
                                "ref": 1440.0, "below_pct": -9.7}]},
        "plan_dev": {"ok": True, "empty": False, "since": "2026-11-20",
                     "extra_ratio": 55.0, "total_amt": 20000, "extra_amt": 11000},
    }
    t = emtai.local_journal(board)
    check("占单笔预算 200%" in t, "风险预算行带使用率")
    check("跌破 2×ATR 参考位" in t, "正文列出跌破参考位的持仓")
    check("核对 贵州茅台 已跌破 2×ATR 参考位" in t, "明日检查点名该持仓")
    check("计划外成交" in t, "明日检查包含计划外成交复盘")


if __name__ == "__main__":
    test_risk_daily()
    test_risk_summary()
    test_plan_deviation()
    test_cost_vs_peers()
    test_kelly_rolling()
    test_journal_checks()
    print("\n" + ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    raise SystemExit(1 if FAILED else 0)
