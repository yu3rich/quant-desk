/**
 * 移动端导航：汉堡按钮、抽屉开合、切页/待办后自动收起、待办随尺寸搬迁。
 *
 * jsdom 不计算媒体查询，所以「显示与否」靠 CSS 断言，交互靠 class 与 DOM 位置断言。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makePage, boot, close } from "./helpers.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

/** 最小可用的接口桩：init 会串起状态/股票池/选股恢复，缺字段会在渲染里抛错 */
function apiFor(url) {
  if (url.includes("/api/screen/status")) {
    return { ok: true,
      data: { snapshot: { loaded: true, rows: 5000 }, dataset: { loaded: true, rows: 1000 },
              cached_total: 5000, universe_total: 5000, modules: [] },
      model: { trained: true, engine: "lightgbm", meta: { metrics: {} } },
      llm: { enabled: false, configured: false }, planned: [] };
  }
  if (url.includes("/api/watchlist")) return { ok: true, items: [], count: 0, default: "" };
  if (url.includes("/api/bulk")) return { ok: true, running: false, cached_total: 5000 };
  if (url.includes("/api/data/hub")) {
    return { ok: true,
      freshness: { ok: true, actions: [], run: { running: false }, as_of: "2026-11-24",
        quote_cached: 5000, quote_lag: 0,
        snapshot: { loaded: true, count: 5000, as_of: "2026-11-24", lag: 0 },
        dataset: { loaded: true, rows: 1000, date_max: "2026-11-24", lag: 0, label_mode: "rank" },
        model: { trained: true, engine: "lightgbm", age_days: 1, ic: 0.02, ic_ir: 0.1,
                 retrain_due: false } },
      scheduler: {}, runs: [], bulk_workers: 6, sources: {}, llm: {} };
  }
  if (url.includes("/api/screen/freshness")) {
    return { ok: true, as_of: "2026-11-24", quote_cached: 5000, quote_lag: 0,
      snapshot: { loaded: true, count: 5000, as_of: "2026-11-24", lag: 0 },
      dataset: { loaded: true, rows: 1000, date_max: "2026-11-24", lag: 0, label_mode: "rank" },
      model: { trained: true, engine: "lightgbm", age_days: 1, ic: 0.02, ic_ir: 0.1,
               retrain_due: false },
      actions: [], run: { running: false, steps: [] } };
  }
  if (url.includes("/api/logs")) return { ok: true, logs: [], count: 0, facets: {} };
  if (url.includes("/api/tasks")) return { ok: true, running: false, tasks: [], overall: null, recent: [] };
  return { ok: true, batches: [], items: [], rows: [], candidates: [] };
}

/** 把视口宽度定成指定值（jsdom 里 innerWidth 可写，但要走 defineProperty） */
function setWidth(ctx, w) {
  Object.defineProperty(ctx.window, "innerWidth", { value: w, configurable: true });
}

test("汉堡按钮只在窄屏出现，桌面端隐藏", () => {
  const css = read("web/style.css");
  assert.match(css, /\.nav-toggle\{display:none;/, "桌面端默认隐藏");
  assert.match(css, /@media\(max-width:860px\)\{[\s\S]*?\.nav-toggle\{display:inline-flex\}/,
    "≤860px 才显示");
  // 顶栏高度从 192px 压到 54px 是这次改造的核心
  assert.match(css, /@media\(max-width:860px\)\{[\s\S]*?\.sidebar\{[^}]*height:54px/);
});

test("抽屉样式：默认移出屏幕，打开后归位并锁滚动", () => {
  const css = read("web/style.css");
  assert.match(css, /\.nav\{position:fixed;top:0;left:0;bottom:0;width:min\(84vw,300px\)/);
  assert.match(css, /transform:translateX\(-104%\)/, "关闭时移出视口");
  assert.match(css, /body\.nav-open \.nav\{transform:none\}/, "打开后归位");
  assert.match(css, /body\.nav-open\{overflow:hidden\}/, "打开时锁背景滚动");
  assert.match(css, /body\.nav-open \.nav-scrim\{opacity:1;pointer-events:auto\}/);
  // 触控高度
  assert.match(css, /\.nav-item\{[^}]*min-height:44px/);
});

test("点击汉堡开合，Esc 与遮罩都能关", async () => {
  const ctx = makePage({ api: apiFor });
  try {
    await boot(ctx);
    const doc = ctx.doc;
    const tg = doc.getElementById("navToggle");
    assert.ok(tg, "应有汉堡按钮");
    assert.equal(tg.getAttribute("aria-expanded"), "false");
    assert.equal(tg.getAttribute("aria-controls"), "nav");

    tg.click();
    assert.equal(doc.body.classList.contains("nav-open"), true, "点一下应打开");
    assert.equal(tg.getAttribute("aria-expanded"), "true", "aria 状态要跟着变");

    doc.dispatchEvent(new ctx.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    assert.equal(doc.body.classList.contains("nav-open"), false, "Esc 关闭");

    tg.click();
    doc.getElementById("navScrim").click();
    assert.equal(doc.body.classList.contains("nav-open"), false, "点遮罩关闭");
  } finally {
    close(ctx);
  }
});

test("选中页签后抽屉自动收起", async () => {
  const ctx = makePage({ api: apiFor });
  try {
    await boot(ctx);
    const doc = ctx.doc;
    doc.getElementById("navToggle").click();
    assert.equal(doc.body.classList.contains("nav-open"), true);
    const tab = doc.querySelector('#nav .nav-item[data-tab="stock"]');
    tab.click();
    assert.equal(doc.body.dataset.page, "stock", "页签要真的切过去");
    assert.equal(doc.body.classList.contains("nav-open"), false, "切完要收起，不用再点遮罩");
  } finally {
    close(ctx);
  }
});

test("待办面板：窄屏挪进抽屉，宽屏挪回侧栏", async () => {
  const ctx = makePage({ api: apiFor });
  try {
    setWidth(ctx, 390);
    await boot(ctx);
    const doc = ctx.doc;
    const sf = doc.querySelector(".side-foot");
    assert.equal(sf.parentElement.id, "nav", "窄屏待办应在抽屉里");

    setWidth(ctx, 1200);
    ctx.window.dispatchEvent(new ctx.window.Event("resize"));
    assert.equal(sf.parentElement.className, "sidebar", "宽屏应回到侧栏");
    assert.equal(doc.body.classList.contains("nav-open"), false, "变宽时抽屉要关");
  } finally {
    close(ctx);
  }
});

test("手机端输入框字号 16px、按钮有触控高度", () => {
  const css = read("web/style.css");
  const m = css.match(/@media\(max-width:560px\)\{([\s\S]*?)\n\}/);
  assert.ok(m, "应有 ≤560px 的规则块");
  const block = m[1];
  assert.match(block, /body \.app input[^}]*font-size:16px/,
    "iOS 对 <16px 的输入框聚焦会放大整页");
  assert.match(block, /\.btn\{min-height:38px/);
  assert.match(block, /\.btn\.sm\{min-height:36px/);
  assert.match(block, /\.chart\{height:250px\}/, "手机上一张图不该占两屏");
});
