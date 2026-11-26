/**
 * 回归：行情看图的副标题必须说清「复权口径」和「当前用的哪个源」。
 * 2026-11-26 腾讯老路径被 WAF 拦成 501 后新增了新浪降级：新浪只有日线、
 * 且不复权，如果界面仍写「前复权」，除权日的跳空会被当成数据错误。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, close } from "./helpers.mjs";

const api = (url) => {
  if (url.includes("/api/screen/status")) {
    return { ok: true,
      data: { snapshot: { loaded: true, rows: 5000 }, dataset: {}, cached_total: 5000,
              universe_total: 5000, modules: [] },
      model: { trained: true, engine: "lightgbm", meta: { metrics: {} } },
      llm: { enabled: false, configured: false }, planned: [] };
  }
  if (url.includes("/api/chart")) {
    return { ok: true, code: "300093", name: "金刚光伏", period: "day", date: "2026-11-26",
      last: 10.56, pct: 3.5, pre_close: 10.2, adjust: "qfq", source: "腾讯",
      bars: [
        { date: "2026-11-25", open: 10, close: 10.2, high: 10.4, low: 9.9, volume: 100, pct: 0 },
        { date: "2026-11-26", open: 10.2, close: 10.56, high: 10.6, low: 10.1, volume: 120, pct: 3.5 },
      ] };
  }
  if (url.includes("/api/screen/freshness")) {
    return { ok: true, as_of: "2026-11-26", quote_cached: 5000, quote_lag: 0,
      snapshot: { count: 5000, as_of: "2026-11-25", lag: 0 },
      dataset: { rows: 1000, date_max: "2026-11-25", label_mode: "rank" },
      model: {}, actions: [], run: { running: false, steps: [] }, data_stale: false };
  }
  if (url.includes("/api/data/hub")) {
    return { ok: true, actions: [], run: { running: false }, scheduler: {}, runs: [],
             freshness: { ok: true, actions: [], run: { running: false } } };
  }
  return { ok: true, rows: [], items: [], batches: [], actions: [] };
};

const bar = { date: "2026-11-26", open: 10, close: 10.5, high: 10.6, low: 9.9, volume: 100, pct: 1 };

test("主源：副标题写「前复权」且不提备用源", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window } = ctx;
  window.mkRender({ ok: true, code: "300093", name: "金刚光伏", period: "day",
    date: "2026-11-26", last: 10.5, pct: 1, pre_close: 10, adjust: "qfq", source: "腾讯",
    bars: [bar] });
  const txt = window.document.getElementById("mkSub").textContent;
  assert.match(txt, /前复权/);
  assert.doesNotMatch(txt, /备用源/);
});

test("降级到新浪：必须写「不复权 · 备用源 新浪」", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window } = ctx;
  window.mkRender({ ok: true, code: "300093", name: "金刚光伏", period: "week",
    date: "2026-11-25", last: 10.2, pct: 1, pre_close: 10, adjust: "none", source: "新浪",
    bars: [bar] });
  const txt = window.document.getElementById("mkSub").textContent;
  assert.match(txt, /不复权/);
  assert.match(txt, /备用源 新浪/);
  assert.doesNotMatch(txt, /前复权/);
});
