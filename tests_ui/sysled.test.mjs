/**
 * 本轮改动：术语表独立成页 + 顶栏状态灯
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, tick, close } from "./helpers.mjs";

/** 可配置 /api/tasks 的返回，用来测两种灯态 */
function makeApi(tasksPayload) {
  return (url) => {
    if (url.includes("/api/tasks")) return tasksPayload;
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
    if (url.includes("/api/data/hub")) {
      return { ok: true,
        freshness: { ok: true, actions: [], run: { running: false },
          as_of: "2026-11-24", quote_cached: 5000, quote_lag: 0,
          snapshot: { loaded: true, count: 5000, as_of: "2026-11-24", lag: 0 },
          dataset: { loaded: true, rows: 1000, date_max: "2026-11-24", lag: 0,
                     label_mode: "rank" },
          model: { trained: true, engine: "lightgbm", age_days: 1, ic: 0.02,
                   ic_ir: 0.1, retrain_due: false } },
        scheduler: {}, runs: [], bulk_workers: 6, sources: {}, llm: {} };
    }
    if (url.includes("/api/logs")) return { ok: true, logs: [], count: 0, facets: {} };
    if (url.includes("/api/universe")) {
      return { ok: true, rows: [], total: 5000, cached: 5000, filtered: 0,
               updated: "2026-11-23", target: "2026-11-20", lag: 0, fresh: true };
    }
    return { ok: true, batches: [], items: [], rows: [] };
  };
}

const IDLE = { ok: true, running: false, tasks: [], overall: null,
               recent: [{ kind: "pipeline", ok: true, finished: "2026-11-23 15:16:43" }] };

const RUNNING = {
  ok: true, running: true, overall: 0.42,
  tasks: [
    { key: "quote", name: "行情增量", done: 2340, total: 5580, pct: 0.42,
      detail: "2340 / 5580 只 · 成功 300 · 跳过 2020 · 失败 20" },
    { key: "pipeline", name: "横截面快照", done: 1, total: 3, pct: 0.33, detail: "第 2 / 3 步" },
  ],
  recent: [],
};

test("术语表是独立页面，不再是弹窗", async (t) => {
  const ctx = makePage({ api: makeApi(IDLE) });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;

  // 页面存在
  const panel = doc.querySelector('.panel[data-panel="terms"]');
  assert.ok(panel, "应有 data-panel=terms 的独立页面");
  // 弹窗应已移除
  assert.equal(doc.getElementById("termModal"), null, "术语表弹窗应已删除");
  assert.equal(doc.getElementById("termMask"), null, "弹窗遮罩应已删除");

  // 导航里有入口
  const navBtn = doc.querySelector('.nav-item[data-tab="terms"]');
  assert.ok(navBtn, "侧栏应有「术语表」入口");
  assert.ok(navBtn.textContent.includes("术语表"), `导航文案应为「术语表」，实际：${navBtn.textContent}`);

  // 切过去能渲染出术语
  window.switchTab("terms");
  await tick(window, 4);
  const list = doc.getElementById("termPageList");
  assert.ok(list, "应有术语列表容器");
  assert.ok(list.querySelectorAll(".tm-item").length > 10,
    `应渲染出多条术语，实际 ${list.querySelectorAll(".tm-item").length} 条`);
  assert.ok(list.querySelectorAll(".tm-group").length >= 2, "应按分类分组");

  // 搜索框可用
  const q = doc.getElementById("termPageQ");
  assert.ok(q, "应有搜索框");
  q.value = "夏普";
  q.dispatchEvent(new window.Event("input", { bubbles: true }));
  await tick(window, 2);
  const after = list.querySelectorAll(".tm-item").length;
  assert.ok(after < 10, `搜索应过滤结果，实际仍有 ${after} 条`);
  assert.ok(list.textContent.includes("夏普"), "搜索结果应包含「夏普」");
});

