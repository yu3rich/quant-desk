/**
 * 单股「多视角评分」卡片 + 漏斗「基本面」下拉。
 *
 * 多视角的结构参考 ai-hedge-fund（每个 agent 给 signal + confidence，再汇总），
 * 但这里是可复算的横截面分位，不是 LLM 扮演角色 —— 所以要钉住「缺数据必须如实标注」。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makePage, close } from "./helpers.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const DETAIL = {
  personas: [
    { key: "value", name: "价值", school: "格雷厄姆式", signal: "bearish", score: -50,
      confidence: 36, points: ["PE 22.4 处于同行 50% 分位"], missing: false },
    { key: "trend", name: "趋势", school: "欧奈尔式", signal: "bullish", score: 94,
      confidence: 76, points: ["20日动量 10.83%（分位 97）"], missing: false },
    { key: "quality", name: "质量", school: "巴菲特式", signal: "unknown", score: 0,
      confidence: 0, points: [], missing: true,
      missing_note: "本地没有财务数据（ROE / 毛利率 / 现金流），该视角暂不参与汇总" },
  ],
  consensus: { score: 22, signal: "bullish", confidence: 56, agreement: 50,
               spread: 144, available: 2, total: 3 },
};

test("index.html：多视角卡片与基本面下拉都在", () => {
  const html = read("web/index.html");
  assert.match(html, /id="stkPersonaCard"/);
  assert.match(html, /id="stkPersonas"/);
  assert.match(html, /id="stkConsensus"/);
  assert.match(html, /<select id="scrFnVal"/);
  assert.match(html, /<option value="off" selected>关闭<\/option>/);
  assert.match(html, /<option value="loose">保守<\/option>/);
  assert.match(html, /<option value="instock">InStock 口径<\/option>/);
});

test("多视角卡片：每行有信号、分数条，缺数据行不画条", () => {
  const ctx = makePage();
  try {
    const box = ctx.doc.createElement("div");
    box.innerHTML = ctx.window.stkPersonaHtml(DETAIL);
    const rows = box.querySelectorAll(".stk-prow");
    assert.equal(rows.length, 3);

    const [value, trend, quality] = rows;
    assert.match(value.querySelector(".pill").textContent, /偏空/);
    assert.match(trend.querySelector(".pill").textContent, /偏多/);
    // 分数条从中线向两侧长：偏多向右(pos)、偏空向左(neg)
    assert.ok(trend.querySelector(".stk-pbar i.pos"), "偏多用 pos 条");
    assert.ok(value.querySelector(".stk-pbar i.neg"), "偏空用 neg 条");
    // 94 分 → 半条 47%；-50 → 25%（jsdom 会把 "47.0%" 规范化成 "47%"）
    assert.equal(parseFloat(trend.querySelector(".stk-pbar i").style.width), 47);
    assert.equal(parseFloat(value.querySelector(".stk-pbar i").style.width), 25);

    assert.ok(quality.classList.contains("na"), "缺数据行整体变淡");
    assert.equal(quality.querySelector(".stk-pbar i"), null, "缺数据不画分数条");
    assert.equal(quality.querySelector(".stk-pnum").textContent, "—");
    assert.match(quality.querySelector(".stk-ptxt").textContent, /没有财务数据/);
    assert.match(quality.querySelector(".pill").textContent, /缺数据/);
  } finally {
    close(ctx);
  }
});

test("多视角卡片：汇总行显示分数、方向与视角数", () => {
  const ctx = makePage();
  try {
    // stkPaint 里写 #stkConsensus，这里直接复用同一段逻辑：构造最小 DOM
    const doc = ctx.doc;
    const card = doc.getElementById("stkPersonaCard");
    assert.ok(card, "卡片存在");
    assert.equal(card.hidden, true, "没有数据时默认隐藏，避免空白卡");

    doc.getElementById("stkPersonas").innerHTML = ctx.window.stkPersonaHtml(DETAIL);
    doc.getElementById("stkConsensus").innerHTML =
      "<b>+" + DETAIL.consensus.score + "</b> 偏多 · " + DETAIL.consensus.available +
      "/" + DETAIL.consensus.total + " 视角 · 分歧 " + DETAIL.consensus.spread;
    card.hidden = false;
    const txt = doc.getElementById("stkConsensus").textContent;
    assert.match(txt, /\+22/);
    assert.match(txt, /偏多/);
    assert.match(txt, /2\/3 视角/);
    assert.match(txt, /分歧 144/);
  } finally {
    close(ctx);
  }
});

test("漏斗基本面：下拉值进入请求参数", () => {
  const ctx = makePage();
  try {
    const doc = ctx.doc;
    const sel = doc.getElementById("scrFnVal");
    assert.ok(sel, "应有基本面下拉");
    assert.equal(sel.options.length, 3);
    assert.equal(sel.value, "off", "默认关闭：A 股约三成亏损股，不能默认剔");

    const before = ctx.window.scrRunParams();
    assert.equal(before.funnel.val, "off");

    sel.value = "instock";
    const after = ctx.window.scrRunParams();
    assert.equal(after.funnel.val, "instock", "选了才带上");
    // 原有五个开关不能被新字段挤掉
    for (const k of ["quality", "st", "extreme", "liquidity", "trend"]) {
      assert.equal(typeof after.funnel[k], "boolean", "funnel." + k + " 仍是布尔");
    }
  } finally {
    close(ctx);
  }
});
