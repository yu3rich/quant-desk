/**
 * 回归：
 *  - 「用模型分」必须默认勾上；旧偏好里存着 false 时要作废一次。
 *    否则模型完全不参与选股，界面只有一个小 pill 提示，看起来就像「模型变了」。
 *  - 用户之后主动关掉的选择要保留，不能被每次加载都强行改回 true。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, close } from "./helpers.mjs";

const api = (url) => {
  if (url.includes("/api/screen/status")) {
    return { ok: true,
      data: { snapshot: { loaded: true, rows: 5000 }, dataset: { loaded: true, rows: 1000 },
              cached_total: 5000, universe_total: 5000, modules: [] },
      model: { trained: true, engine: "lightgbm", meta: { metrics: {} } },
      llm: { enabled: false, configured: false }, planned: [] };
  }
  if (url.includes("/api/screen/freshness")) {
    return { ok: true, snapshot: {}, dataset: {}, model: {}, actions: [],
             run: { running: false, steps: [] } };
  }
  if (url.includes("/api/data/hub")) {
    return { ok: true, freshness: { ok: true, actions: [], run: { running: false } },
             scheduler: {}, runs: [], bulk_workers: 6, sources: {}, llm: {} };
  }
  return { ok: true, rows: [], items: [], actions: [], batches: [], count: 0 };
};

test("旧偏好里的「用模型分=false」被迁移为 true", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window } = ctx;

  // 模拟老版本留下的状态：关掉了模型分，且没有版本号
  window.localStorage.setItem("quantdesk_screen_prefs_v1",
    JSON.stringify({ scrUseModel: false, scrTopN: "20" }));
  window.localStorage.removeItem("quantdesk_screen_prefs_ver");

  window.scrLoadPrefs();

  assert.equal(window.document.getElementById("scrUseModel").checked, true,
    "旧偏好里的 false 必须被作废，模型分默认开启");
  assert.equal(window.localStorage.getItem("quantdesk_screen_prefs_ver"), "2",
    "迁移后要写版本号，避免每次加载都重置");
});

test("已迁移过（版本=2）时尊重用户手动关闭", async (t) => {
  const ctx = makePage({ api });
  t.after(() => close(ctx));
  await boot(ctx);
  const { window } = ctx;

  window.localStorage.setItem("quantdesk_screen_prefs_v1",
    JSON.stringify({ scrUseModel: false }));
  window.localStorage.setItem("quantdesk_screen_prefs_ver", "2");

  window.scrLoadPrefs();

  assert.equal(window.document.getElementById("scrUseModel").checked, false,
    "版本号已是 2，说明用户是有意关掉的，不能强行改回");
});
