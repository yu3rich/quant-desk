# -*- coding: utf-8 -*-
"""全项目审计后的回归：组合、换手、涨跌停、Sortino、股票池导入。"""
import emtbook
import portfolio
import quant
import watchlist

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def _picks(n=10):
    # 每只一个独立行业：否则行业上限会先把总权重压低，换手上限根本不会触发
    return [{"code": "%06d" % (600100 + i), "name": "S%d" % i,
             "score": 10 - i, "close": 10.0, "industry": "行业%d" % i}
            for i in range(n)]


def test_topk_drop():
    print("\n[Topk-Drop：空仓要补满，满仓才受限]")
    ps = _picks(10)
    r = portfolio.target(ps, capital=1000000, current=[],
                         patch={"mode": "equal", "topk": 10, "n_drop": 2})
    check(r.get("n_target") == 10, "空仓 topk=10/ndrop=2 应建 10 只（实际 %s）"
          % r.get("n_target"))

    cur = [{"code": p["code"], "name": p["name"], "qty": 1000, "avail": 1000,
            "cost": 10.0, "price": 10.0, "mv": 10000} for p in ps[:10]]
    r2 = portfolio.target(ps, capital=1000000, current=cur,
                          patch={"mode": "equal", "topk": 10, "n_drop": 2})
    check(r2.get("n_target") == 10, "满仓时仍保持 10 只（实际 %s）" % r2.get("n_target"))
    sells = [x for x in (r2.get("sells") or []) if x.get("action") == "卖出"]
    check(len(sells) <= 2, "满仓时卖出不超过 n_drop（实际 %d）" % len(sells))


def test_old_position_price():
    print("\n[换手上限：未入选老仓必须有价格，否则差额单失效]")
    ps = _picks(10)
    old = [{"code": "000001", "name": "平安银行", "qty": 50000, "avail": 50000,
            "cost": 10.0, "price": 10.0, "mv": 500000}]
    r = portfolio.target(ps, capital=1000000, current=old,
                         patch={"mode": "equal", "topk": 10, "n_drop": 0,
                                "turnover_cap": 0.6})
    row = next((x for x in (r.get("rows") or []) if x["code"] == "000001"), {})
    check(row.get("price", 0) > 0, "老仓带出价格（%s）" % row.get("price"))
    check(row.get("target_qty") != 50000 or row.get("action") != "不动",
          "老仓目标股数不再等于原持仓却标「不动」：%s / %s"
          % (row.get("target_qty"), row.get("action")))
    check(abs((row.get("weight") or 0) - (row.get("target_qty") or 0) * 10.0 / 1000000 * 100) < 0.5,
          "权重与目标股数一致（权重 %s%%，目标 %s 股）"
          % (row.get("weight"), row.get("target_qty")))


def test_odd_lot():
    print("\n[零股持仓：买入差额必须是整手]")
    cur = [{"code": "600100", "name": "S0", "qty": 1050, "avail": 1050,
            "cost": 10.0, "price": 10.0, "mv": 10500}]
    r = portfolio.target(_picks(1), capital=1000000, current=cur,
                         patch={"mode": "equal", "max_w": 1.0})
    b = (r.get("buys") or [{}])[0]
    delta = int(b.get("delta") or 0)
    check(delta > 0 and delta % 100 == 0, "买入差额 %s 是 100 的整数倍" % delta)


def test_turnover_span():
    print("\n[年化换手：分母是观察窗口，不是有成交的天数]")
    orig = emtbook._all_fills
    try:
        days = [{"date": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)} for i in range(180)]
        # 5 天成交，每天买 100 股 × 100 元 = 1 万元，双边成交额 5 万元
        fills = [{"date": days[i]["date"], "code": "600519", "name": "贵州茅台",
                  "side": "买入", "qty": 100, "price": 100.0, "fee": 5.0}
                 for i in range(0, 180, 36)]
        emtbook._all_fills = lambda led: fills
        tr = emtbook._trades(days, {}, 1000000.0)
        rate = tr.get("turnover_rate")
        check(rate is not None and rate < 20,
              "180 天窗口年化约 6.8%%，不应是 244%%（实际 %s）" % rate)
        check(tr.get("span_days") == 180, "span_days=180（实际 %s）" % tr.get("span_days"))
        check(tr.get("sample_days") == 5, "sample_days=5（实际 %s）" % tr.get("sample_days"))
    finally:
        emtbook._all_fills = orig


def test_limit_pct():
    print("\n[涨跌停：按板块取限幅]")
    cfg = {"limit_pct": 0.10}
    check(abs(quant._limit_pct("600519", "", cfg) - 0.10) < 1e-9, "主板 10%")
    check(abs(quant._limit_pct("300750", "", cfg) - 0.20) < 1e-9, "创业板 20%")
    check(abs(quant._limit_pct("688111", "", cfg) - 0.20) < 1e-9, "科创板 20%")
    check(abs(quant._limit_pct("830799", "", cfg) - 0.30) < 1e-9, "北交所 30%")
    check(abs(quant._limit_pct("600519", "ST茅台", cfg) - 0.05) < 1e-9, "ST 5%")


def test_sortino():
    print("\n[Sortino：下行偏差，不是负收益子集的标准差]")
    # 只有一个负收益日：旧写法 std(ddof=1) 为 NaN → sortino=0
    one = quant._downside_dev([0.01, 0.01, -0.02, 0.01])
    check(one > 0, "单个负收益日下行偏差 > 0（%s）" % round(one, 4))
    # 两个等幅负收益日：旧写法 std=0 → sortino=0
    two = quant._downside_dev([-0.01, -0.01, 0.02, 0.02])
    check(two > 0, "两个等幅负收益日下行偏差 > 0（%s）" % round(two, 4))
    check(quant._downside_dev([0.01, 0.02]) == 0, "全正收益时下行偏差为 0")


def test_watchlist_import():
    print("\n[股票池批量导入：merge 去重、replace 清空]")
    orig_state, orig_save = watchlist._state, watchlist._save
    try:
        st = {"items": [{"code": "000001", "name": "平安银行", "source": "手动添加",
                         "added": "2026-01-01"}], "default": "000001"}
        watchlist._state = lambda: st
        watchlist._save = lambda s: None
        r = watchlist.mutate("import", mode="merge",
                             text="600519 贵州茅台\n000001 平安银行\n600519 重复")
        check(r.get("ok"), "merge 返回 ok")
        check(r.get("added") == 1, "只新增 1 只（000001 已存在、600519 重复）（实际 %s）"
              % r.get("added"))
        codes = [i["code"] for i in st["items"]]
        check(codes == ["000001", "600519"], "合并后顺序正确：%s" % codes)
        check(r.get("total") == 2, "返回 total=2（实际 %s）" % r.get("total"))

        r2 = watchlist.mutate("import", mode="replace", text="300750 宁德时代")
        check([i["code"] for i in st["items"]] == ["300750"], "replace 先清空再导入")
        check(r2.get("total") == 1, "replace 后 total=1")
        check(st.get("default") == "300750", "默认标的跟着更新")
    finally:
        watchlist._state, watchlist._save = orig_state, orig_save


if __name__ == "__main__":
    test_topk_drop()
    test_old_position_price()
    test_odd_lot()
    test_turnover_span()
    test_limit_pct()
    test_sortino()
    test_watchlist_import()
    print("\n" + ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    raise SystemExit(1 if FAILED else 0)
