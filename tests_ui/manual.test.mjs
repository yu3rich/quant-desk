/**
 * 首屏行为：优先恢复上次落盘的候选，不重算；无记录才自动出数。
 * 彩虹提醒由「今天是否选过 + 数据是否过期」决定，保持原逻辑。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { makePage, boot, tick, close } from "./helpers.mjs";

const appJs = fs.readFileSync("web/app.js", "utf8");
const html = fs.readFileSync("web/index.html", "utf8");

const RESUME = {
  ok: true, empty: false, restored: true,
  as_of: "2026-11-23", count: 4937, topn: 2,
  last_pick_at: "2026-11-23 14:40:52",
  candidates: [
    { code: "600926", name: "杭州银行", rank: 1, score: 0.0566,
      industry: "银行", close: 48.87, close_real: 17.82, pct: -1.2, mom20: 6.39 },
    { code: "600015", name: "华夏银行", rank: 2, score: 0.05,
      industry: "银行", close: 33.73, close_real: 6.73, pct: -0.8, mom20: 3.2 },
  ],
  funnel: {
    stages: [
      { key: "pool", name: "样本池", before: 5436, after: 5436, dropped: 0,
        note: "本次参与打分的股票", enabled: true },
      { key: "st", name: "ST 与退市", before: 5436, after: 5240, dropped: 196,
        note: "剔除 ST", enabled: true },
      { key: "quality", name: "数据完整性", before: 5240, after: 4937, dropped: 303,
        note: "剔除缺数据", enabled: true },
    ],
    dropped: 499,
  },
  industry: { cap: 0, capped: false, trimmed: [], counts: { 银行: 2 },
    note: "恢复上次落盘候选",
    exposure: { ok: true, n: 2, industries: 1, hhi: 0.5, effective_n: 2,
      max_weight: 100, max_industry: "银行", rows: [
        { industry: "银行", n: 2, weight: 100, base_weight: 12.3, gap: 87.7 },
      ], rows_more: 0 } },
  model: {}, snapshot_reused: true,
  record: { ok: true, skipped: true, recorded_at: "2026-11-23 14:40:52" },
};

function apiFor(url) {
  if (url.includes("/api/screen/resume")) return RESUME;
  if (url.includes("/api/screen/bear")) {
    return { ok: true, saved: true, mode: "llm",
      model: "deepseek-flash", judge_model: "deepseek-flash",
      at: "2026-11-23 14:40:52", dropped: 0, kept: 2,
      rows: [
        { code: "600926", name: "杭州银行", verdict: "keep", enough: true,
          bears: [{ point: "护城河稳定", trigger: "roe>12", watch: "roe" }],
          judge_reason: "基本面稳健" },
        { code: "600015", name: "华夏银行", verdict: "keep", enough: true,
          bears: [{ point: "估值处于低位", trigger: "pb<0.6", watch: "pb" }],
          judge_reason: "估值合理" },
      ] };
  }
  if (url.includes("/api/screen/llm")) {
    return { ok: true, saved: { mode: "llm", model: "deepseek-flash",
      saved_at: "2026-11-23 14:40:58",
      items: [
        { code: "600926", name: "杭州银行", rank_llm: 1, llm_reason: "息差企稳" },
        { code: "600015", name: "华夏银行", rank_llm: 2, llm_reason: "资产质量改善" },
      ] } };
  }
  if (url.includes("/api/screen/run")) {
    apiFor.runCalls = (apiFor.runCalls || 0) + 1;
    return { ok: true, as_of: "2026-11-23", candidates: [], funnel: {},
             neutral: {}, industry: {}, record: { ok: true, skipped: true } };
  }
  if (url.includes("/api/watchlist")) return { ok: true, items: [], count: 0 };
  if (url.includes("/api/bulk")) return { ok: true, running: false, cached_total: 5000 };
  if (url.includes("/api/universe")) {
    return { ok: true, rows: [], total: 5000, cached: 5000, filtered: 0,
             updated: "2026-11-23", target: "2026-11-23", lag: 0, fresh: true };
  }
  if (url.includes("/api/screen/freshness")) {
    return { ok: true, as_of: "2026-11-23", expected: "2026-11-23",
      quote_cached: 5000, quote_cover: 0.99, quote_lag: 0,
      snapshot: { as_of: "2026-11-23", count: 5000, lag: 0 },
      dataset: { rows: 1000, date_max: "2026-11-12", lag: 3, label_mode: "rank" },
      model: { trained: true, age_days: 3, ic: 0.1, retrain_due: false },
      calendar: { covered: true }, actions: [], stale: false,
      run: { running: false, steps: [] } };
  }
  if (url.includes("/api/data/hub")) {
    return { ok: true, freshness: { ok: true, actions: [], run: { running: false } },
             scheduler: {}, runs: [], bulk_workers: 6, sources: {}, llm: {} };
  }
  if (url.includes("/api/screen/status")) {
    return { ok: true,
      data: { snapshot: { loaded: true, rows: 5000 }, dataset: { loaded: true, rows: 1000 },
              cached_total: 5000, universe_total: 5000, modules: [] },
      model: { trained: true, engine: "lightgbm", meta: { metrics: {} } },
      llm: { enabled: false, configured: false }, planned: [] };
  }
  return { ok: true, batches: [], items: [], rows: [] };
}

test("init 先恢复落盘候选，恢复成功就不调 screenRun", async (t) => {
  apiFor.runCalls = 0;
  const ctx = makePage({ api: apiFor });
  t.after(() => close(ctx));
  await boot(ctx);
  await tick(ctx.window, 8);

  assert.equal(apiFor.runCalls, 0, "已有落盘记录时不应自动重算");
});

test("首屏直接显示上次候选，并提示是恢复展示", async (t) => {
  const ctx = makePage({ api: apiFor });
  t.after(() => close(ctx));
  await boot(ctx);
  const { doc } = ctx;
  await tick(ctx.window, 8);

  const rows = doc.querySelectorAll("#scrBody tr[data-code], #scrBody tr .codelink");
  assert.ok(rows.length >= 2, `应渲染出恢复的候选，实际 ${rows.length} 行`);

  const pill = doc.getElementById("scrScorePill");
  assert.ok(pill.textContent.includes("上次候选"), `胶囊应标注恢复，实际：${pill.textContent}`);

  const note = doc.getElementById("scrScoreNote");
  assert.ok(note.textContent.includes("上一次选股记录"),
    `应说明不是重新计算，实际：${note.textContent}`);

  const head = doc.getElementById("scrHeadline");
  assert.ok(head.textContent.includes("重新选股"),
    `应引导用户手动重选，实际：${head.textContent}`);
});

test("彩虹提醒逻辑保留：scrIsStale / scrMarkStale 仍在", () => {
  assert.ok(appJs.includes("function scrIsStale(r)"), "应保留过期判据");
  assert.ok(appJs.includes("function scrMarkStale(on)"), "应保留彩虹提醒");
  assert.ok(appJs.includes("scrMarkStale(scrIsStale(r))"),
    "恢复后应按原判据更新彩虹提醒");
  assert.ok(appJs.includes("async function screenResume()"), "应有恢复函数");
  assert.ok(appJs.includes("const restored = await screenResume()"),
    "init 应先尝试恢复");
});

test("无落盘记录时仍会自动出数（新用户不空表）", async (t) => {
  const emptyApi = (url) => {
    if (url.includes("/api/screen/resume")) {
      return { ok: true, empty: true, candidates: [], as_of: "" };
    }
    return apiFor(url);
  };
  apiFor.runCalls = 0;
  const ctx = makePage({ api: emptyApi });
  t.after(() => close(ctx));
  await boot(ctx);
  await tick(ctx.window, 8);

  assert.ok(apiFor.runCalls >= 1, "无落盘记录时应自动出数一次");
});

test("恢复路径也渲染辩护 / LLM 解读 / 行业分布 / 漏斗（不烧 token）", async (t) => {
  const ctx = makePage({ api: apiFor });
  t.after(() => close(ctx));
  await boot(ctx);
  const { doc } = ctx;
  await tick(ctx.window, 20);   // 等 bearRestore / scrLlmRestore 的二次请求落地

  // 辩护：卡片状态行出现「本批存档 · 辩护 …」
  const bearBody = doc.getElementById("bearBody");
  assert.ok(bearBody, "应有空头辩护卡片");
  assert.ok(bearBody.textContent.includes("本批存档"),
    `恢复后辩护应标「本批存档」，实际：${bearBody.textContent.slice(0, 60)}`);
  assert.ok(bearBody.textContent.includes("辩护"),
    `辩护模型信息应出现，实际：${bearBody.textContent.slice(0, 60)}`);

  // LLM 解读：胶囊变成 LLM · 已恢复
  const llmPill = doc.getElementById("scrLlmMode");
  assert.ok(llmPill && llmPill.textContent.includes("已恢复"),
    `恢复后 LLM 解读应显示「已恢复」，实际：${llmPill && llmPill.textContent}`);

  // 行业分布：跑出「银行」行
  const expo = doc.getElementById("scrExpo");
  assert.ok(expo && !expo.textContent.includes("待计算"),
    `行业分布不应停在待计算，实际：${expo && expo.textContent.slice(0, 60)}`);
  assert.ok(expo && expo.textContent.includes("银行"),
    `行业分布应列出银行，实际：${expo && expo.textContent.slice(0, 60)}`);

  // 漏斗：step 链出现（ST 与退市 等）
  const funnel = doc.getElementById("scrFunnel");
  assert.ok(funnel && funnel.textContent.includes("ST 与退市"),
    `漏斗链应恢复出真实阶段，实际：${funnel && funnel.textContent.slice(0, 80)}`);

  // 样本数：右上角是「参与打分的只数」，不能拿候选只数（2）顶替
  const sampleN = doc.getElementById("scrSampleN");
  assert.equal(sampleN.textContent, "4,937",
    `恢复后样本数应与 screenRun 同口径，实际：${sampleN.textContent}`);
});
