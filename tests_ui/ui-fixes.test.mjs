/**
 * UI 修复回归测试：
 *  1. 回测总览区间日期框：两个 150px 固定宽度会在 274~322px 左列溢出，
 *     必须改成 .range2 内等分弹性（布局靠浏览器实测，这里锁住规则不被改回去）。
 *  2. 上市名单 point-in-time：长标签不能沿用数据链的 44px 固定列。
 *  3. 窄屏（820px）下侧栏与选股工作台表格不得横向溢出。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readSrc } from "./helpers.mjs";

const css = readSrc("web/style.css");
const js = readSrc("web/app.js");
const html = readSrc("web/index.html");

test("区间日期框在窄列里等分弹性，不再固定 150px", () => {
  assert.match(css, /\.range2\{[^}]*min-width:0[^}]*width:100%/,
    "区间容器要能随父级收缩");
  assert.match(css, /\.range2 input\[type=date\]\{width:auto;flex:1 1 0;min-width:0/,
    "区间内日期框应等分弹性，不能沿用全局 150px 固定宽");
});

test("上市名单 KV 用自适应标签列，不复用 44px 的 pipe-kv", () => {
  assert.match(html, /<div class="scr-kv hub-kv" id="hubListKv">/,
    "上市名单容器应带 hub-kv 类");
  assert.match(css, /\.hub-kv\{grid-template-columns:minmax\(76px,max-content\) minmax\(0,1fr\)/,
    "标签列按内容自适应，值列 minmax(0,1fr) 不被撑破");
  assert.match(css, /\.hub-kv \.k\{white-space:normal/,
    "长标签要允许折行，不能 nowrap 压进值列");
});

test("窄屏导航改侧滑抽屉、候选表收紧列宽", () => {
  const mid = css.slice(css.indexOf("@media(max-width:1180px)"),
                        css.indexOf("@media(max-width:980px)"));
  const narrow = css.slice(css.indexOf("@media(max-width:860px)"),
                           css.indexOf("@media(max-width:400px)"));
  // 15 个页签平铺换行会占掉近 1/3 首屏，所以改成汉堡 + 抽屉（细节见 mobile-nav.test.mjs）
  assert.match(narrow, /\.nav\{position:fixed;top:0;left:0;bottom:0/,
    "导航应改为固定定位的抽屉");
  assert.match(narrow, /transform:translateX\(-104%\)/, "关闭时移出视口");
  assert.match(narrow, /\.nav-toggle\{display:inline-flex\}/, "窄屏才显示汉堡按钮");
  assert.match(mid, /#scrTable \.reason\{min-width:104px/,
    "窄屏解读列要收窄，让候选表装进容器");
  assert.doesNotMatch(narrow, /\.nav\{[^}]*overflow-x:auto/,
    "导航不能靠隐藏滚动条的横滚");
});

test("加载提示仍是原来的整屏遮罩，没有骨架层残留", () => {
  assert.match(css, /#loading\{position:fixed;inset:0;background:rgba\(244,246,249,\.82\)/,
    "加载态应恢复成整屏磨砂遮罩");
  assert.doesNotMatch(js, /skel-busy|SK_N|SK_QUIET/,
    "骨架预览逻辑应已完全回退");
});
