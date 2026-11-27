/**
 * 候选榜展开行的排版回归。
 *
 * 最终版：整体左右两栏——左栏「因子明细 5 列（基本信息排第一）+ 当日分时」上下叠，
 * 右栏「空头辩护」独占整个右侧、从顶到底；底部操作行。
 * 三块都直接铺在灰底上，没有白底圆角容器；辩护不设滚动、理由全部自适应展开。
 * 头部标识条（代码 / 名称 / 打分 chip）不显示。
 * 这里锁住设计意图，不断言实现细节（class 名可以换，结构不能退回去）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { makePage, close, readSrc } from "./helpers.mjs";

const css = readSrc("web/style.css");
const appjs = readSrc("web/app.js");

const BEARS = [
  { point: "质量明显偏弱", trigger: "base_score<0", watch: "关注行业排名变化" },
  { point: "5 日动能走平", trigger: "5 日涨幅回落", watch: "20 日动能是否走平" },
  { point: "偏离均线过多", trigger: "ma_dev 与 dd20", watch: "是否均值回归" },
  { point: "同业入选过度集中", trigger: "银行占候选多数", watch: "板块拥挤度" },
  { point: "估值分位偏高", trigger: "pb 分位 >0.9", watch: "同行 PB 中位数" },
  { point: "流动性下滑", trigger: "amount_ratio 走低", watch: "5 日成交额" },
];
const CAND = {
  code: "600016", name: "民生银行", industry: "银行", market: "沪市主板",
  base_score: -0.166, ind_rank: 0.01, ind_n: 42, date: "2026-11-26",
  verdict: "keep", llm_reason: "否决点未触发，暂保留。", bear_case: BEARS,
};

/** scrDetailHtml 返回 <tr>，必须放进 table/tbody 才会被正确解析 */
function detailOf(ctx, x) {
  const table = ctx.doc.createElement("table");
  table.innerHTML = "<tbody>" + ctx.window.scrDetailHtml(x) + "</tbody>";
  return table;
}

