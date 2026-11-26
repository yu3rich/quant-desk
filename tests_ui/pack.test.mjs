/**
 * 回归：行情包的导出/导入面板。
 *  - 只打包 K 线，界面必须写明「不含账本/日记/选股记录」；
 *  - 导出/导入进行中要禁用两个按钮，避免并发写同一个库；
 *  - 完成后「下载」链接必须亮出来，否则用户拿不到文件。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, close } from "./helpers.mjs";

const api = (url) => {
  if (url.includes("/api/data/pack/status")) {
    return { ok: true, running: false, phase: "完成", pct: 100, done: 9815023,
      total: 9815023, message: "导出 9815023 根 · 353 MB", db_size: 969469952,
      file: "C:/x/bars_20261126_1058.zip",
      files: [{ name: "bars_20261126_1058.zip", size: 352654415, at: "11-26 11:00" }] };
  }
  if (url.includes("/api/screen/status")) {
    return { ok: true,
      data: { snapshot: { loaded: true, rows: 5000 }, dataset: {}, cached_total: 5000,
              universe_total: 5000, modules: [] },
      model: { trained: true, engine: "lightgbm", meta: { metrics: {} } },
      llm: { enabled: false, configured: false }, planned: [] };
  }
  if (url.includes("/api/screen/freshness")) {
    return { ok: true, as_of: "2026-11-26", quote_cached: 5000, quote_lag: 0,
      snapshot: { count: 5000, as_of: "2026-11-25", lag: 0 },
      dataset: { rows: 1000, date_max: "2026-11-25", label_mode: "rank" },
      model: {}, actions: [], run: { running: false, steps: [] }, data_stale: false };
  }
  if (url.includes("/api/data/hub")) {
    return { ok: true, freshness: { ok: true, actions: [], run: { running: false } },
             scheduler: {}, runs: [], bulk_workers: 6, sources: {}, llm: {} };
  }
  return { ok: true, rows: [], items: [], batches: [], actions: [] };
};

test("导出完成后显示下载链接与体积", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window } = ctx;

  window.packRender(api("/api/data/pack/status"));
  const down = window.document.getElementById("lnkPackDown");
  assert.equal(down.hidden, false, "有包时必须露出下载入口");
  assert.match(window.document.getElementById("packInfo").textContent, /11-26 11:00/);
  assert.match(window.document.getElementById("packMsg").textContent, /9815023|353/);
});

test("运行中禁用两个按钮并显示进度", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window } = ctx;

  window.packRender({ ok: true, running: true, phase: "写入行情", pct: 42,
    done: 4200000, total: 9815023, message: "合并 bars", db_size: 1e9, files: [] });
  const doc = window.document;
  assert.equal(doc.getElementById("btnPackExport").disabled, true);
  assert.equal(doc.getElementById("btnPackImport").disabled, true);
  assert.equal(doc.getElementById("packProg").hidden, false);
  assert.equal(doc.getElementById("packFi").style.width, "42%");
  assert.match(doc.getElementById("packMsg").textContent, /写入行情/);
});

test("导出范围按年份换算成 since 日期", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window } = ctx;

  const sel = window.document.getElementById("packRange");
  sel.value = "0";
  assert.equal(window.packSince(), "", "全量时不传 since");
  sel.value = "3";
  const d = new Date();
  d.setFullYear(d.getFullYear() - 3);
  const want = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
    String(d.getDate()).padStart(2, "0");
  assert.equal(window.packSince(), want);
});

test("自动下载会生成带 download 的链接并点击", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;

  window.packRender({ ok: true, running: false, pct: 100,
    files: [{ name: "bars_1.zip", size: 1e6, at: "11-26 11:00" }] });
  let captured = null;
  const orig = doc.body.appendChild.bind(doc.body);
  doc.body.appendChild = (el) => {
    if (el.tagName === "A") { captured = el; el.click = () => { el.dataset.clicked = "1"; }; }
    return orig(el);
  };
  window.packAutoDownload();
  assert.ok(captured, "必须创建 <a download> 才能触发浏览器下载");
  assert.match(captured.getAttribute("href"), /\/api\/data\/pack\/download/);
  assert.equal(captured.getAttribute("download"), "bars_1.zip");
  assert.equal(captured.dataset.clicked, "1", "创建后必须点击，否则用户还是要手动点");
});

test("点导出直接发请求，不再弹目录选择", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window } = ctx;

  await window.packExport();
  const call = ctx.calls.find((c) => c.url.includes("/api/data/pack/export"));
  assert.ok(call, "降级路径也必须把导出请求发出去");
  assert.match(call.body, /since/);
});
