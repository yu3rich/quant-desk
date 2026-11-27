/**
 * 本轮改动：术语表独立成页 + 顶栏状态灯
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, tick, close, readSrc } from "./helpers.mjs";

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

  // 空闲态用 data-tip 说明「正常」（原生 title 会和自定义气泡叠成两层）
  assert.ok((led.dataset.tip || "").includes("正常"),
    `空闲应提示系统正常，实际：${led.dataset.tip}`);
  assert.ok(!led.getAttribute("title"), "空闲态也不应设原生 title");
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
  assert.ok(!led.getAttribute("title"), "运行态不应复用原生 title（有延迟）");

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

test("行情包导出的任务在状态灯显示环形进度", async (t) => {
  // 后端把 datapack 聚合进 /api/tasks 后，状态灯应像其他长任务一样画进度环，
  // 而不是只转圈。这是「导出行情包也有进度提示」的核心可见行为。
  const PACK_RUNNING = {
    ok: true, running: true, overall: 0.42,
    tasks: [
      { key: "pack", name: "导出行情包", done: 4200000, total: 10000000, pct: 0.42,
        detail: "写元数据 · 4200000 / 10000000" },
    ],
    recent: [],
  };
  const ctx = makePage({ api: makeApi(PACK_RUNNING) });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;
  await tick(window, 6);

  const led = doc.getElementById("sysLed");
  assert.ok(led.classList.contains("on"), "行情包导出时状态灯应进入运行态");
  assert.ok(!led.classList.contains("local"), "应走后端任务进度环，不是本地转圈");

  const fill = doc.getElementById("sysLedFill");
  const arr = Number(fill.style.strokeDasharray);
  const off = Number(fill.style.strokeDashoffset);
  assert.ok(Math.abs((1 - off / arr) - 0.42) < 0.02, "进度环应画到 42%");

  led.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  await tick(window, 2);
  const pop = doc.getElementById("termPop");
  assert.ok(pop && pop.classList.contains("led-pop"), "悬停应弹状态灯气泡");
  assert.ok(pop.textContent.includes("导出行情包"), "气泡应含任务名「导出行情包」");
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

test("任务启动引导：长任务先转圈再飞向状态灯、结束清理且不锁页面", async (t) => {
  const ctx = makePage({ api: makeApi(IDLE) });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;

  // 记录 animate 关键帧：决策后终点应缩小并飞向状态灯方向
  const frames = [];
  const be = doc.getElementById("taskBeacon");
  const core = be.querySelector(".tb-core");
  core.animate = (kf) => { frames.push(kf); return { cancel() {}, finished: Promise.resolve() }; };
  be.animate = (kf) => { frames.push(kf); return { cancel() {}, finished: Promise.resolve() }; };

  window.sysTaskStart("screen", "选股");
  assert.ok(be.classList.contains("on"), "启动时引导层应出现");
  assert.ok(!be.querySelector(".tb-label"), "中间只显示圆圈，不应有文字");
  assert.ok(be.querySelector(".tb-ring .tb-arc"), "转圈应是 SVG 圆弧（圆头）");
  assert.equal(frames.length, 0, "先在中间转圈，不立即飞向状态灯");

  await new Promise((r) => setTimeout(r, 800));
  assert.ok(frames.length >= 1, "长任务过决策点后应飞向状态灯");
  const kf = frames[0];
  assert.match(kf[kf.length - 1].transform, /scale\(\.42\)/, "终点圆圈应缩小");
  assert.ok(!kf.some((f) => /rotate\(/.test(f.transform || "")),
    "core 不应旋转，旋转交给 SVG 圆弧自身");

  await new Promise((r) => setTimeout(r, 500));
  assert.ok(!be.classList.contains("on"), "动画结束后引导层应撤下（不常驻遮罩）");
  assert.ok(doc.getElementById("sysLed").classList.contains("beacon-ping"), "状态灯应脉冲高亮");
});

test("任务启动引导：短任务原地转完淡出，不飞状态灯也不脉冲", async (t) => {
  const ctx = makePage({ api: makeApi(IDLE) });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;

  const frames = [];
  const be = doc.getElementById("taskBeacon");
  const core = be.querySelector(".tb-core");
  core.animate = (kf) => { frames.push(kf); return { cancel() {}, finished: Promise.resolve() }; };
  be.animate = (kf) => { frames.push(kf); return { cancel() {}, finished: Promise.resolve() }; };

  window.sysTaskStart("spot", "刷新行情");
  window.sysTaskEnd("spot");   // 0.75s 内完成
  await new Promise((r) => setTimeout(r, 800));

  assert.ok(frames.length >= 1, "短任务也应有一轮原地淡出动画");
  const kf = frames[0];
  assert.ok(!kf.some((f) => /scale\(\.42\)/.test(f.transform || "")), "短任务圆圈不应缩小飞走");
  assert.ok(!kf.some((f) => /calc\(-50% \+/.test(f.transform || "")), "短任务不应产生位移");

  await new Promise((r) => setTimeout(r, 350));
  assert.ok(!be.classList.contains("on"), "原地淡出后应撤下引导层");
  assert.ok(!doc.getElementById("sysLed").classList.contains("beacon-ping"), "短任务不应脉冲状态灯");
});

test("CSS：引导层不锁页面、无遮罩无文字、圆弧圆头带阴影", () => {
  const css = readSrc("web/style.css");
  assert.match(css, /#taskBeacon\{[^}]*pointer-events:none/);
  const beaconBlock = css.match(/#taskBeacon\{[^}]*\}/)[0];
  assert.ok(!/backdrop-filter|background:/.test(beaconBlock), "不应虚化/遮罩背景");
  assert.ok(!/#taskBeacon \.tb-label/.test(css), "不应有文字标签样式");
  const ringBlock = css.match(/#taskBeacon \.tb-ring\{[^}]*\}/)[0];
  assert.match(ringBlock, /filter:drop-shadow/, "圆圈应带阴影");
  const arcBlock = css.match(/#taskBeacon \.tb-arc\{[^}]*\}/)[0];
  assert.match(arcBlock, /stroke-linecap:round/, "转圈端点应为圆头");
  assert.match(arcBlock, /stroke-width:6/, "圆弧应够粗");
  assert.match(css, /\.sysled\.beacon-ping::after/);
});
