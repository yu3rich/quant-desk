/**
 * 回归测试：这几个 bug 都是我实际改坏过、且静态检查没发现的。
 * 每条断言都对应一次真实故障，防止再犯。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, tick, close } from "./helpers.mjs";

const PICK_BATCH = {
  ok: true,
  total_batches: 1,
  total_rows: 2,
  shown: 1,
  batches: [
    {
      as_of: "2026-11-20",
      recorded_at: "2026-11-23 14:40:52",
      count: 2,
      last_date: "2026-11-23",
      avg_chg: -1.2,
      win: 1,
      scored: 2,
      horizons: {},
      picks: [
        {
          code: "600926", name: "杭州银行", rank: 1, chg: -0.77,
          industry: "银行", score: 0.1, rec_real: 12.3, last_real: 12.2,
          verdict: "keep", h: {},
          bear: [
            { point: "RSI14 接近超买", trigger: "RSI>70", watch: "RSI14" },
            { point: "量价背离", trigger: "量比<0.85", watch: "量比" },
          ],
        },
        {
          code: "688037", name: "芯源微", rank: 2, chg: -2.94,
          industry: "半导体", score: 0.09, rec_real: 88.1, last_real: 85.5,
          verdict: "drop", h: {},
          bear: [{ point: "跌破 20 日线", trigger: "ma_dev<0", watch: "ma_dev" }],
        },
      ],
    },
  ],
};

function apiFor(url) {
  // 股票池：init 里 loadPool 会读 items，缺了会让 renderPool 抛错
  if (url.includes("/api/watchlist")) {
    return { ok: true, items: [], count: 0, default: "" };
  }
  if (url.includes("/api/bulk")) {
    return { ok: true, running: false, done: 0, total: 0, ok_count: 0, fail: 0,
             skip: 0, rows: 0, cached_total: 5000, cover: 99.9 };
  }
  if (url.includes("/api/universe")) {
    return { ok: true, rows: [], total: 5000, cached: 5000, filtered: 0,
             updated: "2026-11-23", target: "2026-11-20", lag: 0, fresh: true };
  }
  if (url.includes("/api/screen/status")) {
    // renderScrLayers 读 r.data.snapshot / dataset / cached_total / universe_total，
    // 以及 model / llm。缺字段会在 init 阶段就抛错，后面所有断言都没机会跑。
    return {
      ok: true,
      data: {
        snapshot: { loaded: true, rows: 5000, as_of: "2026-11-20" },
        dataset: { loaded: true, rows: 1000, date_max: "2026-11-12" },
        cached_total: 5000,
        universe_total: 5000,
      },
      model: { trained: true, engine: "lightgbm", available: true,
               meta: { rows_train: 800, rows_valid: 200, metrics: { rank_ic: 0.1 } } },
      llm: { enabled: false, configured: false },
      funnel: {}, planned: [],
    };
  }
  if (url.includes("/api/screen/bear")) {
    return {
      ok: true, saved: true, mode: "llm",
      at: "2026-11-23 14:40:52", model: "m1", judge_model: "m2",
      dropped: 1, kept: 1,
      rows: [
        {
          code: "600926", name: "杭州银行", verdict: "keep", enough: true,
          bears: [{ point: "RSI14 接近超买", trigger: "RSI>70", watch: "RSI14" }],
        },
        {
          code: "688037", name: "芯源微", verdict: "drop", enough: true,
          judge_reason: "动量转负",
          bears: [{ point: "跌破 20 日线", trigger: "ma_dev<0", watch: "ma_dev" }],
        },
      ],
    };
  }
  if (url.includes("/api/chart")) {
    // pkChartLoad -> mkQuoteHtml 读 r.price（数组）/ r.vol（数组）/ r.pct。
    // 不需要真实数据，只要类型对，能走到画图那一步。
    return {
      ok: true, code: "600926", name: "杭州银行", period: "intraday",
      price: [12.2, 12.25, 12.18], vol: [100, 200, 150],
      pct: 0, prev_close: 12.2,
      times: ["09:30", "09:31", "09:32"],
      avg: [12.2, 12.22, 12.2],
      dates: [],
    };
  }
  if (url.includes("/api/screen/picks")) return PICK_BATCH;
  if (url.includes("/api/picks/archive")) return { ok: true, items: [] };
  if (url.includes("/api/screen/freshness")) {
    return {
      ok: true, as_of: "2026-11-20", expected: "2026-11-20",
      quote_cached: 5000, quote_cover: 0.99, quote_lag: 0,
      snapshot: { as_of: "2026-11-20", count: 5000, lag: 0 },
      dataset: { rows: 1000, date_max: "2026-11-12", lag: 3, label_mode: "rank" },
      model: { trained: "2026-11-19", age_days: 3, ic: 0.1, retrain_due: false },
      calendar: { covered: true }, actions: [], stale: false,
      run: { running: false, steps: [] },
    };
  }
  return { ok: true };
}

/** 建好页面并等 init 完成，返回 ctx */
async function setup(t) {
  const ctx = makePage({ api: apiFor });
  t.after(() => close(ctx));     // 不清定时器，进程会被挂住
  await boot(ctx);
  return ctx;
}

