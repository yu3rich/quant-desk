/**
 * 测试环境引导：把真实的 index.html + app.js 装进 jsdom。
 *
 * 为什么不改写/拷贝一份 HTML：测试必须跑在「线上那份文件」上，
 * 否则改了页面忘了同步测试副本，测试全绿也没意义。
 *
 * 关键点：
 *  - app.js 是普通 script（非 module），里面对 DOMContentLoaded 挂 init。
 *    这里先建 DOM、桩掉网络与图表，再注入脚本，最后手动触发 init。
 *  - fetch / ECharts 用最小桩件：它们不属于被测逻辑，
 *    但少了会让 init 抛错，掩盖真正的 bug。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export function readSrc(rel) {
  return fs.readFileSync(path.join(ROOT, rel), "utf8");
}

/**
 * 建一个可交互的页面环境。
 * @param {object} opts
 * @param {(url: string, body: string) => any} opts.api  响应用例：返回 JSON 对象
 * @param {boolean} opts.runInit 是否触发 DOMContentLoaded
 */
export function makePage(opts = {}) {
  const html = readSrc("web/index.html");

  const dom = new JSDOM(html, {
    url: "http://127.0.0.1:8765/",
    // 必须用 dangerously：本文件要执行项目自带的 web/app.js。
    // 用 "outside-only" 时 appendChild 的 <script> 不会运行，
    // 于是 window 上什么函数都没有（测试会误报「功能缺失」）。
    // 这里加载的是本仓库自己的代码，不涉及外部内容。
    runScripts: "dangerously",
    pretendToBeVisual: true,
  });

  const { window } = dom;

  // ---- 桩件：网络 ----
  const calls = [];
  const api = opts.api || (() => ({ ok: true }));
  window.fetch = (url, init) => {
    const body = init && init.body ? String(init.body) : "";
    calls.push({ url: String(url), body });
    let payload;
    try {
      payload = api(String(url), body);
    } catch (e) {
      return Promise.reject(e);
    }
    if (payload === undefined || payload === null) payload = { ok: true };
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
      headers: { get: () => "application/json" },
    });
  };

  // ---- 桩件：Web Animations ----
  // jsdom 不实现 Element.animate，但 app.js 的 toast/withBusy 会用到，
  // 不桩掉会让「点击按钮」类测试报 el.animate is not a function。
  if (!window.Element.prototype.animate) {
    window.Element.prototype.animate = function () {
      return { finished: Promise.resolve(), cancel() {}, finish() {}, play() {}, pause() {} };
    };
  }

  // ---- 桩件：图表 ----
  const charts = [];
  window.echarts = {
    init: () => {
      const inst = {
        setOption: () => {},
        clear: () => {},
        dispose: () => {},
        resize: () => {},
        on: () => {},
        off: () => {},
        getWidth: () => 800,
        getHeight: () => 300,
      };
      charts.push(inst);
      return inst;
    },
    getInstanceByDom: () => null,
  };
  if (!window.ResizeObserver) {
    window.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
  // requestAnimationFrame / matchMedia 在 jsdom 里可能缺失
  if (!window.requestAnimationFrame) {
    window.requestAnimationFrame = (cb) => window.setTimeout(cb, 0);
  }
  if (!window.matchMedia) {
    window.matchMedia = () => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
    });
  }

  // ---- 注入 app.js ----
  const script = window.document.createElement("script");
  script.textContent = readSrc("web/app.js");
  window.document.body.appendChild(script);

  return { dom, window, doc: window.document, calls, charts };
}

/** 触发 init（app.js 里挂在 DOMContentLoaded 上） */
export async function boot(ctx) {
  const { window, doc } = ctx;
  // jsdom 在 runScripts:"dangerously" 下，appendChild 的 inline script 执行后
  // 会自己派发一次 DOMContentLoaded；此时 readyState 已是 interactive/complete。
  // 再手动 dispatch 一次就会让 init 跑两遍 —— 所有接口请求数翻倍，
  // 看起来像「页面重复发请求」，其实是测试环境的假象。
  if (doc.readyState === "loading") {
    doc.dispatchEvent(new window.Event("DOMContentLoaded"));
  }
  // init 是 async 函数，内部串了 6+ 次 await（status / pool / screenStatus /
  // screenRun / screenReview / sideTodo）。只等一两个 tick 会在 initTerms()
  // 之前就返回，测试会误报「气泡没创建」。这里等到 DOM 真正稳定为止。
  await settle(window, 60);
}

/**
 * 等到「连续两轮没有新的 fetch」或超时，认为异步初始化已落地。
 * 比固定 sleep 稳：某个接口多一次往返也能覆盖到。
 */
export async function settle(window, maxRounds = 60) {
  for (let i = 0; i < maxRounds; i++) {
    await new Promise((r) => window.setTimeout(r, 0));
  }
}

/** 等若干个宏任务/微任务周期 */
export function tick(window, n = 3) {
  let p = Promise.resolve();
  for (let i = 0; i < n; i++) {
    p = p.then(() => new Promise((r) => window.setTimeout(r, 0)));
  }
  return p;
}

/**
 * 关掉这个页面环境。
 * app.js 里有 setInterval（侧栏待办每 2 分钟、实盘刷新等），
 * 不清理的话 Node 进程会被这些定时器挂住不退出 —— 表现为测试「跑完了但不返回」。
 * 每个用例结束都调它；跑测试时再用 --test-force-exit 兜底。
 */
export function close(ctx) {
  try {
    ctx.window.close();
  } catch (e) {
    /* 已经关掉了 */
  }
}
