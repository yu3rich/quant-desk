/**
 * 回归：
 *  - 模型状态里的「特征重要度（Gain，前 8）」进度条必须真的有宽度
 *    （span 默认 inline，漏了 display:block 就只剩百分比数字）
 *  - 东财降为低频备份后，面板不能继续显示刺眼的「失败」
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, close, readSrc } from "./helpers.mjs";

const api = (url) => {
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
    return { ok: true, freshness: { ok: true, actions: [], run: { running: false } },
             scheduler: {}, runs: [], bulk_workers: 6, sources: {}, llm: {} };
  }
  if (url.includes("/api/universe")) {
    return { ok: true, rows: [], total: 5000, cached: 5000, filtered: 0,
             updated: "2026-11-23", target: "2026-11-20", lag: 0, fresh: true };
  }
  return { ok: true, batches: [], items: [], rows: [] };
};

test("模型状态的特征重要度进度条有 display:block 和内联宽度", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window, doc } = ctx;

  const css = readSrc("web/style.css");
  const rule = css.match(/\.scr-bar\s+\.fi\s*\{([^}]*)\}/);
  assert.ok(rule, "应有 .scr-bar .fi 的样式规则");
  assert.match(rule[1], /display\s*:\s*block/,
    "填充条必须是 block，否则 span 的 width/height 被忽略，只剩百分比数字");

  window.renderScrModel({ trained: true, available: true, meta: {
    engine: "lightgbm", version: "4.0", metrics: { ic: 0.04 },
    feature_importance: Object.fromEntries(
      Array.from({ length: 8 }, (_, i) => ["factor" + i, 0.2 - i * 0.02])),
  } });
  const box = doc.getElementById("scrModel");
  assert.match(box.textContent, /特征重要度（Gain，前 8）/, "应显示重要度标题");
  const bars = box.querySelectorAll(".scr-bar .fi");
  assert.equal(bars.length, 8, "前 8 个因子都应有进度条");
  bars.forEach((b) => assert.match(b.getAttribute("style") || "", /width:\s*[\d.]+%/,
    "每根进度条都应带内联百分比宽度"));
});

test("东财失败显示「备份未通」，普通源仍显示「失败」", (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  const row = (extra) => ctx.window.hubValRow(Object.assign({
    key: "tx", name: "腾讯批量行情", cap: "snapshot", role: "main",
    token: false, speed: "约 4~6 秒", order: 1, enabled: true,
    state: "失败", last: { at: "2026-11-24 15:30", error: "读取超时" },
  }, extra), 6);
  const backup = row({ key: "em", name: "东方财富快照 · 低频备份", backup: true });
  assert.match(backup, /备份未通/, "备份源失败不应标成普通「失败」");
  assert.match(backup, /失败冷却 6h/, "面板要说明它是低频备份");
  assert.doesNotMatch(row({}), /备份未通/, "普通源失败仍显示「失败」");
});