test("空闲时状态灯是绿点，不显示进度环", async (t) => {
  const ctx = makePage({ api: makeApi(IDLE) });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;
  await tick(window, 6);

  const led = doc.getElementById("sysLed");
  assert.ok(led, "顶栏应有状态灯");
  assert.ok(!led.classList.contains("on"), "空闲时不应处于运行态");

  const fill = doc.getElementById("sysLedFill");
  assert.ok(fill, "应有进度环元素");
  // 空闲时环应被清空（dashoffset = 全周长）
  const off = Number(fill.style.strokeDashoffset);
  const arr = Number(fill.style.strokeDasharray);
  assert.ok(arr > 0, "应设置周长");
  assert.ok(Math.abs(off - arr) < 0.5, `空闲时环应为空，offset=${off} vs 周长=${arr}`);

  // 空闲态用 title 说明「正常」
  assert.ok((led.getAttribute("title") || "").includes("正常"),
    `空闲应提示系统正常，实际：${led.getAttribute("title")}`);
});

test("有任务时变成蓝环，环长等于总进度", async (t) => {
  const ctx = makePage({ api: makeApi(RUNNING) });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;
  await tick(window, 6);

  const led = doc.getElementById("sysLed");
  assert.ok(led.classList.contains("on"), "有任务时应进入运行态");

  const fill = doc.getElementById("sysLedFill");
  const arr = Number(fill.style.strokeDasharray);
  const off = Number(fill.style.strokeDashoffset);
  const painted = 1 - off / arr;
  assert.ok(Math.abs(painted - 0.42) < 0.02,
    `环应画到 42%，实际 ${(painted * 100).toFixed(1)}%`);

  // 悬停内容：不再是 data-tip 纯文本，而是 ledTipHtml 渲染的可视化进度条
  assert.ok(led.classList.contains("term"), "运行态应挂成 .term 以便悬停出明细");
  assert.equal(led.getAttribute("title"), "", "运行态不应复用原生 title（有延迟）");

  led.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  await tick(window, 2);
  const pop = doc.getElementById("termPop");
  assert.ok(pop && pop.classList.contains("on"), "悬停应弹出气泡");
  assert.ok(pop.classList.contains("led-pop"), "应是状态灯专用气泡");
  const txt = pop.textContent;
  assert.ok(txt.includes("行情增量"), `气泡应含任务名，实际：${txt.slice(0, 80)}`);
  assert.ok(txt.includes("横截面快照"), `气泡应含第二个任务，实际：${txt.slice(0, 120)}`);
  assert.ok(txt.includes("总进度"), `气泡应含总进度，实际：${txt.slice(-60)}`);
  // 关键：要有真正的进度条元素，不只是文字
  assert.ok(pop.querySelectorAll(".led-bar").length === 2, "每个任务应有一条进度条");
});

test("点击状态灯进入「运行日志」", async (t) => {
  const ctx = makePage({ api: makeApi(IDLE) });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;
  await tick(window, 4);

  doc.getElementById("sysLed").click();
  await tick(window, 5);

  const tab = doc.querySelector("#hubTabs .hub-tab.on");
  assert.equal(tab && tab.dataset.hubtab, "logs", "应切到「运行日志」标签");
  const pane = doc.querySelector(".hub-pane.on");
  assert.equal(pane && pane.dataset.hubtab, "logs", "应显示运行日志面板");
});

test("其他地方不再重复显示任务进度", async (t) => {
  const ctx = makePage({ api: makeApi(RUNNING) });
  t.after(() => close(ctx));
  await boot(ctx);
  const { doc } = ctx;

  // 这些进度容器已被状态灯取代
  assert.equal(doc.getElementById("hubRun"), null, "数据中枢的进度块应已移除");
  assert.equal(doc.getElementById("bulkProg"), null, "股票池的进度条应已移除");
  assert.equal(doc.getElementById("chipBulk"), null, "顶栏下载徽标应已移除");
});
