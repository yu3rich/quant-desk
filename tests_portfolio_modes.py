# -*- coding: utf-8 -*-
"""组合权重模式回归：波动率/固定风险/风险平价/HRP/Kelly/Topk-Drop。"""
import math

import mlmodel
import portfolio
import screener

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def _picks(n=10):
    snap, meta = screener.load_snapshot()
    if snap is None:
        return []
    scored = mlmodel.score_snapshot(rows=screener._rows(snap), snapshot_meta=meta)
    return [{"code": x["code"], "name": x.get("name"),
             "industry": x.get("industry"), "score": x.get("score"),
             "close": x.get("close")} for x in (scored.get("rows") or [])[:n]]


def test_modes(picks):
    print("\n[七种权重模式]")
    for mode in ("equal", "score", "vol_target", "risk_fixed", "rp", "hrp", "kelly"):
        r = portfolio.target(picks, capital=300000, current=[], patch={"mode": mode})
        ok = r.get("ok") and r.get("n_target", 0) > 0
        check(ok, "%s 生成目标持仓（%d 只）" % (mode, r.get("n_target", 0)))
        if not ok:
            continue
        ws = [x["weight"] for x in r["rows"] if x["target_qty"] > 0]
        check(all(0 < w <= 100 for w in ws), "%s 权重都在 (0,100]" % mode)
        if mode in ("vol_target", "risk_fixed"):
            check(any(x.get("atr_pct") for x in r["rows"]),
                  "%s 带出 ATR 百分比" % mode)
    r = portfolio.target(picks, capital=300000, current=[], patch={"mode": "kelly"})
    k = r.get("kelly") or {}
    check("available" in k, "Kelly 返回可用性说明：%s" % k.get("reason", ""))


def test_risk_fixed(picks):
    print("\n[固定风险预算]")
    r = portfolio.target(picks, capital=300000, current=[],
                         patch={"mode": "risk_fixed", "risk_pct": 0.01,
                                "atr_mult": 2, "max_w": 0.5})
    risks = [x.get("risk_amt") for x in r.get("rows") or [] if x.get("risk_amt")]
    if not risks:
        check(False, "应有 risk_amt")
        return
    target = 300000 * 0.01
    # 单票上限会压住高波动票的风险金额，所以只检查「不超过预算太多」
    check(all(x <= target * 1.6 for x in risks),
          "每只风险金额接近预算 %.0f（最大 %.0f）" % (target, max(risks)))


def test_topk_drop(picks):
    print("\n[Topk-Drop]")
    if len(picks) < 6:
        check(False, "候选不足，跳过")
        return
    cur = [{"code": p["code"], "name": p["name"], "qty": 1000, "avail": 1000,
            "cost": float(p.get("close") or 10), "price": float(p.get("close") or 10),
            "mv": 1000 * float(p.get("close") or 10)} for p in picks[:3]]
    r = portfolio.target(picks, capital=300000, current=cur,
                         patch={"mode": "equal", "n_drop": 2, "topk": 10})
    sells = [x for x in (r.get("sells") or []) if x.get("action") == "卖出"]
    # n_drop 限制的是「换股（清仓）」数量，不是减仓：权重从超配回到目标本来就会产生卖出单，
    # 把 trim 也算进来会随行情价格变化而误报（价格高时每只都要减仓）。
    exits = [x for x in sells if int(x.get("target_qty") or 0) == 0]
    check(len(exits) <= 2, "清仓换股不超过 n_drop：%d（卖出单共 %d 笔，其余为减仓）"
          % (len(exits), len(sells)))
    check(r.get("n_drop") == 2, "返回 n_drop 参数")


def test_fallback():
    print("\n[数据不足回退]")
    fake = [{"code": "000001", "name": "A", "score": 1, "close": 10},
            {"code": "000002", "name": "B", "score": 2, "close": 20}]
    for mode in ("rp", "hrp"):
        r = portfolio.target(fake, capital=100000, current=[], patch={"mode": mode})
        check(r.get("ok") and r.get("n_target", 0) > 0,
              "%s 在无行情时回退而不是报错" % mode)


if __name__ == "__main__":
    # 测试会真的生成目标持仓；计划存档会污染实盘体检的「计划-执行偏差」，
    # 所以这里把写库动作换成 no-op，只测算法本身。
    import store
    store.plan_put = lambda payload: 0
    ps = _picks()
    check(len(ps) >= 5, "取到候选样本 %d 只" % len(ps))
    if ps:
        test_modes(ps)
        test_risk_fixed(ps)
        test_topk_drop(ps)
        test_fallback()
    print("\n%s" % ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    raise SystemExit(1 if FAILED else 0)