test("悬停 ? 图标必须弹出说明内容（事件委托要覆盖 .q）", async (t) => {
  const ctx = await setup(t);
  const { window, doc } = ctx;

  const el = doc.querySelector("i.q[data-tip]");
  assert.ok(el, "页面里应存在带 data-tip 的 ? 图标");

  const expected = el.getAttribute("data-tip");
  assert.ok(expected && expected.length > 5, "data-tip 内容不应为空");

  el.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  await tick(window, 2);

  const pop = doc.getElementById("termPop");
  assert.ok(pop, "应创建 #termPop 气泡");
  assert.ok(pop.classList.contains("on"), "气泡应处于显示状态");
  assert.ok(
    pop.textContent.includes(expected.slice(0, 10)),
    `气泡应包含说明文字，实际：${pop.textContent.slice(0, 60)}`,
  );

  el.dispatchEvent(new window.MouseEvent("mouseout", { bubbles: true }));
  await tick(window, 2);
  assert.ok(!pop.classList.contains("on"), "移开后气泡应隐藏");
});

test("点击历史记录行能展开分时图，且不会被异步重绘冲掉", async (t) => {
  const ctx = await setup(t);
  const { window, doc } = ctx;

  window.switchTab("picks");
  await tick(window, 8);

  const row = doc.querySelector("#pkDetail tr.pk-line[data-code]");
  assert.ok(row, "选股记录应渲染出可点击的行");

  row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  // 给异步的辩护加载留足时间 —— 这正是原来「展开了又被冲掉」的窗口
  await tick(window, 12);

  assert.ok(doc.querySelector("#pkDetail tr.pk-cr"), "点击后应出现展开行（分时图容器）");
  assert.ok(doc.querySelector("#pkDetail tr.pk-line.on"), "被点的行应保持选中态");

  const row2 = doc.querySelector("#pkDetail tr.pk-line[data-code]");
  row2.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await tick(window, 6);
  assert.equal(doc.querySelector("#pkDetail tr.pk-cr"), null, "再次点击应收起");
});

test("做辩护 / 离线 A/B 按钮必须与 LLM 解读同组且在其后", async (t) => {
  const ctx = await setup(t);
  const { doc } = ctx;

  const tools = doc.querySelector(".scr-top-tools");
  assert.ok(tools, "应存在 .scr-top-tools 工具条");

  const ids = Array.from(tools.querySelectorAll("button")).map((b) => b.id);
  for (const need of ["btnScrLlm", "btnBearRun", "btnBearAb"]) {
    assert.ok(ids.includes(need), `${need} 应在工具条内，实际：${ids.join(",")}`);
  }
  assert.ok(ids.indexOf("btnBearRun") > ids.indexOf("btnScrLlm"), "做辩护应在 LLM 解读之后");
  assert.ok(ids.indexOf("btnBearAb") > ids.indexOf("btnBearRun"), "离线 A/B 应在做辩护之后");
});

test("辩护结论出现在表格「辩护」列，悬停立即弹出结构化理由", async (t) => {
  const ctx = await setup(t);
  const { window, doc } = ctx;

  window.switchTab("picks");
  await tick(window, 12);

  const cells = doc.querySelectorAll("#pkDetail td.pk-bear-cell");
  assert.ok(cells.length >= 2, `应有辩护单元格，实际 ${cells.length} 个`);

  const chips = Array.from(cells).map((c) => c.textContent.trim());
  assert.ok(chips.includes("保留"), `应显示「保留」，实际：${chips.join("/")}`);
  assert.ok(chips.includes("否决"), `应显示「否决」，实际：${chips.join("/")}`);

  // 否决的那只：悬停应立刻出气泡（不再依赖原生 title 的 1 秒延迟）
  const dropCell = Array.from(cells).find((c) => c.textContent.includes("否决"));
  const chip = dropCell.querySelector(".pk-bear-chip");
  assert.ok(chip, "应有 chip 元素");
  assert.equal(chip.getAttribute("title"), null, "不应再用原生 title（有约 1 秒强制延迟）");
  assert.ok(chip.dataset.bear, "chip 应带 data-bear 结构化内容");

  // 数据结构校验
  const payload = JSON.parse(chip.dataset.bear);
  assert.equal(payload.v, "drop", "应记录裁决结论");
  assert.ok(payload.bears.length > 0, "应带否决理由");
  assert.ok(payload.bears[0].point, "理由应有正文");

  // 模拟悬停，验证气泡内容
  chip.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  await tick(window, 2);

  const pop = doc.getElementById("termPop");
  assert.ok(pop && pop.classList.contains("on"), "悬停应弹出气泡");
  assert.ok(pop.classList.contains("bear-pop"), "应是辩护样式的气泡");
  assert.ok(pop.textContent.includes("否决"), "气泡应显示结论");
  assert.ok(pop.textContent.includes(payload.bears[0].point.slice(0, 8)),
    `气泡应包含理由正文，实际：${pop.textContent.slice(0, 60)}`);
  assert.ok(pop.querySelectorAll(".bp-list li").length > 0, "理由应渲染成列表");
});

test("候选榜用单列布局，不应残留空的右列", async (t) => {
  const ctx = await setup(t);
  const { doc } = ctx;

  const grid = doc.querySelector(".scr-grid");
  assert.ok(grid, "应有 .scr-grid 容器");
  assert.equal(
    grid.children.length, 1,
    `辩护卡片已合并，.scr-grid 应只有 1 个子元素，实际 ${grid.children.length} 个`,
  );
});
