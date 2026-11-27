/**
 * 页面结构与默认态：单股页默认打开最近一次分析、AI 解读排在因子之前、
 * 实盘页不再重复放复盘日记、参数扫描页显式说明当前标的与策略口径。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { makePage, close } from "./helpers.mjs";

const DETAIL = {
  ok: true, code: "600519", name: "贵州茅台", industry: "食品饮料",
  date: "2026-11-24", price: 1460, pct: 1.46, pe: 21.2, pb: 6.7,
  score: 0.018, score_label: "分位", engine: "lightgbm",
  rank: 1430, count: 5416, percentile: 73.6, base_score: 1.5,
  ind_rank: 17, ind_count: 122, basis: "hfq", bars: 2157,
  factors: [{ key: "mom20", name: "20日动量", group: "动量", unit: "%",
              direction: 1, value: 3.2, pct: 88, desc: "" }],
  levels: { atr_pct: 2.34, high20: 1489, low20: 1322, stop_ref: 1391 },
  valuation: {}, snapshot: { as_of: "2026-11-24", count: 5416 }, warnings: [],
};

const REPORT = "## 结论摘要\n- 贵州茅台 **1460** 元\n\n## 模型与因子\n- 分位 73.6%";

function apiFor(url) {
  if (/\/api\/stock\/reports\/9$/.test(url)) {
    return { ok: true, id: 9, code: "600519", name: "贵州茅台", mode: "ai",
             model: "deepseek-flash", at: "2026-11-25 10:30:00",
             payload: { detail: DETAIL, report: REPORT, debate: null } };
  }
  if (url.includes("/api/stock/reports")) {
    return { ok: true, items: [{ id: 9, code: "600519", name: "贵州茅台",
      at: "2026-11-25 10:30:00", mode: "ai", score: 0.018, rank: 1430 }] };
  }
  if (url.includes("/api/stock/detail")) return DETAIL;
  return { ok: true };
}

/** a 是否在 b 之前（文档顺序） */
function before(a, b) {
  return !!(a.compareDocumentPosition(b) & 4);   // DOCUMENT_POSITION_FOLLOWING
}

test("单股页：进页面默认打开最近一次分析", async () => {
  const ctx = makePage({ api: apiFor });
  try {
    await ctx.window.stkLoad();
    const doc = ctx.doc;
    assert.equal(doc.getElementById("stkEmpty").hidden, true, "空态应隐藏");
    assert.equal(doc.getElementById("stkBody").hidden, false, "画像应显示");
    assert.equal(doc.getElementById("stkReportCard").hidden, false, "AI 解读卡应显示");
    assert.match(doc.getElementById("stkReport").textContent, /结论摘要/);
    assert.equal(doc.getElementById("stkMode").textContent, "AI · deepseek-flash");
    assert.equal(doc.getElementById("stkCode").value, "600519", "输入框回填到该股");
    // 历史列表展示全站记录（每条带股票名/代码），不是只剩当前这只
    assert.equal(doc.querySelectorAll("#stkHist .stk-hrow").length, 1);
    assert.equal(doc.getElementById("stkHistCount").textContent, "1 条");
  } finally {
    close(ctx);
  }
});

test("单股页：AI 解读排在因子分位之前", () => {
  const ctx = makePage();
  try {
    const doc = ctx.doc;
    const report = doc.getElementById("stkReportCard");
    const factor = doc.getElementById("stkFactorCard");
    assert.ok(report && factor, "两张卡都应存在");
    assert.ok(before(report, factor), "AI 解读应在因子分位之前");
  } finally {
    close(ctx);
  }
});

test("实盘页：行为体检在交易复盘之前，且不再重复放复盘日记", () => {
  const ctx = makePage();
  try {
    const doc = ctx.doc;
    assert.equal(doc.getElementById("rtJournal"), null, "实盘页不应再有日记编辑框");
    assert.equal(doc.getElementById("rtJMode"), null, "实盘页不应再有日记模式开关");
    assert.equal(doc.getElementById("btnRtSaveJ"), null, "实盘页不应再有保存日记按钮");

    const diagCard = doc.getElementById("rtDiag").closest(".card");
    const tradeCard = doc.getElementById("rtTradeKpis").closest(".card");
    assert.ok(before(diagCard, tradeCard), "行为体检应在交易复盘之前");
    // 两块都还在，只是顺序换了
    assert.ok(doc.getElementById("btnRtHealthGen"), "体检生成按钮应保留");
    assert.ok(doc.getElementById("rtClosedBody"), "交易复盘表应保留");
  } finally {
    close(ctx);
  }
});

test("参数扫描页：显式说明当前标的、区间与策略口径", () => {
  const ctx = makePage();
  try {
    const doc = ctx.doc;
    doc.getElementById("code").value = "600519";
    doc.getElementById("start").value = "2024-01-01";
    doc.getElementById("end").value = "2026-11-25";
    doc.getElementById("strategy").value = "ma_cross";
    ctx.window.scanCtxPaint();
    let txt = doc.getElementById("scanCtx").textContent;
    assert.match(txt, /600519/, "应显示当前标的");
    assert.match(txt, /2024-01-01 ~ 2026-11-25/, "应显示回测区间");
    assert.match(txt, /双均线/);
    assert.doesNotMatch(txt, /不参与扫描/, "双均线时不提示口径差异");

    // 总览换成 MACD：扫描仍固定双均线，必须如实提示，不能默默按别的策略算
    doc.getElementById("strategy").value = "macd";
    ctx.window.scanCtxPaint();
    txt = doc.getElementById("scanCtx").textContent;
    assert.match(txt, /不参与扫描/, "策略不一致时要提示");
  } finally {
    close(ctx);
  }
});
