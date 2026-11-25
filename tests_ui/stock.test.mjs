/**
 * 单股分析页：KPI/因子渲染、分析全流程、历史删除。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makePage, close } from "./helpers.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const FACTORS = Array.from({ length: 24 }, (_, i) => ({
  key: "f" + i, name: "因子" + i, group: "动量", unit: "%",
  direction: i % 3 === 0 ? 0 : 1, desc: "", model_only: false,
  value: i + 0.5, pct: i * 4,
}));

const DETAIL = {
  ok: true, code: "600519", name: "贵州茅台", industry: "食品饮料",
  date: "2026-11-24", price: 1460, pct: 1.46, pe: 21.23, pe_ttm: 22.21,
  pb: 6.73, total_mv: 18251.19, circ_mv: 18251.19, turnover_rate: 0.34,
  volume_ratio: 0.93, valuation_date: "20261124", basis: "hfq", bars: 2157,
  last_date: "2026-11-24", score: 0.018, score_label: "分位", engine: "lightgbm",
  rank: 1430, count: 5416, percentile: 73.6, base_score: 1.5369,
  ind_rank: 17, ind_count: 122, factors: FACTORS,
  levels: { atr_pct: 2.34, atr: 34.18, high20: 1489.09, low20: 1322.6,
            pivot: 1423.9, r1: 1567.0, s1: 1280.0, stop_ref: 1391.64,
            stop_ref_pct: -4.68, dist_high20_pct: -1.95 },
  valuation: { pe_band: [16.29, 21.3, 35.73], pb_band: [1.45, 2.23, 3.04],
               price_band_pe: [1120.27, 1464.81, 2457.17],
               price_band_pb: [314.56, 483.77, 659.49] },
  snapshot: { as_of: "2026-11-24", count: 5416 }, warnings: [],
};

const REPORT = "## 结论摘要\n- 贵州茅台 **1460** 元\n\n## 多空对照\n- 多头：动量强\n\n## 模型与因子\n- 分位 73.6%";

const DEBATE = {
  bull: [{ point: "中期动量强", evidence: "mom20 分位 93.6" }],
  bear: [{ point: "短期动量弱", evidence: "5日动量分位 11.0" }],
  verdict: { stance: "positive", confidence: 62, horizon: "中期", note: "中期证据占优" },
  data_gaps: ["缺资金流水"],
};

function apiFor(url) {
  if (url.includes("/api/stock/analyze")) {
    return { ok: true, id: 9, at: "2026-11-25 10:30:00", mode: "ai",
             model: "deepseek-flash", report: REPORT, detail: DETAIL,
             debate: DEBATE, warning: "" };
  }
  if (/\/api\/stock\/reports\/9$/.test(url)) {
    return { ok: true, id: 9, code: "600519", name: "贵州茅台", mode: "ai",
             model: "deepseek-flash", at: "2026-11-25 10:30:00",
             payload: { detail: DETAIL, report: REPORT, debate: DEBATE } };
  }
  if (url.includes("/api/stock/reports")) {
    return { ok: true, items: [{ id: 9, code: "600519", name: "贵州茅台",
      at: "2026-11-25 10:30:00", mode: "ai", score: 0.018, rank: 1430 }] };
  }
  // init 会顺带拉股票池等接口：给最小可用结构，避免无关页面报错盖住被测逻辑
  return { ok: true, items: [], count: 0, default: "", updated: "-",
           entries: [], runs: [], sources: {} };
}

test("因子表渲染 24 行且进度条带宽度", () => {
  const ctx = makePage({ api: apiFor });
  const html = ctx.window.stkFactors(DETAIL);
  assert.equal((html.match(/<tr>/g) || []).length, 24);
  assert.match(html, /width:0%/);
  assert.match(html, /因子0/);
  assert.match(html, /因子23/);
  close(ctx);
});

test("KPI 展示价格、模型分、排名", () => {
  const ctx = makePage({ api: apiFor });
  const html = ctx.window.stkKpis(DETAIL);
  assert.match(html, /1460\.00/);
  assert.match(html, /0\.0180/);
  assert.match(html, /#1430/);
  assert.match(html, /stk-up/);       // 涨用红色
  assert.match(html, /18251\.19 亿/);
  close(ctx);
});

test("点分析后渲染概览、因子、报告与历史", async () => {
  const ctx = makePage({ api: apiFor });
  ctx.doc.getElementById("stkCode").value = "600519";
  await ctx.window.stkAnalyze();
  assert.equal(ctx.doc.getElementById("stkBody").hidden, false);
  assert.match(ctx.doc.getElementById("stkName").textContent, /贵州茅台/);
  assert.equal(ctx.doc.querySelectorAll("#stkFactors tr").length, 24);
  assert.match(ctx.doc.getElementById("stkReport").innerHTML, /<h3>结论摘要<\/h3>/);
  assert.match(ctx.doc.getElementById("stkReport").innerHTML, /<strong>1460<\/strong>/);
  assert.match(ctx.doc.getElementById("stkMode").textContent, /AI/);
  const riskCard = ctx.doc.getElementById("stkRiskCard");
  assert.equal(riskCard.hidden, false, "风险与价位卡片应显示");
  const riskTxt = ctx.doc.getElementById("stkRisk").textContent;
  assert.match(riskTxt, /ATR14/);
  assert.match(riskTxt, /2×ATR 参考位/);
  assert.match(riskTxt, /同行 PE 区间/);
  const deb = ctx.doc.getElementById("stkDebate");
  assert.equal(deb.hidden, false, "多空辩论应显示");
  assert.match(deb.textContent, /偏多/);
  assert.match(deb.textContent, /中期动量强/);
  assert.match(deb.textContent, /短期动量弱/);
  assert.match(deb.textContent, /缺资金流水/);
  assert.equal(ctx.doc.querySelectorAll("#stkHist .stk-hrow").length, 1);
  close(ctx);
});

test("风险与价位渲染：ATR、区间、估值带", () => {
  const ctx = makePage({ api: apiFor });
  const html = ctx.window.stkRiskHtml(DETAIL);
  assert.match(html, /ATR14/);
  assert.match(html, /2\.34%/);
  assert.match(html, /1322\.60 ~ 1489\.09/);
  assert.match(html, /1120\.27 ~ 1464\.81 ~ 2457\.17/);
  assert.match(html, /非止损/);
  close(ctx);
});

test("多空辩论渲染：立场、置信度、两侧证据、数据缺口", () => {
  const ctx = makePage({ api: apiFor });
  const html = ctx.window.stkDebateHtml(DEBATE);
  assert.match(html, /class="stk-vd pos"/);
  assert.match(html, />偏多</);
  assert.match(html, /证据一致性 62%/);
  assert.match(html, /stk-dcol bull/);
  assert.match(html, /stk-dcol bear/);
  assert.match(html, /中期动量强/);
  assert.match(html, /5日动量分位 11\.0/);
  assert.match(html, /缺资金流水/);
  close(ctx);
});

test("历史删除调用 DELETE 并刷新列表", async () => {
  const ctx = makePage({ api: apiFor });
  ctx.window.confirm = () => true;
  ctx.doc.getElementById("stkCode").value = "600519";
  await ctx.window.stkAnalyze();
  const del = ctx.doc.querySelector("#stkHist .stk-hdel");
  assert.ok(del, "有删除按钮");
  await ctx.window.stkDelete(Number(del.dataset.del));
  // fetch 桩只记录 url，校验删除请求确实打到该 id
  assert.ok(ctx.calls.some((c) => c.url.includes("/api/stock/reports/9")),
    "删除请求打到 /api/stock/reports/9");
  close(ctx);
});

test("进度条填充块必须 display:block", () => {
  const css = fs.readFileSync(path.join(ROOT, "web/style.css"), "utf8");
  assert.match(css, /\.stk-bar span\{display:block/);
});
