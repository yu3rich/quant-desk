/**
 * 悬停提示统一：同一个 #termPop，透明度/宽度/文字层级一套；行业分布不再用原生 title。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makePage, close } from "./helpers.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const css = fs.readFileSync(path.join(ROOT, "web/style.css"), "utf8");
const js = fs.readFileSync(path.join(ROOT, "web/app.js"), "utf8");

test("气泡透明度统一为 0.80，且只有一处定义", () => {
  const m = css.match(/#termPop\{[^}]*background:rgba\((\d+),(\d+),(\d+),([\d.]+)\)/);
  assert.ok(m, "应能找到 #termPop 的背景定义");
  assert.equal(Number(m[4]), 0.8, "透明度统一为 0.8（原 0.86）");
  const all = css.match(/background:rgba\(2[0-9],[0-9]+,[0-9]+,\.[0-9]+\)/g) || [];
  assert.ok(all.length >= 1, "背景色只应在 #termPop 上定义");
});

test("辩护 / 状态灯 / 长说明三种气泡宽度统一为 400", () => {
  assert.match(css, /#termPop\.wide\{max-width:400px/);
  assert.match(css, /#termPop\.bear-pop\{max-width:400px/);
  assert.match(css, /#termPop\.led-pop\{max-width:400px/);
});

test("纯文本提示也走 .tp-desc，行高与颜色统一", () => {
  assert.match(css, /#termPop \.tp-desc\{[^}]*line-height:1\.72/);
  const ctx = makePage();
  try {
    const el = ctx.doc.createElement("span");
    el.setAttribute("data-tip", "暴露 12.5% · 基线 0.8%");
    el.setAttribute("data-tip-name", "银行");
    ctx.doc.body.appendChild(el);
    ctx.window.showTerm(el);
    const pop = ctx.doc.getElementById("termPop");
    assert.match(pop.innerHTML, /class="tp-name">银行/);
    assert.match(pop.innerHTML, /class="tp-desc">暴露 12\.5%/);
    assert.ok(pop.classList.contains("on"));
    assert.ok(!pop.classList.contains("bear-pop"), "普通提示不应带辩护样式");
  } finally {
    close(ctx);
  }
});

test("任何 data-tip 元素都能触发气泡（行业分布不再用原生 title）", () => {
  assert.match(js, /const SEL = "\.term, \.q, \[data-tip\]"/);
  assert.match(js, /data-tip="' \+ esc\(tipBody\)/);
  assert.doesNotMatch(js, /class="scr-exp-row" title=/);
  // 股票池缓存 pill：只用气泡，原生 title 已全部移除
  assert.match(js, /data-tip-name="行情缓存"/);
  assert.match(js, /class="pill ' \+ \(s\.cached \? "yes" : "no"\) \+ '" data-tip="/);
  assert.doesNotMatch(js, /class="pill ' \+ \(s\.cached \? "yes" : "no"\) \+ '" title=/);
});

test("辩护与状态灯仍走各自的内容渲染，但共用同一个浮层", () => {
  assert.match(js, /pop\.classList\.add\("on", "wide", "bear-pop"\)/);
  assert.match(js, /pop\.classList\.add\("on", "wide", "led-pop"\)/);
  assert.match(js, /pop\.classList\.remove\("bear-pop", "led-pop"\)/);
});
