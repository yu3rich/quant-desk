/**
 * 行业分布：暴露 + 基线的横向堆叠条。
 * 行内只显示暴露百分比，基线只在悬停提示里。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makePage, close } from "./helpers.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const EXPO = {
  ok: true, n: 30, industries: 3, hhi: 0.42, effective_n: 2.4,
  max_industry: "电子", max_weight: 30, unclassified: 0,
  rows: [
    { industry: "电子", weight: 30, base_weight: 5 },
    { industry: "银行", weight: 10, base_weight: 20 },
    { industry: "医药", weight: 5, base_weight: 5 },
  ],
};

function rowsOf(doc) {
  return Array.from(doc.querySelectorAll("#scrExpo .scr-exp-row"));
}

test("每行一根堆叠条，蓝色暴露段在前、灰色基线段在后", () => {
  const ctx = makePage();
  try {
    ctx.window.renderExposure(EXPO, { mode: "none" });
    const rows = rowsOf(ctx.doc);
    assert.equal(rows.length, 3, "三个行业三行");
    rows.forEach((r) => {
      const segs = r.querySelectorAll(".bar i");
      assert.equal(segs.length, 2, "每行两段：暴露 + 基线");
      assert.ok(segs[0].classList.contains("exp"), "第一段是暴露");
      assert.ok(segs[1].classList.contains("base"), "第二段是基线");
    });
  } finally {
    close(ctx);
  }
});

test("段宽按「暴露 + 基线」的全局最大值归一", () => {
  const ctx = makePage();
  try {
    ctx.window.renderExposure(EXPO, {});
    const rows = rowsOf(ctx.doc);
    const w = (r, i) => parseFloat(r.querySelectorAll(".bar i")[i].style.width);
    // maxSum = 30+5 = 35
    assert.ok(Math.abs(w(rows[0], 0) - 30 / 35 * 100) < 0.05, "电子暴露段 85.7%");
    assert.ok(Math.abs(w(rows[0], 1) - 5 / 35 * 100) < 0.05, "电子基线段 14.3%");
    assert.ok(Math.abs(w(rows[1], 0) - 10 / 35 * 100) < 0.05, "银行暴露段 28.6%");
    assert.ok(Math.abs(w(rows[1], 1) - 20 / 35 * 100) < 0.05, "银行基线段 57.1%");
  } finally {
    close(ctx);
  }
});

test("行内默认只显示暴露百分比，不再出现「基线」字样", () => {
  const ctx = makePage();
  try {
    ctx.window.renderExposure(EXPO, {});
    const rows = rowsOf(ctx.doc);
    assert.equal(rows[0].querySelector(".vl").textContent, "30.0%");
    assert.equal(rows[1].querySelector(".vl").textContent, "10.0%");
    assert.ok(!rows[0].textContent.includes("基线"), "行内文字不显示基线");
    assert.equal(ctx.doc.querySelectorAll("#scrExpo .bs").length, 0, "旧的基线列已移除");
  } finally {
    close(ctx);
  }
});

test("悬停提示给出暴露、基线与差值方向", () => {
  const ctx = makePage();
  try {
    ctx.window.renderExposure(EXPO, {});
    const rows = rowsOf(ctx.doc);
    const t0 = rows[0].getAttribute("data-tip");
    assert.match(t0, /暴露 30\.0%/);
    assert.match(t0, /基线 5\.0%/);
    assert.match(t0, /高出 25\.0 个百分点/);
    const t1 = rows[1].getAttribute("data-tip");
    assert.match(t1, /低于 10\.0 个百分点/, "银行低于基线要说清方向");
  } finally {
    close(ctx);
  }
});

test("基线缺失时提示写明缺失，段宽只算暴露", () => {
  const ctx = makePage();
  try {
    ctx.window.renderExposure({
      ok: true, n: 10, industries: 1, hhi: 1, effective_n: 1,
      max_industry: "电子", max_weight: 20,
      rows: [{ industry: "电子", weight: 20, base_weight: null }],
    }, {});
    const row = rowsOf(ctx.doc)[0];
    assert.match(row.getAttribute("data-tip"), /基线 数据缺失/);
    assert.equal(parseFloat(row.querySelectorAll(".bar i")[1].style.width), 0, "基线段宽度为 0");
    assert.equal(parseFloat(row.querySelectorAll(".bar i")[0].style.width), 100, "只有暴露时占满整条");
  } finally {
    close(ctx);
  }
});

test("样式：堆叠条是 flex 容器，两段颜色不同", () => {
  const css = fs.readFileSync(path.join(ROOT, "web/style.css"), "utf8");
  assert.match(css, /\.scr-exp-row \.bar\{[^}]*display:flex/);
  assert.match(css, /\.scr-exp-row \.bar \.exp\{[^}]*var\(--blue\)/);
  assert.match(css, /\.scr-exp-row \.bar \.base\{[^}]*#c3cfe2/);
});
