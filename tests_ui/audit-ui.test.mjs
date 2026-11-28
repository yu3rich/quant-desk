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
  // 行情走势：图:今日行情 = 2:1，且周期切换条不再画分割线（容易和涨停线混淆）
  assert.match(css, /\.mk-chart\{flex:2 1 0/, "图表应占 2/3");
  assert.match(css, /\.mk-today\{flex:1 1 0/, "今日行情应占 1/3");
  assert.doesNotMatch(css, /\.mk-bar\{[^}]*border-bottom/, "周期条不应有分割线");
  assert.match(css, /\.mk-tq-px\{font-size:var\(--fs-6\)/, "今日行情价格应放大到 34px（字阶第 6 档）");
  assert.match(css, /\.mk-today-grid dd\{[^}]*font-size:var\(--fs-4\)/, "指标数值应放大");
  // 候选榜展开行：日分时 2/3 + 今日行情 1/3，高度与面板齐平
  assert.match(css, /\.scr-chart-main\{flex:2 1 0/, "分时图应占 2/3");
  assert.match(css, /\.scr-today\{flex:1 1 0/, "今日行情应占 1/3");
  assert.match(css, /\.scr-chart-main \.chart\.scr-mini\{height:286px/, "分时图高度应与面板齐平");
});

test("行情走势右侧今日行情：优先用 qt 快照，缺失回退 K 线", () => {
  const ctx = makePage();
  try {
    const { doc, window } = ctx;
    assert.ok(doc.getElementById("mkTodayGrid"), "走势图右侧应有今日行情容器");
    const t = window.mkTodayHtml({
      period: "intraday",
      quote: { open: 10, high: 11, low: 9.5, pre_close: 9.8, volume: 12345,
        amount: 6789, turnover: 1.2, time: "20261127150000" }
    });
    assert.match(t.grid, /今开/);
    assert.match(t.grid, /11\.00/);
    assert.match(t.grid, /1\.20<i class="unit">%<\/i>/, "单位应降级显示，不跟数字同级");
    // 行情时间只在面板顶部出现一次，底部注释改为口径说明（以前两处重复同一时间戳）
    assert.doesNotMatch(t.note, /11-27 15:00/, "底部注释不应再重复行情时间");
    assert.match(t.note, /腾讯行情快照/);
    const t2 = window.mkTodayHtml({
      period: "day", pre_close: 4.8,
      bars: [{ open: 5, high: 6, low: 4, close: 5.5, volume: 100 }]
    });
    assert.match(t2.grid, /5\.00/);
    assert.match(t2.note, /按最新 K 线/);
    const head = window.mkTodayQuoteHtml({
      period: "intraday", last: 18.3, pre_close: 18.26,
      quote: { price: 18.3, pre_close: 18.26, pct: 0.22, time: "20261127150000" }
    });
    assert.match(head, /mk-tq-px/);
    assert.match(head, /18\.30/);
    assert.match(head, /\+0\.22%/);
    assert.match(head, /截至 11-27 15:00/, "行情时间只保留在顶部一次");
    const side = window.scrTodayHtml({
      period: "intraday", last: 18.3, pre_close: 18.26,
      quote: { price: 18.3, pre_close: 18.26, open: 18.24, high: 18.5, low: 18.1,
        volume: 12345, amount: 6789, turnover: 1.2, pct: 0.22, time: "20261127150000" }
    });
    assert.match(side, /scr-today-grid/);
    assert.equal((side.match(/<dt>/g) || []).length, 8, "应有 8 项指标");
  } finally {
    close(ctx);
  }
});
