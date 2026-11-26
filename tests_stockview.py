# -*- coding: utf-8 -*-
"""单股分析回归：横截面画像、本地模板纪律、多空辩论、存档增删。"""
import json

import stockview
import store

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def test_detail():
    print("\n[单股画像]")
    d = stockview.detail("600519")
    check(d.get("ok"), "600519 能取到画像：%s" % (d.get("error") or ""))
    if not d.get("ok"):
        return
    check(d["name"] and d["code"] == "600519", "名称/代码正确：%s %s" % (d["name"], d["code"]))
    check(len(d["factors"]) == 24, "24 个因子齐全")
    check(d["price"] and d["price"] > 0, "真实价可用：%s" % d["price"])
    check(d["rank"] and d["count"] > 1000, "全市场排名有效：%s / %s" % (d["rank"], d["count"]))
    check(all(f["pct"] is None or 0 <= f["pct"] <= 100 for f in d["factors"]),
          "分位都在 0~100")
    check(d["basis"] == "hfq", "复权口径如实返回：%s" % d["basis"])
    lv = d.get("levels") or {}
    check(lv.get("atr_pct") and lv["atr_pct"] > 0, "ATR 百分比可用：%s" % lv.get("atr_pct"))
    check(lv.get("high20") and lv.get("low20") and lv["high20"] > lv["low20"],
          "20 日区间有效")
    check(lv.get("stop_ref") and lv.get("stop_ref_pct", 0) < 0, "2×ATR 参考位低于现价")
    va = d.get("valuation") or {}
    check(va.get("peers", 0) >= 10, "同行样本足够：%s" % va.get("peers"))
    check(bool(va.get("price_band_pe") or va.get("price_band_pb")),
          "PE 或 PB 相对估值区间至少一个可用（PE=%s PB=%s）"
          % (bool(va.get("price_band_pe")), bool(va.get("price_band_pb"))))
    bad = stockview.detail("999999")
    check(not bad.get("ok"), "无效代码被拒绝")


def test_personas():
    """多视角评分：结构、缺数据标注、汇总口径（参考 ai-hedge-fund 的 agent 汇总）。"""
    print("\n[多视角评分]")
    d = stockview.detail("600519")
    if not d.get("ok"):
        check(False, "取画像失败，跳过")
        return
    ps = d.get("personas") or []
    cons = d.get("consensus") or {}
    check(len(ps) == 6, "六个视角齐全：%d" % len(ps))
    names = [p["name"] for p in ps]
    check(names[:5] == ["价值", "趋势", "反转", "稳健", "量能"], "视角顺序固定：%s" % names)
    check(all(p["signal"] in ("bullish", "bearish", "neutral", "unknown") for p in ps),
          "signal 取值合法")
    check(all(-100 <= p["score"] <= 100 for p in ps), "分数在 -100~100")
    check(all(0 <= p["confidence"] <= 90 for p in ps), "置信度在 0~90")

    # 质量视角没有财务数据，必须如实标缺，不能编一个代理指标
    q = next(p for p in ps if p["key"] == "quality")
    check(q["missing"] and q["signal"] == "unknown" and "财务数据" in q["missing_note"],
          "质量视角如实标注缺数据：%s" % q["missing_note"][:20])

    # 每个有数据的视角都要给出至少一条带数字的依据
    for p in ps:
        if p["missing"]:
            continue
        check(bool(p["points"]) and any(ch.isdigit() for ch in p["points"][0]),
              "%s 视角有量化依据：%s" % (p["name"], (p["points"] or [""])[0][:32]))

    avail = [p for p in ps if not p["missing"]]
    check(cons.get("available") == len(avail) and cons.get("total") == len(ps),
          "汇总只统计有数据的视角：%d/%d" % (cons.get("available"), cons.get("total")))
    check(cons.get("signal") in ("bullish", "bearish", "neutral", "unknown"), "汇总信号合法")
    # 汇总分数应落在各视角分数区间内（按置信度加权，不可能越界）
    lo = min(p["score"] for p in avail)
    hi = max(p["score"] for p in avail)
    check(lo <= cons.get("score") <= hi, "汇总分数在区间内：%d ∈ [%d, %d]"
          % (cons.get("score"), lo, hi))
    check(0 <= cons.get("agreement") <= 100, "同向比例 0~100：%s" % cons.get("agreement"))

    # 缺失数据时不能崩：给一个空画像
    ps2, cons2 = stockview.personas({"factors": [], "valuation": {}})
    check(len(ps2) == 6 and cons2["available"] == 0 and cons2["signal"] == "unknown",
          "全缺数据时返回 unknown 而不是抛错")

