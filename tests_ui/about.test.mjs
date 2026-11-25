/**
 * 关于页（使用说明）：目录、搜索、高亮；术语表新增分类。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { makePage, close } from "./helpers.mjs";

test("关于页渲染完整目录与章节，标题与 id 一一对应", () => {
  const ctx = makePage();
  try {
    ctx.window.aboutRender();
    const links = Array.from(ctx.doc.querySelectorAll("#abToc a[data-sec]"));
    const secs = Array.from(ctx.doc.querySelectorAll("#abDoc .ab-sec"));
    assert.ok(links.length >= 10, "至少 10 个章节，实际 " + links.length);
    assert.equal(links.length, secs.length, "目录项与章节数量一致");
    links.forEach((a, i) => {
      assert.equal(secs[i].id, "ab-" + a.dataset.sec, "第 " + (i + 1) + " 节目录与 id 对应");
      assert.ok(a.textContent.trim().length > 0, "目录文字非空");
      assert.ok(secs[i].querySelector("h3").textContent.trim().length > 0, "章节标题非空");
    });
    assert.match(ctx.doc.getElementById("abHit").textContent, /共 \d+ 节/);
  } finally {
    close(ctx);
  }
});

test("搜索只保留命中章节并高亮关键字", () => {
  const ctx = makePage();
  try {
    ctx.window.aboutRender();
    const all = ctx.doc.querySelectorAll("#abDoc .ab-sec").length;
    ctx.window.aboutRender("Kelly");
    const hit = ctx.doc.querySelectorAll("#abDoc .ab-sec").length;
    assert.ok(hit > 0 && hit < all, "命中章节应少于全部：" + hit + "/" + all);
    const marks = Array.from(ctx.doc.querySelectorAll("#abDoc mark"));
    assert.ok(marks.length > 0, "命中词要加 mark");
    assert.ok(marks.every((m) => /kelly/i.test(m.textContent)), "mark 内容就是关键字");
    assert.match(ctx.doc.getElementById("abHit").textContent, /命中 \d+ 节/);
    assert.match(ctx.doc.getElementById("abDoc").textContent, /分数 Kelly/);
  } finally {
    close(ctx);
  }
});

test("搜索无结果时给提示而不是空白", () => {
  const ctx = makePage();
  try {
    ctx.window.aboutRender("这个词肯定不存在zzz");
    assert.match(ctx.doc.getElementById("abDoc").textContent, /没有匹配的章节/);
    assert.equal(ctx.doc.querySelectorAll("#abToc a").length, 0, "目录清空");
  } finally {
    close(ctx);
  }
});

test("搜索不会破坏标签结构（高亮只处理标签外文本）", () => {
  const ctx = makePage();
  try {
    ctx.window.aboutRender("b");
    const doc = ctx.doc.getElementById("abDoc");
    assert.ok(doc.querySelectorAll("mark").length > 0, "单字母也会命中，属于预期");
    assert.equal(doc.querySelectorAll("mark b").length, 0, "mark 不应把加粗标签包进去");
    assert.ok(doc.querySelectorAll("b").length > 0, "加粗标签仍完好");
    assert.ok(doc.querySelectorAll(".ab-p").length > 0, "段落结构未被打碎");
  } finally {
    close(ctx);
  }
});

test("术语表包含新分类，且每个分类都有条目", () => {
  const ctx = makePage();
  try {
    ctx.window.termPageRender("");
    const box = ctx.doc.getElementById("termPageList");
    const groups = Array.from(box.querySelectorAll(".tm-group h4"));
    const names = groups.map((h) => h.textContent.replace(/\s*\d+$/, "").trim());
    assert.ok(names.includes("仓位与执行"), "新增「仓位与执行」分类：" + names.join("/"));
    assert.equal(groups.length, 5, "共 5 个分类");
    groups.forEach((h) => {
      assert.match(h.textContent, /\d+/, "分类标题带条目数");
    });
    const txt = box.textContent;
    for (const k of ["ATR（真实波幅）", "2×ATR 参考位", "分数 Kelly", "Topk-Drop", "计划-执行偏差", "差额单"]) {
      assert.ok(txt.includes(k), "包含术语 " + k);
    }
  } finally {
    close(ctx);
  }
});

test("术语表搜索命中数量与关键字", () => {
  const ctx = makePage();
  try {
    ctx.window.termPageRender("kelly");
    const box = ctx.doc.getElementById("termPageList");
    assert.match(box.textContent, /分数 Kelly/);
    assert.equal(box.querySelectorAll(".tm-item").length, 1, "kelly 只命中一条");
    assert.match(ctx.doc.getElementById("termCount").textContent, /匹配 1 \/ \d+ 条/);
  } finally {
    close(ctx);
  }
});
