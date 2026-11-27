/**
 * 本轮改动：
 *   1. 样本上限的「0 = 全市场」提示移到输入框内部（后缀）
 *   2. 日期控件：删掉快捷按钮，全项目 8 个日期框统一样式
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const css = fs.readFileSync("web/style.css", "utf8");
const html = fs.readFileSync("web/index.html", "utf8");
const appJs = fs.readFileSync("web/app.js", "utf8");

test("样本上限提示在输入框内部，且与输入框同一容器", () => {
  assert.ok(!/<div class="hint"[^>]*>\s*0 = 全市场/.test(html),
    "不应再有独立的提示行");

  const m = html.match(/<div class="input-with-suffix"[\s\S]{0,300}?<\/div>/);
  assert.ok(m, "应有 input-with-suffix 容器");
  assert.ok(m[0].includes('id="scrLimit"'), "样本上限输入框应在容器内");
  // 用户明确要求去掉框内的「0 = 全市场」后缀，说明改由字段标签承担
  assert.ok(!m[0].includes("0 = 全市场"), "框内不应再有「0 = 全市场」后缀");

  assert.ok(css.includes(".input-with-suffix input{width:100%;padding-right:12px}"),
    "去掉后缀后不再预留 82px 空白");
  assert.ok(css.includes(".field-err.in{position:absolute"),
    "校验提示应绝对定位在框内，不占布局");
});

test("日期控件没有快捷按钮（用户要求去掉）", () => {
  // HTML/CSS/JS 三处都不能残留快捷按钮
  assert.ok(!html.includes("date-quick"), "HTML 不应有 date-quick");
  assert.ok(!html.includes("date-row"), "HTML 不应有 date-row");
  assert.ok(!css.includes(".date-quick"), "CSS 不应有 date-quick 样式");
  assert.ok(!appJs.includes("data-date-target"), "JS 不应有快捷按钮绑定");
  assert.ok(!appJs.includes("data-days"), "JS 不应有 data-days 逻辑");
});

test("全项目 8 个日期框都是原生 date input，无额外包装", () => {
  const dates = html.match(/<input[^>]*type="date"[^>]*>/g) || [];
  assert.equal(dates.length, 8, `应有 8 个日期输入框，实际 ${dates.length}`);
  // 全部裸 input，不套快捷按钮容器
  for (const tag of dates) {
    assert.ok(!/class="date-/.test(tag), "日期框不应带快捷按钮相关 class");
  }
});

test("日期框走统一控件外观 + 统一宽度，且无单独覆盖", () => {
  // 1. input[type=date] 进了「通用控件外观」列表
  // 从「通用控件外观」注释到它的选择器块结束
  const start = css.indexOf("通用控件外观");
  const block = css.slice(start, start + 900);
  // input[type=date] 出现在「:focus / :hover 等状态规则」之前的选择器列表里即可
  assert.ok(/input\[type=date\]/.test(block),
    "日期框应加入通用控件外观列表（边框/圆角/焦点环统一）");

  // 2. 日期专属规则里统一宽度 150px
  const dateRule = css.match(/input\[type=date\]\{font-variant-numeric:tabular-nums;\s*width:150px/);
  assert.ok(dateRule, "所有日期框宽度应统一为 150px");

  // 3. 日期专属规则段落里只有 150px，没有旧的 132/138
  const dateSection = css.slice(css.indexOf("日期输入：全项目统一样式"),
                                css.indexOf("日期输入：全项目统一样式") + 700);
  assert.ok(!/132px|138px/.test(dateSection), "日期规则段不应残留旧宽度");

  // 4. 关键：jnDay 曾单独覆盖外观（7px 圆角/12px 字/边框色不同），必须已清除
  const jnRule = css.match(/#jnDay\{[^}]*\}/);
  assert.ok(jnRule, "应保留 #jnDay 布局规则");
  assert.ok(!/border:|border-radius|font-size|background/.test(jnRule[0]),
    `#jnDay 不应再覆盖外观，实际：${jnRule[0]}`);
  assert.ok(jnRule[0].trim() === "#jnDay{flex:0 0 auto}",
    "jnDay 应只剩布局约束");
});

test("日历图标样式仍在（hover/focus 加深）", () => {
  assert.ok(css.includes("input[type=date]::-webkit-calendar-picker-indicator"),
    "日历图标统一样式应存在");
  assert.ok(css.includes("input[type=date]:hover::-webkit-calendar-picker-indicator"),
    "hover/focus 应加深图标");
});