def test_local_report():
    print("\n[本地模板]")
    d = stockview.detail("600519")
    if not d.get("ok"):
        check(False, "画像不可用，跳过模板检查")
        return
    r = stockview.local_report(d)
    for sec in ("## 结论摘要", "## 多空对照", "## 模型与因子",
                "## 基本面与估值", "## 风险与观察点"):
        check(sec in r, "含小节 " + sec)
    for word in ("买入", "卖出", "止损", "止盈", "加仓", "减仓", "建议"):
        check(word not in r, "不出现「%s」" % word)
    # 允许「不是目标价」这种合规声明，但不能把目标价当结论给出来
    check(r.replace("不是目标价", "").count("目标价") == 0,
          "只在否定语境提到目标价")
    check("本地模板" in r, "明确标注本地模板")
    check("ATR14" in r, "含 ATR 参考位")
    check(("PE 分位" in r or "PB 分位" in r) and "不是目标价" in r,
          "含相对估值区间且注明不是目标价")


def test_archive_roundtrip():
    print("\n[存档增删]")
    rid = store.report_put("600519", "2026-11-24",
                           {"detail": {"x": 1}, "report": "# 测试"}, mode="local",
                           name="贵州茅台", score=0.1, rank=10, price=100.0, pct=1.0)
    try:
        got = store.report_get(rid)
        check(got and got["code"] == "600519" and got["payload"]["report"] == "# 测试",
              "写入后能按 id 读回")
        check(any(x["id"] == rid for x in store.report_list("600519")),
              "列表里能看到这条")
        check(store.report_delete(rid) == 1, "删除返回 1 行")
        check(store.report_get(rid) == {}, "删除后读不到")
    finally:
        store.report_delete(rid)


def test_analyze_local_fallback():
    print("\n[LLM 未配置时回退]")
    orig = stockview.llm.configured
    stockview.llm.configured = lambda: False
    rid = None
    try:
        r = stockview.analyze("600519")
        check(r.get("ok") and r.get("mode") == "local", "回退为本地模板")
        check("未配置 LLM" in (r.get("warning") or ""), "警告里说明原因")
        rid = r.get("id")
        check(bool(rid), "仍然存档（不丢历史）")
        if rid:
            check(stockview.get(rid).get("ok"), "按 id 能取回存档")
            check(stockview.delete(rid).get("ok"), "能删除")
    finally:
        stockview.llm.configured = orig
        if rid:
            store.report_delete(rid)


def test_debate_clean():
    print("\n[辩论结果清洗]")
    check(stockview._clean_debate(None) == {}, "非 dict 返回空")
    check(stockview._clean_debate({"bull": []}) == {}, "没有论据返回空")
    d = stockview._clean_debate({
        "bull": [{"point": "动量强", "evidence": "mom20 93.6"}] * 6,
        "bear": [{"point": "短期弱", "evidence": ""}],
        "verdict": {"stance": "bullish", "confidence": 180, "horizon": "中期", "note": "x"},
        "data_gaps": ["a", "b", "c", "d"],
    })
    check(len(d["bull"]) == 4, "论据最多 4 条")
    check(d["verdict"]["stance"] == "neutral", "非法 stance 归中性")
    check(d["verdict"]["confidence"] == 100, "置信度夹到 0~100")
    check(len(d["data_gaps"]) == 3, "数据缺口最多 3 条")


def test_debate_flow():
    print("\n[两轮：先辩论后报告]")
    orig_conf, orig_chat = stockview.llm.configured, stockview.llm.chat
    calls, rid = [], None

    def fake_chat(messages, max_tokens=0, c=None):
        sysmsg = messages[0]["content"]
        calls.append("debate" if "多空辩论主持" in sysmsg else "report")
        if "多空辩论主持" in sysmsg:
            return {"ok": True, "content": json.dumps({
                "bull": [{"point": "中期动量强", "evidence": "mom20 分位 93.6"}],
                "bear": [{"point": "短期动量弱", "evidence": "5日动量分位 11.0"}],
                "verdict": {"stance": "positive", "confidence": 62,
                            "horizon": "中期", "note": "中期证据占优"},
                "data_gaps": ["缺资金流水"]}, ensure_ascii=False)}
        return {"ok": True,
                "content": "\n\n".join(s + "\n正文" for s in stockview.SECTIONS)}

    try:
        stockview.llm.configured = lambda: True
        stockview.llm.chat = fake_chat
        r = stockview.analyze("600519")
        check(r.get("ok") and r.get("mode") == "ai", "两轮调用成功")
        check(calls == ["debate", "report"], "先辩论后报告：%s" % calls)
        d = r.get("debate") or {}
        check(d.get("verdict", {}).get("stance") == "positive", "裁决立场保留")
        check(len(d.get("bull") or []) == 1 and len(d.get("bear") or []) == 1,
              "多空论据保留")
        check(stockview._report_ok(r.get("report") or ""), "报告五节完整")
        rid = r.get("id")
        got = stockview.get(rid)
        check(bool((got.get("payload") or {}).get("debate")), "辩论结果随报告存档")
    finally:
        stockview.llm.configured, stockview.llm.chat = orig_conf, orig_chat
        if rid:
            stockview.delete(rid)


if __name__ == "__main__":
    test_detail()
    test_local_report()
    test_archive_roundtrip()
    test_analyze_local_fallback()
    test_debate_clean()
    test_debate_flow()
    test_personas()
    print("\n%s" % ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    raise SystemExit(1 if FAILED else 0)