test("展开行：左栏＝因子 + 分时，右栏＝空头辩护独占整个右侧，无头部标识条", () => {
  const ctx = makePage();
  try {
    const t = detailOf(ctx, CAND);
    const td = t.querySelector("tr.scr-detail-row > td");
    assert.ok(td, "展开行仍是表格里的一行");
    assert.equal(td.getAttribute("colspan"), "11", "跨满候选榜 11 列");
    assert.equal(t.querySelector("tr.scr-detail-row").dataset.detail, "600016",
      "展开行带 code，重绘时才能恢复展开态");

    assert.equal(t.querySelectorAll(".scr-detail-head").length, 0, "头部标识条已移除");
    assert.equal(t.querySelectorAll(".scr-dh-chips").length, 0, "速览 chip 已移除");

    const top = Array.from(t.querySelectorAll(".scr-detail > *")).map((e) => e.className);
    assert.deepEqual(top, ["scr-detail-cols", "scr-detail-foot"],
      "主体就是左右两栏 + 底部操作行");

    const cols = t.querySelectorAll(".scr-detail-cols > *");
    assert.equal(cols.length, 2, "主体是左右两栏");
    assert.equal(cols[0].className, "scr-detail-left", "左栏包住因子与分时");
    assert.equal(cols[1].className, "scr-bear-plain", "右栏＝空头辩护，与左栏平级");
    assert.equal(cols[1].parentElement.className, "scr-detail-cols",
      "辩护不在左栏里，独占整个右侧（从顶到底）");

    const left = Array.from(cols[0].children).map((e) => e.className);
    assert.deepEqual(left, ["scr-line-factor", "scr-line-chart"],
      "左栏顺序：因子明细在上、当日分时在下");
    assert.ok(t.querySelector(".scr-line-factor .scr-fac-groups"), "因子明细在左栏");
    assert.ok(t.querySelector(".scr-line-chart #scrCh_600016"), "分时图在因子下面");
    assert.equal(t.querySelector(".scr-line-chart h4").textContent, "日分时",
      "标题精简为「日分时」");
    assert.ok(t.querySelector(".scr-detail-foot"), "底部有操作行");

    // 三块都不套白底圆角容器
    [t.querySelector(".scr-line-factor"), t.querySelector(".scr-line-chart"), cols[1]].forEach((e) => {
      assert.equal(e.classList.contains("scr-panel"), false, "不套白卡");
    });
    assert.equal(t.querySelectorAll("section.scr-panel").length, 0);
    assert.doesNotMatch(appjs, /section class="scr-panel/, "JS 里不再生成白卡");

    // 更早就废掉的东西不能回来
    assert.equal(t.querySelectorAll(".scr-detail-grid").length, 0, "不再有旧的文字墙");
    assert.equal(t.querySelectorAll(".scr-detail-chart").length, 0, "不再有通栏扁图");
    assert.equal(t.querySelectorAll(".bear-inline").length, 0, "辩护不再用裸列表");
    assert.equal(t.querySelectorAll(".scr-fac-flat").length, 0, "因子不再压成横条");
  } finally {
    close(ctx);
  }
});

test("因子明细＝5 列分组：基本信息排第一，后面 4 组共 18 项", () => {
  const ctx = makePage();
  try {
    const t = detailOf(ctx, CAND);
    const groups = t.querySelectorAll(".scr-fac-groups > .scr-fac-group");
    assert.equal(groups.length, 5, "5 个分组列");
    assert.deepEqual(
      Array.from(groups).map((g) => g.querySelector("h5").textContent),
      ["基本信息", "动量与趋势", "波动与位置", "量能", "行业相对"],
      "基本信息排第一列"
    );
    const facItems = Array.from(groups).slice(1)
      .flatMap((g) => Array.from(g.querySelectorAll(".scr-fac-item")));
    assert.equal(facItems.length, 18, "18 个因子一个不少");
    const labels = facItems.map((i) => i.querySelector("span").textContent);
    assert.equal(new Set(labels).size, labels.length, "因子标签不重复");
    facItems.forEach((i) => assert.ok(i.querySelector("b"), "每项都有数值"));
    assert.equal(groups[0].querySelectorAll(".scr-fac-item").length, 5,
      "基本信息是第一列，和后面 4 列同样式");
    assert.match(groups[0].textContent, /板块|行业|基准分|数据日/, "基本信息内容齐全");
  } finally {
    close(ctx);
  }
});

test("分时图容器与报价位 id 保留（scrChartLoad 依赖），操作按钮不丢", () => {
  const ctx = makePage();
  try {
    const t = detailOf(ctx, CAND);
    assert.ok(t.querySelector("#scrCh_600016.chart.scr-mini"), "分时图容器 id 不变");
    assert.ok(t.querySelector("#scrChQ_600016"), "报价位 id 不变");
    const acts = Array.from(t.querySelectorAll(".scr-detail-foot button")).map((b) => b.dataset.act);
    assert.deepEqual(acts, ["chart", "backtest", "pooladd"]);
    assert.equal(t.querySelector('[data-act="pooladd"]').dataset.name, "民生银行");
  } finally {
    close(ctx);
  }
});

test("空头辩护全部展开：不滚动、无滚动提示，逐条列出并带裁决；无理由时给空态", () => {
  const ctx = makePage();
  try {
    const withBear = detailOf(ctx, CAND);
    assert.equal(withBear.querySelectorAll(".scr-bear-list li").length, BEARS.length,
      "6 条理由全部列出，不被裁掉");
    assert.equal(withBear.querySelectorAll(".scr-bear-hint").length, 0,
      "不再有「列表内可滚动」提示");
    assert.match(withBear.querySelector(".scr-bear-verdict").textContent, /保留/,
      "keep 显示保留");
    assert.ok(withBear.querySelector(".scr-bear-verdict").classList.contains("keep"));
    assert.match(withBear.querySelector(".scr-bear-judge").textContent, /裁决/);
    assert.equal(withBear.querySelectorAll(".scr-bear-meta i").length, BEARS.length * 2,
      "触发 / 观察各自带标签");

    const dropped = detailOf(ctx, { ...CAND, verdict: "drop" });
    assert.ok(dropped.querySelector(".scr-bear-verdict").classList.contains("drop"));
    assert.match(dropped.querySelector(".scr-bear-verdict").textContent, /否决/);

    const noBear = detailOf(ctx, { ...CAND, bear_case: [], llm_reason: "" });
    assert.ok(noBear.querySelector(".scr-bear-plain.is-empty"), "无理由时是空态");
    assert.equal(noBear.querySelectorAll(".scr-bear-list li").length, 0);
    assert.equal(noBear.querySelectorAll(".scr-bear-hint").length, 0);
  } finally {
    close(ctx);
  }
});

test("样式锁：左对齐、5 列自适应、无白卡、辩护不限高", () => {
  // 展开 td 是 tr 里唯一单元格，会命中 .pool-table td:first-child 的
  // text-align:center；不重置会让分组标题和辩护正文全部居中。
  assert.match(css, /\.scr-detail\{[^}]*text-align:left/, "必须重置继承来的居中");
  assert.match(css, /\.scr-fac-groups\{[^}]*repeat\(auto-fit,minmax\(min\(120px,100%\)/,
    "因子 5 列靠 auto-fit，宽屏一行、窄屏自动折行（左栏较窄，min 不能取 190px）");
  assert.match(css, /\.scr-detail-cols\{[^}]*display:flex[^}]*flex-wrap:wrap/,
    "两栏用 flex-wrap 自适应，右栏辩护与左栏顶部对齐、独占整个右侧");
  assert.match(css, /\.scr-detail-left\{[^}]*flex:4 1 400px/,
    "左栏吃到约 2/3 宽，才放得下 5 列因子");
  assert.match(css, /\.scr-bear-plain\{[^}]*flex:1 1 330px/,
    "右栏辩护是独立的一栏、宽度已加宽，不是左栏里的第二块");
  assert.match(css, /\.chart\.scr-mini\{height:1[0-8]\dpx/, "分时图高度收窄（旧版 210px）");

  // 三块都直接铺在灰底上：没有白卡规则，也没有白底 / 圆角 / 投影
  assert.doesNotMatch(css, /\.scr-panel\{/, "白卡样式已整体删除");
  [".scr-line-factor", ".scr-line-chart", ".scr-bear-plain", ".scr-detail"].forEach((sel) => {
    const rule = css.match(new RegExp(sel.replace(/\./g, "\\.") + "\\{[^}]*\\}"));
    assert.ok(rule, "有 " + sel + " 规则");
    assert.doesNotMatch(rule[0], /background:#fff|border-radius|box-shadow/,
      sel + " 不是白底圆角容器");
  });

  // 辩护全部展开：列表不设最大高度 / 滚动
  const listRule = css.match(/\.scr-bear-list\{[^}]*\}/);
  assert.ok(listRule, "有 .scr-bear-list 规则");
  assert.doesNotMatch(listRule[0], /max-height|overflow-y|overflow:/,
    "辩护列表不设滚动，理由全部展开");
  assert.doesNotMatch(css, /\.scr-bear-hint\{/, "滚动提示样式已删除");

  assert.doesNotMatch(css, /\.scr-detail-grid\{/, "旧的文字墙规则已清除");
  assert.doesNotMatch(css, /\.scr-detail-head\{/, "头部标识条样式已清除");
  assert.doesNotMatch(css, /\.scr-fac-flat\{/, "因子横条样式已清除");
});
