/**
 * 本轮三项改动的回归测试：
 *   1. 状态灯蓝环不被气泡的 .on 清掉（闪烁 bug）
 *   2. 状态灯悬停显示可视化进度条
 *   3. hub-duo 并排 + 下拉在 flex 容器里不撑满换行
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { makePage, boot, tick, close } from "./helpers.mjs";

const appJs = fs.readFileSync("web/app.js", "utf8");
const css = fs.readFileSync("web/style.css", "utf8");
const html = fs.readFileSync("web/index.html", "utf8");

/** 抽取 term 相关函数，在隔离环境里跑（避免整份 app.js 的副作用） */
function termEnv() {
  const start = appJs.indexOf("let _termPop = null");
  // 锚点用「关于页数据块」的开头：原来的 openTermModal 已作为死代码删除
  const end = appJs.indexOf("/* ---------------- 关于：整站使用说明");
  assert.ok(start >= 0 && end > start, "应能定位 term 函数段");
  return appJs.slice(start, end);
}

function apiFor(url) {
  if (url.includes("/api/tasks")) {
    return {
      ok: true, running: true, overall: 0.42,
      tasks: [
        { key: "quote", name: "行情增量", done: 2340, total: 5580,
          pct: 0.42, detail: "2340 / 5580 只" },
        { key: "pipeline", name: "快照", done: 1, total: 3,
          pct: 0.33, detail: "第 2 / 3 步" },
      ],
      recent: [],
    };
  }
  return { ok: true, batches: [], items: [], rows: [] };
}

const LED_HTML = '<button class="sysled on term" id="sysLed">' +
  '<svg><circle id="sysLedFill"></circle></svg><i class="sysled-dot"></i></button>';

test("修复闪烁：悬停/移开气泡都不清除状态灯的 .on（蓝环）", async () => {
  // 用 jsdom 建一个最小环境，注入 term 函数段
  const { JSDOM } = await import("jsdom");
  const dom = new JSDOM("<!DOCTYPE html><body>" + LED_HTML + "</body>",
    { runScripts: "outside-only", url: "http://localhost/" });
  const { window } = dom;
  const snippet = termEnv();
  // 预置依赖（与 app.js 顶层作用域一致，用 var 保证 eval 内可见）
  const escStart = appJs.indexOf("function esc(s)");
  const escFn = appJs.slice(escStart, appJs.indexOf("\n}", escStart) + 2);
  const pre = "var TERMS={}; var TERM_CAT_NAME={}; var BEAR_VERDICT={drop:{txt:'否决'},keep:{txt:'保留'},thin:{txt:'理由不足'}};" +
    "\n" + escFn + "\n";
  // SYSLED 由 snippet 自己定义；eval 后再填数据
  window.eval(pre + snippet + "\nwindow.__term = { showTerm, hideTerm, setData: function(d){ SYSLED.data = d; } };");
  window.__term.setData({ running: true, overall: 0.42,
    tasks: [{ name: '行情增量', pct: 0.42, detail: '2340/5580' }] });

  const led = window.document.getElementById("sysLed");
  assert.ok(led.classList.contains("on"), "初始应为蓝环运行态");

  window.__term.showTerm(led);
  assert.ok(led.classList.contains("on"), "悬停后蓝环必须保留（不能直接 remove('on')）");
  assert.ok(led.classList.contains("term-open"), "悬停应用独立类 term-open");

  window.__term.hideTerm(true);
  assert.ok(led.classList.contains("on"), "移开后蓝环仍须保留 —— 这就是闪烁根因");
  assert.ok(!led.classList.contains("term-open"), "移开应只清 term-open");
  window.close();
});

test("代码里不再用通用 .on 类管理状态灯悬停", () => {
  // 状态灯的 .on 专用于「运行中=蓝环」，气泡应用独立的 term-open
  assert.ok(appJs.includes('if (el.id === "sysLed") el.classList.add("term-open");'),
    "showTerm 应为状态灯加 term-open");
  assert.ok(appJs.includes('if (_termCur.id !== "sysLed") _termCur.classList.remove("on");'),
    "hideTerm 应跳过状态灯的 .on（蓝环专用）");
  assert.ok(appJs.includes('if (el.id !== "sysLed") el.classList.add("on");'),
    "showTerm 不应给状态灯加通用 .on");
});

test("状态灯气泡渲染可视化进度条（不是纯文字）", () => {
  assert.ok(appJs.includes("function ledTipHtml(d)"), "应有 ledTipHtml");
  assert.ok(appJs.includes('pop.innerHTML = ledTipHtml(SYSLED.data)'),
    "showTerm 应用 ledTipHtml 渲染状态灯气泡");

  // 进度条 DOM 结构
  assert.ok(appJs.includes('class="led-bar"'), "应生成进度条元素");
  assert.ok(appJs.includes('class="led-task"'), "每个任务一条");
  assert.ok(appJs.includes('class="led-total"'), "应有总进度");

  // CSS 里进度条样式存在
  assert.ok(css.includes("#termPop .led-bar i"), "进度条填充样式应存在");
});

test("上市名单与资金流并排容器存在", () => {
  assert.ok(html.includes('class="hub-duo"'), "应有 hub-duo 并排容器");
  assert.ok(css.includes(".hub-duo{display:grid"), "hub-duo 应为 grid");
  // 更新策略提示已并回数据链卡片，不再是独立的 hub-note-card
  assert.ok(!html.includes('class="card hub-note-card"'), "独立的割裂提示卡应移除");
  assert.ok(html.includes("hub-chain-note"), "提示应作为数据链卡片的一部分");
});

test("flex 容器里的下拉不再撑满整行（修正 width:100%）", () => {
  // 回归：.ep-sel 曾写死 width:100%，在 .log-filter/.inline 的 flex 容器里
  // 会被拉伸并逐个换行，三个筛选下拉各占一行。
  assert.ok(css.includes(".ep-sel{position:relative;display:inline-block;width:auto"),
    "包装层默认宽度应为 auto，不是 100%");
  assert.ok(css.includes(".field > .ep-sel{width:100%"),
    "只有纵向表单 .field 内才铺满");
  // 筛选条里的包装层用 ep-sel--mini 标记（不再继承 mini-sel，避免双边框）
  assert.ok(css.includes(".log-filter .ep-sel--mini{flex:0 0 150px;width:150px}"),
    "筛选条里的下拉应固定宽度横排");
});
