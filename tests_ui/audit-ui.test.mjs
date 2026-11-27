/**
 * 本轮 UI 一致性改动的回归：
 *  - 数字输入非法时当场标红（不再静默回退默认值）
 *  - 选股记录切换排序只重排，不重建详情/不丢分时行
 *  - 候选榜展示价来源有明确标注
 */
import test from "node:test";
import assert from "node:assert/strict";
import { makePage, close, readSrc } from "./helpers.mjs";

const css = readSrc("web/style.css");

test("数字输入非法：标红 + 行内说明，改回合法值后恢复", () => {
  const ctx = makePage();
  try {
    const { doc, window } = ctx;
    const el = doc.getElementById("scrTopN");
    assert.ok(el, "选股页应有 TopN 输入");
    const rule = window.eval("NUM_RULES.scrTopN");
    el.value = "0";
    assert.equal(window.checkNum(el, rule), false);
    assert.ok(el.classList.contains("bad"), "非法值应标红");
    const tip = el.parentElement.querySelector(".field-err");
    assert.ok(tip && /1~100/.test(tip.textContent), "应说明合法范围");

    el.value = "20";
    assert.equal(window.checkNum(el, rule), true);
    assert.ok(!el.classList.contains("bad"), "合法值应取消标红");
    assert.equal(el.parentElement.querySelector(".field-err"), null, "错误提示应移除");
  } finally {
    close(ctx);
  }
});

test("选股记录切换排序：只重排行，分时行跟着主行走", () => {
  const ctx = makePage();
  try {
    const { doc, window } = ctx;
    const det = doc.getElementById("pkDetail");
    det.innerHTML = '<div class="pk-sort">' +
      '<button class="pk-sbtn" data-sort="rank"></button>' +
      '<button class="pk-sbtn" data-sort="chg"></button></div>' +
      '<div class="pk-bars"><span class="pk-bw" data-code="A"></span>' +
      '<span class="pk-bw" data-code="B"></span></div>' +
      '<div class="pk-tbl"><table><tbody>' +
      '<tr data-code="A"></tr><tr class="pk-cr"></tr><tr data-code="B"></tr>' +
      "</tbody></table></div>";
    window.eval("PK.sort = \"chg\"");
    window.pkResort({ picks: [{ code: "A", chg: 1, rank: 1 }, { code: "B", chg: 5, rank: 2 }] });

    const order = Array.from(det.querySelectorAll("tbody > tr"))
      .map((tr) => tr.dataset.code || "cr");
    assert.deepEqual(order, ["B", "A", "cr"], "按涨跌降序，且 A 的分时行跟着 A");
    const bars = Array.from(det.querySelectorAll(".pk-bw")).map((el) => el.dataset.code);
    assert.deepEqual(bars, ["B", "A"], "柱状图同步重排");
    assert.ok(det.querySelector('[data-sort="chg"]').classList.contains("on"));
    assert.ok(!det.querySelector('[data-sort="rank"]').classList.contains("on"));
  } finally {
    close(ctx);
  }
});

test("展示价来源：本地快照 / 后复权 / 混合都有明确文案", () => {
  const ctx = makePage();
  try {
    const { doc, window } = ctx;
    const el = doc.getElementById("scrPriceTag");
    assert.ok(el, "候选榜标题旁应有价格来源位");
    window.scrPriceNote({ price_source: "spot", price_at: "2026-11-27 14:58:00" });
    assert.match(el.textContent, /本地快照 14:58/);
    window.scrPriceNote({ price_source: "hfq" });
    assert.match(el.textContent, /后复权/);
    window.scrPriceNote({ price_source: "mixed", price_at: "2026-11-27 14:58:00" });
    assert.match(el.textContent, /部分为后复权/);
  } finally {
    close(ctx);
  }
});

test("样式锁：手机端冻结候选榜前两列，错误 pill 不再与上涨同色", () => {
  assert.match(css, /#scrTable tr:not\(\.scr-detail-row\) > \*:nth-child\(1\)\{position:sticky/,
    "首列应冻结");
  assert.match(css, /#scrTable tr:not\(\.scr-detail-row\) > \*:nth-child\(2\)\{position:sticky/,
    "代码列应冻结");
  assert.doesNotMatch(css, /\.pill\.bad\{background:#fdeceb/, "错误 pill 不能再用上涨红");
});
