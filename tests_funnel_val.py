# -*- coding: utf-8 -*-
"""漏斗「基本面」层回归：三档预设的淘汰口径、缺数据放行、逐层归因。

口径参考 InStock 的「基本面选股」（0<PE≤20、PB≤10），
但遵循本项目的漏斗原则：缺数据一律放行，绝不因为估值源当天没跑就砍掉整批。
"""
import pandas as pd

import screener

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def _row(code, name, pe, pb, mv, pct=1.0):
    return {"code": code, "name": name, "pe_ttm": pe, "pb": pb, "total_mv": mv,
            "pct": pct, "vol20": 2.0, "turnover": 3.0,
            # 必需因子：不齐会被「数据完整性」层先淘汰，测不到基本面层
            "mom20": 1.0, "mom60": 1.0, "rsi14": 50.0, "ma_dev": 1.0}


def _df():
    return pd.DataFrame([
        _row("000001", "正常", 12.0, 1.5, 500.0),
        _row("000002", "亏损", -8.0, 3.0, 300.0),
        _row("000003", "高PE", 400.0, 2.0, 100.0),
        _row("000004", "高PB", 30.0, 80.0, 100.0),
        _row("000005", "微盘", 15.0, 2.0, 5.0),
        _row("000006", "缺估值", None, None, None),
        _row("000007", "中PE", 50.0, 5.0, 100.0),
    ])


def _kept(preset, **kw):
    cfg = {"val": preset, "quality": True, "st": True, "extreme": True,
           "liquidity": False, "trend": False}
    cfg.update(kw)
    kept, stages = screener.funnel(_df(), cfg)
    stage = next(x for x in stages if x["key"] == "valuation")
    return set(kept["code"]), stage


def test_presets():
    print("\n[三档预设]")
    allc = {r["code"] for r in _df().to_dict("records")}

    kept, stage = _kept("off")
    check(kept == allc and stage["dropped"] == 0, "off：一只不淘汰")
    check(stage["enabled"] is False, "off：该层标记为未启用（界面上显示已关闭）")

    kept, stage = _kept("loose")
    check("000002" not in kept, "loose：剔除亏损（PE<0）")
    check("000003" not in kept, "loose：剔除 PE>300")
    check("000004" not in kept, "loose：剔除 PB>50")
    check("000005" not in kept, "loose：剔除总市值<10 亿")
    check("000007" in kept, "loose：PE 50 保留（保守档只剔极端值）")
    check("000006" in kept, "loose：估值缺失按「未知放行」")
    check("PE ≤ 300" in stage["note"] and "PB ≤ 50" in stage["note"], "loose：说明里写清阈值")

    kept2, stage2 = _kept("instock")
    check("000007" not in kept2, "instock：PE 50 被剔除（0<PE≤20）")
    check(kept2 == {"000001", "000006"}, "instock：只剩低估值与缺数据的两只：%s" % sorted(kept2))
    check(stage2["dropped"] > stage["dropped"], "instock 比 loose 更严")

    # 未指定/未知预设一律按关闭处理，不能默认把股票砍掉
    kept3, stage3 = _kept("nonsense")
    check(kept3 == allc and stage3["dropped"] == 0, "未知预设按关闭处理")


def test_missing_and_negative():
    print("\n[边界]")
    # 只卡 PE 上限、不剔亏损时，负 PE 应当保留（负 PE 是「亏损」，不是「太贵」）
    kept, stage = _kept("loose", val_loss=False)
    check("000002" in kept, "关掉「剔除亏损」后负 PE 保留")
    check("000003" not in kept, "PE 上限仍然生效")

    # 负净资产（PB<0）属于异常，但本项目不静默判死：只在超过上限时剔除
    df = pd.DataFrame([_row("000008", "负净资产", 20.0, -3.0, 100.0)])
    kept, _ = screener.funnel(df, {"val": "loose"})
    check("000008" in set(kept["code"]), "PB<0 不因「小于上限」被误杀")

    # 空表不能抛错
    kept, stages = screener.funnel(pd.DataFrame(columns=list(_df().columns)), {"val": "instock"})
    check(len(kept) == 0, "空表返回空结果而不是异常")


def test_attribution():
    print("\n[逐层归因]")
    df = _df()
    kept, stages, killed = screener.funnel(
        df, {"val": "instock", "quality": True, "st": True, "extreme": True}, detail=True)
    check(killed.get("000002") == "基本面", "亏损股被归因到「基本面」层：%s" % killed.get("000002"))
    check(killed.get("000007") == "基本面", "中PE 股被归因到「基本面」层")
    check("000001" not in killed, "保留下来的票不在淘汰名单里")


if __name__ == "__main__":
    test_presets()
    test_missing_and_negative()
    test_attribution()
    print("\n%s" % ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    raise SystemExit(1 if FAILED else 0)
