/**
 * 本轮改动的回归测试：
 *  - 候选榜不再内嵌滚动条（避免「页滚动 + 表滚动」双滚动）
 *  - 漏斗摘要并进样本池容器，不再单独占一行
 *  - 首屏请求不重复（init 只跑一次、sideTodoLoad 有节流）
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { makePage, boot, tick, close } from "./helpers.mjs";

function apiFor(url) {
  // 首屏优先恢复落盘候选：这些布局测试关心的是「有候选时的 DOM」，
  // 故意让 resume 返回空，走自动出数分支（下面的 screen/run 桩件）。
  if (url.includes("/api/screen/resume")) {
    return { ok: true, empty: true, candidates: [], as_of: "" };
  }
  if (url.includes("/api/screen/run")) {
    return {
      ok: true, as_of: "2026-11-20", engine: "lightgbm",
      score_label: "模型分",
      candidates: Array.from({ length: 20 }, (_, i) => ({
        code: "6009" + String(i).padStart(2, "0"), name: "股票" + i,
        rank: i + 1, score: 0.1, close: 10, close_real: 10,
        pct: -1, mom20: -5, industry: "银行",
      })),
      funnel: {
        dropped: 4815, kept: 481,
        stages: [
          { key: "pool", name: "样本池", before: 5211, after: 5211, dropped: 0, enabled: true },
          { key: "st", name: "ST 与退市", before: 5211, after: 5150, dropped: 61, enabled: true },
          { key: "extreme", name: "极端风险", before: 5150, after: 481, dropped: 4669, enabled: true },
        ],
      },
      industry: { note: "单行业上限 2 只，因超限剔除 46 只候选",
                  counts: Object.fromEntries(Array.from({ length: 12 }, (_, i) => ["行业" + i, 1])) },
      neutral: { mode: "none" }, record: { ok: true, skipped: true },
    };
  }
  if (url.includes("/api/screen/status")) {
    return { ok: true,
      data: { snapshot: { loaded: true, rows: 5000 }, dataset: { loaded: true, rows: 1000 },
              cached_total: 5000, universe_total: 5000, modules: [] },
      model: { trained: true, engine: "lightgbm", meta: { metrics: {} } },
      llm: { enabled: false, configured: false }, planned: [] };
  }
  if (url.includes("/api/watchlist")) return { ok: true, items: [], count: 0 };
  if (url.includes("/api/bulk")) return { ok: true, running: false, cached_total: 5000 };
  if (url.includes("/api/screen/freshness")) {
    return { ok: true, snapshot: {}, dataset: {}, model: {}, actions: [],
             run: { running: false, steps: [] } };
  }
  if (url.includes("/api/data/hub")) {
    return { ok: true, freshness: { ok: true, actions: [], run: { running: false } },
             scheduler: {}, runs: [], bulk_workers: 6, sources: {}, llm: {} };
  }
  if (url.includes("/api/universe")) {
    return { ok: true, rows: [], total: 5000, cached: 5000, filtered: 0,
             updated: "2026-11-23", target: "2026-11-20", lag: 0, fresh: true };
  }
  if (url.includes("/api/screen/llm")) return { ok: true, saved: null };
  return { ok: true, batches: [], items: [], rows: [] };
}

async function setup(t) {
  const ctx = makePage({ api: apiFor });
  t.after(() => close(ctx));
  await boot(ctx);
  // 首屏已改为手动选股；布局测试需要完整候选 DOM，主动点一次「立即选股」
  const btn = ctx.doc.getElementById("btnScrEmptyRun");
  if (btn) {
    btn.dispatchEvent(new ctx.window.MouseEvent("click", { bubbles: true }));
    await tick(ctx.window, 10);
  }
  return ctx;
}

test("漏斗摘要与样本池数字在同一张卡内、紧邻显示", async (t) => {
  const ctx = await setup(t);
  const { window, doc } = ctx;

  // 等候选榜渲染完
  await tick(window, 20);

  const sub = doc.querySelector("#scrSampleSub");
  assert.ok(sub, "应有摘要节点 #scrSampleSub");

  const txt = sub.textContent || "";
  assert.ok(txt.includes("共剔除"), `摘要应含「共剔除」，实际：${txt}`);
  assert.ok(txt.includes("单行业上限"), `摘要应含行业上限说明，实际：${txt}`);
  assert.ok(txt.includes("覆盖 12 个行业"), `摘要应含行业数，实际：${txt}`);

  // 摘要不再放进 #scrSampleBox（那个格子太窄，长摘要会把标题挤成竖排），
  // 但必须仍在同一张卡（.scr-dash）内，且紧跟在结论条之后。
  const pool = doc.querySelector("#scrSampleBox");
  assert.ok(pool, "样本数容器应保留");
  assert.ok(pool.querySelector("#scrSampleN"), "样本数节点应保留");

  const card = pool.closest(".scr-dash");
  assert.ok(card, "样本池应在「本期候选」卡内");
  assert.ok(card.contains(sub), "摘要应与样本池在同一张卡内");

  // 摘要应排在结论条之后（不是塞在右上角那个小格子里）
  const top = card.querySelector(".scr-top");
  assert.ok(top, "应有结论条 .scr-top");
  assert.ok(
    top.compareDocumentPosition(sub) & window.Node.DOCUMENT_POSITION_FOLLOWING,
    "摘要应位于结论条之后",
  );
});

test("候选榜不再内嵌滚动容器", async (t) => {
  const ctx = await setup(t);
  const { doc } = ctx;

  const wrap = doc.querySelector(".scr-table-wrap");
  assert.ok(wrap, "应存在表格容器");

  // 关键：class 里不能再带 .scroll（那个类会加 max-height + overflow）
  assert.ok(
    !wrap.classList.contains("scroll"),
    `.scr-table-wrap 不应再带 .scroll（会内嵌滚动条），实际 class="${wrap.className}"`,
  );
});

test("首屏各接口不应重复请求", async (t) => {
  // 这里用裸 boot，不点选股 —— 测的是「打开页面」这一步的请求数
  const ctx = makePage({ api: apiFor });
  t.after(() => close(ctx));
  await boot(ctx);
  const { calls } = ctx;

  const count = {};
  for (const c of calls) {
    const path = c.url.replace("http://127.0.0.1:8765", "").split("?")[0];
    count[path] = (count[path] || 0) + 1;
  }

  // status / screen/status 必须只发一次
  for (const one of ["/api/status", "/api/screen/status"]) {
    assert.equal(count[one] || 0, 1, `${one} 应只请求 1 次，实际 ${count[one] || 0} 次`);
  }
  // 首屏先试 resume；本测试桩件让它返回空，因此会回退一次 screen/run。
  // 关键是「只走一次」，不能 resume 和 run 各来好几遍。
  assert.equal(count["/api/screen/resume"] || 0, 1, "应尝试恢复一次");
  assert.equal(count["/api/screen/run"] || 0, 1, "无落盘记录时应回退出数一次");
  // 整页请求总数给个上限，防止以后又加回重复调用
  const total = Object.values(count).reduce((a, b) => a + b, 0);
  assert.ok(total <= 17, `首屏请求总数应 ≤17，实际 ${total}：${JSON.stringify(count)}`);
});
