/**
 * 复盘日记：Markdown 预览渲染。
 * 只测纯函数，不启动 init（避免被无关接口拖慢）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { makePage, close } from "./helpers.mjs";

test("mdRender 把日记的四节标题、列表、加粗排出来", () => {
  const ctx = makePage({ api: () => ({ ok: true }) });
  const { window } = ctx;
  const html = window.mdRender(
    "## 今日事实\n- 总资产 **6884.91** 元\n- 持仓 1 只\n\n" +
    "## 过程复盘\n- 已平仓 24 笔，胜率 50%\n\n> 本地规则模板\n");

  assert.match(html, /<h3>今日事实<\/h3>/);
  assert.match(html, /<h3>过程复盘<\/h3>/);
  assert.match(html, /<ul><li>总资产 <strong>6884\.91<\/strong> 元<\/li>/);
  assert.match(html, /<li>已平仓 24 笔，胜率 50%<\/li>/);
  assert.match(html, /<blockquote>本地规则模板<\/blockquote>/);
  close(ctx);
});

test("mdRender 先转义 HTML，正文里的标签不会被执行", () => {
  const ctx = makePage({ api: () => ({ ok: true }) });
  const html = ctx.window.mdRender('<img src=x onerror=alert(1)>\n\n**安全**');
  assert.ok(!html.includes("<img"), "不应输出原始 img 标签");
  assert.ok(html.includes("&lt;img"), "应转义成文本");
  assert.match(html, /<strong>安全<\/strong>/);
  close(ctx);
});

test("空正文渲染为空字符串（占位提示由 jnPaint 负责）", () => {
  const ctx = makePage({ api: () => ({ ok: true }) });
  const html = ctx.window.mdRender("");
  assert.equal(html, "");
  close(ctx);
});
