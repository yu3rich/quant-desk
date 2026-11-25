/**
 * 本轮：模型卡片展示 Top10 超额/胜率；候选榜默认 20 只；死代码清理。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makePage, close } from "./helpers.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(ROOT, "web/index.html"), "utf8");
const js = fs.readFileSync(path.join(ROOT, "web/app.js"), "utf8");

test("模型卡片显示 Top10 超额与胜率", () => {
  const ctx = makePage();
  try {
    ctx.window.renderScrModel({
      trained: true,
      meta: {
        engine: "lightgbm", rows_train: 126043, rows_valid: 45295,
        split_date: "20250828", features: new Array(24).fill("f"),
        neutral: "none", label_mode: "rank", trained: "2026-11-25 16:29:19",
        metrics: { ic: 0.0811, rank_ic: 0.1057, ic_ir: 0.64, head10: 0.3724,
                   head10_win: 0.5, top20_excess: 0.2045, all_excess: -0.2753 },
        feature_importance: { turnover: 0.237, ind_rel20: 0.099 },
      },
    });
    const txt = ctx.doc.getElementById("scrModel").textContent;
    assert.match(txt, /Top10 超额/, "应出现 Top10 超额一行");
    assert.match(txt, /0\.37/, "显示 head10 数值 0.37");
    assert.match(txt, /胜率 50%/, "显示胜率");
    assert.match(txt, /RankIC/, "原有指标仍在");
  } finally {
    close(ctx);
  }
});

test("候选榜默认 20 只（HTML 与 JS 兜底一致）", () => {
  assert.match(html, /id="scrTopN"[^>]*value="20"/, "输入框默认 20");
  assert.match(js, /scrNum\("#scrTopN", 20\)/, "JS 兜底 20");
  assert.doesNotMatch(js, /scrNum\("#scrTopN", 10\)/, "不再残留 10 的兜底");
});

test("旧偏好迁移：TopN=10 / 训练上限=300 会升级到新默认", () => {
  const ctx = makePage();
  try {
    ctx.window.localStorage.setItem("quantdesk_screen_prefs_v1", JSON.stringify({
      scrTopN: 10, scrMaxSymbols: 300, scrHorizon: 7,
    }));
    ctx.window.scrLoadPrefs();
    assert.equal(ctx.doc.getElementById("scrTopN").value, "20", "TopN 10 → 20");
    assert.equal(ctx.doc.getElementById("scrMaxSymbols").value, "800", "上限 300 → 800");
    assert.equal(ctx.doc.getElementById("scrHorizon").value, "7", "用户自定义值不被改动");
  } finally {
    close(ctx);
  }
});

test("死代码已清理，必要函数仍在", () => {
  for (const dead of ["openTermModal", "renderTermList", "rtCfgClose", "pkPending"]) {
    assert.ok(!js.includes("function " + dead), dead + " 应已删除");
  }
  assert.ok(js.includes("function closeTermModal"), "Esc 关闭仍需要它");
  assert.ok(js.includes("function termPageRender"), "术语表渲染仍在");
});
