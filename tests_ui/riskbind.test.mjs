/**
 * 实盘复盘：风险预算走势与计划-执行偏差的渲染。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makePage, close } from "./helpers.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const BOARD = {
  risk: {
    items: [
      { name: "潍柴动力", mv_share: 26.7, atr_pct: 5.22, risk_pct: 2.79 },
      { name: "贵州茅台", mv_share: 22.4, atr_pct: 2.34, risk_pct: 1.05 },
    ],
    risk_pct: 1.0, atr_mult: 2.0, total: 3.84, usage: 384,
    over: 1,
    below_ref: [{ name: "潍柴动力", cost: 18.0, price: 16.2, ref: 17.1, below_pct: -5.3 }],
  },
  risk_trend: [
    { date: "2026-11-21", usage: 210, total: 2.1, n_pos: 2 },
    { date: "2026-11-24", usage: 355, total: 3.55, n_pos: 2 },
    { date: "2026-11-25", usage: 384, total: 3.84, n_pos: 2 },
  ],
  plan_dev: {
    ok: true, empty: false, since: "2026-11-20",
    planned_buy: 4, planned_sell: 2, done_buy: 3, done_sell: 1,
    extra_ratio: 46.5, extra_amt: 12000, total_amt: 25800,
  },
};

test("风险预算：KPI、走势条、超预算标红", () => {
  const ctx = makePage();
  try {
    ctx.window.rtRiskPaint(BOARD);
    const box = ctx.doc.getElementById("rtRisk");
    assert.equal(box.hidden, false, "有持仓风险数据时显示");
    const kpi = box.querySelectorAll(".rt-risk-kpi span");
    assert.equal(kpi.length, 5, "五个 KPI：使用率/合计/预算/超预算/跌破");
    assert.match(kpi[0].textContent, /384%/, "预算使用率 384%");
    assert.ok(kpi[3].classList.contains("bad"), "超预算只数标红");
    assert.ok(kpi[4].classList.contains("bad"), "跌破参考位标红");

    const bars = box.querySelectorAll(".rt-bars i");
    assert.equal(bars.length, 3, "每期一根走势条");
    assert.ok(bars[2].classList.contains("over"), "使用率 384% 的条形标红");
    assert.ok(bars[0].classList.contains("over"), "210% 同样超过 100%，也标红");
    assert.match(bars[2].getAttribute("title"), /使用率 384%/);
  } finally {
    close(ctx);
  }
});

test("计划-执行：显示计划外占比与执行数量", () => {
  const ctx = makePage();
  try {
    ctx.window.rtRiskPaint(BOARD);
    const plan = ctx.doc.querySelector("#rtRisk .rt-plan");
    assert.match(plan.textContent, /计划日 2026-11-20/);
    assert.match(plan.textContent, /计划外成交/);
    assert.match(plan.textContent, /47%/, "占比按整数显示");
    assert.match(plan.textContent, /计划买卖 4\/2，实际 3\/1/);
    assert.ok(plan.querySelector(".bad"), "占比超过 40% 标红");
  } finally {
    close(ctx);
  }
});

test("没有计划时提示先去目标持仓生成", () => {
  const ctx = makePage();
  try {
    ctx.window.rtRiskPaint({ risk: BOARD.risk, risk_trend: [], plan_dev: {} });
    const plan = ctx.doc.querySelector("#rtRisk .rt-plan");
    assert.match(plan.textContent, /还没有目标持仓计划/);
  } finally {
    close(ctx);
  }
});

test("空仓时整块隐藏，不占位", () => {
  const ctx = makePage();
  try {
    ctx.window.rtRiskPaint({ risk: { items: [] }, risk_trend: [], plan_dev: {} });
    assert.equal(ctx.doc.getElementById("rtRisk").hidden, true);
  } finally {
    close(ctx);
  }
});

test("样式：条形是块级且超预算用红色", () => {
  const css = fs.readFileSync(path.join(ROOT, "web/style.css"), "utf8");
  assert.match(css, /\.rt-bars i\{[^}]*display:block/);
  assert.match(css, /\.rt-bars i\.over\{[^}]*var\(--up\)/);
});
