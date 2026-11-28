/**
 * 设计系统回归：P0 的四阶中性色 / 六档字阶 / 单层容器 / 品牌蓝，
 * 以及 P1 的数字排印、首屏英雄位、8px 网格。
 *
 * 这些都是「全站一致性」约束，散落在各功能测试里容易漏，单独锁一份。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { makePage, close, readSrc } from "./helpers.mjs";

const css = readSrc("web/style.css");
const js = readSrc("web/app.js");
const svg = readSrc("web/icon.svg");
const rootEnd = css.indexOf("\n}") + 2;
const root = css.slice(0, rootEnd);
const body = css.slice(rootEnd);

test("P0：四阶中性色 + 语义色集中在 :root，正文不再散落近白灰", () => {
  for (const t of ["--canvas:#f2f4f7", "--surface:#fff", "--inset:#f8f9fb",
                   "--stroke:#dde2ea", "--blue:#1c5fd8", "--up:#d3403f",
                   "--down:#12996a", "--amber:#b8790c"]) {
    assert.ok(root.includes(t), `根令牌缺失：${t}`);
  }
  // 旧变量名保留兼容，但值必须指向新四阶，不能再各自写死近似灰
  assert.match(root, /--bg:var\(--canvas\)/);
  assert.match(root, /--card:var\(--surface\)/);
  assert.match(root, /--line:var\(--stroke\)/);
  assert.match(root, /--line2:var\(--hairline\)/);
  const near = [...body.matchAll(/#[0-9a-fA-F]{6}\b/g)].filter((m) => {
    const h = m[0];
    const r = parseInt(h.slice(1, 3), 16), g = parseInt(h.slice(3, 5), 16), b = parseInt(h.slice(5, 7), 16);
    return Math.min(r, g, b) >= 225 && Math.max(r, g, b) - Math.min(r, g, b) <= 35;
  });
  assert.deepEqual(near.map((m) => m[0]), [], "样式表正文不应再出现手调的近白灰");
});

test("P0：字阶收敛到 6 档；16px 只保留给手机输入框防缩放", () => {
  for (const [k, v] of [["--fs-1", "11px"], ["--fs-2", "12px"], ["--fs-3", "13px"],
                        ["--fs-4", "15px"], ["--fs-5", "20px"], ["--fs-6", "34px"]]) {
    assert.ok(root.includes(`${k}:${v}`), `字阶缺失：${k}`);
  }
  const sizes = [...body.matchAll(/font-size\s*:\s*([^;}]+)/g)].map((m) => m[1].trim());
  const stray = sizes.filter((v) => !/^var\(--fs-[1-6]\)$/.test(v) && v !== "inherit" && v !== "16px");
  assert.deepEqual(stray, [], "不应再有手调字号");
  assert.equal(sizes.filter((v) => v === "16px").length, 2, "16px 只允许出现在两处触控规则");
  const strayJs = [...js.matchAll(/font-size:(\d+(?:\.\d+)?)px/g)];
  assert.deepEqual(strayJs.map((m) => m[1]), [], "JS 内联字号也要走统一字阶");
});

test("P0：品牌改实心主蓝，红色只留给上涨", () => {
  const brand = css.match(/\.brand \.logo\{[^}]*\}/)[0];
  assert.doesNotMatch(brand, /linear-gradient|#ffd9d6|rgba\(255,80,75/);
  assert.match(brand, /background:var\(--blue\)/);
  assert.match(css, /\.brand \.logo img\{[^}]*filter:brightness\(0\) invert\(1\)/,
    "实心蓝底上图标要反白");
  assert.match(svg, /fill="#1c5fd8"/, "favicon 也应改主蓝");
});

test("P0：今日行情面板与子卡降为单层 + 细线", () => {
  const inner = css.match(/\.mk-today-inner\{[^}]*\}/)[0];
  assert.doesNotMatch(inner, /background:|border-radius/);
  assert.match(inner, /border-left:1px solid var\(--stroke\)/);
  const cell = css.match(/\.mk-today-grid>div\{[^}]*\}/)[0];
  assert.doesNotMatch(cell, /background:|border-radius/);
  assert.match(cell, /border-top:1px solid var\(--hairline\)/);
  const sub = css.match(/\.subcard\{[^}]*\}/)[0];
  assert.doesNotMatch(sub, /background:|border-radius/);
  assert.match(sub, /border-top:1px solid var\(--stroke\)/);
});

test("P0：行情时间只出现一次，底部注释改口径说明", () => {
  assert.match(js, /"腾讯行情快照 · 盘口口径"/);
  assert.doesNotMatch(js, /"行情时间 " \+ mkQTime/, "旧注释里的时间戳已移除");
  assert.match(js, /"截至 " \+ mkQTime/, "时间只保留在面板顶部");
});

test("P1：首屏结论卡是唯一英雄位，另两卡降阴影", () => {
  const hero = css.match(/\.scr-topline>\.scr-dash\{[^}]*\}/)[0];
  assert.doesNotMatch(hero, /border-left/, "结论卡不要左侧蓝色色条，三卡边框保持对齐");
  assert.match(hero, /linear-gradient\(180deg,var\(--blue-soft\)/, "顶部淡蓝渐变保留");
  assert.match(hero, /box-shadow:0 4px 16px/, "轻投影保留");
  assert.match(css, /\.scr-topline>\.card:not\(\.scr-dash\)\{box-shadow:none\}/);
});

test("P1：数字排印（等宽/齐线/千分位/真减号/单位降级）", () => {
  assert.match(css, /\.unit\{[^}]*color:var\(--ink3\)/);
  assert.match(css, /font-variant-numeric:tabular-nums lining-nums/);
  assert.match(css, /font-feature-settings:"tnum" 1,"lnum" 1/);
  assert.match(css, /th\.num,td\.num,\.num\{text-align:right\}/);
  const ctx = makePage();
  try {
    const w = ctx.window;
    assert.equal(w.numText(-1234567.891, 2), "\u22121,234,567.89", "千分位 + 真减号");
    assert.equal(w.signed(-1.2), "\u22121.20%");
    assert.match(w.fmtPct(-1.2, { cls: true }), /\u22121\.20%/);
    assert.match(w.unitTag("%"), /class="unit"/);
    assert.match(w.mkTodayHtml({
      period: "intraday", quote: { open: 10, high: 11, low: 9.5, pre_close: 9.8,
        volume: 12345, amount: 6789, turnover: 1.2, time: "20261127150000" }
    }).grid, /<i class="unit">万手<\/i>/);
  } finally {
    close(ctx);
  }
});

test("P1：padding / margin / gap 收敛到 4px 倍数", () => {
  const props = "(padding|margin|gap|padding-top|padding-right|padding-bottom|padding-left|margin-top|margin-right|margin-bottom|margin-left|row-gap|column-gap)";
  const re = new RegExp(props + "\\s*:\\s*([^;{}]*)", "g");
  const bad = [];
  for (const m of body.matchAll(re)) {
    const val = m[2];
    if (/[a-zA-Z%(]/.test(val.replace(/px/g, ""))) continue;  // calc()/env()/em/%/auto 原样保留
    for (const x of val.matchAll(/(?<![\d.])(\d+(?:\.\d+)?)px/g)) {
      const n = Number(x[1]);
      if (n > 2 && n % 4) bad.push(`${m[1]}:${val.trim()}`);
    }
  }
  assert.deepEqual(bad, [], "留白节奏应落在 4px 基准网格上");
});
