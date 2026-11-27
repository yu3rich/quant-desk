/**
 * 前端长任务：不再全屏遮罩，改由右上角状态灯转圈，页面保持可操作。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makePage, close } from "./helpers.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("登记本地任务后状态灯转圈、页面不遮罩", () => {
  const ctx = makePage({ api: () => ({ ok: true }) });
  const { window, doc } = ctx;
  window.sysTaskStart("factors", "单因子体检");

  const led = doc.getElementById("sysLed");
  assert.ok(led.classList.contains("on"), "有本地任务时应进入运行态");
  assert.ok(led.classList.contains("local"), "应标记为本地任务（绿点转圈）");
  assert.match(led.dataset.tip || "", /单因子体检/,
    "悬停应说明正在做什么（统一走 data-tip，不用原生 title）");
  assert.ok(!led.getAttribute("title"), "不应再设原生 title，否则会和气泡叠成两层");

  const loading = doc.getElementById("loading");
  assert.ok(!loading.classList.contains("on"), "不应再弹全屏遮罩");
  close(ctx);
});

test("任务结束后恢复绿点", () => {
  const ctx = makePage({ api: () => ({ ok: true }) });
  const { window, doc } = ctx;
  window.sysTaskStart("factors", "单因子体检");
  window.sysTaskEnd("factors");
  const led = doc.getElementById("sysLed");
  assert.ok(!led.classList.contains("local"), "结束后不再转圈");
  assert.ok(!led.classList.contains("on"), "没有任务时回到空闲态");
  close(ctx);
});

test("多个任务并存时状态灯保持运行，全部结束才恢复", () => {
  const ctx = makePage({ api: () => ({ ok: true }) });
  const { window, doc } = ctx;
  window.sysTaskStart("factors", "单因子体检");
  window.sysTaskStart("screen", "选股");
  window.sysTaskEnd("factors");
  const led = doc.getElementById("sysLed");
  assert.ok(led.classList.contains("local"), "还剩选股，灯应继续转");
  window.sysTaskEnd("screen");
  assert.ok(!led.classList.contains("local"), "全部结束才恢复");
  close(ctx);
});

test("CSS：转圈尺寸与进度环一致", () => {
  const css = fs.readFileSync(path.join(ROOT, "web/style.css"), "utf8");
  assert.match(css, /\.sysled\.local \.sysled-dot\{[^}]*width:24px[^}]*height:24px/);
  assert.match(css, /\.sysled\.local \.sysled-dot\{[^}]*animation:sysledspin/);
  assert.match(css, /@keyframes sysledspin/);
});

test("源码：长任务走状态灯，只有首屏账本仍用全屏遮罩", () => {
  const js = fs.readFileSync(path.join(ROOT, "web/app.js"), "utf8");
  assert.equal((js.match(/loading\(true/g) || []).length, 1,
    "只应剩「首次读取实盘账本」一处全屏遮罩");
  for (const key of ["factors", "backtest", "screen", "scan", "stock-analyze", "bear"]) {
    assert.ok(js.includes('sysTaskStart("' + key + '"'), key + " 应登记到状态灯");
    assert.ok(js.includes('sysTaskEnd("' + key + '"'), key + " 结束时撤下");
  }
});
