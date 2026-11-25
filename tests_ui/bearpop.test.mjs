/** 验证辩护气泡的交互细节：点击固定、Esc 关闭、滚动隐藏 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, tick, close } from "./helpers.mjs";

function apiFor(url) {
  if (url.includes("/api/screen/bear")) {
    return {
      ok: true, saved: true, mode: "llm", at: "2026-11-23 14:40:52",
      model: "m1", judge_model: "m2", dropped: 1, kept: 1,
      rows: [
        { code: "600926", name: "杭州银行", verdict: "keep", enough: true,
          bears: [{ point: "RSI14 接近超买", trigger: "RSI>70", watch: "RSI14" }] },
        { code: "688037", name: "芯源微", verdict: "drop", enough: true,
          judge_reason: "动量转负",
          bears: [{ point: "跌破 20 日线", trigger: "ma_dev<0", watch: "ma_dev" }] },
      ],
    };
  }
  if (url.includes("/api/screen/picks")) {
    return { ok: true, total_batches: 1, total_rows: 2, shown: 1,
      batches: [{ as_of: "2026-11-20", recorded_at: "2026-11-23 14:40:52",
        count: 2, last_date: "2026-11-23", avg_chg: -1.2, win: 1, scored: 2,
        horizons: {},
        picks: [
          { code: "600926", name: "杭州银行", rank: 1, chg: -0.77, industry: "银行",
            score: 0.1, rec_real: 12.3, last_real: 12.2, verdict: "keep", h: {},
            bear: [{ point: "RSI14 接近超买", trigger: "RSI>70", watch: "RSI14" }] },
          { code: "688037", name: "芯源微", rank: 2, chg: -2.94, industry: "半导体",
            score: 0.09, rec_real: 88.1, last_real: 85.5, verdict: "drop", h: {},
            bear: [{ point: "跌破 20 日线", trigger: "ma_dev<0", watch: "ma_dev" }] },
        ] }] };
  }
  if (url.includes("/api/picks/archive")) return { ok: true, items: [] };
  if (url.includes("/api/screen/status")) {
    return { ok: true,
      data: { snapshot: { loaded: true, rows: 5000 }, dataset: { loaded: true, rows: 1000 },
              cached_total: 5000, universe_total: 5000, modules: [] },
      model: { trained: true, engine: "lightgbm", meta: { metrics: {} } },
      llm: { enabled: false, configured: false }, planned: [] };
  }
  if (url.includes("/api/watchlist")) return { ok: true, items: [], count: 0 };
  if (url.includes("/api/bulk")) return { ok: true, running: false, cached_total: 5000 };
  if (url.includes("/api/screen/freshness")) {
    return { ok: true, snapshot: {}, dataset: {}, model: {}, actions: [],
             run: { running: false, steps: [] } };
  }
  if (url.includes("/api/chart")) {
    return { ok: true, price: [1, 2], vol: [1, 2], pct: 0, times: [] };
  }
  return { ok: true, batches: [], items: [], rows: [] };
}

async function setup(t) {
  const ctx = makePage({ api: apiFor });
  t.after(() => close(ctx));
  await boot(ctx);
  ctx.window.switchTab("picks");
  await tick(ctx.window, 12);
  return ctx;
}

function dropChip(doc) {
  const cells = doc.querySelectorAll("#pkDetail td.pk-bear-cell");
  const cell = Array.from(cells).find((c) => c.textContent.includes("否决"));
  return cell ? cell.querySelector(".pk-bear-chip") : null;
}
test("悬停立即显示，无需等待", async (t) => {
  const ctx = await setup(t);
  const { window, doc } = ctx;
  const chip = dropChip(doc);
  assert.ok(chip, "应找到否决 chip");

  const pop = () => doc.getElementById("termPop");

  chip.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  // 只等一个 tick：原生 title 需要 ~1s，这里应当立刻可见
  await tick(window, 1);
  assert.ok(pop() && pop().classList.contains("on"), "悬停后一个 tick 内就应显示");

  chip.dispatchEvent(new window.MouseEvent("mouseout", { bubbles: true }));
  await tick(window, 1);
  assert.ok(!pop().classList.contains("on"), "移开应立即隐藏");
});

test("点击可固定气泡，再点关闭", async (t) => {
  const ctx = await setup(t);
  const { window, doc } = ctx;
  const pop = () => doc.getElementById("termPop");

  // 每次操作前重新查询：辩护结论是异步到达的，会替换 td 内的 chip，
  // 一直持有旧引用会点在已经游离于 DOM 之外的节点上（事件冒泡不到 document）。
  const fire = (type) => {
    const chip = dropChip(doc);
    assert.ok(chip, "应能找到 chip");
    assert.ok(doc.contains(chip), "chip 必须还在文档里");
    chip.dispatchEvent(new window.MouseEvent(type, { bubbles: true }));
    return chip;
  };

  fire("mouseover");
  await tick(window, 1);
  fire("click");
  await tick(window, 1);
  assert.ok(pop().classList.contains("on"), "点击应显示并固定");

  fire("mouseout");
  await tick(window, 1);
  assert.ok(pop().classList.contains("on"), "固定后移开鼠标不应消失");

  fire("mouseover");
  await tick(window, 1);
  fire("click");
  await tick(window, 1);
  assert.ok(!pop().classList.contains("on"), "再点一次应关闭");
});

test("Esc 关闭气泡", async (t) => {
  const ctx = await setup(t);
  const { window, doc } = ctx;
  const chip = dropChip(doc);
  const pop = () => doc.getElementById("termPop");

  chip.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await tick(window, 1);
  assert.ok(pop().classList.contains("on"), "先显示");

  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await tick(window, 1);
  assert.ok(!pop().classList.contains("on"), "Esc 应关闭");
});

test("内容包含裁决理由与触发条件", async (t) => {
  const ctx = await setup(t);
  const { window, doc } = ctx;
  const chip = dropChip(doc);

  chip.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  await tick(window, 1);
  const pop = doc.getElementById("termPop");
  const txt = pop.textContent;

  assert.ok(txt.includes("判决") || txt.includes("裁决"), `应显示裁决理由，实际：${txt.slice(0, 80)}`);
  assert.ok(pop.querySelector(".bp-list"), "应有理由列表");
  assert.ok(txt.includes("触发") || txt.includes("观察"),
    `应包含触发条件或观察指标，实际：${txt.slice(0, 120)}`);
});
