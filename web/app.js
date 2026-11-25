/* QuantDesk frontend - no build step, plain JS + local ECharts */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const S = { charts: {}, last: null, best: null, fp: "" };

const STRATS = {
  ma_cross: { label: "双均线金叉 / 死叉", fields: [["fast", "快线周期", 10], ["slow", "慢线周期", 30]] },
  macd: { label: "MACD 金叉 / 死叉", fields: [] },
  rsi: { label: "RSI 均值回归", fields: [["rsi_n", "RSI 周期", 14], ["lower", "超卖阈值", 30], ["upper", "超买阈值", 70]] },
  boll: { label: "布林带突破", fields: [] },
  factor: { label: "Alpha 因子择时", fields: [["threshold", "信号阈值", 0.3]] }
};

function todayStr() {
  const d = new Date();
  return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, "0"), String(d.getDate()).padStart(2, "0")].join("-");
}
function num(n, d) { return Number(n).toFixed(d === undefined ? 2 : d); }
function signCls(n) { return n > 0 ? "pos" : (n < 0 ? "neg" : ""); }
function signed(n, d) { return (n > 0 ? "+" : "") + num(n, d) + "%"; }

const TOAST_KIND = {
  ok: { title: "任务完成", icon: "✓" },
  err: { title: "操作失败", icon: "!" },
  info: { title: "提示", icon: "i" }
};
// 新通知从顶部插入，旧卡片顺滑下移；移除时先收高度再把后面的卡片提上来。
function _toastFlip(before) {
  before.forEach((b, n) => {
    if (!n || !n.isConnected) return;
    const a = n.getBoundingClientRect();
    const dy = b.top - a.top;
    if (Math.abs(dy) > 0.5) {
      n.animate([{ transform: "translateY(" + dy + "px)" }, { transform: "none" }],
        { duration: 320, easing: "cubic-bezier(.22,.8,.28,1)" });
    }
  });
}
function _toastClose(el) {
  if (!el || el.dataset.closing) return;
  el.dataset.closing = "1";
  const sibs = new Map();
  $$("#toast .msg").forEach((n) => { if (n !== el) sibs.set(n, n.getBoundingClientRect()); });
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  el.animate([
    { opacity: 1, transform: "none", height: r.height + "px",
      paddingTop: cs.paddingTop, paddingBottom: cs.paddingBottom, marginBottom: "0px" },
    { opacity: 0, transform: "translateX(16px) scale(.97)", height: r.height + "px",
      paddingTop: cs.paddingTop, paddingBottom: cs.paddingBottom, marginBottom: "0px", offset: .55 },
    { opacity: 0, transform: "translateX(16px) scale(.97)", height: "0px",
      paddingTop: "0px", paddingBottom: "0px", marginBottom: "-8px" }
  ], { duration: 360, easing: "cubic-bezier(.4,0,.7,1)", fill: "forwards" }).onfinish = () => {
    el.remove();
    _toastFlip(sibs);
  };
}
// ---------------------------------------------------------------- 通知回流
// 右上角弹出的通知也要能事后查：用户说「刚才弹了个错」，日志里得找得到。
// 三个约束：
//   1) 不逐条发请求 —— 轮询失败会连弹，攒 1.5 秒合成一批；
//   2) 同一句 20 秒内只记一次，重复折成「（×N）」，避免刷屏把有用信息埋掉；
//   3) 上报失败绝不再弹提示，否则自己把自己喂成死循环。
const TOAST_LOG = { buf: [], timer: null, seen: new Map(), on: true };
try { TOAST_LOG.on = localStorage.getItem("quantdesk_logtoast") !== "0"; } catch (e) { /* 隐私模式 */ }

function toastLogLevel(type) {
  return type === "err" ? "error" : (type === "warn" ? "warn" : "info");
}
function toastLogPush(type, body) {
  if (!TOAST_LOG.on) return;
  const text = String(body || "").trim();
  if (!text) return;
  const level = toastLogLevel(type);
  const key = level + "|" + text;
  const now = Date.now();
  const hit = TOAST_LOG.seen.get(key);
  if (hit && now - hit.at < 20000) {
    hit.n += 1;
    const rec = TOAST_LOG.buf.find((x) => x.key === key);
    if (rec) rec.msg = text + "（×" + hit.n + "）";
    return;
  }
  if (TOAST_LOG.seen.size > 200) {          // 老的赶紧清，别在长开页面里慢慢涨
    TOAST_LOG.seen.forEach((v, k) => { if (now - v.at > 60000) TOAST_LOG.seen.delete(k); });
  }
  TOAST_LOG.seen.set(key, { at: now, n: 1 });
  TOAST_LOG.buf.push({
    key: key, level: level, type: String(type || "info"), msg: text,
    // 记下用户当时在哪一页：「在实盘复盘页点了同步」比单看文字有用
    detail: "页面 " + ((location.hash || "#home").replace(/^#/, "")) +
      " · " + new Date().toLocaleString("zh-CN")
  });
  if (!TOAST_LOG.timer) TOAST_LOG.timer = setTimeout(toastLogFlush, 1500);
}
function toastLogFlush() {
  TOAST_LOG.timer = null;
  const items = TOAST_LOG.buf.splice(0, 40);
  if (!items.length) return;
  const body = JSON.stringify({
    items: items.map((x) => ({ level: x.level, act: x.type, msg: x.msg, detail: x.detail }))
  });
  try {
    // keepalive：点完按钮就切页，这条记录也不会丢
    fetch("/api/logs/ui", {
      method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json" }, body: body
    }).catch(() => {});
  } catch (e) { /* 静默：记日志失败不能再弹提示 */ }
}

function toast(msg, type) {
  const host = $("#toast");
  if (!host) return;
  const kind = TOAST_KIND[type] || TOAST_KIND.info;
  const raw = String(msg == null ? "" : msg);
  // 原调用方给成功/失败消息加了统一前缀，这里剥掉，标题取正文里更具体的那一段
  let body = raw.replace(/^(成功|失败|提示)[：:]\s*/, "");
  let title = kind.title;
  // 前缀里不放数字：正文常以时间开头（「已同步 11:02 · …」），不能把 11:02 当标题
  const m = body.match(/^([^：:0-9]{2,14})[：:]\s*([\s\S]+)$/);
  if (m) { title = m[1]; body = m[2]; }

  const el = document.createElement("div");
  el.className = "msg " + (type || "info");
  el.setAttribute("role", type === "err" ? "alert" : "status");
  el.innerHTML = '<span class="msg-ic"></span><div class="msg-main">' +
    '<div class="msg-top"><b class="msg-title"></b><span class="msg-time">刚刚</span></div>' +
    '<p class="msg-txt"></p></div>';
  el.querySelector(".msg-ic").textContent = kind.icon;
  el.querySelector(".msg-title").textContent = title;
  el.querySelector(".msg-txt").textContent = body;
  el.querySelector(".msg-title").title = title;
  // 回流到运行日志（用户可见的提示本身就是一条排查线索）
  toastLogPush(type, title && title !== kind.title ? (title + "：" + body) : body);

  // 先记下旧卡片位置，插入后按位移反向播放，堆叠是滑动而不是跳变
  const before = new Map();
  $$("#toast .msg").forEach((n) => before.set(n, n.getBoundingClientRect()));
  host.prepend(el);
  el.animate([
    { opacity: 0, transform: "translateY(-14px) scale(.96)" },
    { opacity: 1, transform: "translateY(2px) scale(1.005)", offset: .68 },
    { opacity: 1, transform: "none" }
  ], { duration: 460, easing: "cubic-bezier(.16,1,.3,1)" });
  _toastFlip(before);

  // 卡片太多会盖住页面：只留最近 5 条
  const cards = $$("#toast .msg").filter((n) => !n.dataset.closing);
  for (let i = 5; i < cards.length; i++) _toastClose(cards[i]);

  // 鼠标悬停暂停计时，移开后接着走完剩余时间
  let left = type === "err" ? 7000 : 4200, since = Date.now(), timer = null;
  const arm = () => { since = Date.now(); timer = setTimeout(() => _toastClose(el), left); };
  el.addEventListener("mouseenter", () => {
    clearTimeout(timer); timer = null;
    left = Math.max(800, left - (Date.now() - since));
  });
  el.addEventListener("mouseleave", () => { if (!el.dataset.closing && !timer) arm(); });
  arm();
}
function loading(on, text) {
  const el = $("#loading");
  if (on) { $(".spin .txt").textContent = text || "正在计算…"; el.classList.add("on"); }
  else el.classList.remove("on");
}
async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    return await r.json();
  } catch (e) { return { ok: false, error: String(e) }; }
}
function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
function chart(id) {
  if (!S.charts[id]) S.charts[id] = echarts.init(document.getElementById(id));
  return S.charts[id];
}
function resizeIn(panel) {
  $$(panel + " .chart").forEach((el) => { if (S.charts[el.id]) S.charts[el.id].resize(); });
}

/* ---------------- sidebar ---------------- */
function renderParams() {
  const s = $("#strategy").value;
  const f = STRATS[s].fields;
  $("#paramBox").innerHTML = f.length ? f.map((x) =>
    '<div class="field"><label>' + x[1] + '</label><input type="number" step="any" data-k="' + x[0] + '" value="' + x[2] + '"></div>'
  ).join("") : '<div class="hint">该策略使用内置固定参数（MACD 12/26/9 或 布林 20日±2σ），无需额外设置。</div>';
  const sh = $("#stratHint");
  if (sh) sh.textContent = STRATS[s].label;
}
function payload(extra) {
  const p = {};
  $$("#paramBox input").forEach((i) => { p[i.dataset.k] = Number(i.value); });
  return Object.assign({
    code: ($("#code").value.trim() || POOL.default || "601318").replace(/\D/g, "").slice(0, 6).padStart(6, "0"),
    start: $("#start").value.replace(/-/g, ""),
    end: $("#end").value.replace(/-/g, ""),
    strategy: $("#strategy").value,
    params: p,
    capital: Number($("#capital").value),
    commission: Number($("#commission").value) / 1000,
    stamp_tax: Number($("#stamp_tax").value) / 1000,
    slippage: Number($("#slippage").value) / 1000,
    refresh: false
  }, extra || {});
}

/* 参数指纹：回测要不要重跑，只能按「完整参数」判断。
   之前只比 code，而代码框默认是空的（留空用默认标的），"" !== "601318"
   于是每次进总览都判成「标的变了」重跑一遍；反过来改了起始日或策略，
   倒因为 code 没变而不重跑。两个方向都是错的。 */
function btFingerprint(b) {
  const p = b || payload();
  return JSON.stringify([p.code, p.start, p.end, p.strategy, p.params,
    p.capital, p.commission, p.stamp_tax, p.slippage]);
}

/* ---------------- backtest ---------------- */
async function runBacktest(extra) {
  const b = payload(extra);
  sysTaskStart("backtest", "回测 " + b.code);
  $("#btnRun").disabled = true;
  const r = await post("/api/backtest", b);
  $("#btnRun").disabled = false;
  sysTaskEnd("backtest");
  if (!r.ok) { toast(r.error || "回测失败", "err"); return; }
  S.last = r;
  S.fp = btFingerprint(b);
  renderBacktest(r);
  toast("回测完成：" + r.bars + " 根 K 线，" + r.metrics.trades + " 笔平仓交易", "ok");
}
function renderBacktest(r) {
  const m = r.metrics;
  $("#chipSource").innerHTML = "数据源 <b>" + r.source + "</b>";
  $("#chipRange").innerHTML = "区间 <b>" + r.date_from + " ~ " + r.date_to + "</b>";
  $("#chipBars").innerHTML = "<b>" + r.bars + "</b> 根日线";
  $("#stSummary").innerHTML = "<b>" + r.name + " (" + r.code + ")</b> · " +
    r.bars + " 根日线 · 数据源 " + esc(r.source || "—");
  if (r.warnings && r.warnings.length) {
    $("#stSummary").innerHTML += '<div style="color:var(--amber);margin-top:6px;line-height:1.65">⚠ ' +
      r.warnings.join("<br>⚠ ") + "</div>";
  }

  // 第五项是术语键：鼠标悬停 / 点击指标名即可看到解释，不用在卡片里堆说明
  const items = [
    ["累计收益", signed(m.total), "相对初始资金", signCls(m.total), ""],
    ["年化收益", signed(m.annual), "按 252 交易日折算", signCls(m.annual), ""],
    ["夏普比率", num(m.sharpe), "无风险利率 2%", signCls(m.sharpe), "sharpe"],
    ["最大回撤", num(m.max_drawdown) + "%", "峰值到谷底的最深跌幅", "", "dd"],
    ["胜率", num(m.win_rate) + "%", m.trades + " 笔平仓交易", "", "winrate"],
    ["盈亏比", num(m.pl_ratio), "平均盈利 / 平均亏损", "", "plratio"],
    ["买入持有基准", signed(m.benchmark), "同期同股票", signCls(m.benchmark), "benchmark"],
    ["超额收益", signed(m.excess), "策略 − 基准", signCls(m.excess), "excess"]
  ];
  $("#kpis").innerHTML = items.map((x) => {
    const k = x[4] ? '<span class="term" data-term="' + x[4] + '">' + x[0] + "</span>" : x[0];
    return '<div class="kpi"><div class="k">' + k + '</div><div class="n ' + x[3] + '">' + x[1] +
      '</div><div class="s">' + x[2] + "</div></div>";
  }).join("");

  drawEquity(r); drawDrawdown(r); drawMonthly(r); drawKline(r);
  const rows = r.trades;
  $("#tradesBody").innerHTML = rows.map((t) =>
    "<tr><td>" + t.date + '</td><td class="' + (t.side === "买入" ? "buy" : "sell") + '">' + t.side +
    "</td><td>" + num(t.price) + "</td><td>" + t.shares + "</td><td>" + num(t.amount) +
    "</td><td>" + num(t.fee) + "</td><td class='" + (t.pnl === null ? "" : signCls(t.pnl)) + "'>" +
    (t.pnl === null ? "—" : signed(t.pnl, 2).replace("%", "")) + "</td><td class='" +
    (t.pnl_pct === null ? "" : signCls(t.pnl_pct)) + "'>" +
    (t.pnl_pct === null ? "—" : signed(t.pnl_pct)) + "</td></tr>"
  ).join("");
  $("#tradesEmpty").style.display = rows.length ? "none" : "block";
  $("#btnExport").style.display = rows.length ? "inline-block" : "none";
  resizeIn('[data-panel="overview"]');
  resizeIn('[data-panel="kline"]');
}

/* 净值曲线：指标卡旁的紧凑卡片，带坐标轴/图例/滚轮缩放（原大图已并入此处） */
function drawEquity(r) {
  const el = document.getElementById("chartSpark");
  if (!el || !r.curve || !r.curve.length) return;
  const dates = r.curve.map((x) => x.date);
  chart("chartSpark").setOption({
    tooltip: { trigger: "axis" },
    legend: { data: ["策略净值", "买入持有"], top: 0, itemWidth: 14, itemHeight: 8,
      textStyle: { color: "#525c6b", fontSize: 11 } },
    grid: { left: 58, right: 16, top: 30, bottom: 24 },
    xAxis: { type: "category", data: dates, boundaryGap: false,
      axisLabel: { formatter: (v) => String(v).slice(2), color: "#8a939f", fontSize: 10 } },
    yAxis: { type: "value", scale: true, name: "元", nameTextStyle: { color: "#8a939f", fontSize: 10 },
      axisLabel: { color: "#8a939f", fontSize: 10 }, splitLine: { lineStyle: { color: "#eef1f5" } } },
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "策略净值", type: "line", data: r.curve.map((x) => x.equity), showSymbol: false,
        lineStyle: { width: 2, color: "#1c5fd8" }, areaStyle: { color: "rgba(28,95,216,.08)" } },
      { name: "买入持有", type: "line", data: r.curve.map((x) => x.benchmark), showSymbol: false,
        lineStyle: { width: 1.5, color: "#9aa5b4", type: "dashed" } }
    ]
  }, true);
}
function drawDrawdown(r) {
  const dates = r.curve.map((x) => x.date);
  chart("chartDD").setOption({
    tooltip: { trigger: "axis", formatter: (p) => p[0].axisValue + "<br/>回撤 " + num(p[0].data) + "%" },
    grid: { left: 58, right: 22, top: 18, bottom: 44 },
    xAxis: { type: "category", data: dates, boundaryGap: false,
      axisLabel: { formatter: (v) => String(v).slice(2), color: "#8a939f" } },
    yAxis: { type: "value", max: 0, name: "%", axisLabel: { color: "#8a939f" },
      splitLine: { lineStyle: { color: "#eef1f5" } } },
    dataZoom: [{ type: "inside" }],
    series: [{ type: "line", data: r.curve.map((x) => x.drawdown), showSymbol: false,
      lineStyle: { color: "#12996a", width: 1.4 }, areaStyle: { color: "rgba(18,153,106,.16)" } }]
  }, true);
}
function drawMonthly(r) {
  const years = Array.from(new Set(r.monthly.map((m) => m.month.slice(0, 4))));
  const months = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"];
  const data = r.monthly.map((m) => [years.indexOf(m.month.slice(0, 4)), Number(m.month.slice(5, 7)) - 1, m.ret]);
  const mx = Math.max(5, Math.ceil(Math.max.apply(null, r.monthly.map((m) => Math.abs(m.ret))) / 5) * 5);
  chart("chartMonthly").setOption({
    tooltip: { position: "top", formatter: (p) => years[p.data[0]] + " " + months[p.data[1]] + "：" + signed(p.data[2]) },
    grid: { left: 52, right: 20, top: 12, bottom: 62 },
    xAxis: { type: "category", data: years, splitArea: { show: true }, axisLabel: { color: "#8a939f" } },
    yAxis: { type: "category", data: months, splitArea: { show: true }, axisLabel: { color: "#8a939f" } },
    visualMap: { min: -mx, max: mx, calculable: true, orient: "horizontal", left: "center", bottom: 4,
      inRange: { color: ["#12996a", "#e8f4ee", "#ffffff", "#fbe6e4", "#d3403f"] },
      textStyle: { color: "#8a939f" } },
    series: [{ type: "heatmap", data: data, label: { show: true, formatter: (p) => num(p.data[2], 1) },
      itemStyle: { borderColor: "#fff", borderWidth: 1 }, emphasis: { itemStyle: { shadowBlur: 8 } } }]
  }, true);
}
function drawKline(r) {
  const q = r.quotes, dates = q.map((x) => x.date);
  const marks = r.marks.slice(0, 80).map((m) => ({
    coord: [m.date, m.price], value: m.type === "buy" ? "买" : "卖",
    symbol: "triangle", symbolRotate: m.type === "buy" ? 0 : 180, symbolSize: 13,
    itemStyle: { color: m.type === "buy" ? "#d3403f" : "#12996a" },
    label: { show: false }
  }));
  chart("chartK").setOption({
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    legend: { data: ["K线", "MA快", "MA慢", "成交量"], top: 0, textStyle: { color: "#525c6b" } },
    grid: [
      { left: 66, right: 24, top: 36, height: "54%" },
      { left: 66, right: 24, top: "72%", height: "17%" }
    ],
    xAxis: [
      { type: "category", data: dates, boundaryGap: true, axisLabel: { show: false } },
      { type: "category", gridIndex: 1, data: dates, boundaryGap: true,
        axisLabel: { formatter: (v) => String(v).slice(2), color: "#8a939f" } }
    ],
    yAxis: [
      { scale: true, axisLabel: { color: "#8a939f" }, splitLine: { lineStyle: { color: "#eef1f5" } } },
      { scale: true, gridIndex: 1, name: "成交量", nameTextStyle: { color: "#8a939f" },
        axisLabel: { color: "#8a939f" }, splitLine: { show: false } }
    ],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] },
      { type: "slider", xAxisIndex: [0, 1], height: 15, bottom: 2 }],
    series: [
      { name: "K线", type: "candlestick", data: q.map((x) => [x.o, x.c, x.l, x.h]),
        itemStyle: { color: "#d3403f", color0: "#12996a", borderColor: "#d3403f", borderColor0: "#12996a" },
        markPoint: { data: marks } },
      { name: "MA快", type: "line", data: q.map((x) => x.ma_fast), showSymbol: false,
        connectNulls: true, lineStyle: { width: 1.2, color: "#e8a33d" } },
      { name: "MA慢", type: "line", data: q.map((x) => x.ma_slow), showSymbol: false,
        connectNulls: true, lineStyle: { width: 1.2, color: "#1c5fd8" } },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
        data: q.map((x) => x.v), itemStyle: { color: "#aeb9c9" } }
    ]
  }, true);
}

/* ---------------- 行情走势：分时 / 五日 / 日 / 周 / 月 ---------------- */
const MK = { code: "", period: "intraday", data: null, key: "", manual: false };
/* 记住上次看图的周期与标的：刷新后回到同一张图，符合行情软件的习惯 */
(function () {
  try {
    const p = localStorage.getItem("quantdesk_mk_period");
    const c = localStorage.getItem("quantdesk_mk_code");
    if (p) MK.period = p;
    if (c) { MK.code = c; MK.manual = true; }
  } catch (e) {}
})();
const MK_LABEL = { intraday: "分时", five: "五日", day: "日K", week: "周K", month: "月K" };
/* 分时横轴固定为 09:30~11:30 + 13:00~15:00 共 242 个点，只在几个整点打刻度 */
const MK_TICKS = { 0: "09:30", 60: "10:30", 120: "11:30/13:00", 181: "14:00", 241: "15:00" };
/* 窄容器里「11:30/13:00」这种长标签会和相邻刻度叠在一起，单列一个短版 */
const MK_TICKS_S = { 0: "09:30", 60: "10:30", 120: "11:30", 181: "14:00", 241: "15:00" };

/* 顶部五个主要指数：一次请求全拿，盘中 30 秒刷新 */
function mkSparkSvg(points, pct) {
  const vals = (points || []).map(Number).filter(Number.isFinite);
  const p = Number(pct);
  const cls = !Number.isFinite(p) || p === 0 ? "flat" : (p > 0 ? "up" : "down");
  if (vals.length < 2) return '<svg class="mk-spark ' + cls + '" viewBox="0 0 64 28" aria-hidden="true"></svg>';
  const W = 64, H = 28, PAD = 2;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
  const x = (i) => PAD + i * (W - PAD * 2) / (vals.length - 1);
  const y = (v) => PAD + (hi - v) * (H - PAD * 2) / (hi - lo);
  const line = vals.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ");
  const area = "M" + x(0).toFixed(1) + " " + (H - PAD) + " " +
    vals.map((v, i) => "L" + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ") +
    " L" + x(vals.length - 1).toFixed(1) + " " + (H - PAD) + " Z";
  return '<svg class="mk-spark ' + cls + '" viewBox="0 0 64 28" aria-hidden="true">' +
    '<path class="mk-spark-area" d="' + area + '"/>' +
    '<path class="mk-spark-line" d="' + line + '"/></svg>';
}
async function mkIndicesLoad() {
  const box = $("#mkIndexRow");
  if (!box) return;
  const r = await api("/api/indices");
  if (!r.ok || !r.list || !r.list.length) return;
  box.innerHTML = r.list.map((x) => {
    const pct = x.pct == null ? null : Number(x.pct);
    const cls = pct == null ? "" : (pct > 0 ? "up" : (pct < 0 ? "down" : ""));
    const chg = x.change == null ? "" :
      (Number(x.change) > 0 ? "+" : "") + num(x.change, 2) + " · ";
    const tip = (x.name || "") + " · 高 " + (x.high == null ? "—" : num(x.high, 2)) +
      " 低 " + (x.low == null ? "—" : num(x.low, 2)) + (x.time ? " · " + x.time : "");
    return '<div class="mk-idx" title="' + esc(tip) + '">' +
      '<div class="mk-idx-body">' +
      '<div class="nm">' + esc(x.name) + "</div>" +
      '<div class="px">' + num(x.price, 2) + "</div>" +
      '<div class="pct ' + cls + '">' +
      (pct == null ? "—" : chg + signed(pct)) + "</div></div>" +
      mkSparkSvg(x.spark, pct) + "</div>";
  }).join("");
}

/* 查看记录：存在浏览器本地，切换周期不重复记，最多 12 条 */
const MK_HIST_KEY = "quantdesk_mk_history";
const MK_HIST_MAX = 12;
function mkHistRead() {
  try {
    const a = JSON.parse(localStorage.getItem(MK_HIST_KEY) || "[]");
    return Array.isArray(a)
      ? a.filter((x) => x && /^\d{6}$/.test(String(x.code))).slice(0, MK_HIST_MAX)
      : [];
  } catch (e) { return []; }
}
function mkHistSave(a) {
  try { localStorage.setItem(MK_HIST_KEY, JSON.stringify(a)); } catch (e) {}
}
function mkHistRender() {
  const box = $("#mkHistList");
  if (!box) return;
  const a = mkHistRead();
  const clr = $("#btnMkHistClear");
  if (clr) { clr.hidden = !a.length; clr.classList.remove("armed"); }
  box.innerHTML = a.length ? a.map((x) => {
    const nm = x.name || "";
    return '<span class="mk-hchip" role="button" tabindex="0" data-code="' + x.code +
      '" title="查看 ' + esc((nm + " " + x.code).trim()) + '">' +
      '<span class="hc-name">' + esc(nm || x.code) + "</span>" +
      (nm ? '<span class="hc-code">' + x.code + "</span>" : "") +
      '<button class="hc-x" data-del="' + x.code + '" title="删除这条记录" aria-label="删除">×</button>' +
      "</span>";
  }).join("") : '<span class="mk-hist-empty">暂无，打开个股后自动记录</span>';
}
function mkHistAdd(code, name) {
  code = String(code || "");
  if (!/^\d{6}$/.test(code)) return;
  const a = mkHistRead().filter((x) => x.code !== code);
  a.unshift({ code: code, name: String(name || "").slice(0, 20), at: Date.now() });
  mkHistSave(a.slice(0, MK_HIST_MAX));
  mkHistRender();
}
function mkHistRemove(code) {
  mkHistSave(mkHistRead().filter((x) => x.code !== code));
  mkHistRender();
}

function mkCode(raw) {
  const s = String(raw || "").trim().toLowerCase();
  const m = s.match(/\d{6}/);
  return m ? m[0] : "";
}

/* 从候选榜、行业成分股等任何地方跳到「行情走势」看这只票 */
function openChart(code) {
  const c = mkCode(code);
  if (!c) return;
  MK.code = c;
  MK.manual = true;
  MK.key = "";
  // 输入框里的旧代码优先级高于 MK.code，跳转时必须一起改，否则还看上一只
  const box = $("#mkCode");
  if (box) box.value = c;
  if (document.body.dataset.page === "kline") mkLoad(null, { force: true });
  else switchTab("kline");
}

function mkBusy(on) {
  const n = $("#mkNote");
  if (n) n.dataset.busy = on ? "1" : "";
}

async function mkLoad(period, opts) {
  const o = opts || {};
  const box = $("#mkCode");
  const raw = box ? box.value.trim() : "";
  let code = mkCode(raw);
  if (!code && raw) { toast("请输入 6 位股票代码，如 600000", "err"); return; }
  if (!code) code = MK.code;
  if (!code) code = mkCode(($("#code") && $("#code").value) || "") || "600000";
  if (period) MK.period = period;
  MK.code = code;
  if (box && box.value !== code) box.value = code;

  const id = code + ":" + MK.period;
  if (!o.force && MK.key === id && MK.data) { mkRender(MK.data); return; }
  mkBusy(true);
  const r = await api("/api/chart?code=" + encodeURIComponent(code) + "&period=" + MK.period);
  mkBusy(false);
  MK.key = id;
  if (!r.ok) {
    MK.data = null;
    chart("chartMK").clear();
    $("#mkName").textContent = "行情走势";
    $("#mkSub").textContent = r.error || "读取失败";
    $("#mkQuote").innerHTML = "";
    return;
  }
  MK.data = r;
  try { localStorage.setItem("quantdesk_mk_code", code); } catch (e) {}
  mkHistAdd(code, r.name);
  mkRender(r);
}

function mkQuoteHtml(r) {
  const up = (r.pct || 0) >= 0;
  const cls = up ? "pos" : "neg";
  const px = r.last == null ? "—" : num(r.last, 2);
  let parts = [];
  if (r.bars) {
    const b = r.bars[r.bars.length - 1];
    parts = ["开 " + num(b.open, 2), "高 " + num(b.high, 2), "低 " + num(b.low, 2),
      "量 " + mkVol(b.volume)];
  } else {
    const v = r.price.filter((x) => x != null);
    const mx = v.length ? Math.max.apply(null, v) : null;
    const mn = v.length ? Math.min.apply(null, v) : null;
    const total = (r.vol || []).reduce((s, x) => s + (x || 0), 0);
    parts = ["高 " + (mx == null ? "—" : num(mx, 2)), "低 " + (mn == null ? "—" : num(mn, 2)),
      "量 " + mkVol(total)];
  }
  return '<span class="mk-px ' + cls + '">' + px + "</span>" +
    '<span class="mk-pct ' + cls + '">' + (r.pct == null ? "—" : signed(r.pct)) + "</span>" +
    '<span class="mk-meta">' + parts.map(esc).join(" · ") + "</span>";
}

function mkVol(v) {
  const n = Number(v) || 0;
  if (n >= 1e8) return num(n / 1e8, 2) + " 亿手";
  if (n >= 1e4) return num(n / 1e4, 2) + " 万手";
  return num(n, 0) + " 手";
}

function mkDate(s) {
  const t = String(s || "");
  return /^\d{8}$/.test(t) ? t.slice(0, 4) + "-" + t.slice(4, 6) + "-" + t.slice(6, 8) : t;
}

function mkRender(r) {
  const c = chart("chartMK");
  const title = ($("#mkName"));
  title.textContent = (r.name ? r.name + " " : "") + r.code;
  const min = r.period === "intraday" || r.period === "five";
  $("#mkSub").textContent = MK_LABEL[r.period] + " · " +
    (min ? "数据日 " : "截至 ") + (mkDate(r.date) || "—") +
    (r.period === "five" && r.days ? " · 共 " + r.days + " 个交易日" : "") +
    (min ? "" : " · 前复权");
  $("#mkQuote").innerHTML = mkQuoteHtml(r);
  $$("#mkTabs .mk-tab").forEach((t) => t.classList.toggle("on", t.dataset.period === r.period));
  if (r.period === "intraday" || r.period === "five") mkDrawMinute(r, c);
  else mkDrawCandle(r, c);
  c.resize();   // 从隐藏面板切入时补一次尺寸，避免图被压成一窄条
}

/* 分时 / 五日：价格线 + 均价线 + 分钟量，涨跌以昨收（五日为首日开盘）为基准 */
function mkDrawMinute(r, c, compact) {
  const pre = r.pre_close || r.last || 0;
  const vals = r.price.filter((x) => x != null);
  if (!vals.length || !pre) { c.clear(); return; }
  const hi = Math.max.apply(null, vals), lo = Math.min.apply(null, vals);
  const dev = Math.max(Math.abs(hi - pre), Math.abs(lo - pre), pre * 0.004);
  const pad = dev * 0.14;
  const yMin = pre - dev - pad, yMax = pre + dev + pad;
  const pctMax = ((dev + pad) / pre) * 100;
  const five = r.period === "five";
  const lite = window.innerWidth < 640;
  const mark = {};
  (r.marks || []).forEach((m) => { mark[m.index] = m.label; });

  // 分钟量按「相对上一分钟」的涨跌着色，跌绿涨红
  let lastP = pre;
  const vols = (r.vol || []).map((v, i) => {
    const p = r.price[i];
    if (p == null) { lastP = lastP; return null; }
    const up = p >= lastP;
    lastP = p;
    return { value: v, itemStyle: { color: up ? "#d9544f" : "#3aa77c" } };
  });

  c.setOption({
    animation: false,
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross" },
      formatter: (ps) => {
        if (!ps || !ps.length) return "";
        const i = ps[0].dataIndex;
        const p = r.price[i];
        if (p == null) return "";
        const a = r.avg[i];
        const pct = ((p / pre - 1) * 100);
        const up = pct >= 0;
        return "<b>" + esc(r.axis[i]) + "</b>" +
          "<br/>价 <b style='color:" + (up ? "#d3403f" : "#12996a") + "'>" + num(p, 2) +
          "</b> " + signed(pct) +
          "<br/>均价 " + (a == null ? "—" : num(a, 2)) +
          "<br/>量 " + mkVol(r.vol[i]);
      }
    },
    grid: [
      { left: lite ? 54 : 66, right: lite ? 46 : 60, top: 30, height: "52%" },
      { left: lite ? 54 : 66, right: lite ? 46 : 60, top: "74%", height: "16%" }
    ],
    legend: { data: ["价格", "均价"], top: 0, right: 8, itemWidth: 12, itemHeight: 2,
      textStyle: { color: "#6b7482", fontSize: 10 } },
    xAxis: [
      { type: "category", data: r.axis, boundaryGap: false,
        axisLine: { lineStyle: { color: "#e2e7ef" } },
        axisTick: { show: false },
        axisLabel: { color: "#8a939f", fontSize: 10, interval: 0,
          formatter: (v, i) => (five ? (mark[i] || "") : ((compact ? MK_TICKS_S : MK_TICKS)[i] || "")) } },
      { type: "category", gridIndex: 1, data: r.axis, boundaryGap: false,
        axisLine: { lineStyle: { color: "#e2e7ef" } }, axisTick: { show: false },
        axisLabel: { show: false } }
    ],
    yAxis: [
      { min: yMin, max: yMax, interval: (yMax - yMin) / 4,
        axisLabel: { color: "#8a939f", fontSize: 10,
          formatter: (v) => (yMax - yMin < 1 ? num(v, 2) : v.toFixed(2)) },
        splitLine: { lineStyle: { color: "#eef1f5" } } },
      { min: -pctMax, max: pctMax, interval: pctMax / 2, position: "right",
        axisLabel: { color: "#8a939f", fontSize: 10,
          formatter: (v) => (v > 0 ? "+" : "") + v.toFixed(2) + "%" },
        splitLine: { show: false } },
      { gridIndex: 1, scale: true, splitNumber: 2,
        axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false },
        splitLine: { show: false } }
    ],
    dataZoom: five
      ? [{ type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 }]
      : [],
    series: [
      { name: "价格", type: "line", data: r.price, showSymbol: false, connectNulls: false,
        lineStyle: { width: 1.4, color: "#1c5fd8" },
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: "rgba(28,95,216,.16)" },
          { offset: 1, color: "rgba(28,95,216,.01)" }]) },
        markLine: { silent: true, symbol: "none", label: { show: false },
          lineStyle: { color: "#9aa4b2", type: "dashed", width: 1 },
          data: [{ yAxis: pre }] },
        z: 3 },
      { name: "均价", type: "line", data: r.avg, showSymbol: false, connectNulls: true,
        lineStyle: { width: 1, color: "#e8a33d" }, z: 2 },
      { name: "量", type: "bar", xAxisIndex: 1, yAxisIndex: 2, data: vols, barMaxWidth: 6 }
    ]
  }, true);
}

/* 日 / 周 / 月：前复权蜡烛 + MA5/10/20/60 + 成交量 */
function mkDrawCandle(r, c) {
  const bars = r.bars || [];
  if (!bars.length) { c.clear(); return; }
  const dates = bars.map((b) => b.date);
  const ma = (n) => bars.map((_, i) => {
    if (i < n - 1) return null;
    let s = 0;
    for (let k = i - n + 1; k <= i; k++) s += bars[k].close;
    return +(s / n).toFixed(3);
  });
  // 默认只显示最近 120 根，减少首屏压缩感
  const span = Math.min(bars.length, 120);
  const start = Math.max(0, 100 - (span / bars.length) * 100);
  const lite = window.innerWidth < 640;

  c.setOption({
    animation: false,
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross" },
      formatter: (ps) => {
        if (!ps || !ps.length) return "";
        const i = ps[0].dataIndex, b = bars[i];
        if (!b) return "";
        const up = b.pct >= 0;
        const col = up ? "#d3403f" : "#12996a";
        return "<b>" + b.date + "</b>" +
          "<br/>开 " + num(b.open, 2) + "　高 " + num(b.high, 2) +
          "<br/>低 " + num(b.low, 2) + "　收 <b style='color:" + col + "'>" + num(b.close, 2) + "</b>" +
          "<br/>涨跌 <b style='color:" + col + "'>" + signed(b.pct) + "</b>" +
          "<br/>量 " + mkVol(b.volume);
      }
    },
    legend: { data: ["MA5", "MA10", "MA20", "MA60"], top: 0, right: 10,
      itemWidth: 12, itemHeight: 2, textStyle: { color: "#6b7482", fontSize: 10 } },
    grid: [
      { left: lite ? 50 : 64, right: lite ? 14 : 22, top: 26, height: "56%" },
      { left: lite ? 50 : 64, right: lite ? 14 : 22, top: "74%", height: "15%" }
    ],
    xAxis: [
      { type: "category", data: dates, boundaryGap: true, axisLabel: { show: false },
        axisLine: { lineStyle: { color: "#e2e7ef" } } },
      { type: "category", gridIndex: 1, data: dates, boundaryGap: true,
        axisLine: { lineStyle: { color: "#e2e7ef" } }, axisTick: { show: false },
        axisLabel: { color: "#8a939f", fontSize: 10, formatter: (v) => String(v).slice(2) } }
    ],
    yAxis: [
      { scale: true, splitNumber: 4, axisLabel: { color: "#8a939f", fontSize: 10 },
        splitLine: { lineStyle: { color: "#eef1f5" } } },
      { gridIndex: 1, scale: true, splitNumber: 2, axisLabel: { show: false },
        axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } }
    ],
    dataZoom: bars.length > 60
      ? [{ type: "inside", xAxisIndex: [0, 1], start: start, end: 100 },
         { type: "slider", xAxisIndex: [0, 1], height: 14, bottom: 2, start: start, end: 100,
           borderColor: "#e5e9f0", fillerColor: "rgba(28,95,216,.08)",
           handleStyle: { color: "#c3cbd8" }, textStyle: { color: "#8a939f", fontSize: 9 } }]
      : [{ type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 }],
    series: [
      { name: "K线", type: "candlestick", barMaxWidth: 26,
        data: bars.map((b) => [b.open, b.close, b.low, b.high]),
        itemStyle: { color: "#d3403f", color0: "#12996a",
          borderColor: "#d3403f", borderColor0: "#12996a" } },
      { name: "MA5", type: "line", data: ma(5), showSymbol: false, connectNulls: true,
        lineStyle: { width: 1, color: "#e8a33d" } },
      { name: "MA10", type: "line", data: ma(10), showSymbol: false, connectNulls: true,
        lineStyle: { width: 1, color: "#1c5fd8" } },
      { name: "MA20", type: "line", data: ma(20), showSymbol: false, connectNulls: true,
        lineStyle: { width: 1, color: "#9b59b6" } },
      { name: "MA60", type: "line", data: ma(60), showSymbol: false, connectNulls: true,
        lineStyle: { width: 1, color: "#5c9e6f" } },
      { name: "量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 8,
        data: bars.map((b) => ({ value: b.volume,
          itemStyle: { color: b.pct >= 0 ? "#d9544f" : "#3aa77c" } })) }
    ]
  }, true);
}

/* ---------------- scan ---------------- */
/* 扫描页本身没有标的/区间输入：它们来自「回测总览」的参数条。
   不写清楚，用户会以为两页重复；这里把当前口径显式摊开。 */
function scanCtxPaint() {
  const el = $("#scanCtx");
  if (!el) return;
  const code = (($("#code") && $("#code").value) || "").replace(/\D/g, "").slice(0, 6) ||
    POOL.default || "601318";
  const start = ($("#start") && $("#start").value) || "—";
  const end = ($("#end") && $("#end").value) || "—";
  const st = ($("#strategy") && $("#strategy").value) || "ma_cross";
  const name = (POOL.items || []).filter((x) => x.code === code).map((x) => x.name)[0] || "";
  el.innerHTML = "当前标的 <b>" + esc(code) + (name ? " " + esc(name) : "") +
    "</b> · 区间 " + esc(start) + " ~ " + esc(end) +
    " · 扫描固定用双均线（快 / 慢线）" +
    (st === "ma_cross" ? "，与总览一致。"
      : "；总览选的策略不参与扫描，改标的与区间请到「回测总览」。");
}

async function runScan() {
  const b = payload();
  const fast = $("#scanFast").value.split(",").map((x) => Number(x.trim())).filter((x) => x > 0);
  const slow = $("#scanSlow").value.split(",").map((x) => Number(x.trim())).filter((x) => x > 0);
  if (!fast.length || !slow.length) { toast("参数网格格式有误", "err"); return; }
  if (fast.length * slow.length > 120) { toast("网格最多 120 组，请减少参数个数", "err"); return; }
  sysTaskStart("scan", "参数扫描 " + fast.length * slow.length + " 组");
  $("#btnScan").disabled = true;
  const r = await post("/api/scan", {
    code: b.code, start: b.start, end: b.end,
    capital: b.capital, commission: b.commission,
    stamp_tax: b.stamp_tax, slippage: b.slippage,
    grid: { fast: fast, slow: slow }
  });
  $("#btnScan").disabled = false;
  sysTaskEnd("scan");
  if (!r.ok) { toast(r.error || "扫描失败", "err"); return; }
  renderScan(r);
  toast("扫描完成：" + r.count + " 组有效参数", "ok");
}
function renderScan(r) {
  const valid = r.cells.filter((c) => c.valid);
  const data = valid.map((c) => [r.slows.indexOf(c.slow), r.fasts.indexOf(c.fast), c.sharpe]);
  const vals = valid.map((c) => c.sharpe);
  const lo = vals.length ? Math.min.apply(null, vals) : 0;
  const hi = vals.length ? Math.max.apply(null, vals) : 1;
  chart("chartScan").setOption({
    tooltip: { position: "top", formatter: (p) => "快线 " + r.fasts[p.data[1]] + " / 慢线 " +
      r.slows[p.data[0]] + "<br/>夏普 " + num(p.data[2]) },
    grid: { left: 62, right: 26, top: 20, bottom: 68 },
    xAxis: { type: "category", data: r.slows.map(String), name: "慢线", nameLocation: "middle",
      nameGap: 28, axisLabel: { color: "#8a939f" }, splitArea: { show: true } },
    yAxis: { type: "category", data: r.fasts.map(String), name: "快线",
      axisLabel: { color: "#8a939f" }, splitArea: { show: true } },
    visualMap: { min: lo, max: hi, calculable: true, orient: "horizontal", left: "center", bottom: 2,
      inRange: { color: ["#12996a", "#e8f4ee", "#fff8e8", "#e8a33d", "#d3403f"] },
      textStyle: { color: "#8a939f" } },
    series: [{ type: "heatmap", data: data, label: { show: true, formatter: (p) => num(p.data[2], 1) },
      itemStyle: { borderColor: "#fff", borderWidth: 1 } }]
  }, true);
  const best = valid.slice().sort((a, b) => b.sharpe - a.sharpe)[0];
  S.best = best || null;
  $("#scanInfo").innerHTML = best
    ? "共 <b>" + r.count + "</b> 组有效参数。夏普最高：<b>快线 " + best.fast + " / 慢线 " + best.slow +
      "</b>（夏普 " + num(best.sharpe) + "，累计 " + signed(best.total) + "，最大回撤 " + num(best.max_drawdown) + "%）。点击热力图格子可回填参数。"
    : "没有有效参数组合，请扩大网格或延长回测区间。";
  $("#btnApply").style.display = best && $("#strategy").value === "ma_cross" ? "inline-block" : "none";
  const c = chart("chartScan");
  c.off("click");
  c.on("click", (p) => {
    if (!p.data || !p.data.length) return;
    const f = r.fasts[p.data[1]], s = r.slows[p.data[0]];
    $("#strategy").value = "ma_cross"; renderParams();
    const fi = $('#paramBox input[data-k="fast"]'), si = $('#paramBox input[data-k="slow"]');
    if (fi) fi.value = f; if (si) si.value = s;
    switchTab("overview");
    toast("已回填 快线 " + f + " / 慢线 " + s + "，点「运行回测」查看结果");
  });
  resizeIn('[data-panel="scan"]');
}

/* 旧的「股票池因子打分」页已移除：与选股工作台的 16 因子基准分重复，
   统一由 /api/screen/run 的基准分模式承接。后端 /api/factors 保留，因子体检页仍在用；
   quant.factor_rank 已删。 */

/* --------- 二次点击确认：内嵌预览窗口会静默拦截 window.confirm --------- */
const ARMED = new Map();
function resetArms() {
  ARMED.forEach((st, btn) => {
    clearTimeout(st.timer);
    if (btn.isConnected) { btn.textContent = st.label; btn.classList.remove("armed"); }
  });
  ARMED.clear();
}
function twoStep(btn, label, action) {
  const hit = ARMED.get(btn);
  if (hit) {                       // 第二次点击 → 真正执行
    clearTimeout(hit.timer);
    btn.textContent = hit.label;
    btn.classList.remove("armed");
    ARMED.delete(btn);
    action();
    return;
  }
  const orig = btn.textContent;  // 第一次点击 → 进入确认态
  btn.textContent = label;
  btn.classList.add("armed");
  ARMED.set(btn, { label: orig, timer: setTimeout(() => {
    if (btn.isConnected) { btn.textContent = orig; btn.classList.remove("armed"); }
    ARMED.delete(btn);
  }, 5000) });
  toast("再点一次「" + label + "」确认执行（5 秒内有效）", "ok");
}

/* ---------------- 我的股票池 ---------------- */
let POOL = { items: [], count: 0, default: "" };
function esc(s) {
  // 股票简称只含中文/字母/数字，滤掉 HTML 控制字符即可避免注入
  return String(s == null ? "" : s).replace(/[&<>"']/g, "_");
}
function syncStockSelect(forceDefault) {
  const cur = $("#code").value.trim();
  $("#stockSel").innerHTML = POOL.items.map((s) =>
    '<option value="' + s.code + '">' + s.code + " " + esc(s.name) + "</option>").join("");
  // 首次打开必须尊重用户设置的「默认」；只有用户本次会话主动选过，才保留当前值
  const keepCur = !forceDefault && cur && POOL.items.some((s) => s.code === cur);
  const want = keepCur ? cur : (POOL.default || (POOL.items[0] || {}).code || "");
  $("#stockSel").value = want;
  $("#code").value = want;
}
function renderPool() {
  resetArms();
  const pn = $("#ptabN_mine"); if (pn) pn.textContent = POOL.count;
  $("#poolMeta").textContent = "共 " + POOL.count + " 只 · 默认 " + (POOL.default || "未设置") +
    " · 更新于 " + POOL.updated + " · 保存在 data/watchlist.json";
  $("#poolBody").innerHTML = POOL.items.map((s, i) =>
    "<tr class='rowlink' data-code='" + s.code + "' title='点击查看走势'><td>" + (i + 1) + "</td><td><b>" + s.code + "</b>" +
    (s.code === POOL.default ? ' <span class="pill def">默认</span>' : "") +
    "</td><td>" + esc(s.name) + "</td><td>" + esc(s.source) +
    // 悬停走统一气泡；title="" 是为了压掉 <tr> 的「点击查看走势」，
    // 否则两个提示会同时弹出来。
    '</td><td><span class="pill ' + (s.cached ? "yes" : "no") + '" title="" data-tip="' +
    (s.cached ? "本地 SQLite 缓存 · 截至 " + esc(s.bar_date) + " · " + s.bars + " 根日线"
              : "本地 SQLite 暂无该股日线，可到数据中枢补抓") +
    '" data-tip-name="行情缓存">' + (s.cached ? "已缓存" : "未缓存") + "</span></td>" +
    '<td><div class="op">' +
    '<button class="opb" data-act="default" data-code="' + s.code + '">设为默认</button>' +
    '<button class="opb" data-act="move" data-dir="up" data-code="' + s.code + '">↑</button>' +
    '<button class="opb" data-act="move" data-dir="down" data-code="' + s.code + '">↓</button>' +
    '<button class="opb del" data-act="remove" data-code="' + s.code + '">删除</button>' +
    "</div></td></tr>").join("");
  $("#poolEmpty").style.display = POOL.count ? "none" : "block";
}
async function loadPool(forceDefault) {
  const r = await api("/api/watchlist");
  if (!r.ok) { toast(r.error || "股票池读取失败", "err"); return; }
  POOL = r;
  renderPool();
  syncStockSelect(!!forceDefault);
}
function afterPoolChange(r, msg) {
  if (!r.ok) { toast(r.error || "操作失败", "err"); return false; }
  POOL = r;
  renderPool(); syncStockSelect(); updateStatus();
  if (msg) toast(msg, "ok");
  return true;
}
async function poolAdd() {
  const v = $("#poolCode").value.trim();
  if (!v) { toast("请输入股票代码", "err"); return; }
  const q = await api("/api/symbol?q=" + encodeURIComponent(v));
  if (!q.ok) { toast(q.error, "err"); return; }
  $("#poolAddHint").textContent = "识别结果：" + q.code + " " + q.name +
    (q.valid ? "" : "（公开接口未查到名称，请核对代码）");
  const r = await post("/api/watchlist", { action: "add", code: q.code, name: q.name });
  if (afterPoolChange(r, "已添加 " + q.code + " " + q.name)) $("#poolCode").value = "";
}
function poolExport(fmt) {
  const a = document.createElement("a");
  a.href = "/api/watchlist/export?format=" + fmt;
  a.download = "watchlist." + (fmt === "json" ? "json" : "txt");
  a.click();
  toast("股票池已导出", "ok");
}

/* ---------------- 东财账户同步（可选） ---------------- */
async function loadAccount() {
  const r = await api("/api/account");
  if (!r.ok) return;
  $("#acctStatus").textContent = r.configured
    ? ("已配置 " + r.masked + (r.from_env ? " · 环境变量" : " · 本机文件"))
    : "未配置（可跳过，不影响任何功能）";
  $("#acctKey").value = "";
  $("#acctKey").placeholder = r.configured ? "已保存，留空表示不修改" : "粘贴你的 MX_APIKEY（mkt_…）";
}
async function acctSave() {
  const k = $("#acctKey").value.trim();
  if (!k) { toast("请先粘贴 API Key", "err"); return; }
  const r = await post("/api/account", { apikey: k });
  toast(r.ok ? (r.message || "已保存") : (r.error || "保存失败"), r.ok ? "ok" : "err");
  if (r.ok) loadAccount();
}
async function acctClear() {
  twoStep($("#btnAcctClear"), "确认清除", async () => {
    const r = await post("/api/account", { action: "clear" });
    toast(r.ok ? "已清除本地 API Key" : (r.error || "清除失败"), r.ok ? "ok" : "err");
    loadAccount();
  });
}
async function acctSync() {
  sysTaskStart("acct", "同步东财自选");
  const r = await post("/api/account/sync", { mode: $("#acctMode").value });
  sysTaskEnd("acct");
  if (!r.ok) { toast(r.error || "同步失败", "err"); return; }
  await loadPool(); updateStatus();
  toast("同步成功：" + (r.synced || 0) + " 只自选股，新增 " + (r.added || 0) + " 只", "ok");
}

/* ---------------- 全 A 股名单 / 行情预热 ---------------- */
const UNI = { offset: 0, limit: 100, filtered: 0, total: 0, checked: new Set(), poll: null };
function fmtMoney(n) {
  return Number(n || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function uniParams() {
  return { q: $("#uniQ").value.trim(), market: $("#uniMarket").value,
           cached: $("#uniCached").checked ? "1" : "0",
           limit: UNI.limit, offset: UNI.offset };
}
async function uniLoad() {
  const r = await api("/api/universe?" + new URLSearchParams(uniParams()).toString());
  if (!r.ok) { toast(r.error || "名单读取失败", "err"); return; }
  UNI.filtered = r.filtered;
  UNI.total = r.total;
  const tn = $("#ptabN_all"); if (tn) tn.textContent = r.total;
  // 名单与实时价各自标一次「旧」：以前只在名单落后时提示一句，实时价悄悄用
  // 上一次的快照，页面看着是新的、其实是昨天的价。判断口径与侧栏待办一致，
  // 都拿「应到交易日」（后端给的 target）比，不按自然日。
  const sp = r.spot || {};
  const dg = (v) => String(v || "").replace(/[^0-9]/g, "").slice(0, 8);
  const spDay = dg(sp.updated), tgtDay = dg(r.target);
  const spOld = !!(spDay && tgtDay && spDay < tgtDay);
  $("#uniMeta").innerHTML = "全市场 <b>" + r.total + "</b> 只 · 已缓存行情 <b>" + r.cached +
    "</b> 只 · 名单更新 " + r.updated +
    " · 实时价 " + (sp.updated ? String(sp.updated).slice(5, 16) : "未抓取") +
    " · 当前筛选 " + r.filtered + " 只" +
    (r.fresh ? "" : ' <span style="color:var(--amber)">（名单落后 ' +
      (r.lag == null ? "若干" : r.lag) + ' 个交易日，建议刷新）</span>') +
    (spOld ? ' <span style="color:var(--amber)">（实时价是旧价，建议刷新）</span>' : "");
  $("#uniBody").innerHTML = r.rows.length ? r.rows.map((x) =>
    "<tr class='rowlink' data-code='" + x.code + "' title='点击查看走势'><td><input type='checkbox' class='uniCk' data-code='" + x.code + "'" +
    (UNI.checked.has(x.code) ? " checked" : "") + "></td><td>" + x.code + "</td><td>" + esc(x.name) +
    "</td><td>" + esc(x.market) + "</td><td>" + (x.price === "" ? "-" : esc(x.price)) +
    "</td><td class='" + (x.pct === "" ? "" : signCls(Number(x.pct))) + "'>" +
    (x.pct === "" ? "-" : signed(Number(x.pct))) +
    "</td><td><span class='pill " + (x.cached ? "yes" : "no") + "'>" +
    (x.cached ? "已缓存" : "未缓存") + "</span></td></tr>").join("")
    : '<tr><td colspan="7" style="text-align:center;color:var(--ink3);padding:24px">没有匹配的股票；若名单尚未生成，请点「刷新名单」</td></tr>';
  const from = r.filtered ? r.offset + 1 : 0, to = Math.min(r.offset + r.limit, r.filtered);
  $("#uniPage").textContent = "第 " + from + "–" + to + " 条 / 共 " + r.filtered + " 条";
  $("#btnUniPrev").disabled = r.offset <= 0;
  $("#btnUniNext").disabled = r.offset + r.limit >= r.filtered;
  $("#uniCheckAll").checked = false;
}
async function uniApply(mode, checkedOnly) {
  if (checkedOnly) {
    const codes = Array.from(UNI.checked);
    if (!codes.length) { toast("请先勾选要加入的股票", "err"); return; }
    const rows = $$("#uniBody .uniCk:checked").map((c) => {
      const tr = c.closest("tr");
      return c.dataset.code + " " + (tr.cells[2].textContent || "");
    });
    sysTaskStart("uni-import", "加入股票池 " + codes.length + " 只");
    const r = await post("/api/watchlist", { action: "import", mode: mode, text: rows.join("\n") });
    sysTaskEnd("uni-import");
    if (!r.ok) { toast(r.error || "加入失败", "err"); return; }
    UNI.checked.clear();
    await loadPool(); updateStatus(); await uniLoad();
    toast("已加入 " + r.added + " 只，股票池共 " + r.total + " 只", "ok");
    return;
  }
  const doApply = async () => {
    const p = uniParams();
    sysTaskStart("uni-apply", "写入股票池");
    const r = await post("/api/universe/apply", { mode: mode, q: p.q, market: p.market,
                                                 cached_only: $("#uniCached").checked, cap: 6000 });
    sysTaskEnd("uni-apply");
    if (!r.ok) { toast(r.error || "操作失败", "err"); return; }
    await loadPool(); updateStatus(); await uniLoad();
    toast("已" + (mode === "replace" ? "替换" : "加入") + "，股票池现有 " + r.count + " 只", "ok");
  };
  if (mode === "replace") return twoStep($("#btnUniReplace"), "确认替换", doApply);
  return doApply();
}
async function uniRefresh() {
  twoStep($("#btnUniRefresh"), "确认刷新", async () => {
    sysTaskStart("uni-refresh", "刷新全 A 名单");
    const r = await post("/api/universe/refresh", {});
    sysTaskEnd("uni-refresh");
    if (!r.ok) { toast(r.error || "刷新失败", "err"); return; }
    toast(r.source + " · " + r.fetched + " 只" + (r.warning ? "｜" + r.warning : ""), "ok");
    UNI.offset = 0; UNI.checked.clear();
    await uniLoad();
    uniRunsLoad();
    sideTodoLoad();
  });
}
const BULK = { poll: null };

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + " 秒";
  if (sec < 3600) return Math.floor(sec / 60) + " 分" + (sec % 60 ? " " + (sec % 60) + " 秒" : "");
  return (sec / 3600).toFixed(1) + " 小时";
}

function renderBulk(s) {
  const chip = $("#chipBulk");   // 若旧的下载徽标存在（保留兼容），否则为 null
  const total = s.total || 0, done = s.done || 0;
  const pct = total ? Math.min(100, Math.round(done / total * 100)) : 0;
  const active = !!s.running;

  // 进度条 DOM 已移除：任务进度统一由顶栏状态灯显示（蓝环 + 悬停明细）。
  // 这里只保留「一行文字状态」，它是数据本身（已缓存多少只），不是进度条。

  if (chip) {
    chip.hidden = !active;
    if (active) chip.textContent = "行情下载 " + pct + "%";
  }
  const el = $("#bulkStatus");
  if (el) {
    const parts = [];
    if (active) {
      parts.push("运行中 " + done + " / " + total + " 只");
      parts.push("成功 " + (s.ok_count || 0));
      parts.push("失败 " + (s.fail || 0));
      if (s.workers > 1) parts.push(s.workers + " 进程");
      if (s.eta > 0) parts.push("剩余约 " + fmtDur(s.eta));
    } else {
      parts.push(s.message || "空闲");
    }
    parts.push("已缓存 " + (s.cached_total || 0) + " / " + (s.universe_total || 0) +
               " 只（" + (s.cover || 0) + "%）");
    el.textContent = parts.join(" · ");
  }
}
function bulkPoll(s) {
  if (!s || !s.running || BULK.poll) return;
  BULK.poll = setInterval(async () => {
    const t = await api("/api/bulk");
    if (!t.ok) return;
    renderBulk(t);
    if (!t.running) {
      clearInterval(BULK.poll); BULK.poll = null;
      await uniLoad(); updateStatus(); uniRunsLoad(); sideTodoLoad();
      const bad = t.fail || 0;
      toast("行情更新" + (bad ? "结束（有失败）" : "完成") + "：成功 " + (t.ok_count || 0) +
        " 只 · 失败 " + bad + " 只 · 新增 " + (t.rows || 0) + " 行", bad ? "err" : "ok");
    }
  }, 1500);
}
async function bulkStatus() {
  const s = await api("/api/bulk");
  if (!s || !s.ok) return s;
  renderBulk(s);
  bulkPoll(s);
  return s;
}
async function bulkAskFull() {
  const s = await api("/api/bulk");
  if (s && s.ok && s.running) {
    toast("已有下载任务在运行，进度见下方进度条", "err");
    renderBulk(s); bulkPoll(s);
    return;
  }
  const total = (s && s.universe_total) || 0, cached = (s && s.cached_total) || 0;
  const left = Math.max(0, total - cached);
  const mins = Math.round(left * 1.2 / 60);
  $("#bulkConfirmText").innerHTML = "将补全全部 <b>" + total + "</b> 只：已有缓存 <b>" +
    cached + "</b> 只（只补最新交易日），剩余 <b>" + left + "</b> 只需完整下载，约 " +
    (mins >= 60 ? (mins / 60).toFixed(1) + " 小时" : mins + " 分钟") +
    "。期间可随时停止，已下载的不会重下。";
  $("#bulkConfirm").hidden = false;
}
async function bulkStart(mode) {
  const lim = Number($("#bulkLimit").value) || 0;
  const wk = Math.max(1, Math.min(8, Number($("#bulkWorkers").value) || 6));
  const r = await post("/api/bulk", { mode: mode, limit: lim || null, scope: "all", workers: wk });
  if (!r.ok) {
    toast(r.error || "启动失败", "err");
    if (r.total) { renderBulk(r); bulkPoll(r); }   // 把真实状态摊开，而不是静默失败
    return;
  }
  toast((mode === "full" ? "全量补全" : "增量更新") + "已启动：共 " + (r.started || 0) +
    " 只 · " + (r.workers || 1) + " 进程并行", "ok");
  // 立刻挂出 0% 占位，避免「点了没反应」的空窗
  renderBulk({ running: true, mode: mode, done: 0, total: r.started || 0, ok: 0, fail: 0,
    skip: 0, rows: 0, workers: r.workers || 1, message: "启动中…" });
  setTimeout(bulkStatus, 600);
}
async function uniSpot() {
  sysTaskStart("spot", "刷新全市场实时价");
  const r = await post("/api/spot", { action: "refresh" });
  sysTaskEnd("spot");
  if (!r.ok) { toast(r.error || "刷新失败", "err"); return; }
  toast("实时价已更新：" + r.count + " 只 · " + (r.updated || ""), "ok");
  await uniLoad();
  uniRunsLoad();
  sideTodoLoad();
}

/* 全 A 名单右侧的更新历史：行情下载 / 名单刷新 / 实时价 / 流水线 */
const UNI_RUN_KINDS = { quote: 1, universe: 1, spot: 1, pipeline: 1 };
async function uniRunsLoad() {
  const r = await api("/api/data/runs?limit=40");
  if (!r || !r.ok) return;
  const runs = (r.runs || []).filter((x) => UNI_RUN_KINDS[x.kind]).slice(0, 12);
  hubRuns(runs, { body: "uniRunsBody", empty: "uniRunsEmpty" });
  const meta = $("#uniHistMeta");
  if (meta) {
    const l = runs[0];
    meta.textContent = l
      ? "最近 " + (l.started || "").slice(5, 16) + " · " + hubKind(l.kind, l.level) +
        " · " + (l.ok === null ? "进行中" : l.ok ? "成功" : "失败") + " · 近 " + runs.length + " 条"
      : "行情下载与名单刷新记录，最新在前。";
  }
}

/* ---------------- 实盘复盘（emtl 只读接入 + 本地账本 + 历史回流） ---------------- */
const RT = { loaded: false, data: null, busy: false, syncSeen: "", timer: null,
  ai: null, aiBusy: { health: false }, aiTried: {} };

function rtMoney(v, d) {
  const n = Number(v || 0);
  return (n < 0 ? "-" : "") + "¥" + Math.abs(n).toLocaleString("zh-CN", {
    minimumFractionDigits: d === undefined ? 2 : d, maximumFractionDigits: d === undefined ? 2 : d
  });
}
function rtPnl(v) {                       // 盈亏：正数带 +，红涨绿跌
  const n = Number(v || 0);
  return (n > 0 ? "+" : n < 0 ? "-" : "") + "¥" + Math.abs(n).toLocaleString("zh-CN",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function rtWan(v) {
  const n = Number(v || 0);
  return Math.abs(n) >= 10000 ? (n / 10000).toFixed(2) + " 万" : n.toFixed(0);
}

async function rtStatusOnly() {
  const st = await api("/api/emt/status");
  rtPaintStatus(st);
  return st;
}

function rtPaintStatus(st) {
  const dot = $("#rtDot"), who = $("#rtWho"), when = $("#rtWhen");
  if (!dot) return;
  st = st || {};
  let cls = "", name = "", hint = "";
  if (!st.configured) {
    cls = ""; name = "未配置账户";
    hint = "点「账户设置」填东财证券的账号密码，只读取资金与持仓，不提供下单";
  } else if (st.last_ok === false) {
    cls = "err"; name = st.user + " · 上次同步失败";
    hint = (st.last_msg || "未知原因") + "　（" + (st.last_sync || "") + "）";
  } else if (st.last_sync) {
    cls = "ok"; name = st.user + " · 已连接";
    hint = "上次同步 " + st.last_sync + " · 账本 " + (st.ledger_days || 0) + " 个交易日";
  } else {
    cls = "warn"; name = st.user + " · 待同步";
    hint = "账号已配置，还没拉过快照。点右侧「同步账户」";
  }
  // 盘中自动刷新单独说明：让用户知道页面里的数字为什么会自己变
  if (st.configured && st.auto_refresh) {
    const rf = "盘中每 " + (st.refresh_min || 5) + " 分钟自动刷新" +
      (st.in_session ? "" : "（当前非交易时段）");
    hint += " · " + rf;
  }
  dot.className = "rt-dot " + cls;
  who.textContent = name;
  when.textContent = hint;
  when.title = st.refresh_state ? ("刷新状态：" + st.refresh_state) : "";
  const btn = $("#btnRtSync");
  if (btn) btn.textContent = st.configured ? "同步账户" : "先配置账号";
  rtPaintHist(st.history);
}

function rtRefreshPaint(st) {
  st = st || {};
  const chk = $("#rtAutoRefresh"), sel = $("#rtRefreshMin");
  const state = $("#rtRefreshState"), msg = $("#rtRefreshMsg");
  if (chk) chk.checked = !!st.auto_refresh;
  if (sel && st.refresh_min) {
    const v = String(st.refresh_min);
    if (!Array.from(sel.options || []).some((o) => o.value === v)) sel.add(new Option(v, v));
    sel.value = v;
  }
  if (state) {
    state.textContent = !st.auto_refresh ? "已关闭"
      : (st.in_session ? (st.refresh_state || "交易时段，等待刷新") : "非交易时段，暂不刷新");
  }
  if (msg) {
    if (!st.configured) msg.textContent = "先配置账号，交易时段才会自动刷新。";
    else if (st.refresh_at) {
      msg.textContent = "上次自动刷新 " + st.refresh_at +
        (st.refresh_ok === false ? " · 失败：" + (st.refresh_msg || "未知原因") : "");
    } else {
      msg.textContent = "还没有自动刷新记录。";
    }
  }
}

function rtPaintHist(h) {
  const el = $("#rtHist");
  if (!el) return;
  h = h || {};
  const n = h.fills || 0;
  if (!n) {
    el.textContent = "历史成交 —";
    el.className = "rt-hist dim";
    return;
  }
  el.textContent = "历史成交 " + n + " 笔 · " + (h.first_day || "").slice(2) + " ~ " +
    (h.last_day || "").slice(2);
  el.className = "rt-hist";
  el.title = "成交 " + n + " 笔 / 委托 " + (h.orders || 0) + " 笔 / 流水 " + (h.flows || 0) +
    " 条" + (h.at ? "\n最近拉取 " + h.at : "") + (h.msg ? "\n" + h.msg : "");
}

async function rtLoad() {
  if (RT.busy) return;
  RT.busy = true;
  if (!RT.loaded) loading(true, "正在读取实盘账本…");
  try {
    const b = await api("/api/emt/board?days=180");
    RT.busy = false;
    loading(false);
    RT.loaded = true;
    if (!b || b.ok === false) { toast((b && b.error) || "账本读取失败", "err"); return; }
    RT.data = b;
    RT.syncSeen = ((b.status || {}).last_sync) || b.at || "";
    rtPaintStatus(b.status);
    rtRefreshPaint(b.status);
    rtRender(b);
  } catch (e) {
    RT.busy = false;
    loading(false);
    toast("账本读取失败：" + e, "err");
  }
}

function rtRender(b) {
  rtKpi(b);
  rtChart(b);
  rtAttr(b);
  rtPos(b);
  rtTrades(b);
  rtFlow(b);
  rtOq(b);
  rtVs(b);
  rtPaintHist(b.history || (b.status || {}).history);
  rtAiRender(b);
}

/* ① 账户总览 */
function rtKpi(b) {
  const box = $("#rtKpis");
  if (!box) return;
  if (b.empty) {
    box.innerHTML = '<div class="kpi" style="grid-column:1/-1"><div class="k">还没有实盘数据</div>' +
      '<div class="n" style="font-size:14px;font-weight:600">先同步账户</div>' +
      '<div class="s">点「同步账户」拉资金与持仓，再点「拉取历史」补成交与流水。</div></div>';
    return;
  }
  const o = b.overview || {}, d = b.delta || {}, c = b.curve || {};
  if (!b.has_equity) {
    box.innerHTML = '<div class="kpi" style="grid-column:1/-1"><div class="k">只有成交流水，没有资金快照</div>' +
      '<div class="n" style="font-size:14px;font-weight:600">点「同步账户」拉一次快照</div>' +
      '<div class="s">历史成交补不了总资产与持仓 —— 资金曲线、仓位、集中度都要靠账户快照。</div></div>';
    return;
  }
  const dd = (k) => {
    const x = (d[k] || {});
    if (x.chg === null || x.chg === undefined) return "无上一账本日可比";
    return "较上次 " + rtPnl(x.chg) + (x.pct === null || x.pct === undefined ? "" : " (" + signed(x.pct) + ")");
  };
  const items = [
    ["总资产", rtMoney(o.total), dd("total"), ""],
    ["当日盈亏", rtPnl(o.day_pnl), (o.total ? signed(o.day_pnl / o.total * 100) : "—") + " · 东财口径", signCls(o.day_pnl)],
    ["累计盈亏", rtPnl(o.total_pnl), "本账户开户以来", signCls(o.total_pnl)],
    ["持仓市值", rtMoney(o.mv), "仓位 " + num(o.exposure, 1) + "%", ""],
    ["可用资金", rtMoney(o.avail), "可取 " + rtMoney(o.withdraw, 0), ""],
    ["区间表现", signed(c.ret),
      c.ret_twr !== null && c.ret_twr !== undefined
        ? "剔除出入金 " + signed(c.ret_twr) + " · " + c.days + " 个账本日"
        : c.days + " 个账本日 · 最大回撤 " + num(c.mdd, 2) + "%", signCls(c.ret)]
  ];
  box.innerHTML = items.map((x) =>
    '<div class="kpi"><div class="k">' + x[0] + '</div><div class="n ' + x[3] + '">' + x[1] +
    '</div><div class="s">' + esc(x[2]) + "</div></div>").join("");
}

/* ② 资金曲线 + 回撤 */
function rtChart(b) {
  const el = document.getElementById("rtEquity");
  if (!el) return;
  const pts = (b.curve && b.curve.points) || [];
  const hint = $("#rtCurveHint"), foot = $("#rtCurveFoot");
  if (S.charts.rtEquity) { S.charts.rtEquity.dispose(); delete S.charts.rtEquity; }
  if (!pts.length) {
    el.innerHTML = '<div class="rt-empty">还没有账本日。同步一次账户，这里就会开始画曲线。</div>';
    if (hint) hint.textContent = "账本还空着";
    if (foot) foot.innerHTML = "";
    return;
  }
  el.innerHTML = "";
  if (hint) hint.textContent = pts[0].date + " ~ " + pts[pts.length - 1].date + " · " + pts.length + " 个点";
  const c = chart("rtEquity");
  c.setOption({
    animation: pts.length > 60 ? false : true,
    grid: { left: 58, right: 50, top: 16, bottom: 24 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", label: { backgroundColor: "#525c6b" } },
      formatter: (ps) => {
        const i = ps[0].dataIndex, p = pts[i];
        return "<b>" + p.date + "</b><br>总资产 " + rtMoney(p.total) +
          "<br>当日盈亏 " + rtPnl(p.day_pnl) +
          (p.net_in_flow ? "<br>当日净入金 " + rtPnl(p.net_in_flow) : "") +
          (p.ret_twr !== null && p.ret_twr !== undefined
            ? "<br>剔除出入金累计 " + signed(p.ret_twr) + "%" : "") +
          "<br>仓位 " + num(p.exposure, 1) + "% · 回撤 " + num(p.dd, 2) + "%";
      }
    },
    xAxis: {
      type: "category", data: pts.map((p) => p.date.slice(5)), boundaryGap: false,
      axisLabel: { fontSize: 10, color: "#8a939f" },
      axisLine: { lineStyle: { color: "#e4e8ee" } }, axisTick: { show: false }
    },
    yAxis: [
      {
        type: "value", scale: true, splitNumber: 4,
        axisLabel: { fontSize: 10, color: "#8a939f", formatter: (v) => rtWan(v) },
        splitLine: { lineStyle: { color: "#eef1f5" } }
      },
      {
        type: "value", max: 0, splitNumber: 3, position: "right",
        axisLabel: { fontSize: 10, color: "#12996a", formatter: "{value}%" },
        splitLine: { show: false }
      }
    ],
    series: [
      {
        name: "总资产", type: "line", data: pts.map((p) => p.total),
        symbol: "none", smooth: false, lineStyle: { width: 2, color: "#1c5fd8" },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "rgba(28,95,216,.16)" }, { offset: 1, color: "rgba(28,95,216,0)" }
          ])
        }
      },
      {
        name: "回撤", type: "line", yAxisIndex: 1, data: pts.map((p) => p.dd || 0),
        symbol: "none", lineStyle: { width: 1, color: "#12996a", type: "dashed", opacity: 0.85 }
      }
    ]
  });
  if (foot) {
    const x = b.curve;
    const rows = [
      ["起始", x.start + " · " + rtMoney(x.base, 0)],
      ["最新", x.end + " · " + rtMoney(x.last, 0)],
      ["总资产涨跌", '<b class="' + signCls(x.ret) + '">' + signed(x.ret) + "</b>"]
    ];
    if (x.ret_twr !== null && x.ret_twr !== undefined) {
      rows.push(["剔除出入金", '<b class="' + signCls(x.ret_twr) + '">' + signed(x.ret_twr) + "</b>"]);
      rows.push(["净入金", '<b class="' + signCls(x.net_in) + '">' + rtPnl(x.net_in) + "</b>"]);
    }
    rows.push(["最大回撤", '<b>' + num(x.mdd, 2) + "%</b>" +
      (x.mdd_from ? "（" + x.mdd_from.slice(5) + " → " + x.mdd_to.slice(5) + "）" : "")]);
    rows.push(["日盈亏累加", '<b class="' + signCls(x.pnl_cum) + '">' + rtPnl(x.pnl_cum) + "</b>"]);
    rows.push(["最好 / 最差", (x.best ? x.best.date.slice(5) + " " + rtPnl(x.best.pnl) : "—") +
      " / " + (x.worst ? x.worst.date.slice(5) + " " + rtPnl(x.worst.pnl) : "—")]);
    foot.innerHTML = rows.map((p) => "<span>" + p[0] + " " + p[1] + "</span>").join("");
  }
}

/* ③ 当日盈亏归因 */
function rtAttr(b) {
  const box = $("#rtAttr"), dt = $("#rtAttrDate");
  if (!box) return;
  if (b.empty) { box.innerHTML = '<p class="note" style="margin:0">等待账本。</p>'; return; }
  if (!b.has_equity) {
    box.innerHTML = '<p class="note" style="margin:0">没有资金快照，算不出当日盈亏。' +
      "东财的账户当日盈亏只有登录后才拿得到，历史成交里没有这个数。</p>";
    if (dt) dt.textContent = b.date || "";
    return;
  }
  const a = b.attribution || {};
  if (dt) dt.textContent = b.date ? b.date + (b.snaps_today > 1 ? " · 当日 " + b.snaps_today + " 次快照" : "") : "";
  const items = (a.items || []).filter((x) => x.day_pnl !== 0).slice(0, 8);
  if (!items.length) {
    box.innerHTML = '<p class="note" style="margin:0">这一天没有持仓，或持仓当日无盈亏变化。</p>';
    return;
  }
  const maxAbs = Math.max(...items.map((x) => Math.abs(x.day_pnl)), 1);
  box.innerHTML = items.map((x) => {
    const w = Math.abs(x.day_pnl) / maxAbs * 100;
    return '<div class="rt-att">' +
      '<div class="nm">' + esc(x.name) + '<i>' + esc(x.code) + '</i></div>' +
      '<div class="vl ' + signCls(x.day_pnl) + '">' + rtPnl(x.day_pnl) + "</div>" +
      '<div class="rt-att-bar"><i class="' + (x.day_pnl > 0 ? "up" : "down") +
      '" style="width:' + w.toFixed(1) + '%;' + (x.day_pnl > 0 ? "left:0" : "right:0") + '"></i></div>' +
      "</div>";
  }).join("") +
    '<div class="rt-att-total"><span>账户当日</span><b class="' + signCls(a.day_pnl) + '">' + rtPnl(a.day_pnl) + "</b></div>" +
    '<div class="rt-curve-foot" style="border:0;padding-top:2px;margin-top:0">' +
    [["持仓贡献", '<b class="' + signCls(a.held) + '">' + rtPnl(a.held) + "</b>"],
     ["当日费用", '<b>' + rtMoney(a.fee) + "</b>"],
     ["未解释", '<b class="' + signCls(a.other) + '">' + rtPnl(a.other) + "</b>"],
     ["成交", '<b>' + (a.traded || 0) + " 笔 / " + rtMoney(a.turnover, 0) + "</b>"]
    ].map((p) => "<span>" + p[0] + " " + p[1] + "</span>").join("") +
    "</div>";
}

/* ④ 持仓明细 + 行业分布 */
function rtPos(b) {
  const body = $("#rtPosBody"), hint = $("#rtPosHint"), ind = $("#rtInd");
  if (!body) return;
  const p = b.positions || {};
  const items = p.items || [];
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="10" style="color:var(--ink3);padding:18px 9px">' +
      (b.has_equity ? "当前空仓。" : "没有资金快照，持仓未知（账本里只有历史成交）。") + "</td></tr>";
    if (hint) hint.textContent = "";
    if (ind) ind.innerHTML = "";
    return;
  }
  if (hint) {
    hint.textContent = items.length + " 只 · 市值 " + rtMoney(p.mv, 0) +
      " · 前三大 " + num(p.top3, 1) + "% · 有效分散 " + num(p.eff_n, 1) + " 只（1/HHI）";
  }
  body.innerHTML = items.map((x) =>
    "<tr>" +
    "<td>" + esc(x.code) + "</td>" +
    "<td>" + esc(x.name) + "</td>" +
    '<td class="num">' + (x.qty || 0).toLocaleString() + "</td>" +
    '<td class="num">' + num(x.cost, 3) + "</td>" +
    '<td class="num">' + num(x.price, 3) + "</td>" +
    '<td class="num">' + rtMoney(x.mv, 0) + "</td>" +
    '<td class="num"><div class="rt-share"><div class="bar"><i style="width:' +
    Math.min(100, x.mv_share || 0).toFixed(1) + '%"></i></div><span>' + num(x.mv_share, 1) + "%</span></div></td>" +
    '<td class="num ' + signCls(x.day_pnl) + '">' + rtPnl(x.day_pnl) + "</td>" +
    '<td class="num ' + signCls(x.pnl) + '">' + rtPnl(x.pnl) +
    '<br><span style="font-size:10.5px;color:var(--ink3)">' + signed(x.pnl_pct) + "</span></td>" +
    "<td>" + esc(x.industry || "未分类") + "</td>" +
    "</tr>").join("") +
    '<tr class="sum"><td colspan="5">合计</td><td class="num">' + rtMoney(p.mv, 0) + "</td>" +
    '<td class="num">100%</td><td class="num ' + signCls(p.day_pnl) + '">' + rtPnl(p.day_pnl) + "</td>" +
    '<td class="num ' + signCls(p.pnl) + '">' + rtPnl(p.pnl) + "</td><td></td></tr>";

  if (ind) {
    const rows = (p.industry || []).slice(0, 8);
    const max = Math.max(...rows.map((x) => x.share), 1);
    ind.innerHTML = rows.map((x) =>
      '<div class="rt-ind-row"><span class="nm">' + esc(x.name) + '</span>' +
      '<span class="bar"><i style="width:' + (x.share / max * 100).toFixed(1) + '%"></i></span>' +
      '<span class="vl">' + num(x.share, 1) + "% · " + x.n + " 只</span></div>").join("");
  }
}

/* ⑤ 交易复盘 */
function rtTrades(b) {
  const box = $("#rtTradeKpis"), body = $("#rtClosedBody"), hint = $("#rtTradeHint");
  if (!box) return;
  const t = b.trades || {};
  const closed = t.closed || 0;
  if (!t.fills) {
    box.innerHTML = '<div class="kpi" style="grid-column:1/-1"><div class="k">还没有成交流水</div>' +
      '<div class="s">成交来自「拉取历史」（query_history_trades）与每日同步；还没有成交就先拉一次历史。</div></div>';
    if (body) body.innerHTML = "";
    if (hint) hint.textContent = "";
    return;
  }
  if (hint) {
    const rf = t.real_fee_n || 0, ef = t.fee_est_n || 0;
    hint.textContent = "累计 " + t.fills + " 笔成交 · 配对出 " + closed + " 笔平仓" +
      (rf ? " · 真实费用 " + rf + " 笔" : "") +
      (ef ? " · 按费率估算 " + ef + " 笔" : "") +
      (t.first_day ? " · " + t.first_day + " ~ " + t.last_day : "");
  }
  const items = [
    ["胜率", closed ? num(t.win_rate, 0) + "%" : "—", closed + " 笔平仓里 " + (t.win_rate ? Math.round(t.win_rate * closed / 100) : 0) + " 笔盈利", ""],
    ["盈亏比", t.pl_ratio === null || t.pl_ratio === undefined ? "—" : num(t.pl_ratio, 2), "平均盈利 ÷ 平均亏损", ""],
    ["已实现盈亏", rtPnl(t.realized), "含摊薄后的费用", signCls(t.realized)],
    ["平均持有", num(t.avg_hold_days, 1) + " 天", "从买入到卖出的自然日", ""],
    ["单笔最大盈利", t.top_win ? rtPnl(t.top_win.pnl) : "—", t.top_win ? t.top_win.name : "", t.top_win ? signCls(t.top_win.pnl) : ""],
    ["单笔最大亏损", t.top_loss ? rtPnl(t.top_loss.pnl) : "—", t.top_loss ? t.top_loss.name : "", t.top_loss ? signCls(t.top_loss.pnl) : ""],
    ["交易费用", rtMoney(t.closed_fee !== undefined ? t.closed_fee : t.fee),
      t.real_fee_n ? "平仓摊薄 · " + t.real_fee_n + " 笔用真实费用"
        : "平仓摊薄 · 按费率估算（委托没带回佣金）", ""],
    ["年化换手", t.turnover_rate === null || t.turnover_rate === undefined ? "样本不足" :
      num(t.turnover_rate, 0) + "%", t.turnover_rate === null || t.turnover_rate === undefined
        ? "只累积了 " + t.sample_days + " 个交易日，不外推" : "成交额 ÷ 平均总资产 × 244", ""]
  ];
  box.innerHTML = items.map((x) =>
    '<div class="kpi"><div class="k">' + x[0] + '</div><div class="n ' + x[3] + '" style="font-size:16px">' +
    x[1] + '</div><div class="s">' + esc(x[2]) + "</div></div>").join("");

  if (!body) return;
  const list = t.closed_list || [];
  body.innerHTML = list.length ? list.map((x) =>
    "<tr>" +
    "<td>" + esc(x.sell_date) + "</td>" +
    "<td>" + esc(x.code) + "</td>" +
    "<td>" + esc(x.name) + "</td>" +
    '<td class="num">' + (x.qty || 0).toLocaleString() + "</td>" +
    '<td class="num">' + num(x.buy_price, 3) + "</td>" +
    '<td class="num">' + num(x.sell_price, 3) + "</td>" +
    '<td class="num">' + x.days + " 天</td>" +
    '<td class="num ' + signCls(x.pnl) + '">' + rtPnl(x.pnl) + "</td>" +
    '<td class="num ' + signCls(x.pnl_pct) + '">' + signed(x.pnl_pct) + "</td>" +
    "</tr>").join("")
    : '<tr><td colspan="9" style="color:var(--ink3);padding:16px 9px">还没有平仓记录 —— 只有卖出才会产生「平仓笔」，买入未卖的不计入胜率。</td></tr>';
}

/* ⑥ 资金流水 */
function rtFlow(b) {
  const box = $("#rtFlowKpis"), body = $("#rtFlowBody"), hint = $("#rtFlowHint");
  if (!box) return;
  const c = b.cashflow || {};
  if (c.empty) {
    box.innerHTML = '<div class="kpi" style="grid-column:1/-1"><div class="k">没有资金流水</div>' +
      '<div class="n" style="font-size:14px;font-weight:600">点「拉取历史」补 query_funds_flow</div>' +
      '<div class="s">' + esc(c.note || "") + "</div></div>";
    if (body) body.innerHTML = "";
    if (hint) hint.textContent = "出入金 / 分红 / 利息 / 税";
    return;
  }
  if (hint) {
    hint.textContent = c.n + " 条 · " + (c.first_day || "") + " ~ " + (c.last_day || "");
  }
  const items = [
    // 「全量」两个字必须写清楚：资金曲线上的净入金只统计账本区间内，
    // 两个数不一样不是 bug，是口径不同，不标注会被当成算错。
    // 小字只放一段短信息，避免在 4 列 KPI 里被拆成两行；区间口径在资金曲线下方
    ["净入金 · 全量", rtPnl(c.net_in), "累计转入 " + rtWan(c.in_sum), signCls(c.net_in)],
    ["现金分红", rtMoney(c.dividend), "红利入账（不含送转股）", ""],
    ["利息", rtMoney(c.interest), "结息 / 利息归本", ""],
    ["红利税", rtMoney(c.tax), "按持股期限差别征收", ""]
  ];
  box.innerHTML = items.map((x) =>
    '<div class="kpi"><div class="k">' + x[0] + '</div><div class="n ' + x[3] +
    '" style="font-size:16px">' + x[1] + '</div><div class="s">' + esc(x[2]) + "</div></div>").join("");

  if (!body) return;
  const rows = (c.rows || []).slice(0, 30);
  const cls = { in: "净流入", out: "净流出", dividend: "分红", interest: "利息", tax: "税", fee: "费用" };
  body.innerHTML = rows.length ? rows.map((x) =>
    "<tr>" +
    '<td style="width:64px">' + esc(x.date.slice(5)) + "</td>" +
    "<td>" + esc(x.kind || cls[x.class] || "—") +
    (x.name ? ' <span style="color:var(--ink3);font-size:10.5px">' + esc(x.name) + "</span>" : "") + "</td>" +
    '<td class="num ' + signCls(x.amount) + '">' + rtPnl(x.amount) + "</td>" +
    '<td class="num">' + (x.balance ? rtMoney(x.balance, 0) : "—") + "</td>" +
    "</tr>").join("")
    : '<tr><td colspan="4" style="color:var(--ink3);padding:16px 9px">流水为空。</td></tr>';
}

/* ⑦ 委托质量 */
function rtOq(b) {
  const box = $("#rtOqKpis"), note = $("#rtOqNote"), hint = $("#rtOqHint");
  if (!box) return;
  const q = b.order_quality || {};
  if (q.empty) {
    box.innerHTML = '<div class="kpi" style="grid-column:1/-1"><div class="k">没有历史委托</div>' +
      '<div class="n" style="font-size:14px;font-weight:600">点「拉取历史」补 query_history_orders</div>' +
      '<div class="s">' + esc(q.note || "") + "</div></div>";
    if (note) note.textContent = "";
    if (hint) hint.textContent = "撤单率 / 成交率 / 滑点";
    const b0 = $("#rtOqBody");
    if (b0) b0.innerHTML = "";
    return;
  }
  if (hint) hint.textContent = q.n + " 笔委托 · " + (q.first_day || "") + " ~ " + (q.last_day || "");
  const items = [
    ["委托笔数", q.n + " 笔", "成 " + q.filled + " / 撤 " + q.canceled, ""],
    // 撤单率与滑点用中性色：它们不是涨跌，染成红绿会被误读成盈亏
    ["撤单率", num(q.cancel_rate, 0) + "%", "已撤 ÷ 委托", ""],
    ["成交率", num(q.fill_rate, 0) + "%", "按笔；按量 " + num(q.qty_fill_rate, 0) + "%", ""],
    ["平均滑点", q.slippage === null || q.slippage === undefined ? "—" : (q.slippage > 0 ? "+" : "") + num(q.slippage, 3) + "%",
      (q.slippage_n || 0) + " 笔有成交", ""]
  ];
  box.innerHTML = items.map((x) =>
    '<div class="kpi"><div class="k">' + x[0] + '</div><div class="n ' + x[3] +
    '" style="font-size:16px">' + x[1] + '</div><div class="s">' + esc(x[2]) + "</div></div>").join("");
  if (note) note.textContent = q.note || "";

  const body = $("#rtOqBody");
  if (!body) return;
  const rows = q.recent || [];
  body.innerHTML = rows.length ? rows.map((x) =>
    "<tr>" +
    '<td style="width:64px">' + esc((x.date || "").slice(5)) + "</td>" +
    "<td>" + esc(x.name || x.code) + "</td>" +
    '<td class="' + (x.side === "买入" ? "buy" : "sell") + '">' + esc(x.side) + "</td>" +
    '<td class="num">' + num(x.price, 2) + "</td>" +
    '<td class="num">' + (x.filled ? num(x.avg, 2) : "—") + "</td>" +
    '<td class="num">' + (x.slip === null || x.slip === undefined ? "—"
      : (x.slip > 0 ? "+" : "") + num(x.slip, 2) + "%") + "</td>" +
    '<td style="color:var(--ink3)">' + esc(x.state) + "</td>" +
    "</tr>").join("")
    : '<tr><td colspan="7" style="color:var(--ink3);padding:16px 9px">没有委托记录。</td></tr>';
}

/* ⑧ 复盘 AI：行为体检 + 日记，本地规则 / AI 双模式 */
function rtSegPaint(id, mode) {
  $$(id + " button[data-mode]").forEach((btn) => {
    const on = btn.dataset.mode === mode;
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function rtLlmLink() {
  return '<button type="button" class="linklike" data-llm-cfg>LLM 设置</button>';
}

function rtDiagList(list, mode) {
  const box = $("#rtDiag");
  if (!box) return;
  if (!list.length) {
    box.innerHTML = '<p class="note" style="margin:0">' +
      (mode === "ai" ? "尚未生成 AI 体检。" : "等待账本。") + "</p>";
    return;
  }
  const label = { risk: "注意", warn: "提示", ok: "正常" };
  box.innerHTML = list.map((x) => {
    const ev = x.evidence !== undefined ? x.evidence : x.detail;
    const check = x.check || "";
    return '<div class="rt-diag-item ' + (x.level || "ok") + '">' +
      '<span class="lv">' + (mode === "ai" ? "AI·" : "") + (label[x.level] || "提示") + "</span>" +
      '<div class="tx"><b>' + esc(x.title || "") + "</b>" +
      (ev ? "<p>" + esc(ev) + "</p>" : "") +
      (check ? '<p class="todo"><i>检查</i>' + esc(check) + "</p>" : "") +
      "</div></div>";
  }).join("");
}

function rtRiskPaint(b) {
  const box = $("#rtRisk");
  if (!box) return;
  const rk = (b && b.risk) || {};
  const dev = (b && b.plan_dev) || {};
  const tr = (b && b.risk_trend) || [];
  const items = rk.items || [];
  if (!items.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;

  const f = (v, d) => (v === null || v === undefined || !isFinite(Number(v)))
    ? "—" : Number(v).toFixed(d === undefined ? 2 : d);
  const usage = Number(rk.usage);
  const over = Number(rk.over) || 0;
  const below = rk.below_ref || [];

  const kpi = '<div class="rt-risk-kpi">' +
    '<span><b>' + (isFinite(usage) ? f(usage, 0) + "%" : "—") + '</b>预算使用率</span>' +
    '<span><b>' + f(rk.total) + '%</b>组合合计</span>' +
    '<span><b>' + f(rk.risk_pct) + '%</b>单笔预算</span>' +
    '<span class="' + (over ? "bad" : "") + '"><b>' + over + '</b>超预算只数</span>' +
    '<span class="' + (below.length ? "bad" : "") + '"><b>' + below.length + '</b>跌破参考位</span>' +
    "</div>";

  // 走势：按最大值归一，超过 100% 用红色；条数少时也保持柱宽均匀
  let chart = "";
  if (tr.length) {
    const max = Math.max(100, ...tr.map((x) => Number(x.usage) || 0));
    const bars = tr.map((x) => {
      const u = Number(x.usage) || 0;
      const h = Math.max(2, Math.min(100, u / max * 100));
      const tip = x.date + " · 使用率 " + f(u, 0) + "% · 合计 " + f(x.total) +
        "% · " + (x.n_pos || 0) + " 只持仓";
      return '<i class="' + (u > 100 ? "over" : "") + '" style="height:' + h + '%" title="' +
        esc(tip) + '"></i>';
    }).join("");
    chart = '<div class="rt-bars">' + bars + "</div>" +
      '<div class="rt-bars-x"><span>' + esc(tr[0].date || "") + "</span><span>" +
      esc(tr[tr.length - 1].date || "") + "</span></div>";
  }

  let plan = "";
  if (dev && dev.empty) {
    plan = "计划-执行 · " + esc(dev.note || "计划生成后还没有成交");
  } else if (dev && dev.since) {
    const r = Number(dev.extra_ratio) || 0;
    plan = "计划-执行 · 计划日 " + esc(dev.since) + " · 计划外成交 <b class=\"" +
      (r >= 40 ? "bad" : "") + "\">" + f(r, 0) + "%</b>（" +
      f(dev.extra_amt, 0) + " / " + f(dev.total_amt, 0) + " 元）· 计划买卖 " +
      (dev.planned_buy || 0) + "/" + (dev.planned_sell || 0) + "，实际 " +
      (dev.done_buy || 0) + "/" + (dev.done_sell || 0);
  } else {
    plan = "计划-执行 · 还没有目标持仓计划；在「目标持仓」生成一次后，这里会对比计划与实际成交。";
  }

  box.innerHTML = kpi + chart + '<div class="rt-plan">' + plan + "</div>";
}

function rtAiRender(b) {
  const ai = b.ai || {};
  RT.ai = ai;
  const modes = ai.modes || { health: "local" };
  rtSegPaint("#rtHealthMode", modes.health || "local");

  const hmode = modes.health || "local";
  const h = ai.health;
  const useAi = hmode === "ai" && h && (h.items || []).length;
  rtDiagList(useAi ? h.items : (ai.local_health || b.diagnose || []), useAi ? "ai" : "local");

  const hn = $("#rtHealthNote");
  if (hn) {
    let html = "";
    if (hmode === "local") {
      html = "<b>本地规则</b> · 命中规则才列出，不走网络；不打分、不做仓位建议。";
    } else if (useAi) {
      html = "<b>AI 体检</b> · " + esc(h.model || "模型") + (h.at ? " · " + esc(h.at) : "") +
        (h.summary ? "<br>" + esc(h.summary) : "") +
        (h.data_note ? "<br>数据口径：" + esc(h.data_note) : "");
    } else if (!(ai.llm || {}).ready) {
      html = '<span class="err">LLM 未启用或未配置</span>，暂时显示本地规则。' + rtLlmLink();
    } else {
      html = "<b>AI 体检</b> · 尚未生成；收盘日结后会自动生成，也可以点「AI 体检」。";
    }
    hn.innerHTML = html;
  }

  const hb = $("#btnRtHealthGen");
  if (hb) {
    hb.hidden = hmode !== "ai";
    hb.textContent = useAi ? "重新体检" : "AI 体检";
    hb.disabled = RT.aiBusy.health || !(ai.llm || {}).ready;
  }

  rtRiskPaint(b);
  rtAiAuto(ai);
}

function rtAiAuto(ai) {
  const day = (RT.data && (RT.data.date || ai.day)) || "";
  const modes = ai.modes || {};
  if (ai.llm && ai.llm.ready) {
    if (modes.health === "ai" && !ai.health && !RT.aiBusy.health &&
        !RT.aiTried[day + "health"]) {
      RT.aiTried[day + "health"] = true;
      setTimeout(() => rtGenerate("health", true), 350);
    }
  }
}

async function rtSetMode(kind, mode) {
  if (mode !== "local" && mode !== "ai") return;
  const r = await post("/api/emt/ai/config", { [kind]: mode });
  if (!r || !r.ok) { toast((r && r.error) || "切换失败", "err"); return; }
  const ai = RT.ai || {};
  ai.modes = Object.assign({}, ai.modes, { [kind]: mode });
  ai.llm = r.llm || ai.llm;
  RT.ai = ai;
  if (RT.data) RT.data.ai = ai;
  rtAiRender(RT.data || {});
  const m2 = ai.modes || {};
  rtSegPaint("#hubAiHealth", m2.health || "local");
  if (mode === "ai") {
    if ((r.llm || {}).ready) {
      if (kind === "health" && !ai.health) rtGenerate("health", true);
    } else {
      toast("LLM 未启用：先到「数据中枢 → AI 服务」填端点并启用", "info");
    }
  }
}

/* 只生成行为体检：复盘日记已独立成页（左侧「复盘日记」），这里不再重复一份 */
async function rtGenerate(kind, auto) {
  if (kind !== "health") return;
  if (!RT.data || RT.data.empty) { toast("还没有账本，先同步一次", "err"); return; }
  if (RT.aiBusy.health) return;
  const ai = RT.ai || {};
  const btn = $("#btnRtHealthGen");
  const oldText = btn ? btn.textContent : "";

  RT.aiBusy.health = true;
  if (btn) { btn.disabled = true; btn.textContent = "生成中…"; }
  try {
    const r = await post("/api/emt/ai/generate",
                         { kind: "health", day: (RT.data.date || ""), force: false });
    if (!r || !r.ok) {
      toast((r && r.error) || "生成失败", "err");
      return;
    }
    if (r.busy) { toast(r.error || "正在生成，请稍候", "info"); return; }
    if (r.mode === "ai" && (r.items || []).length) {
      ai.health = { items: r.items, summary: r.summary || "", data_note: r.data_note || "",
        model: r.model || "", at: r.at || "" };
      toast(auto ? "AI 体检已生成" : "AI 体检完成", "ok");
    } else {
      ai.health = null;
      toast(r.warning || "AI 不可用，已回退本地规则", "info");
    }
    if (RT.data) RT.data.ai = ai;
  } catch (e) {
    toast("生成失败：" + e, "err");
  } finally {
    RT.aiBusy.health = false;
    if (btn) { btn.disabled = false; btn.textContent = oldText; }
    rtAiRender(RT.data || {});
  }
}

/* ⑦ 与选股系统对照 */
function rtVs(b) {
  const box = $("#rtVs"), hint = $("#rtVsHint");
  if (!box) return;
  const v = b.vs_picks || {};
  if (v.empty) {
    box.innerHTML = '<p class="note" style="margin:0">' + esc(v.note || "还没有选股记录，先在「选股工作台」跑一次。") + "</p>";
    if (hint) hint.textContent = "";
    return;
  }
  if (!v.ok) {
    box.innerHTML = '<p class="note" style="margin:0">对照失败：' + esc(v.error || "") + "</p>";
    return;
  }
  if (hint) {
    hint.textContent = "最近 " + v.batches + " 批选股 · 最新一期 Top" + v.top_n +
      " 里持有 " + (v.hit || []).length + " 只（" + num(v.hit_rate, 0) + "%）";
  }
  const tag = (x, hit) => '<span class="rt-tag ' + (hit ? "hit" : "dim") + '">' +
    (x.rank ? "<b>#" + x.rank + "</b> " : "") + esc(x.name) + '<span style="opacity:.65">' + esc(x.code) + "</span>" +
    (hit && x.hold_pnl_pct !== null && x.hold_pnl_pct !== undefined
      ? '<span class="' + signCls(x.hold_pnl_pct) + '">' + signed(x.hold_pnl_pct) + "</span>" : "") + "</span>";
  const hit = v.hit || [], miss = v.miss || [];
  const perf = v.held_perf && v.held_perf.length ? v.held_perf : [];
  box.innerHTML =
    '<div class="rt-vs-sec"><h4>系统候选里你确实持有的（' + hit.length + " 只）</h4>" +
    '<div class="rt-tags">' + (hit.length ? hit.map((x) => tag(x, true)).join("")
      : '<span class="hint">没有重合 —— 你的持仓不在最新一期 Top' + v.top_n + " 里。</span>") + "</div></div>" +
    '<div class="rt-vs-sec"><h4>系统选了但你没买的（前 12 只）</h4>' +
    '<div class="rt-tags">' + (miss.length ? miss.slice(0, 12).map((x) => tag(x, false)).join("")
      : '<span class="hint">全部命中。</span>') + "</div></div>" +
    (perf.length ?
      '<div class="rt-vs-sec"><h4>重合标的从选股日起的涨幅（后复权，与选股记录同口径）</h4><div class="rt-tags">' +
      perf.slice(0, 8).map((x) =>
        '<span class="rt-tag"><b>' + esc(x.name) + "</b>" + esc(x.as_of.slice(5)) + " 记录 " +
        num(x.from, 2) + " → " + num(x.to, 2) + ' <span class="' + signCls(x.ret) + '">' +
        signed(x.ret) + "</span></span>").join("") + "</div>" +
      (v.held_perf_avg !== null && v.held_perf_avg !== undefined
        ? '<p class="note">重合标的平均 ' + signed(v.held_perf_avg) + "。" + esc(v.note || "") + "</p>" : "") +
      "</div>" :
      (v.pick_ret_avg !== null && v.pick_ret_avg !== undefined
        ? '<p class="note">最近 ' + v.batches + " 批候选共 " + v.perf_n +
          " 条记录，从选股日到最新收盘平均 " + signed(v.pick_ret_avg) + "。" + esc(v.note || "") + "</p>" : ""));
}

/* ---- 动作 ---- */
async function rtSync() {
  const st = RT.data && RT.data.status;
  if (st && !st.configured) { rtCfgOpen(); return; }
  const btn = $("#btnRtSync");
  if (btn) { btn.disabled = true; btn.textContent = "同步中…"; }
  sysTaskStart("emt-sync", "同步实盘账户");
  const r = await post("/api/emt/sync", {});
  sysTaskEnd("emt-sync");
  if (btn) { btn.disabled = false; }
  if (!r || !r.ok) {
    toast((r && r.error) || "同步失败", "err");
    if (btn) btn.textContent = "同步账户";
    await rtStatusOnly();
    return;
  }
  toast("已同步 " + r.at + " · 总资产 " + rtMoney((r.overview || {}).total), "ok");
  RT.loaded = false;
  await rtLoad();
}

async function rtPullHistory() {
  const st = RT.data && RT.data.status;
  if (st && !st.configured) { rtCfgOpen(); return; }
  const what = [];
  if (($("#rtHistTrades") || {}).checked) what.push("trades");
  if (($("#rtHistOrders") || {}).checked) what.push("orders");
  if (($("#rtHistFlows") || {}).checked) what.push("flows");
  if (!what.length) { toast("至少选一类（成交 / 委托 / 流水）", "err"); return; }

  const start = ($("#rtHistFrom") || {}).value || "";
  const end = ($("#rtHistTo") || {}).value || "";
  const btn = $("#btnRtPull");
  if (btn) { btn.disabled = true; btn.textContent = "拉取中…"; }
  sysTaskStart("emt-history", "拉取实盘历史");
  const r = await post("/api/emt/history", { start: start, end: end, size: 500, what: what });
  sysTaskEnd("emt-history");
  if (btn) { btn.disabled = false; btn.textContent = "开始拉取"; }

  const msg = $("#rtHistMsg");
  if (!r || r.ok === false) {
    toast((r && r.error) || "拉取失败", "err");
    if (msg) msg.textContent = "拉取失败：" + ((r && r.error) || "未知原因");
    return;
  }
  const bits = [];
  if (r.trades) bits.push("成交新增 " + r.trades + " 笔");
  if (r.orders) bits.push("委托新增 " + r.orders + " 笔");
  if (r.flows) bits.push("流水新增 " + r.flows + " 条");
  if (msg) {
    msg.innerHTML = "已处理 " + r.months + " 个月（" + r.from + " ~ " + r.to + "）· " +
      (bits.length ? bits.join(" · ") : "没有新增（这些区间已经拉过）") +
      ((r.truncated || []).length ? '<br><span style="color:var(--warn,#b7791f)">⚠ ' +
        (r.truncated || []).slice(0, 3).map(esc).join("；") + "</span>" : "") +
      ((r.errors || []).length ? '<br><span class="bad">' +
        (r.errors || []).slice(0, 3).map(esc).join("；") + "</span>" : "");
  }
  toast(bits.length ? "历史已更新：" + bits.join("，") : "没有新增数据", bits.length ? "ok" : "");
  RT.loaded = false;
  await rtLoad();
}

async function rtProbe() {
  const out = $("#rtProbeOut");
  if (!out) return;
  const r = await api("/api/emt/probe");
  if (!r || r.ok === false) { toast((r && r.error) || "字段探测失败", "err"); return; }
  const tags = { trades: "成交", orders: "委托", flows: "流水" };
  const parts = [];
  Object.keys(tags).forEach((k) => {
    const x = r[k];
    if (!x) return;
    parts.push("【" + tags[k] + "】" + x.count + " 条 · " + (x.at || ""));
    parts.push("  已识别：" + (x.known || []).join(", "));
    if ((x.unknown || []).length) parts.push("  未识别：" + x.unknown.join(", "));
    parts.push("  样本：" + JSON.stringify((x.sample || [])[0] || {}, null, 1));
    parts.push("");
  });
  out.hidden = false;
  out.textContent = parts.length ? parts.join("\n")
    : "还没有拉过数据。先点「同步账户」或「开始拉取」，再回来看字段。";
  out.scrollIntoView({ block: "nearest" });
}

function rtCfgFill() {
  const st = (RT.data && RT.data.status) || {};
  // 不回填账号：只显示掩码，避免误改
  const u = $("#rtUser"); if (u) u.value = "";
  const p = $("#rtPass"); if (p) p.value = "";
  const chk = $("#rtAutoSync"); if (chk) chk.checked = !!st.auto_sync;
  const t = $("#rtAutoTime"); if (t && st.auto_time) t.value = st.auto_time;
  rtRefreshPaint(st);
  const msg = $("#rtCfgMsg");
  if (msg) msg.textContent = st.configured ? ("当前账号：" + st.user + "（留空则不改动）") : "还没有配置账号。";

  // 历史区间默认值：上次拉到哪儿就从哪儿起，没有就回退 12 个月
  const h = (RT.data && (RT.data.history || (RT.data.status || {}).history)) || st.history || {};
  const from = $("#rtHistFrom"), to = $("#rtHistTo");
  if (from && !from.value) {
    const d0 = new Date();
    d0.setFullYear(d0.getFullYear() - 1);
    const iso = d0.toISOString().slice(0, 10);
    from.value = (h.last_day && h.last_day > iso) ? h.last_day : iso;
  }
  if (to && !to.value) to.value = new Date().toISOString().slice(0, 10);
  const hm = $("#rtHistMsg");
  if (hm && h.fills) {
    hm.textContent = "已回流：成交 " + h.fills + " 笔 / 委托 " + (h.orders || 0) + " 笔 / 流水 " +
      (h.flows || 0) + " 条" + (h.at ? "，上次拉取 " + h.at : "");
  }

  rtStatusOnly().then((s) => {
    const c = $("#rtAutoSync"); if (c) c.checked = !!s.auto_sync;
    const tt = $("#rtAutoTime"); if (tt && s.auto_time) tt.value = s.auto_time;
    rtRefreshPaint(s);
  });
}
function rtCfgOpen() { goHub("real"); }

async function rtCfgSave() {
  const user = ($("#rtUser").value || "").trim();
  const pass = $("#rtPass").value || "";
  const body = { enabled: true };
  if (user) body.user = user;
  if (pass) body.password = pass;
  if (!user && !pass) { toast("账号和密码都空着，没有可保存的内容", "err"); return; }
  const r = await post("/api/emt/config", body);
  if (!r || !r.ok) { toast((r && r.error) || "保存失败", "err"); return; }
  $("#rtPass").value = "";
  const msg = $("#rtCfgMsg");
  if (msg) msg.textContent = "已保存：" + (r.user || "");
  toast("账号已保存，正在试连…", "ok");
  const t = await post("/api/emt/test", {});
  if (t && t.ok) {
    toast("连接成功 · 总资产 " + rtMoney((t.overview || {}).total) + " · 持仓 " + t.positions + " 只", "ok");
    if (msg) msg.textContent = "连接成功：" + (t.user || "") + " · 快照 " + t.at;
  } else {
    toast("试连失败：" + ((t && t.error) || "未知原因"), "err");
    if (msg) msg.textContent = "试连失败：" + ((t && t.error) || "");
  }
  await rtStatusOnly();
}
async function rtLogout() {
  await post("/api/emt/logout", {});
  toast("已退出登录（账号与账本都还在）", "ok");
  await rtStatusOnly();
}
/* ---------------- 复盘日记：一天一篇 Markdown ---------------- */
const JN = {
  loaded: false, entries: [], day: "", text: "", src: "", at: "", dir: "",
  dirty: false, busy: false, loading: false, view: "preview"
};
const JN_SRC_LABEL = { manual: "手写", ai: "AI 稿", local: "本地模板" };

/* 极简 Markdown 渲染：只支持日记会用到的标题/列表/引用/加粗/行内代码。
   先转义再拼标签，正文里的 HTML 一律当文字，不执行。 */
function mdEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function mdInline(s) {
  return mdEsc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
}
function mdRender(src) {
  const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let para = [], list = null, quote = [];
  const flushPara = () => {
    if (para.length) { out.push("<p>" + mdInline(para.join(" ")) + "</p>"); para = []; }
  };
  const flushList = () => {
    if (list) {
      out.push("<" + list.tag + ">" +
        list.items.map((x) => "<li>" + mdInline(x) + "</li>").join("") +
        "</" + list.tag + ">");
      list = null;
    }
  };
  const flushQuote = () => {
    if (quote.length) { out.push("<blockquote>" + mdInline(quote.join(" ")) + "</blockquote>"); quote = []; }
  };
  const flushAll = () => { flushPara(); flushList(); flushQuote(); };
  for (const raw of lines) {
    const s = raw.trim();
    if (!s) { flushAll(); continue; }
    let m = /^(#{1,6})\s+(.*)$/.exec(s);
    if (m) {
      flushAll();
      const lv = Math.min(5, m[1].length + 1);   // # → h2，## → h3，页面里再大就压过卡片标题了
      out.push("<h" + lv + ">" + mdInline(m[2]) + "</h" + lv + ">");
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(s)) { flushAll(); out.push("<hr>"); continue; }
    if ((m = /^[-*+]\s+(.*)$/.exec(s))) {
      flushPara(); flushQuote();
      if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
      list.items.push(m[1]);
      continue;
    }
    if ((m = /^\d+[.)]\s+(.*)$/.exec(s))) {
      flushPara(); flushQuote();
      if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
      list.items.push(m[1]);
      continue;
    }
    if ((m = /^>\s?(.*)$/.exec(s))) {
      flushPara(); flushList(); quote.push(m[1]); continue;
    }
    if (list || quote.length) { flushList(); flushQuote(); }
    para.push(s);
  }
  flushAll();
  return out.join("");
}

function jnWeekday(day) {
  const d = new Date(String(day) + "T00:00:00");
  return isFinite(d.getTime()) ? "周" + "日一二三四五六"[d.getDay()] : "";
}
function jnHas(day) {
  return JN.entries.some((e) => e.day === day);
}
function jnSrcHtml(src) {
  if (!src) return '<span class="jn-src jn-src-empty">未保存</span>';
  return '<span class="jn-src ' + esc(src) + '">' + esc(JN_SRC_LABEL[src] || src) + "</span>";
}
function jnRenderList() {
  const el = $("#jnList");
  if (!el) return;
  $("#jnCount").textContent = JN.entries.length ? JN.entries.length + " 篇" : "";
  if (!JN.entries.length) {
    el.innerHTML = '<div class="jn-empty">还没有日记。<br>点右上角「写今天」新建第一篇。</div>';
    return;
  }
  el.innerHTML = JN.entries.map((e) => {
    const on = e.day === JN.day;
    return '<button type="button" class="jn-row' + (on ? " on" : "") +
      '" data-day="' + esc(e.day) + '">' +
      '<div class="jn-row-h"><b>' + esc(String(e.day).slice(5)) + "</b>" +
        '<span class="jn-wd">' + jnWeekday(e.day) + "</span>" +
        '<span class="jn-yr">' + esc(String(e.day).slice(0, 4)) + "</span>" +
        '<em class="jn-tag ' + esc(e.src || "manual") + '">' +
          esc(JN_SRC_LABEL[e.src] || "手写") + "</em></div>" +
      '<p>' + esc(e.preview || "（空）") + "</p>" +
      '<div class="jn-row-f"><span>' + (e.chars || 0) + " 字</span><span>" +
        esc(String(e.updated_at || "").slice(5, 16)) + "</span></div>" +
      "</button>";
  }).join("");
}
function jnPaintStat() {
  const stat = $("#jnStat");
  if (!stat) return;
  const chars = JN.entries.reduce((s, e) => s + (Number(e.chars) || 0), 0);
  stat.textContent = JN.entries.length
    ? "共 " + JN.entries.length + " 篇 · " + chars.toLocaleString("zh-CN") + " 字"
    : "还没有日记";
}
function jnPaint() {
  const exists = jnHas(JN.day);
  const srcEl = $("#jnSrc");
  if (srcEl) srcEl.innerHTML = jnSrcHtml(JN.src);
  const meta = $("#jnMeta");
  if (meta) {
    meta.textContent = exists
      ? (JN.at ? "更新于 " + JN.at + " · " : "") + "data/journal/" + JN.day + ".md"
      : (JN.day ? "这篇还没有保存，保存后生成 " + JN.day + ".md" : "选一天开始记录");
  }
  const ta = $("#jnText"), pv = $("#jnPreview");
  if (ta && pv) {
    const preview = JN.view !== "edit";
    ta.hidden = preview;
    pv.hidden = !preview;
    if (preview) {
      const body = ta.value || "";
      pv.innerHTML = body.trim()
        ? mdRender(body)
        : '<p class="jn-pv-empty">这篇还没有内容，点右上角「编辑」开始写。</p>';
    }
  }
  document.querySelectorAll("#jnView button").forEach((b) => {
    b.classList.toggle("on", (b.dataset.v || "") === (JN.view || "preview"));
  });
  const chars = $("#jnChars");
  if (chars) chars.textContent = ($("#jnText").value || "").length.toLocaleString("zh-CN") + " 字";
  const dirty = $("#jnDirty");
  if (dirty) dirty.textContent = JN.dirty ? "未保存" : "";
  const save = $("#btnJnSave");
  if (save) { save.disabled = JN.busy; save.textContent = JN.busy ? "保存中…" : "保存"; }
  const del = $("#btnJnDelete");
  if (del) del.disabled = JN.busy || !exists;
  const fmt = $("#btnJnFormat");
  if (fmt) fmt.disabled = JN.busy || !exists;
  jnPaintStat();
}
async function jnLoad(day) {
  if (JN.loading) return;
  JN.loading = true;
  try {
    const r = await api("/api/journal?limit=800");
    if (!r.ok) { toast(r.error || "日记读取失败", "err"); return; }
    JN.entries = r.entries || [];
    JN.dir = r.dir || "";
    JN.loaded = true;
    jnRenderList();
    jnPaintStat();
    if (JN.dirty) { jnPaint(); return; }   // 有未保存草稿：只刷新左栏，不重开编辑器
    // 默认停在今天：当天的日记就应该是最顺手的入口；历史从左侧列表回看
    const pick = String(day || JN.day || "").slice(0, 10);
    const target = /^\d{4}-\d{2}-\d{2}$/.test(pick) ? pick : todayStr();
    await jnOpen(target, true);
  } catch (e) {
    toast("日记读取失败：" + (e.message || e), "err");
  } finally {
    JN.loading = false;
    jnPaint();
  }
}
async function jnOpen(day, force) {
  day = String(day || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
  if (!force && JN.dirty && day !== JN.day) {
    if (!confirm("当前日记有未保存的修改，切换后不会保留。继续吗？")) {
      $("#jnDay").value = JN.day;
      return;
    }
  }
  let r;
  try { r = await api("/api/journal/" + encodeURIComponent(day)); }
  catch (e) { toast("读取失败：" + (e.message || e), "err"); return; }
  if (!r.ok) { toast(r.error || "读取失败", "err"); return; }
  JN.day = day;
  JN.text = r.text || "";
  JN.src = r.src || "";
  JN.at = r.updated_at || "";
  JN.dirty = false;
  $("#jnDay").value = day;
  $("#jnText").value = JN.text;
  jnRenderList();
  jnPaint();
}
async function jnSave() {
  if (JN.busy) return;
  const day = $("#jnDay").value || JN.day;
  const text = $("#jnText").value || "";
  if (!text.trim()) { toast("日记内容为空，未保存", "err"); return; }
  const src = (JN.src && text === JN.text) ? JN.src : "manual";
  JN.busy = true;
  jnPaint();
  try {
    const r = await post("/api/journal", { day: day, text: text, src: src });
    if (!r || !r.ok) { toast((r && r.error) || "保存失败", "err"); return; }
    JN.day = r.day || day;
    JN.text = r.text || text;
    JN.src = r.src || src;
    JN.at = r.journal_at || "";
    JN.dirty = false;
    toast("已保存 " + JN.day + " 的复盘日记", "ok");
    await jnLoad(JN.day);
  } catch (e) {
    toast("保存失败：" + (e.message || e), "err");
  } finally {
    JN.busy = false;
    jnPaint();
  }
}
async function jnDelete() {
  const day = JN.day;
  if (!day || !jnHas(day)) { toast("这一天还没有日记", "info"); return; }
  if (!confirm("删除 " + day + " 的复盘日记？\n对应 Markdown 文件会一起删除，不可恢复。")) return;
  JN.busy = true;
  jnPaint();
  try {
    const r = await api("/api/journal/" + encodeURIComponent(day), { method: "DELETE" });
    if (!r || !r.ok) { toast((r && r.error) || "删除失败", "err"); return; }
    JN.dirty = false;
    JN.day = "";
    toast("已删除 " + day + " 的日记", "ok");
    await jnLoad("");
  } finally {
    JN.busy = false;
    jnPaint();
  }
}
function jnNew() {
  const today = todayStr();
  if (jnHas(today)) { jnOpen(today); toast("今天已有日记，已打开", "info"); return; }
  if (JN.dirty && !confirm("当前日记有未保存的修改，新建后不会保留。继续吗？")) return;
  JN.day = today;
  JN.text = "";
  JN.src = "";
  JN.at = "";
  JN.dirty = false;
  JN.view = "edit";   // 新建就是要写，直接进编辑
  $("#jnDay").value = today;
  $("#jnText").value = "";
  jnRenderList();
  jnPaint();
  $("#jnText").focus();
}
async function jnFormat() {
  const day = JN.day;
  if (!day || !jnHas(day)) { toast("这一天还没有日记", "info"); return; }
  if (JN.dirty && !confirm("当前有未保存的修改，整理格式会重新读取文件并丢弃草稿。继续吗？")) return;
  JN.busy = true;
  jnPaint();
  try {
    const r = await post("/api/journal/normalize", { day: day });
    if (!r || !r.ok) { toast((r && r.error) || "整理失败", "err"); return; }
    if (!r.changed) { toast("这篇已经是 Markdown 结构，无需调整", "info"); return; }
    JN.dirty = false;
    await jnLoad(day);
    toast("已补上 Markdown 标题", "ok");
  } catch (e) {
    toast("整理失败：" + (e.message || e), "err");
  } finally {
    JN.busy = false;
    jnPaint();
  }
}
function goJournal(day) {
  if (day && /^\d{4}-\d{2}-\d{2}$/.test(String(day))) JN.day = String(day).slice(0, 10);
  switchTab("journal");
}

/* ---------------- 单股分析：模型分 + 因子分位 + AI 解读（每次存档） ---------------- */
const STK = {
  loaded: false, busy: false, code: "", id: null, detail: null,
  report: "", mode: "", model: "", at: "", warn: "", reports: [], debate: null
};

function stkNum(v, d) {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || !isFinite(n)) return "—";
  return n.toFixed(d === undefined ? 2 : d);
}
function stkPct(v, d) {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || !isFinite(n)) return "—";
  return (n > 0 ? "+" : "") + n.toFixed(d === undefined ? 2 : d) + "%";
}
function stkCls(v) {
  const n = Number(v);
  return n > 0 ? "stk-up" : n < 0 ? "stk-down" : "";
}
function stkKpis(d) {
  const items = [
    ["最新价", stkNum(d.price), stkPct(d.pct), stkCls(d.pct)],
    ["模型分", stkNum(d.score, 4),
      "全市场 #" + (d.rank || "—") + " / " + (d.count || "—"), ""],
    ["横截面分位", d.percentile == null ? "—" : stkNum(d.percentile, 1) + "%",
      "行业 " + (d.ind_rank || "—") + " / " + (d.ind_count || "—"), ""],
    ["PE / PB", stkNum(d.pe) + " / " + stkNum(d.pb),
      d.total_mv == null ? "市值 —" : "总市值 " + stkNum(d.total_mv) + " 亿", ""],
    ["换手 / 量比", stkNum(d.turnover_rate) + "% / " + stkNum(d.volume_ratio),
      "数据日 " + (d.date || "—"), ""],
    ["行情缓存", (d.bars || 0) + " 根",
      (d.basis || "—") + " · 至 " + (d.last_date || "—"), ""]
  ];
  return items.map(([k, v, s, cls]) =>
    '<div class="stk-kpi"><span>' + esc(k) + '</span><b class="' + cls + '">' + esc(v) +
    "</b><em>" + esc(s) + "</em></div>").join("");
}
function stkFactors(d) {
  return (d.factors || []).map((f) => {
    const p = f.pct == null ? null : Number(f.pct);
    const w = p == null ? 0 : Math.max(0, Math.min(100, p));
    const val = f.value == null ? "—" : stkNum(f.value, 3) + (f.unit || "");
    return "<tr><td><b>" + esc(f.name) + '</b><span class="stk-fk">' + esc(f.key) + "</span></td>" +
      "<td>" + esc(f.group || "—") +
        (f.model_only ? ' <i class="stk-mo">模型</i>' : "") + "</td>" +
      '<td class="num">' + esc(val) + "</td>" +
      '<td><div class="stk-bar"><span style="width:' + w + '%"></span></div>' +
        "<em>" + (p == null ? "—" : p.toFixed(1) + "%") + "</em></td></tr>";
  }).join("");
}
function stkRiskHtml(d) {
  const lv = d.levels || {}, va = d.valuation || {};
  const n = (v, dd) => stkNum(v, dd);
  const band = (b) => (b && b.length) ? b.map((x) => stkNum(x)).join(" ~ ") : "—";
  const items = [
    ["ATR14", lv.atr_pct == null ? "—" : n(lv.atr_pct, 2) + "%",
      lv.atr == null ? "波动参考" : "≈ " + n(lv.atr) + " 元"],
    ["20 日区间", lv.low20 == null ? "—" : n(lv.low20) + " ~ " + n(lv.high20),
      lv.dist_high20_pct == null ? "" : "距高点 " + n(lv.dist_high20_pct, 1) + "%"],
    ["Pivot / R1 / S1", [lv.pivot, lv.r1, lv.s1]
      .map((x) => x == null ? "—" : n(x)).join(" / "), ""],
    ["2×ATR 参考位", lv.stop_ref == null ? "—" : n(lv.stop_ref),
      lv.stop_ref_pct == null ? "波动参考，非止损" : "约 " + n(lv.stop_ref_pct, 2) + "%，非止损"],
    ["同行 PE 区间", band(va.pe_band),
      va.price_band_pe ? "对应价 " + band(va.price_band_pe)
        : (va.pe ? "同行样本不足" : "当日 PE 缺失")],
    ["同行 PB 区间", band(va.pb_band),
      va.price_band_pb ? "对应价 " + band(va.price_band_pb)
        : (va.pb ? "同行样本不足" : "当日 PB 缺失")]
  ];
  return items.map(([k, v, s]) =>
    '<div class="stk-kpi"><span>' + esc(k) + "</span><b>" + esc(v) +
    "</b><em>" + esc(s) + "</em></div>").join("");
}
function stkDebateHtml(d) {
  if (!d) return "";
  const v = d.verdict || {};
  const label = { positive: "偏多", neutral: "中性", negative: "偏空" }[v.stance] || "中性";
  const cls = v.stance === "positive" ? "pos" : v.stance === "negative" ? "neg" : "mid";
  const li = (arr) => (arr || []).map((x) =>
    "<li><b>" + esc(x.point || "") + "</b>" +
    (x.evidence ? "<em>" + esc(x.evidence) + "</em>" : "") + "</li>").join("");
  const gaps = (d.data_gaps || []).filter(Boolean);
  return '<div class="stk-vd ' + cls + '"><b>' + label + "</b>" +
    "<span>证据一致性 " + (v.confidence == null ? "—" : v.confidence + "%") +
    (v.horizon ? " · " + esc(v.horizon) : "") + "</span>" +
    (v.note ? "<em>" + esc(v.note) + "</em>" : "") + "</div>" +
    '<div class="stk-dcols">' +
      '<div class="stk-dcol bull"><h4>多头</h4><ul>' + li(d.bull) + "</ul></div>" +
      '<div class="stk-dcol bear"><h4>空头</h4><ul>' + li(d.bear) + "</ul></div>" +
    "</div>" +
    (gaps.length ? '<p class="stk-gaps">数据缺口：' + gaps.map(esc).join("；") + "</p>" : "");
}
function stkRenderHist() {
  const el = $("#stkHist");
  if (!el) return;
  const list = STK.reports || [];
  $("#stkHistCount").textContent = list.length ? list.length + " 条" : "";
  $("#btnStkClear").hidden = !(list.length && STK.code);
  if (!list.length) {
    el.innerHTML = '<div class="jn-empty">还没有分析记录。<br>选一只股票点「分析」。</div>';
    return;
  }
  el.innerHTML = list.map((r) => {
    const on = r.id === STK.id;
    return '<div class="stk-hrow' + (on ? " on" : "") + '">' +
      '<button type="button" class="stk-hopen" data-id="' + r.id + '">' +
        "<b>" + esc(String(r.at || "").slice(5, 16)) + "</b>" +
        '<i class="stk-tag ' + (r.mode === "ai" ? "ai" : "local") + '">' +
          (r.mode === "ai" ? "AI" : "本地") + "</i>" +
        "<em>" + (r.score == null ? "—" : stkNum(r.score, 4)) +
          " · #" + (r.rank || "—") + "</em>" +
      "</button>" +
      '<button type="button" class="stk-hdel" data-del="' + r.id +
        '" title="删除这条记录">×</button></div>';
  }).join("");
}
function stkPaint() {
  const d = STK.detail;
  const empty = $("#stkEmpty"), body = $("#stkBody");
  if (!d) {
    if (empty) empty.hidden = false;
    if (body) body.hidden = true;
    $("#stkFactorCard").hidden = true;
    $("#stkReportCard").hidden = true;
    return;
  }
  empty.hidden = true;
  body.hidden = false;
  $("#stkName").textContent = (d.name || d.code) + " " + d.code;
  $("#stkSub").textContent = [d.industry || "未分类",
    d.date ? "数据日 " + d.date : "", d.engine ? "引擎 " + d.engine : ""]
    .filter(Boolean).join(" · ");
  $("#stkScore").innerHTML = "<b>" + stkNum(d.score, 4) + "</b><span>全市场 #" +
    (d.rank || "—") + " / " + (d.count || "—") + " · 分位 " +
    (d.percentile == null ? "—" : stkNum(d.percentile, 1) + "%") + "</span>";
  $("#stkKpis").innerHTML = stkKpis(d);
  const ws = (d.warnings || []).concat(STK.warn ? [STK.warn] : []);
  const warn = $("#stkWarn");
  warn.hidden = !ws.length;
  warn.textContent = ws.join("；");
  $("#stkFactorCard").hidden = false;
  $("#stkFactors").innerHTML = stkFactors(d);
  const riskCard = $("#stkRiskCard");
  if (riskCard) {
    const hasRisk = !!(d.levels && Object.keys(d.levels).length);
    riskCard.hidden = !hasRisk;
    if (hasRisk) $("#stkRisk").innerHTML = stkRiskHtml(d);
  }
  const deb = $("#stkDebate");
  if (deb) {
    deb.hidden = !STK.debate;
    deb.innerHTML = STK.debate ? stkDebateHtml(STK.debate) : "";
  }
  const rc = $("#stkReportCard");
  rc.hidden = !STK.report;
  if (STK.report) {
    $("#stkReport").innerHTML = mdRender(STK.report);
    $("#stkReportMeta").textContent = [STK.at ? "生成于 " + STK.at : "",
      d.name + " " + d.code, "数据日 " + (d.date || "—")].filter(Boolean).join(" · ");
    $("#stkMode").textContent = STK.mode === "ai"
      ? "AI" + (STK.model ? " · " + STK.model : "") : "本地模板";
    $("#stkMode").className = "pill " + (STK.mode === "ai" ? "yes" : "warn");
  }
}
async function stkLoadReports(code) {
  const q = code ? "?code=" + encodeURIComponent(code) : "";
  const r = await api("/api/stock/reports" + q);
  if (r && r.ok) {
    STK.reports = r.items || [];
    stkRenderHist();
  }
}
async function stkLoad() {
  STK.loaded = true;
  const code = STK.code || "";
  if (/^\d{6}$/.test(code)) {
    // 已指定标的（从别处跳进来）：出画像 + 该股历史
    await stkDetail(code, true);
    stkPaint();
    return;
  }
  // 直接进页面：默认打开最近一次分析，不用先点历史才看到内容
  await stkLoadReports("");
  const latest = (STK.reports || [])[0];
  if (!latest) { stkPaint(); return; }
  await stkOpen(latest.id);
  // 历史列表收敛到这只股票，否则右侧列的是全站记录、左侧却是单只
  await stkLoadReports(STK.code);
  stkRenderHist();
}

/* 只取画像，不生成报告：快（一次快照读取），用于页面预加载与输入后即时回显 */
async function stkDetail(code, quiet) {
  const r = await api("/api/stock/detail?code=" + encodeURIComponent(code));
  if (!r || !r.ok) {
    if (!quiet) toast((r && r.error) || "读取画像失败", "err");
    return false;
  }
  STK.code = r.code;
  STK.detail = r;
  STK.report = "";        // 换了标的，旧报告不能留在页面上
  STK.debate = null;
  STK.mode = "";
  STK.model = "";
  STK.at = "";
  STK.id = null;
  STK.warn = "";
  const inp = $("#stkCode");
  if (inp) inp.value = r.code || "";
  stkPaint();
  await stkLoadReports(r.code);
  return true;
}
async function stkAnalyze() {
  if (STK.busy) return;
  const raw = ($("#stkCode").value || "").trim();
  if (!raw) { toast("请输入股票代码或名称", "err"); return; }
  STK.busy = true;
  const btn = $("#btnStkAnalyze");
  btn.disabled = true;
  btn.textContent = "分析中…";
  sysTaskStart("stock-analyze", "单股分析 " + raw);
  try {
    // 先出画像再等报告：LLM 要 10~30 秒，别让用户对着空白页等
    if (/^\d{6}$/.test(raw) && (!STK.detail || STK.code !== raw)) {
      await stkDetail(raw, true);
    }
    const r = await post("/api/stock/analyze", { code: raw });
    if (!r || !r.ok) { toast((r && r.error) || "分析失败", "err"); return; }
    STK.code = r.detail.code;
    STK.detail = r.detail;
    STK.report = r.report || "";
    STK.debate = r.debate || null;
    STK.mode = r.mode || "local";
    STK.model = r.model || "";
    STK.at = r.at || "";
    STK.id = r.id || null;
    STK.warn = r.warning || "";
    $("#stkCode").value = r.detail.code;
    stkPaint();
    await stkLoadReports(STK.code);
    toast(r.mode === "ai" ? "AI 分析完成，已存档" : "已用本地模板生成并存档", "ok");
  } catch (e) {
    toast("分析失败：" + (e.message || e), "err");
  } finally {
    STK.busy = false;
    btn.disabled = false;
    btn.textContent = "分析";
    sysTaskEnd("stock-analyze");
  }
}
async function stkOpen(id) {
  const r = await api("/api/stock/reports/" + id);
  if (!r || !r.ok) { toast((r && r.error) || "读取失败", "err"); return; }
  const p = r.payload || {};
  STK.id = r.id;
  STK.code = r.code;
  STK.detail = p.detail || null;
  STK.report = p.report || "";
  STK.debate = p.debate || null;
  STK.mode = r.mode || "local";
  STK.model = r.model || "";
  STK.at = r.at || "";
  STK.warn = "";
  $("#stkCode").value = r.code || "";
  stkPaint();
  stkRenderHist();
}
async function stkDelete(id) {
  if (!confirm("删除这条分析记录？不可恢复。")) return;
  const r = await api("/api/stock/reports/" + id, { method: "DELETE" });
  if (!r || !r.ok) { toast((r && r.error) || "删除失败", "err"); return; }
  if (STK.id === id) { STK.id = null; STK.report = ""; }
  await stkLoadReports(STK.code);
  stkPaint();
  toast("已删除这条分析记录", "ok");
}
async function stkClear() {
  if (!STK.code) return;
  if (!confirm("清空 " + STK.code + " 的全部分析记录？不可恢复。")) return;
  const r = await post("/api/stock/reports/clear", { code: STK.code });
  if (!r || !r.ok) { toast((r && r.error) || "清空失败", "err"); return; }
  STK.id = null;
  STK.report = "";
  await stkLoadReports(STK.code);
  stkPaint();
  toast("已清空该股记录", "ok");
}

/* ---------------- 选股工作台：快照 → LightGBM → LLM ---------------- */
const SCR = {
  candidates: [], last: null, llmRows: null, llmReady: false, llmMode: "",
  noteBase: "", status: null, detail: new Set(), busy: false, autoTimer: null
};
const SCR_PREF_KEY = "quantdesk_screen_prefs_v1";
// 截止日不进偏好：它是一次性查询条件，被记住后会静默把数据截在历史某天，
// 表现为「数据日一直不更新」，且用户完全看不出是自己设的。
// 「优先复用本期截面」同理不进偏好：它决定的是性能而不是口径，服务端会校验数据日，
// 默认开就是最优行为，被旧偏好记住 false 反而让人以为系统变慢了。
const SCR_PREF_IDS = ["scrScope", "scrTopN", "scrUseModel",
  "scrAutoLlm", "scrHorizon", "scrStep", "scrMaxSymbols", "scrTrainStart", "scrTrainEnd",
  "scrIndustryCap", "scrFnQuality", "scrFnSt", "scrFnExtreme", "scrFnLiquidity", "scrFnTrend",
  "scrNeutral", "scrLimit", "scrLabelMode"];
const SCR_FACTOR_LABELS = {
  ret1: "1日收益%", mom5: "5日动量%", mom20: "20日动量%", mom60: "60日动量%",
  dist_ma60: "偏离60日线%", ma_dev: "偏离20日线%", macd_hist: "MACD柱",
  rsi14: "RSI14", boll_pos: "布林位置", dd20: "距20日高点%",
  vol20: "20日波动%", vol60: "60日波动%", amp20: "20日振幅%",
  vol_ratio: "量比5/20", amount_ratio: "额比5/20", turnover: "换手率%",
  ind_mom20: "行业20日动能%", ind_rel20: "行业内相对动量%"
};
const NEUTRAL_LABEL = {
  industry: "行业中性", "industry+size": "行业+市值中性", none: "不中性化", "": "—"
};

function scrNum(id, def) {
  const v = Number($(id).value);
  return Number.isFinite(v) && v > 0 ? v : def;
}
function scrFmt(v, d) {
  if (v == null || v === "" || !Number.isFinite(Number(v))) return "—";
  return Number(v).toFixed(d === undefined ? 2 : d);
}
// 默认值升级：旧偏好里存的是上一版默认值（TopN=10、训练上限=300），
// 直接恢复会让新默认不生效。只在「存的正好等于旧默认」时迁移，用户手动改过的值不动。
const SCR_PREF_MIGRATE = { scrTopN: [10, 20], scrMaxSymbols: [300, 800] };

function scrLoadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(SCR_PREF_KEY) || "{}");
    SCR_PREF_IDS.forEach((id) => {
      const el = document.getElementById(id);
      if (!el || p[id] == null) return;
      let v = p[id];
      const mig = SCR_PREF_MIGRATE[id];
      if (mig && Number(v) === mig[0]) v = mig[1];
      if (el.type === "checkbox") el.checked = !!v;
      else el.value = v;
    });
    // 旧版本的默认上限是 1500（抽样版全市场），现在默认 0＝真正的全市场。
    // 老偏好里存着的 1500 要迁掉，否则用户打开还是只算 1500 只。
    const limEl = document.getElementById("scrLimit");
    if (limEl && String(p.scrLimit) === "1500") limEl.value = "0";
    // 旧版本把截止日也存进了偏好：读到直接清掉，否则数据日会一直停在那一天
    const ao = document.getElementById("scrAsOfInput");
    if (ao) ao.value = "";
  } catch (e) { /* 本地偏好损坏时忽略，用默认值 */ }
}
function scrSavePrefs() {
  try {
    const p = {};
    SCR_PREF_IDS.forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      p[id] = el.type === "checkbox" ? el.checked : el.value;
    });
    localStorage.setItem(SCR_PREF_KEY, JSON.stringify(p));
  } catch (e) { /* 隐私模式下 localStorage 可能不可写 */ }
}
async function withBusy(btn, work) {
  if (!btn) return await work();
  if (btn.disabled) return;
  // 首次记录原始文案：与二次确认态（twoStep 会临时改文字）共存时不会把按钮改坏
  if (!btn.dataset.origLabel) btn.dataset.origLabel = btn.textContent;
  btn.disabled = true;
  try {
    return await work();
  } catch (e) {
    // 任何未捕获异常都要让用户看见，否则按钮只是“闪一下没反应”
    toast("操作失败：" + ((e && e.message) || e), "err");
  } finally {
    btn.disabled = false;
    btn.textContent = btn.dataset.origLabel;
    btn.classList.remove("armed");
  }
}
function scrRunParams() {
  // 0 = 全市场（默认）；正数才是「最多扫多少只」的抽样上限
  const lim = scrNum("#scrLimit", 0);
  return {
    scope: $("#scrScope").value,
    as_of: $("#scrAsOfInput").value || "",
    topn: scrNum("#scrTopN", 20),
    industry_cap: Number($("#scrIndustryCap").value) || 0,
    neutral: ($("#scrNeutral") && $("#scrNeutral").value) || "auto",
    funnel: {
      quality: $("#scrFnQuality").checked,
      st: $("#scrFnSt").checked,
      extreme: $("#scrFnExtreme").checked,
      liquidity: $("#scrFnLiquidity").checked,
      trend: $("#scrFnTrend").checked
    },
    horizon: scrNum("#scrHorizon", 10),
    step: scrNum("#scrStep", 5),
    max_symbols: scrNum("#scrMaxSymbols", 800),
    label_mode: ($("#scrLabelMode") && $("#scrLabelMode").value) || "market",
    limit: lim > 0 ? Math.max(200, Math.min(20000, lim)) : 0,
    use_model: $("#scrUseModel").checked,
    reuse_snapshot: $("#scrReuse").checked
  };
}
function scrTrainDates() {
  return {
    start: $("#scrTrainStart").value.replace(/-/g, "") || "20220101",
    end: $("#scrTrainEnd").value.replace(/-/g, "") || todayStr().replace(/-/g, "")
  };
}
function renderScrLayers(r) {
  const d = r.data, m = r.model || {}, l = r.llm || {};
  const mt = (m.meta && m.meta.metrics) || {};
  const dTag = d.snapshot && d.snapshot.loaded ? "已生成" : "未生成";
  const mTag = m.trained ? (m.engine + " 已训练") : (m.available ? "就绪·未训练" : "降级模式");
  const lTag = (l.enabled && l.configured) ? "已启用" : (l.configured ? "已配置·未启用" : "未配置");
  const dTxt = "本地缓存 <b>" + d.cached_total + "</b> 只 / 全 A 名单 <b>" + d.universe_total +
    "</b> 只 · 快照 <b>" + ((d.snapshot || {}).rows || 0) + "</b> 行 · 训练集 <b>" +
    ((d.dataset || {}).rows || 0) + "</b> 行";
  const mTxt = m.trained
    ? ("训练 <b>" + (m.meta.rows_train || 0) + "</b> 行 / 验证 <b>" + (m.meta.rows_valid || 0) +
       "</b> 行 · 验证 IC <b>" + scrFmt(mt.ic, 4) + "</b> · RankIC <b>" + scrFmt(mt.rank_ic, 4) + "</b>")
    : ("LightGBM " + (m.available ? m.version : "不可用") + " · 未训练时用可解释基准分，不伪装预测");
  const lTxt = (l.model ? "模型 <b>" + esc(l.model) + "</b>" : "未填写模型") +
    " · 默认：" + esc(l.mode_default || "本地规则解读");
  $("#scrLayers").innerHTML = [
    ["① 数据层 · 横截面快照", dTag, dTxt],
    ["② 模型层 · LightGBM 打分", mTag, mTxt],
    ["③ LLM 层 · 重排与解读", lTag, lTxt]
  ].map((x) => '<div class="scr-layer"><div class="t">' + x[0] +
    '<span class="pill ' + (x[1].indexOf("未") >= 0 ? "off" : "live") + '">' + esc(x[1]) +
    '</span></div><div class="s">' + x[2] + "</div></div>").join("");
}
function renderScrModel(m) {
  if (!m || !m.trained || !m.meta) {
    $("#scrModel").innerHTML = '<p class="hint" style="margin:0">尚未训练，候选榜使用方向加权基准分（' +
      (m && m.available ? "LightGBM " + esc(m.version) + " 已就绪" : "LightGBM 不可用，将降级岭回归") +
      "）。按上面 ①②③ 顺序执行即可换用模型分。</p>";
    return;
  }
  const x = m.meta, mt = x.metrics || {}, imp = x.feature_importance || {};
  const top = Object.entries(imp).slice(0, 8);
  const maxV = Math.max.apply(null, top.map((e) => e[1]).concat([0.0001]));
  const kv = [
    ["算法", esc(x.engine || "-") + (x.version ? " " + esc(x.version) : "") + " · 最佳迭代 " + (x.best_iteration || 0)],
    ["样本", "训练 " + (x.rows_train || 0) + " 行 / 验证 " + (x.rows_valid || 0) +
      " 行 · 切分日 " + esc(x.split_date || "-")],
    ["因子口径", (x.features ? x.features.length : 18) + " 个因子 · " +
      esc(NEUTRAL_LABEL[x.neutral || "none"] || "不中性化") +
      (x.label_mode === "rank" ? " · 横截面分位标签" :
        x.label_mode === "industry" ? " · 行业内超额标签" : " · 全市场超额标签")],
    ["标签", esc(x.target || "未来 N 日超额收益")],
    ["验证 IC", scrFmt(mt.ic, 4)], ["RankIC", scrFmt(mt.rank_ic, 4)],
    ['<span class="term" data-term="icir">IC 稳定性</span>', mt.ic_ir == null ? "—" :
      "IR " + scrFmt(mt.ic_ir, 2) + " · 逐期均值 " + scrFmt(mt.ic_mean, 4) +
      " · 标准差 " + scrFmt(mt.ic_std, 4) + " · 正 IC 占比 " + Math.round((mt.ic_pos_ratio || 0) * 100) +
      "% · " + (mt.ic_periods || 0) + " 期"],
    ["RMSE", mt.rmse == null ? "—（分位标签不适用）" : scrFmt(mt.rmse, 3)],
    // 最贴近实盘的两个数：照榜单买前 10 只平均多赚多少、多少比例跑赢基准
    ["Top10 超额 / 胜率", mt.head10 == null ? "—" :
      scrFmt(mt.head10, 2) + " % · 胜率 " + Math.round((mt.head10_win || 0) * 100) + "%"],
    ["Top20% 全市场超额", scrFmt(mt.top20_excess, 3) + " %（全体均值 " + scrFmt(mt.all_excess, 3) + " %）"],
    ["Top20% 行业超额", mt.top20_ind == null ? "—" :
      scrFmt(mt.top20_ind, 3) + " %（行业内 IC " + scrFmt(mt.ic_ind, 4) + "）"],
    ["训练时间", esc(x.trained || "-")]
  ];
  $("#scrModel").innerHTML = '<div class="scr-kv">' + kv.map((p) =>
    '<div class="k">' + p[0] + '</div><div class="v">' + p[1] + "</div>").join("") + "</div>" +
    (top.length ? '<div style="font-size:12px;font-weight:700;margin:12px 0 4px">特征重要度（Gain，前 8）</div>' +
      top.map((e) => '<div class="scr-bar"><span class="nm">' + esc(e[0]) +
        '</span><span class="tr"><span class="fi" style="width:' + Math.max(3, e[1] / maxV * 100) +
        '%"></span></span><span class="vl">' + scrFmt(e[1] * 100, 1) + "%</span></div>").join("") : "") +
    (x.warning ? '<p class="note" style="margin-top:10px;color:var(--amber)">' + esc(x.warning) + "</p>" : "") +
    (m.feature_drift ? '<p class="note" style="margin-top:6px;color:var(--amber)">这个模型没用到新增因子（' +
      esc((m.drift_keys || []).join("、")) + "）：到「进阶」重建训练集后重训即可纳入，旧模型仍可正常打分。</p>" : "");
}
/* ---------------- 流水线时效：四段数据链是否跟上 ---------------- */
const PIPE = { data: null, timer: null };

function pipeDate(v) {
  const s = String(v || "").replace(/-/g, "");
  return s.length >= 8 ? s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8) : (s || "—");
}

function pipeRender(r) {
  const body = $("#pipeBody");
  if (!body) return;
  const pill = $("#pipePill"), tip = $("#pipeTip"), chip = $("#chipFresh");
  if (!r || !r.ok) {
    if (pill) { pill.textContent = "不可用"; pill.className = "pipe-pill warn"; }
    body.innerHTML = '<div class="k">状态</div><div class="v">' +
      esc((r && r.error) || "读取失败") + "</div>";
    return;
  }
  PIPE.data = r;
  const run = r.run || {};
  // 只看「数据链」是否落后：模型该重训是人工待办，自动更新消不掉，
  // 不能让顶栏一直挂着「数据待更新」。
  const dataStale = r.data_stale == null ? !!r.stale : !!r.data_stale;
  const modelDue = !!(r.model && r.model.retrain_due);
  if (pill) {
    pill.textContent = run.running ? "运行中"
      : dataStale ? "需更新" : (modelDue ? "模型待重训" : "已是最新");
    pill.className = "pipe-pill " +
      (run.running ? "busy" : ((dataStale || modelDue) ? "warn" : "good"));
  }
  const lag = (n) => (n == null ? "—" : (n > 0 ? "落后 " + n + " 日" : "最新"));
  const qlag = Number(r.quote_lag || 0);
  const lm = r.dataset.label_mode === "rank" ? "分位标签"
    : r.dataset.label_mode === "industry" ? "行业内超额" : "全市场超额";
  const kv = [
    ["行情", pipeDate(r.as_of) + " · 缓存 " + r.quote_cached + " 只",
      r.quote_blocked ? "限流·快照补"
        : r.quote_source && r.quote_source.indexOf("spot") === 0 ? "快照补日线"
        : qlag > 0 ? ("落后 " + qlag + " 日" + (r.expected ? " · 应到 " + pipeDate(r.expected) : ""))
        : (r.quote_stale ? "今日未入库" : "最新")],
    ["快照", (r.snapshot.count || 0) + " 只 · " + pipeDate(r.snapshot.as_of), lag(r.snapshot.lag)],
    ["训练集", (r.dataset.rows || 0) + " 行 · " + pipeDate(r.dataset.date_max),
      lm + " · " + lag(r.dataset.lag)],
    ["模型", r.model.trained
      ? (r.model.age_days == null ? "已训练" : r.model.age_days + " 天前训练") +
        (r.model.ic == null ? "" : " · RankIC " + scrFmt(r.model.ic, 3)) +
        (r.model.ic_ir == null ? "" : " · IR " + scrFmt(r.model.ic_ir, 2))
      : "未训练", r.model.retrain_due ? "建议重训" : "可用"]
  ];
  // 估值与日历都是「过期了也看不出」的环节，直接列在数据链里：
  // 有几千行估值不代表它是新的，日历到期更会让上面所有「最新」失去依据。
  const vas = r.valuation || {};
  if (vas.configured) {
    kv.push(["估值", vas.last_date ? pipeDate(vas.last_date) : "未拉取",
      vas.stale ? ("落后 " + vas.lag + " 日" + (vas.expected ? " · 应到 " + pipeDate(vas.expected) : ""))
        : "最新"]);
  }
  const cal = r.calendar || {};
  if (cal.covered === false) {
    kv.push(["日历", cal.to ? ("只到 " + pipeDate(cal.to)) : "读不到",
      cal.to ? "时效检查失效" : "请联网刷新"]);
  }
  body.innerHTML = kv.map((p) => '<div class="k">' + p[0] + '</div><div class="v">' +
    esc(p[1]) + ' <span class="pipe-flag">' + esc(p[2]) + "</span></div>").join("");
  // 上一次跑失败了就把原因留在面板上：toast 一闪而过，用户回到这里得能查到究竟卡在哪一步
  const badStep = (run.steps || []).filter((s) => !s.ok)[0];
  if (badStep) {
    body.innerHTML += '<div class="k bad">上次失败</div><div class="v bad">' +
      esc(badStep.name + "：" + (badStep.detail || "未返回原因")) + "</div>";
  }
  if (tip) {
    const acts = r.actions || [];
    tip.textContent = run.running ? "正在执行，完成后自动刷新"
      : badStep ? ("上次更新失败 · " + badStep.name + "：" + (badStep.detail || "未返回原因"))
      : r.quote_blocked
        ? "行情源今日登录过于频繁已被限流，当根日线由实时快照补齐；别再连点，明天自动恢复"
      : (acts.length ? ((acts.find((a) => a.level === "warn") || acts[0]).text)
        : "四段数据链都在最新状态");
    if (tip.classList) tip.classList.toggle("bad", !!badStep || (!!r.quote_blocked && !run.running));
  }
  if (chip) {
    chip.hidden = !dataStale && !run.running;
    chip.textContent = run.running ? "数据更新中…" : "数据待更新";
  }
  clearTimeout(PIPE.timer);
  if (run.running) PIPE.timer = setTimeout(pipePoll, 2500);
}

async function pipeLoad() {
  const r = await api("/api/screen/freshness");
  pipeRender(r);
  return r;
}

async function pipePoll() {
  const r = await pipeLoad();
  const run = (r && r.run) || {};
  if (!run.running && run.steps && run.steps.length) {
    const bad = run.steps.filter((s) => !s.ok);
    // 自动更新刻意不重训模型：行情/快照/训练集完成后，模型待办仍会保留。
    // 这时不能说「已更新到最新」，否则用户会以为没有剩余动作。
    const left = (r.actions || []).filter((a) => a.level === "warn");
    // 失败时把这一步的具体原因带出来，只报「1 步失败」等于什么都没说
    toast(bad.length
      ? ("更新完成，" + bad.length + " 步失败 · " + bad[0].name + "：" + (bad[0].detail || "见设置面板"))
      : (left.length ? ("数据链已更新；仍有待办：" + left[0].text)
        : "数据链已更新到最新"), bad.length ? "err" : "ok");
    await screenStatus();
    // 数据链更新完，快照数据日也跟着变了；勾选保持不变，服务端会按数据日判断能否复用
    autoLoad();          // 手动/定时跑完后，把「上次执行」和「下次时间」一起刷掉
  }
}

async function pipeRun(level, btn) {
  const quote = !!($("#scrPipeQuote") && $("#scrPipeQuote").checked);
  const go = async () => {
    const r = await post("/api/screen/pipeline", { level, quote });
    if (!r.ok) { toast(r.error || "启动失败", "err"); return; }
    toast(quote ? "已开始：先更新行情，再重建数据链" : "已开始更新数据链", "ok");
    pipePoll();
  };
  if (level === "full" && btn) { twoStep(btn, "确认重训", go); return; }
  await go();
}

/* ---------------- 内置定时任务：每个交易日收盘后自动更新数据链 ---------------- */
const AUTO = { data: null };

function autoDays(wd) {
  const k = (wd || []).join(",");
  if (k === "1,2,3,4,5") return "周一至周五";
  if (k === "1,2,3,4,5,6,7") return "每天";
  const m = { 1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日" };
  return "周" + (wd || []).map((x) => m[x] || "").join("、");
}

function autoRender(r) {
  const info = $("#autoInfo");
  if (!info) return;
  if (!r || !r.ok) {
    info.textContent = "定时任务读取失败";
    info.classList.add("bad");
    return;
  }
  AUTO.data = r;
  const box = $("#autoSched"), tm = $("#autoTime");
  if (box) box.checked = !!r.enabled;
  if (tm) {
    // 正在输入时别把用户敲到一半的时刻覆盖掉
    if (document.activeElement !== tm) tm.value = r.time || "18:10";
    tm.disabled = !r.enabled;
  }
  const last = r.last || {};
  const st = last.at
    ? "上次 " + last.at.slice(5, 16) + " " +
      (last.ok === false ? "失败" : last.ok === null ? "执行中" : "成功") +
      (last.brief ? " · " + last.brief : "")
    : "还没有自动执行记录";
  const head = !r.enabled ? "已关闭，只有手点更新时才跑"
    : r.running ? "正在执行，完成后自动记录结果"
    : r.next_run ? "下次 " + r.next_run.slice(5) + " · " + autoDays(r.weekdays)
    : "暂无后续计划";
  info.innerHTML = esc(head) + "<br>" + esc(st);
  info.classList.toggle("bad", last.ok === false || (!!r.enabled && r.state === "待执行"));
}

async function autoLoad() {
  const r = await api("/api/screen/scheduler");
  autoRender(r);
  return r;
}

async function autoSave(patch, msg) {
  const r = await post("/api/screen/scheduler", patch);
  if (!r || !r.ok) {
    toast((r && r.error) || "保存失败", "err");
    return autoLoad();
  }
  autoRender(r);
  if (msg) toast(msg, "ok");
  return r;
}

function renderScrLlm(l) {
  if (!l) return;
  SCR.llmReady = !!(l.enabled && l.configured);
  // 配置表单已搬到数据中枢，这里只同步状态徽标，避免旧引用报错
  const lp = $("#hubLlmPill");
  if (lp) {
    lp.textContent = SCR.llmReady ? "已启用" : (l.configured ? "已配置·未启用" : "未配置");
    lp.className = "pill " + (SCR.llmReady ? "yes" : "no");
  }
}
function renderScrPlanned(list) {
  const cards = (list || []).map((x) =>
    '<div class="it"><b>' + esc(x.name) + "</b>" +
    '<span class="pill ' + (x.built ? "yes" : "todo") + '" style="margin-bottom:5px">' +
    (x.built ? "已落地" : "未建") + "</span><span>" + esc(x.desc) + "</span></div>").join("");
  const box = $("#scrPlanned");
  if (box) box.innerHTML = '<div class="scr-plan">' + cards + "</div>";
  const side = $("#scrSideTodo");
  if (side) side.innerHTML = (list || []).map((x) =>
    '<p class="hint" style="margin:0 0 7px"><b>' + esc(x.name) + "</b>：" +
    (x.built ? "已落地" : "未建") + "</p>").join("");
}
async function screenStatus() {
  const r = await api("/api/screen/status");
  if (!r.ok) {
    $("#scrHint").textContent = "框架状态读取失败：" + (r.error || "未知错误");
    return null;
  }
  SCR.status = r;
  renderScrLayers(r); renderScrModel(r.model); renderScrLlm(r.llm);
  renderScrPlanned(r.data.modules);
  const ind = r.data.industry || {};
  const indEl = $("#scrIndustryStat");
  if (indEl) {
    indEl.textContent = ind.count
      ? ("行业分类：" + ind.count + " 只 / " + ind.industries + " 个行业 · 覆盖本机缓存 " +
        (ind.sample_cover == null ? "—" : ind.sample_cover + "%") + " · " + (ind.source || "") +
        (ind.stale ? "（已过期，建议同步）" : ""))
      : "行业分类：尚未同步（点右侧按钮，约 40~90 秒）";
  }
  const d = r.data, m = r.model || {}, l = r.llm || {};
  const mt = (m.meta && m.meta.metrics) || {};
  $("#chipUniverse").textContent = "全 A " + d.universe_total + " 只";
  $("#chipCached").innerHTML = "已缓存 <b>" + d.cached_total + "</b> 只";
  $("#chipModel").textContent = m.trained ? ("模型 " + m.engine + " · IC " + scrFmt(mt.ic, 3)) : "模型 未训练";
  $("#chipModel").className = "chip " + (m.trained ? "good" : "warn");
  const llmOn = l.enabled && l.configured;
  SCR.llmReady = !!llmOn;
  $("#chipLlm").textContent = llmOn ? ("LLM " + l.model) : "LLM 本地解读";
  $("#chipLlm").className = "chip " + (llmOn ? "good" : "");
  $("#scrDataHint").innerHTML = d.feature_count +
    ' 个<span class="term" data-term="factor">因子</span> · <a href="#" data-act="preheat">补行情</a>';
  if ($("#scrFeatCount")) $("#scrFeatCount").textContent = d.feature_count;
  const sn = d.snapshot || {}, ds = d.dataset || {};
  $("#scrHint").textContent = "快照 " + (sn.loaded ? sn.rows + " 行 / " + (sn.date_max || "—") : "无") +
    " · 训练集 " + (ds.loaded ? ds.rows + " 行" : "无");
  pipeLoad();          // 顺带检查四段数据链是否跟上，过期就在顶栏亮个提醒
  sideTodoLoad(true);  // 左下角待办：行情/快照/训练集/模型/行业缺什么（这里已有最新状态，强制刷新）
  return r;
}
function scrLocalReason(x) {
  // 未调用 LLM 时给一句可解释的短评；数字列里已有的不再重复，避免整行被文字撑高
  const bits = [];
  if (x.mom20 != null) bits.push("动量 " + scrFmt(x.mom20, 0) + "%");
  if (x.vol20 != null) bits.push("波动 " + scrFmt(x.vol20, 0) + "%");
  if (x.rsi14 != null) bits.push("RSI " + scrFmt(x.rsi14, 0));
  return (bits.join(" · ") || "因子数据不足") + " · 本地规则";
}
function indCell(x) {
  const name = x.industry || "未分类";
  const r = (x.ind_rank == null) ? "" : '<i title="本行业内打分百分位">' +
    Math.round(Number(x.ind_rank) * 100) + "%</i>";
  return '<td class="ind-cell"><span>' + esc(name) + "</span>" + r + "</td>";
}
function scrDetailHtml(x) {
  const bits = Object.keys(SCR_FACTOR_LABELS).map((k) =>
    "<div>" + SCR_FACTOR_LABELS[k] + "：<b>" + scrFmt(x[k], 3) + "</b></div>").join("");
  const rk = (x.ind_rank == null) ? "—" :
    ("行业内前 " + Math.max(1, Math.round((1 - Number(x.ind_rank)) * 100)) + "%（共 " +
      (x.ind_n || "?") + " 只样本）");
  const bearBits = (x.bear_case && x.bear_case.length)
    ? '<div class="bear-inline"><b class="bear-inline-h">空头辩护</b>' +
      x.bear_case.map((b) => '<div class="bear-li"><span>' + esc(b.point) + "</span>" +
        (b.trigger ? "<i>触发：" + esc(b.trigger) + "</i>" : "") +
        (b.watch ? "<i>观察：" + esc(b.watch) + "</i>" : "") + "</div>").join("") +
      (x.llm_reason ? '<p class="note" style="margin:4px 0 0">裁决：' + esc(x.llm_reason) + "</p>" : "") +
      "</div>"
    : "";
  return '<tr class="scr-detail-row" data-detail="' + x.code + '"><td colspan="11">' +
    '<div class="scr-detail-grid">' + bits +
    "<div>板块：<b>" + esc(x.market || "—") + "</b></div>" +
    "<div>行业：<b>" + esc(x.industry || "待补齐") + "</b></div>" +
    "<div>行业内排名：<b>" + esc(rk) + "</b></div>" +
    "<div>基准分：<b>" + scrFmt(x.base_score, 3) + "</b></div>" +
    "<div>数据日：<b>" + esc(x.date || "—") + "</b></div></div>" + bearBits +
    '<div class="scr-detail-chart">' +
    '<div class="scr-ch-h"><b>' + esc(x.code) + " " + esc(x.name) + "</b>" +
      '<span class="scr-ch-note" title="表格收盘价已换算成真实价（未复权），与分时图可直接对照；因子与收益按后复权口径计算">当日分时 · 真实价</span>' +
      '<span class="scr-ch-q" id="scrChQ_' + x.code + '">读取分时…</span></div>' +
    '<div id="scrCh_' + x.code + '" class="chart scr-mini"></div></div>' +
    '<div class="scr-detail-actions">' +
    '<button class="opb" data-act="chart" data-code="' + x.code + '">完整行情 ↗</button>' +
    '<button class="opb" data-act="backtest" data-code="' + x.code + '">看这只票的回测</button>' +
    '<button class="opb" data-act="pooladd" data-code="' + x.code + '" data-name="' + esc(x.name) +
    '">加入股票池</button></div></td></tr>';
}
function renderScrRows() {
  // innerHTML 重绘会冲掉容器，先释放旧的分时图实例，避免挂在游离节点上
  Object.keys(S.charts).forEach((id) => {
    if (id.indexOf("scrCh_") === 0) {
      try { S.charts[id].dispose(); } catch (e) {}
      delete S.charts[id];
    }
  });
  const rows = SCR.llmRows || SCR.candidates;
  // 辩护结论建一次索引：原来是每行 find() 一遍（O(n²)），
  // 20 只票时无感，但工作台一次渲染会连同展开行一起重跑，能省就省。
  const bmap = new Map();
  (BEAR.data && BEAR.data.rows || []).forEach((y) => bmap.set(String(y.code), y));
  $("#scrBody").innerHTML = rows.map((x) => {
    const detail = SCR.detail.has(x.code) ? scrDetailHtml(x) : "";
    const pctCell = x.pct == null ? "—" : signed(x.pct);
    // 辩护结论列：数据来自 BEAR.data（工作台跑完辩护就存在内存里），
    // 与选股记录页共用同一个 bearCellHtml，两边显示一致。
    const bm = bmap.get(String(x.code));
    return "<tr><td><span class='rank'>" + (x.rank_llm || x.rank) + "</span></td>" +
      '<td><button type="button" class="codelink" data-code="' + x.code +
      '" aria-label="展开 ' + esc(x.name) + ' 的因子详情">' + x.code + "</button></td>" +
      "<td>" + esc(x.name) + "</td>" + indCell(x) +
      "<td title=\"" + (x.close_real != null && Math.abs(x.close_real - x.close) > 0.01
        ? "真实价；后复权口径 " + scrFmt(x.close)
        : "选股日收盘价") + "\">" + scrFmt(x.close_real == null ? x.close : x.close_real) + "</td>" +
      '<td class="' + (x.pct == null ? "" : signCls(x.pct)) + '">' + pctCell + "</td>" +
      bearCellHtml(bm) +
      '<td class="score">' + scrFmt(x.score, 3) + "</td>" +
      "<td>" + scrFmt(x.mom20, 1) + "%</td>" +
      (x.llm_score == null
        ? (SCR.llmMode === "local"
          ? '<td><span class="pill no">本地</span></td>'
          : '<td class="llm-empty">未启用</td>')
        : '<td class="score">' + scrFmt(x.llm_score, 1) + "</td>") +
      '<td><span class="reason">' + esc(x.reason || scrLocalReason(x)) + "</span></td></tr>" + detail;
  }).join("");
  const has = rows.length > 0;
  $("#scrEmpty").style.display = has ? "none" : "block";
  $("#scrEmptyActions").style.display = has ? "none" : "flex";
  // 展开行直接内嵌分时图，不用再点「看走势」跳走
  SCR.detail.forEach((code) => scrChartLoad(code));
}
/* 候选榜展开行的行内分时图：代码进 SCR.detail 就会自动加载 */
async function scrChartLoad(code) {
  const box = document.getElementById("scrCh_" + code);
  if (!box) return;
  let c;
  try { c = chart("scrCh_" + code); } catch (e) { return; }
  const q = document.getElementById("scrChQ_" + code);
  if (q) q.innerHTML = '<span class="scr-dim">读取分时…</span>';
  let r;
  try { r = await api("/api/chart?code=" + encodeURIComponent(code) + "&period=intraday"); }
  catch (e) { r = { ok: false, error: String(e) }; }
  // 等待期间用户可能收起了这一行或重绘过，容器已不属于当前 DOM
  if (!document.getElementById("scrCh_" + code) || !SCR.detail.has(code)) return;
  if (!r.ok) {
    c.clear();
    if (q) q.innerHTML = '<span class="scr-dim">' + esc(r.error || "分时读取失败") + "</span>";
    return;
  }
  if (q) q.innerHTML = mkQuoteHtml(r);
  mkDrawMinute(r, c, true);
  c.resize();
}
function toggleScrDetail(code) {
  if (SCR.detail.has(code)) SCR.detail.delete(code); else SCR.detail.add(code);
  renderScrRows();
}
/* 「重新选股」彩虹流光：只有「今天还没选过 + 榜单数据已过期」才提醒。
   不能只看数据日（as_of）距今多久——行情天然滞后一两个交易日，
   那样即使当天已经选过股，第二天早上按钮还是会一直闪。 */
const SCR_RUN_KEY = "quantdesk_scr_last_run";
function scrLastRunDay() {
  try { return String(localStorage.getItem(SCR_RUN_KEY) || "").slice(0, 10); }
  catch (e) { return ""; }
}
function scrMarkRun(day) {
  const d = /^\d{4}-\d{2}-\d{2}$/.test(String(day || "")) ? String(day) : todayStr();
  try { localStorage.setItem(SCR_RUN_KEY, d); } catch (e) { /* 隐私模式下写不了，忽略 */ }
}
function scrIsStale(r) {
  // 行情落后时「今天已经选过」不算数：同一批旧数据只会选出同一批票，
  // 提示必须一直亮着，直到数据补上来。
  const ql = Number((PIPE.data && PIPE.data.quote_lag) || 0);
  if (ql > 0) return true;
  if (scrLastRunDay() === todayStr()) return false;   // 今天已经选过：不再提醒
  const day = r && r.as_of ? String(r.as_of).slice(0, 10) : "";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  // 数据日是当天收盘截面，从收盘 15:00 起算满 24 小时才算过期
  const close = new Date(day + "T15:00:00");
  return isFinite(close.getTime()) && Date.now() - close.getTime() > 24 * 3600 * 1000;
}
function scrMarkStale(on) {
  const btn = $("#btnScrRun");
  if (!btn) return;
  btn.classList.toggle("stale", !!on);
  btn.title = on ? "今天还没留下选股记录，且榜单数据已超过 24 小时，建议重新选股留档" : "";
}
/** 首屏恢复：直接显示上一次落盘的候选，不重算。
    数据是否过期交给「重新选股」按钮的彩虹流光提示（scrIsStale），
    而不是每次打开页面都默默重建全市场快照。 */
async function screenResume() {
  let r;
  try { r = await api("/api/screen/resume"); }
  catch (e) { return false; }
  if (!r || !r.ok || !r.candidates || !r.candidates.length) return false;

  SCR.last = r;
  SCR.candidates = r.candidates || [];
  SCR.llmRows = null; SCR.llmMode = "";
  SCR.detail.clear();
  renderScrRows();
  renderFunnel(r.funnel, r.industry, r.candidates.length);
  // 行业分布：resume 恢复的是「上一次落盘」的那批候选，行业暴露按候选现场重算
  renderExposure(r.industry && r.industry.exposure, r.neutral);
  const tag = $("#scrAsOfTag");
  if (tag) tag.textContent = r.as_of ? ("数据日 " + r.as_of.slice(5)) : "";
  const pill = $("#scrScorePill");
  if (pill) {
    pill.textContent = "上次候选 · " + r.candidates.length + " 只";
    pill.className = "pill off";
  }
  const n = $("#scrSampleN");
  if (n) {
    // 右上角是「参与打分的样本数」，不是候选只数；和 screenRun 用同一口径/格式。
    const sampleN = Number(r.count || 0);
    n.textContent = sampleN > 0 ? sampleN.toLocaleString("zh-CN") : "—";
  }
  const h = $("#scrHeadline");
  if (h) h.textContent = "上次落盘的候选 · 点「重新选股」生成新结果";
  const note = $("#scrScoreNote");
  if (note) note.textContent = "显示上一次选股记录（" +
    String(r.last_pick_at || r.as_of || "").slice(5, 16) + "），不是重新计算的结果。";

  // 彩虹提醒沿用原有判据：今天是否选过 + 数据是否过期
  scrMarkStale(scrIsStale(r));

  // 恢复同批次的空头辩护与 LLM 解读（都随批次存档，读回不烧 token）：
  // 候选榜 / 辩护 / 解读本该是同一批记录的三张面孔，刷新一次不能只剩候选。
  if (r.bear && r.bear.mode && r.bear.mode !== "off") { BEAR.data = r.bear; bearRender(r.bear); }
  else bearRestore();
  scrLlmRestore(r.as_of || "");
  // 恢复的榜单同样能导出：以前只有「重新选股」成功后才显示这个按钮
  if ($("#btnScrExport")) $("#btnScrExport").style.display = "inline-block";
  return true;
}

async function screenRun(silent, btn) {
  if (SCR.busy) return;
  SCR.busy = true;
  if (silent) {
    // 首屏静默自动出数：8 秒仍无结果就显式给出重试入口，避免停在「正在生成…」
    clearTimeout(SCR.autoTimer);
    SCR.autoTimer = setTimeout(() => {
      if (!SCR.candidates.length) {
        $("#scrEmptyText").textContent = "生成偏慢或数据不足，可手动重试，或先预热行情。";
        $("#scrEmptyActions").style.display = "flex";
      }
    }, 8000);
  }
  const work = async () => {
    if (!silent) {
      sysTaskStart("screen", "选股");
      $("#scrSampleN").textContent = "…";
      $("#scrHeadline").textContent = "正在用本地行情生成横截面候选…";
    }
    const p = scrRunParams();
    // 空头辩护要不要跑由后端判断（未启用时 bear_case 直接返回 mode=off，不会剔票）。
    // 只有用户主动选股才带上，首屏静默出数不烧 token。
    p.bear = !silent;
    if (silent) {
      p.reuse_snapshot = true;   // 首屏优先复用；后端缺快照时自动构建
      // 首屏自动出数是「展示用」的，不能写进选股记录：否则每刷新一次页面就把当期的
      // 历史记录覆盖成这次自动跑的结果，用户手动选的 20 只被悄悄换成 10 只。
      p.record = false;
    }
    let r;
    try { r = await post("/api/screen/run", p); }
    finally { if (!silent) sysTaskEnd("screen"); }
    clearTimeout(SCR.autoTimer);
    if (!r.ok) {
      $("#scrScorePill").textContent = "暂无候选";
      $("#scrScorePill").className = "pill off";
      $("#scrSampleN").textContent = "0";
      $("#scrHeadline").textContent = "本期没有可用候选：" + (r.error || "请先预热行情");
      scrMarkStale(false);
      $("#scrEmptyText").textContent = "暂无候选。请先到「股票池 → 全 A 股名单」预热行情，再回来选股。";
      $("#scrEmpty").style.display = "block";
      $("#scrEmptyActions").style.display = "flex";
      toast("选股失败：" + (r.error || "未知错误"), "err");   // 自动出数失败也要让用户看见
      return;
    }
    SCR.last = r; SCR.candidates = r.candidates || []; SCR.llmRows = null; SCR.llmMode = "";
    // 「今天选过」以真正落盘的记录为准：首屏静默出数（record=false）只是把界面填满，
    // 不算用户的一次选股，否则每次打开页面都会把彩虹提醒永久压掉。
    const pickDay = String(r.last_pick_at || "").slice(0, 10);
    const recorded = !!(r.record && r.record.ok && !r.record.skipped);
    if (pickDay === todayStr() || recorded) scrMarkRun(pickDay === todayStr() ? pickDay : "");
    SCR.detail.clear();
    renderScrRows();
    renderFunnel(r.funnel, r.industry, SCR.candidates.length);
    // 后端只在开启时才会带 bear；静默出数（不烧 token）则取回这一批已存的辩护，
    // 让「候选榜 + 辩护」永远成对出现
    const b = r.bear || null;
    if (b && b.mode && b.mode !== "off") { BEAR.data = b; bearRender(b); }
    else bearRestore();
    renderExposure(r.industry && r.industry.exposure, r.neutral);
    scrLlmBadge("未重排", false);
    const lbl = r.engine === "lightgbm" ? "LightGBM 模型分" :
      (r.engine === "ridge-fallback" ? "岭回归降级分" : "基准分");
    $("#scrScorePill").textContent = lbl;
    $("#scrScorePill").className = "pill " + (r.engine === "baseline" ? "no" : "live");
    const sampleN = Number(r.count || 0);
    $("#scrSampleN").textContent = isFinite(sampleN) ? sampleN.toLocaleString("zh-CN") : "—";
    $("#scrHeadline").textContent = "按打分取前 " + SCR.candidates.length + " 只" +
      (r.industry && r.industry.cap != null ? " · 单行业上限 " + r.industry.cap : "");
    scrMarkStale(scrIsStale(r));
    const tagEl = $("#scrAsOfTag");
    const tg = [];
    if (r.as_of) tg.push("数据截至 " + r.as_of);
    if (r.is_historical) tg.push("历史截止日");
    // 行情落后时把话写在榜单头上：连着两天选出同一批票，十有八九是数据日
    // 没往前走，不是模型变懒；不点出来用户只会以为选股没生效。
    const qlag = Number((PIPE.data && PIPE.data.quote_lag) || 0);
    if (qlag > 0) {
      tg.push("行情落后 " + qlag + " 个交易日" +
        (PIPE.data.expected ? "（应到 " + PIPE.data.expected + "）" : ""));
    }
    // 复用当期截面时说明一句：数字是现算的，只是没重新扫全市场原始 K 线
    if (r.snapshot_reused) tg.push("复用本期截面");
    tagEl.textContent = tg.join(" · ");
    tagEl.classList.toggle("warn", !!r.is_historical || qlag > 0);
    tagEl.title = r.snapshot_reused
      ? "截面数据日已是最新交易日，且股票范围、样本上限都与请求一致，直接复用磁盘快照；改了其中任一项都会自动重建"
      : "";
    SCR.noteBase = r.score_label + " · " +
      (NEUTRAL_LABEL[(r.neutral && r.neutral.mode) || ""] || "—");
    $("#scrScoreNote").textContent = SCR.noteBase;
    const warnEl = $("#scrWarn");
    if (warnEl) { warnEl.textContent = r.warning || ""; warnEl.hidden = !r.warning; }
    $("#btnScrExport").style.display = "inline-block";
    const recErr = r.record && r.record.ok === false ? (r.record.error || "未知错误") : "";
    if (!silent) {
      toast(recErr ? ("候选已算出，但选股记录落盘失败：" + recErr)
        : ("选股完成：" + SCR.candidates.length + " 只候选"), recErr ? "err" : "ok");
    }
    // 已启用且配置完整时，手动选股后自动重排；首屏静默出数不触发，避免每次刷新都烧 token
    if (!silent && ($("#scrAutoLlm").checked || SCR.llmReady)) await screenLlm();
    else await scrLlmRestore(r.as_of);
  };
  try { if (btn) await withBusy(btn, work); else await work(); }
  finally { SCR.busy = false; }
}
async function screenSnapshot(btn) {
  const work = () => withBusy(btn, async () => {
    const p = scrRunParams();
    sysTaskStart("snapshot", "生成横截面快照");
    let r;
    try { r = await post("/api/screen/snapshot", { scope: p.scope, as_of: p.as_of, limit: p.limit }); }
    finally { sysTaskEnd("snapshot"); }
    if (!r.ok) { toast("快照失败：" + (r.error || "未知错误"), "err"); return; }
    toast("快照完成：" + r.count + " 只 / 截至 " + r.date_max +
      (r.warning ? "（日期不齐，建议预热）" : ""), "ok");
    await screenStatus();
  });
  const has = !!(SCR.status && SCR.status.data.snapshot && SCR.status.data.snapshot.loaded);
  if (btn && has) twoStep(btn, "确认覆盖快照", work);
  else await work();
}
async function screenDataset(btn) {
  const work = () => withBusy(btn, async () => {
    const p = scrRunParams(), d = scrTrainDates();
    // 构造训练集是为了训练「新」模型，口径不该跟随当前模型（那正是要被替换的东西）；
    // 「自动」取实测最优的出厂口径：原始因子 + 横截面分位标签
    const neutralForTrain = p.neutral === "auto" ? "none" : p.neutral;
    sysTaskStart("dataset", "构造训练集");
    let r;
    try {
      r = await post("/api/screen/dataset", {
        start: d.start, end: d.end, horizon: p.horizon, step: p.step,
        max_symbols: p.max_symbols, scope: p.scope,
        neutral: neutralForTrain, label_mode: p.label_mode
      });
    } finally { sysTaskEnd("dataset"); }
    if (!r.ok) { toast("数据集构造失败：" + (r.error || "未知错误"), "err"); return; }
    const lm = r.label_mode === "rank" ? "横截面分位标签"
      : r.label_mode === "industry" ? "行业内超额标签" : "全市场超额标签";
    toast("训练集：" + r.rows + " 行 / " + r.cross_sections + " 期 / " + r.symbols + " 只 · " +
      (NEUTRAL_LABEL[r.neutral] || "—") + " · " + lm +
      (r.neutral_note ? "（" + r.neutral_note + "）" : ""), "ok");
    await screenStatus();
  });
  const has = !!(SCR.status && SCR.status.data.dataset && SCR.status.data.dataset.loaded);
  if (btn && has) twoStep(btn, "确认覆盖训练集", work);
  else await work();
}
async function screenTrain(btn) {
  const work = async () => {
    sysTaskStart("train", "训练 LightGBM");
    let r;
    try { r = await post("/api/screen/train", {}); }
    finally { sysTaskEnd("train"); }
    if (!r.ok) { toast("训练失败：" + (r.error || "未知错误"), "err"); return; }
    const mt = r.metrics || {};
    toast("训练完成：" + r.engine + " · 验证 IC " + scrFmt(mt.ic, 4) +
      " · RankIC " + scrFmt(mt.rank_ic, 4) +
      (mt.top20_ind == null ? "" : " · 行业超额 " + scrFmt(mt.top20_ind, 3) + "%"), "ok");
    await screenStatus();   // 用服务端真实状态刷新，不构造假的 status 对象
  };
  if (btn && SCR.status && SCR.status.model && SCR.status.model.trained) {
    twoStep(btn, "确认覆盖重训", () => withBusy(btn, work));
  } else {
    await withBusy(btn, work);
  }
}
async function scrLlmRestore(asOf) {
  // 恢复上次重排：分数/理由取存档，价格用本次候选。
  //
  // 以前要求「存档条数 === 当前候选数 且 every(code in 候选)」，一旦 TopN 改过
  // （比如存档是 TopN=18、首屏默认 TopN=10）就不相等，直接放弃恢复 ——
  // 界面上表现为「刷新一下 LLM 分析就没了」，其实数据一直在库里。
  // 现在改成按代码取交集：匹配上的套回解读，没匹配上的保持原样。
  try {
    const rec = (SCR.last && SCR.last.record && SCR.last.record.recorded_at) || "";
    const s = await post("/api/screen/llm", {
      action: "last", as_of: asOf || "", recorded_at: rec,
    });
    const sv = s && s.saved;
    if (!sv || !sv.items || !sv.items.length || !SCR.candidates.length) return;
    // 数据日不同也不硬拦：只取能对上的代码，对不上的不显示解读。
    const m = new Map(sv.items.filter((x) => x && x.code).map((x) => [x.code, x]));
    const hit = SCR.candidates.filter((x) => m.has(x.code));
    if (!hit.length) return;                       // 一只都对不上才放弃
    const rows = SCR.candidates.map((x) => Object.assign({}, x, m.get(x.code) || {}));
    // 只对匹配到的那部分按 rank_llm 排序，没匹配的留在原位（按原 rank）
    rows.sort((a, b) => {
      const ra = (a && a.rank_llm) || 999, rb = (b && b.rank_llm) || 999;
      if (ra !== rb) return ra - rb;
      return ((a && a.rank) || 999) - ((b && b.rank) || 999);
    });
    SCR.llmRows = rows; SCR.llmMode = sv.mode;
    renderScrRows();
    const isLlm = sv.mode === "llm";
    const part = hit.length < SCR.candidates.length
      ? "（" + hit.length + "/" + SCR.candidates.length + " 只有解读）" : "";
    scrLlmBadge((isLlm ? "LLM · " + (sv.model || "") : "本地规则解释") + " · 已恢复" + part, isLlm);
    // resume 路径在 scrScoreNote 写了「显示上一次选股记录」，不能整体覆盖；
    // LLM 信息并到后面，两者共存。
    const base = $("#scrScoreNote").textContent || "";
    const llmBit = (isLlm ? "LLM 重排" : "本地规则解读") +
      "（" + esc(String(sv.saved_at || "").slice(5, 16)) + " 存档）";
    $("#scrScoreNote").textContent = (base || (SCR.noteBase || "")) + " · " + llmBit;
  } catch (e) { /* 恢复失败不影响候选展示 */ }
}
async function screenLlm(btn) {
  if (!SCR.candidates.length) { toast("请先点「重新选股」生成候选", "err"); return; }
  await withBusy(btn, async () => {
    if (btn) btn.textContent = "解读中…";
    sysTaskStart("screen-llm", "候选解读");
    let r;
    try {
      r = await post("/api/screen/llm", {
        action: "rerank", candidates: SCR.candidates,
        as_of: (SCR.last && SCR.last.as_of) || "", horizon: scrNum("#scrHorizon", 10),
        // 带上批次号，后端按 (as_of, recorded_at) 入库，换一批不会互相覆盖
        batch: {
          as_of: (SCR.last && SCR.last.as_of) || "",
          recorded_at: ((SCR.last && SCR.last.record) || {}).recorded_at || "",
        },
      });
    } finally { sysTaskEnd("screen-llm"); }
    if (!r.ok) { toast("LLM 重排失败：" + (r.error || "未知错误"), "err"); return; }
    const isLlm = r.mode === "llm";
    const localMsg = r.warning || "当前为本地规则解读（未调用大模型）";
    const failed = !isLlm && /失败|无法解析|未返回|为空|不是可解析/.test(localMsg);
    SCR.llmRows = r.rows; SCR.llmMode = isLlm ? "llm" : "local";
    renderScrRows();                       // 表格立刻反映「本地/LLM」两种结果，不再看起来没变化
    scrLlmBadge(isLlm ? ("LLM · " + (r.model || "")) : (failed ? "本地回退" : "本地规则解释"), isLlm);
    const mode = isLlm
      ? ("LLM 重排 · " + (r.model || "—") + " · " + r.latency_ms + "ms")
      : "本地规则解读（未调用大模型）";
    $("#scrScoreNote").textContent = (SCR.noteBase || "") + " · " + mode;
    // 失败时把 finish_reason / reasoning_tokens / 原始片段放进配置卡的诊断折叠区，便于自查
    renderLlmDiag(isLlm ? null : (r.diag || { finish_reason: "", content_len: 0 }), isLlm ? "" : localMsg);
    if (isLlm) {
      toast("已用 " + (r.model || "LLM") + " 重排 " + r.rows.length + " 只候选（" + r.latency_ms + "ms）", "ok");
    } else if (failed) {
      toast(localMsg + " 诊断信息已写在「数据中枢 → AI 服务 → 上次调用」里。", "err");
    } else {
      toast("这次没有调用大模型：本地规则解读已填入「一眼理由」。要真正重排，请在「数据中枢 → AI 服务」填完整并保存。", "err");
    }
  });
}
function scrLlmBadge(text, on) {
  const el = $("#scrLlmMode");
  if (!el) return;
  el.textContent = text;
  el.className = "pill " + (on ? "yes" : "no");
}

function renderFunnel(fn, ind, picked) {
  const box = $("#scrFunnel");
  const sub = $("#scrSampleSub");
  const stages = (fn && fn.stages) || [];
  if (!stages.length) {
    if (box) box.innerHTML = "";
    if (sub) sub.textContent = "";
    return;
  }
  // 结论摘要（共剔除多少 / 行业上限剔了多少 / 覆盖几个行业）并进样本池那一格，
  // 不再单独占一行 —— 它描述的就是「这个数字是怎么来的」，本来就该贴着数字。
  if (sub) {
    const capNote = (ind && ind.note) ? ind.note : "";
    const dist = (ind && ind.counts) ? Object.keys(ind.counts).length : 0;
    const bits = [];
    if (fn && fn.dropped) bits.push("共剔除 " + fn.dropped + " 只");
    if (capNote) bits.push(capNote);
    if (dist) bits.push("覆盖 " + dist + " 个行业");
    sub.textContent = bits.join(" · ");
    sub.hidden = !bits.length;
  }
  // 漏斗链条本身仍画在表格上方（它是可逐层核对的明细，不塞进小格子）
  if (!box) return;
  // 只展示真正改变了数量的层：连续几个相同数字会让链条看起来很长且没有信息量
  const shown = stages.filter((s, i) => i === 0 || Number(s.dropped || 0) > 0);
  const arrows = [];
  shown.forEach((s, i) => {
    if (i) arrows.push('<span class="scr-fn-arrow">›</span>');
    const dropped = s.dropped ? ('<em>−' + s.dropped + "</em>") : "";
    arrows.push('<span class="scr-fn-step' + (s.enabled ? "" : " off") + '" title="' +
      esc(s.note || "") + '"><b>' + s.after + "</b>" + dropped + "<i>" + esc(s.name) + "</i></span>");
  });
  arrows.push('<span class="scr-fn-arrow">›</span>');
  arrows.push('<span class="scr-fn-step final"><b>' + (picked || 0) + "</b><i>入选候选</i></span>");
  box.innerHTML = '<div class="scr-fn-track">' + arrows.join("") + "</div>";
}

function renderExposure(expo, ne) {
  const box = $("#scrExpo");
  const stat = $("#scrExpoDesc");
  if (!box) return;
  const pill = $("#scrExpoPill"), hint = $("#scrExpoHint");
  if (!expo || !expo.ok) {
    box.innerHTML = '<p class="note">' + esc((expo && expo.note) || "本次没有可统计的行业分布。") + "</p>";
    if (stat) stat.innerHTML = "";
    if (pill) { pill.textContent = "待计算"; pill.className = "pill no"; }
    if (hint) hint.textContent = "";
    return;
  }
  const rows = expo.rows || [];
  // 横向堆叠柱：每行一根条，蓝色段 = 组合暴露，灰色段 = 全市场基线，
  // 两段首尾相接，条长按「暴露 + 基线」的全局最大值归一。
  // 这样既能横向比较各行业总量，又能一眼看出暴露比基线多出多少；
  // 行内默认只显示暴露百分比，基线数值放在悬停提示里，避免三列数字挤在一起。
  const sumOf = (r) => (Number(r.weight) || 0) + (Number(r.base_weight) || 0);
  const maxSum = Math.max.apply(null, rows.map(sumOf).concat([1]));
  const body = rows.map((r) => {
    const w = Number(r.weight) || 0;
    const b = r.base_weight == null ? null : (Number(r.base_weight) || 0);
    const expW = w / maxSum * 100;
    const baseW = b == null ? 0 : b / maxSum * 100;
    const diff = b == null ? null : w - b;
    // 悬停走统一气泡（data-tip + data-tip-name），与辩护/状态灯同一套外框；
    // 原生 title 的样式改不了，会和它们像两个组件。
    const tipBody = "暴露 " + scrFmt(w, 1) + "%" +
      (b == null ? " · 基线 数据缺失"
        : " · 基线 " + scrFmt(b, 1) + "% · " +
          (diff >= 0 ? "高出 " : "低于 ") + scrFmt(Math.abs(diff), 1) + " 个百分点");
    return '<div class="scr-exp-row" data-tip="' + esc(tipBody) +
      '" data-tip-name="' + esc(r.industry) + '" aria-label="' +
      esc(r.industry + " " + tipBody) + '">' +
      '<span class="nm">' + esc(r.industry) + "</span>" +
      '<span class="bar">' +
      '<i class="exp" style="width:' + expW.toFixed(2) + '%"></i>' +
      '<i class="base" style="width:' + baseW.toFixed(2) + '%"></i>' +
      "</span>" +
      '<span class="vl">' + scrFmt(w, 1) + "%</span></div>";
  }).join("");
  const kv = [
    ["集中度 HHI", scrFmt(expo.hhi, 3)],
    ["有效行业数", scrFmt(expo.effective_n, 1) + " 个"],
    ["行业覆盖", expo.industries + " 个 / " + expo.n + " 只" +
      (expo.unclassified ? "（含 " + expo.unclassified + " 只未分类）" : "")],
    ["最大暴露", esc(expo.max_industry) + " · " + scrFmt(expo.max_weight, 1) + "%"],
    ["打分口径", esc(NEUTRAL_LABEL[(ne && ne.mode) || ""] || "—")]
  ];
  if (stat) {
    stat.innerHTML = '<div class="scr-kv">' + kv.map((p) =>
      '<div class="k">' + p[0] + '</div><div class="v">' + p[1] + "</div>").join("") + "</div>" +
      (expo.rows_more ? '<p class="note">另有 ' + expo.rows_more + " 个行业未显示。</p>" : "");
  }
  box.innerHTML = body ||
    '<p class="note">入选组合没有可展示的行业权重。</p>';
  const concentrated = Number(expo.hhi) > 0.2;
  if (pill) {
    pill.textContent = concentrated ? "偏集中" : "分散良好";
    pill.className = "pill " + (concentrated ? "sim" : "yes");
  }
  if (hint) hint.textContent = "蓝 = 组合暴露 · 灰 = 全市场基线 · 悬停看两者";
}

function screenReview(btn, silent) {
  const work = async () => {
    const box = $("#scrReview");
    if (!silent) box.innerHTML = '<p class="note" style="margin:0">正在重放历史各期…</p>';
    let r;
    try { r = await api("/api/screen/review?days=60&step=5&topn=" + scrNum("#scrTopN", 20)); }
    catch (e) { r = { ok: false, error: String(e) }; }
    if (!r.ok) {
      box.innerHTML = '<p class="note" style="margin:0">后验失败：' + esc(r.error || "未知错误") + "</p>";
      if (!silent) toast("后验失败：" + (r.error || "未知错误"), "err");
      return;
    }
    renderReview(r);
    if (!silent) toast("后验已重算：" + r.periods + " 期 · " + (r.engine === "baseline" ? "基准分" : r.engine), "ok");
  };
  return btn ? withBusy(btn, work) : work();
}

function renderReview(r) {
  const hs = r.horizons || [];
  const tag = $("#scrReviewTag");
  if (tag) tag.textContent = r.periods + " 期 · Top" + r.topn;
  const cell = (v) => {
    if (v == null) return '<td class="dim">—</td>';
    return '<td class="' + signCls(v) + '">' + (v > 0 ? "+" : "") + v + "%</td>";
  };
  // 左列只有 348px，九列横表会被压成一条缝：把「周期」做成列、「指标」做成行，
  // 反而更好横着比 —— 同一个指标三个持有周期一眼看完。
  const metrics = [
    ["平均收益", (h) => cell(h.avg_ret)],
    ["超额·全市场", (h) => cell(h.avg_excess)],
    ["超额·同行业", (h) => cell(h.avg_ind_excess)],
    ["胜率", (h) => '<td>' + (h.win_rate == null ? "—" : h.win_rate + "%") + "</td>"],
    ["跑赢率", (h) => '<td>' + (h.beat_rate == null ? "—" : h.beat_rate + "%") + "</td>"],
    ["最好", (h) => cell(h.best)],
    ["最差", (h) => cell(h.worst)]
  ];
  let html = '<div class="scroll"><table class="scr-review-table"><thead><tr><th>指标</th>' +
    hs.map((h) => '<th class="rv-r">T+' + h.h + "</th>").join("") + "</tr></thead><tbody>";
  metrics.forEach((m) => {
    html += "<tr><td class=\"rv-k\">" + m[0] + "</td>" + hs.map((h) => m[1](h)).join("") + "</tr>";
  });
  html += "</tbody></table></div>";
  const tr = r.track || {};
  html += '<p class="scr-fn-note">' + (r.universe || 0) + " 只可评估 · 落盘记录 " + (tr.rows || 0) + " 条 / " +
    (tr.periods || 0) + " 期，已到期 " + (tr.matured_periods || 0) + " 期。";
  if (tr.horizons && tr.horizons.length) {
    html += "　" + tr.horizons.map((h) => "T+" + h.h + " 超额 " +
      (h.avg_excess == null ? "—" : (h.avg_excess > 0 ? "+" : "") + h.avg_excess + "%")).join("　");
  }
  html += "</p>";
  if (r.sample === "in_sample") {
    html += '<p class="note" style="margin:0 0 6px;color:var(--amber)">' +
      "样本内回放：模型训练时已见过这段历史，指标偏乐观；真实表现请看下方「落盘记录」的 T+N。</p>";
  }
  if (r.note) {
    html += '<details class="mini" style="margin-top:6px"><summary>口径说明</summary>' +
      '<p class="hint" style="margin:5px 0 0">' + esc(r.note) + "</p></details>";
  }
  $("#scrReview").innerHTML = html;
}

function screenIndustrySync(btn) {
  return indSync(btn);
}
async function screenPoolAdd(code, name) {
  const r = await post("/api/watchlist", { action: "add", code: code, name: name || "" });
  toast(r.ok ? ("已加入股票池：" + code) : ("加入失败：" + (r.error || "未知错误")), r.ok ? "ok" : "err");
}
function renderLlmDiag(diag, warning) {
  const box = $("#hubLlmDiag");
  if (!box) return;
  if (!diag) { box.innerHTML = ""; return; }
  const rt = Number(diag.reasoning_tokens || 0);
  const hint = rt > 0
    ? "推理型模型把 " + rt + " 个 token 花在思考上（预算 " + (diag.max_tokens || 0) +
      "）。把「最大输出 tokens」调到 3000 以上再试。"
    : (diag.finish_reason === "length" ? "输出被截断，加大「最大输出 tokens」。" : "");
  box.innerHTML = '<details class="mini" style="margin-top:10px"><summary>上次调用的诊断信息</summary>' +
    '<div class="inner"><p class="note" style="margin:0 0 6px">finish_reason：<b>' +
    esc(diag.finish_reason || "—") + "</b> · reasoning_tokens：<b>" + (diag.reasoning_tokens || 0) +
    "</b> · 预算：<b>" + (diag.max_tokens || 0) + "</b> · 内容长度：<b>" + (diag.content_len || 0) +
    "</b></p>" + (hint ? '<p class="note" style="margin:0 0 6px;color:var(--amber)">' + esc(hint) + "</p>" : "") +
    (warning ? '<p class="note" style="margin:0 0 6px">' + esc(warning) + "</p>" : "") +
    (diag.raw_preview ? '<pre class="scr-raw">' + esc(diag.raw_preview) + "</pre>" : "") +
    "</div></details>";
}
function screenExport() {
  const rows = SCR.llmRows || SCR.candidates;
  if (!rows.length) return;
  const keys = ["rank", "code", "name", "industry", "ind_rank", "ind_n", "market", "date", "close",
    "score", "base_score", "ind_mom20", "ind_rel20",
    "mom5", "mom20", "mom60", "vol20", "vol_ratio", "rsi14", "ma_dev", "macd_hist",
    "llm_score", "reason"];
  const head = "排名,代码,名称,行业,行业内百分位,行业样本数,板块,日期,收盘(真实),打分,基准分," +
    "行业20日动能,行业内相对动量,5日动量,20日动量,60日动量,20日波动,量比,RSI,均线偏离,MACD柱,LLM分,解读\n";
  const body = rows.map((x) => keys.map((k) => {
    let v = x[k];
    if (k === "close" && x.close_real != null) v = x.close_real;
    v = v == null ? "" : String(v).replace(/[,\n\r]/g, " ");
    return k === "code" ? '="' + v + '"' : v;
  }).join(",")).join("\n");
  const blob = new Blob(["\ufeff" + head + body], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "screen_candidates_" + todayStr() + ".csv";
  a.click(); URL.revokeObjectURL(a.href);
  toast("候选榜已导出", "ok");
}

/* ---------------- 选股记录 ---------------- */
async function picksLoad(force) {
  if (PK.busy) return;
  PK.busy = true;
  if (force || !PK.data) $("#pkSum").textContent = "正在读取…";
  try {
    const r = await api("/api/screen/picks?limit=40");
    if (!r.ok) {
      $("#pkSum").textContent = "—";
      $("#pkList").innerHTML = "";
      $("#pkDetail").innerHTML = '<p class="pk-empty">' + esc(r.error || "读取失败") + "</p>";
      return;
    }
    PK.data = r;
    PK.loaded = true;
    // 服务端最新的选股记录也能证明「今天选过」：换浏览器 / 清了缓存时照样不误闪
    const latestDay = String((r.batches && r.batches[0] && r.batches[0].recorded_at) || "").slice(0, 10);
    if (/^\d{4}-\d{2}-\d{2}$/.test(latestDay) && latestDay > scrLastRunDay()) scrMarkRun(latestDay);
    if (scrLastRunDay() === todayStr()) scrMarkStale(false);
    renderPicks(r);
  } catch (e) {
    $("#pkSum").textContent = "—";
    $("#pkList").innerHTML = "";
    $("#pkDetail").innerHTML = '<p class="pk-empty">读取失败：' + esc(String(e.message || e)) + "</p>";
  } finally {
    PK.busy = false;
  }
}
async function pkDelete(b) {
  if (!b) return;
  const when = String(b.recorded_at).slice(0, 16);
  if (!confirm("删除这一批选股记录？\n数据日 " + b.as_of + " · 选股日 " + when + " · " + b.count + " 只\n删除后不可恢复。")) return;
  let r;
  try { r = await post("/api/screen/picks", { action: "delete", as_of: b.as_of, recorded_at: b.recorded_at }); }
  catch (e) { r = { ok: false, error: String(e.message || e) }; }
  if (!r.ok) { toast(r.error || "删除失败", "err"); return; }
  toast("已删除该批 " + r.deleted + " 条记录", "ok");
  PK.sel = "";   // 被删的批次不再选中，重读后回到最新一批
  await picksLoad(true);
}
function pkPct(v) {
  if (v == null || !Number.isFinite(Number(v))) return '<span class="pk-na">—</span>';
  const n = Number(v);
  return '<span class="' + signCls(n) + '">' + (n > 0 ? "+" : "") + n.toFixed(2) + "%</span>";
}
/* 这批记录「有没有新行情可以评估」。
   以前只看 last_date <= as_of，于是「数据日本身就是最新交易日」的批次永远显示
   「待更新」—— 可它并不是没更新，而是市场还没往前走，再等多久也不会有变化。
   两种情况必须分开说，否则用户会一直以为数据坏了。
   返回 "fresh"（有新行情） / "same"（行情停在数据日） / "stale"（行情反而更早） */
function pkState(b) {
  const ld = String(b.last_date || "").slice(0, 10);
  const ad = String(b.as_of || "").slice(0, 10);
  if (!ld) return "fresh";              // 有 last_date 说明能取到最新价
  if (ld > ad) return "fresh";          // 行情已往前走，可算涨跌与 T+N
  if (ld === ad) return "same";         // 停在数据日：记录有效，只是还没有"后来"
  return "stale";                       // 行情比数据日还早，异常
}

function pkKey(b) { return b.as_of + "|" + b.recorded_at; }

/* 左栏一行＝一次选股。做成对齐的窄表而不是卡片：垂直扫读时数字能直接上下比。 */
function pkRow(b, i, on) {
  const hz = b.horizons || {};
  const st = pkState(b);
  const pending = st !== "fresh";
  const side = pending ? "" : (b.avg_chg > 0 ? " up" : b.avg_chg < 0 ? " down" : "");
  const dataDay = String(b.as_of || "").slice(0, 10);
  const recAt = String(b.recorded_at || "");
  const tip = "数据日 " + dataDay + " · 选股日 " + recAt + " · " + b.count + " 只" +
    (st === "same" ? "（行情仍停在数据日，暂无后续涨跌可评估）" : "");
  // 「至今」列：没有新行情时给明确的「暂无」，不是「待更新」。
  // 只有 stale（行情真的落后于数据日）才值得提醒。
  const nowCell = st === "fresh" ? pkPct(b.avg_chg)
    : st === "same" ? '<span class="pk-na" title="行情仍停在数据日，还没有后续涨跌">暂无</span>'
    : '<span class="pk-na warn" title="行情落后于数据日，建议先更新行情">待更新</span>';
  return '<button type="button" class="pk-row' + (on ? " on" : "") + side + '" data-i="' + i +
    '" title="' + esc(tip) + '">' +
    '<span class="pk-c1">' + esc(dataDay.slice(5)) + "</span>" +
    '<span class="pk-c2">' + esc(recAt ? recAt.slice(5, 10) : "—") + "</span>" +
    '<span class="pk-c3">' + b.count + "</span>" +
    '<span class="pk-c4">' + nowCell + "</span>" +
    '<span class="pk-c4">' + (hz["5"] ? pkPct(hz["5"].ret) : '<span class="pk-na">—</span>') + "</span>" +
    '<span class="pk-c5">' + (pending ? "—" : b.win + "/" + (b.scored || 0)) + "</span>" +
    "</button>";
}

function pkSorted(b) {
  const arr = (b.picks || []).slice();
  if (PK.sort === "chg") {
    arr.sort((x, y) => (y.chg == null ? -1e9 : y.chg) - (x.chg == null ? -1e9 : x.chg));
  } else {
    arr.sort((x, y) => (x.rank == null ? 9e9 : x.rank) - (y.rank == null ? 9e9 : y.rank));
  }
  return arr;
}

function pkDetail(b) {
  const det = $("#pkDetail");
  if (!b) { det.innerHTML = ""; return; }
  const hz = b.horizons || {};
  const st = pkState(b);
  const pending = st !== "fresh";
  const picks = pkSorted(b);
  const vals = picks.map((p) => p.chg).filter((v) => v != null && Number.isFinite(Number(v)));
  const maxAbs = Math.max(1e-6, ...vals.map((v) => Math.abs(Number(v))));

  // 一根柱一只股票，以中线为 0：一眼看出这次选股是多空混杂还是集体同向
  const bars = picks.map((p) => {
    const nm = p.code + " " + (p.name || "");
    if (p.chg == null) return '<span class="pk-bw" title="' + esc(nm + " 待更新") + '"></span>';
    const v = Number(p.chg);
    const h = Math.max(2, Math.abs(v) / maxAbs * 38);
    const cls = v > 0 ? "up" : (v < 0 ? "down" : "flat");
    return '<span class="pk-bw" title="' + esc(nm + "  " + (v > 0 ? "+" : "") + v.toFixed(2) + "%") +
      '"><i class="' + cls + '" style="height:' + h.toFixed(1) + 'px"></i></span>';
  }).join("");

  const kpi = (lbl, val, sub) =>
    '<div class="pk-k"><dt>' + lbl + "</dt><dd>" + val +
    (sub ? "<i>" + sub + "</i>" : "") + "</dd></div>";
  // T+N 显示「—」太含糊：用户分不清是「没算」还是「还没到」。
  // 这里按「还差几个交易日」给出一句可核对的说明 —— 持有期要求 T+1 建仓后
  // 再走满 h 个交易日，所以最新交易日离目标日还有几天，是确定性可算的。
  const hzK = (h, name) => {
    const v = hz[String(h)];
    if (!v) {
      const need = h + 1;   // T+1 建仓 + 持有 h 日
      return kpi(name, '<span class="pk-na">未到期</span>',
        "需再等 " + need + " 个交易日");
    }
    return kpi(name, pkPct(v.ret), (v.excess >= 0 ? "超额 +" : "超额 ") + v.excess.toFixed(2));
  };

  const head = '<div class="pk-dh">' +
    '<div class="pk-dh-l"><h4>' + esc(b.as_of) + '<em>数据日</em></h4>' +
      '<h4 class="pk-dh-pick">' + esc(String(b.recorded_at).slice(0, 16)) + '<em>选股日</em></h4>' +
      '<p>' + b.count + " 只" +
      (b.last_date ? " · 最新收盘 " + esc(String(b.last_date).slice(5)) : "") +
      (b.legacy ? ' <span class="pk-tag">旧口径</span>' : "") + "</p></div>" +
    '<div class="pk-sort">' +
      '<button class="pk-sbtn' + (PK.sort !== "chg" ? " on" : "") + '" data-sort="rank">按排名</button>' +
      '<button class="pk-sbtn' + (PK.sort === "chg" ? " on" : "") + '" data-sort="chg">按涨跌</button>' +
      "</div>" +
    '<button class="pk-del" data-del="1" title="删除这一批选股记录（不可恢复）">删除本批</button>' +
    "</div>";

  const kpis = '<div class="pk-kpis">' +
    kpi("至今均值", pending ? '<span class="pk-na">待更新</span>' : pkPct(b.avg_chg), pending ? "" : "记录价→最新") +
    kpi("胜率", pending ? "—" : b.win + "/" + (b.scored || 0), pending ? "" : "上涨只数") +
    hzK(5, "T+5") + hzK(10, "T+10") + hzK(20, "T+20") + "</div>";

  const rows = picks.map((x) => {
    const hs = x.h || {};
    const open = PK.open === x.code;
    // 辩护结论合并两个来源：
    //   1) picks 行自带 bear 数组（存档时的有效理由）
    //   2) pick_bear 表里那一批的裁决结果（含 drop）—— 记录页点开时先取回，见 pkBearLoad
    // 后者的 verdict 才是「到底拦没拦」的权威结论，优先用它。
    const bc = (PK.bear && PK.bear[x.code]) || null;
    const merged = bc
      ? { verdict: bc.verdict, enough: bc.enough, bears: bc.bears, judge_reason: bc.judge_reason }
      : (x.bear && x.bear.length
        ? { verdict: x.verdict, enough: true, bears: x.bear }
        : null);
    const tr = '<tr class="rowlink pk-line' + (open ? " on" : "") + '" data-code="' + esc(x.code) +
      '" data-name="' + esc(x.name || "") + '"><td class="pk-rk">' +
      (x.rank == null ? '<span class="pk-na">—</span>' : x.rank) + "</td>" +
      '<td><button class="pk-link" title="展开当日分时">' + esc(x.code) + "</button></td>" +
      "<td>" + esc(x.name) + "</td>" +
      bearCellHtml(merged) +
      '<td class="pk-r">' + pkPct(x.chg) + "</td>" +
      '<td class="pk-opt3">' + (x.industry ? esc(x.industry) : '<span class="pk-na">未分类</span>') + "</td>" +
      '<td class="pk-r pk-opt">' + (x.score == null ? "—" : Number(x.score).toFixed(3)) + "</td>" +
      '<td class="pk-r pk-opt">' + (x.rec_real == null ? (x.rec_close == null ? "—" : x.rec_close.toFixed(2)) : x.rec_real.toFixed(2)) + "</td>" +
      '<td class="pk-r pk-opt2">' + (x.last_real == null ? (x.last_close == null ? "—" : x.last_close.toFixed(2)) : x.last_real.toFixed(2)) + "</td>" +
      '<td class="pk-r">' + pkPct(hs["5"]) + "</td>" +
      '<td class="pk-r">' + pkPct(hs["10"]) + "</td>" +
      '<td class="pk-r">' + pkPct(hs["20"]) + "</td></tr>";
    if (!open) return tr;
    // 展开行就地插在这只股票下面：点哪儿看哪儿，不用滚到页面另一头找图
    return tr + '<tr class="pk-cr"><td colspan="12"><div class="pk-ch">' +
      '<div class="pk-ch-h"><b>' + esc(x.code) + " " + esc(x.name) + "</b>" +
        '<span class="pk-ch-note" title="表格价格已换算成真实价（未复权），与分时图可直接对照；涨跌幅按后复权同口径计算">当日分时 · 真实价</span>' +
        '<span class="pk-ch-q" id="pkChQ">读取分时…</span>' +
        '<button class="btn sec sm" data-jump="' + esc(x.code) + '">行情走势 ↗</button></div>' +
      '<div id="pkCh" class="chart pk-mini"></div></div></td></tr>';
  }).join("");

  // 每次重绘都要换掉旧实例：容器被 innerHTML 冲掉后，旧 ECharts 实例会挂在游离节点上
  if (S.charts["pkCh"]) { try { S.charts["pkCh"].dispose(); } catch (e) {} delete S.charts["pkCh"]; }
  det.innerHTML = head + kpis +
    (pending
      ? '<p class="pk-bars-none">' + esc(
          st === "same"
            ? "这批选股的数据日就是当前最新交易日（" + String(b.as_of).slice(5) +
              "），行情还没往前走，所以「至今涨跌」与 T+N 暂时无值 —— 等下一个交易日收盘后再来看。这不是数据缺失。"
            : "本地行情比这批记录的数据日还早，先到「数据中枢」更新行情，涨跌与 T+N 才会算出来。")
        + "</p>"
      : '<div class="pk-bars" aria-hidden="true">' + bars + "</div>") +
    '<div class="pk-tbl"><table><thead><tr>' +
    '<th class="pk-rk">#</th><th>代码</th><th>名称</th><th>辩护</th><th class="pk-r">涨跌</th><th class="pk-opt3">行业</th>' +
    '<th class="pk-r pk-opt">分数</th><th class="pk-r pk-opt">记录价</th><th class="pk-r pk-opt2">最新价</th>' +
    '<th class="pk-r">T+5</th><th class="pk-r">T+10</th><th class="pk-r">T+20</th>' +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>" +
    '<p class="pk-foot">价格按本地缓存的后复权序列计算，与行情软件显示的真实价可能不同。' +
    (b.legacy ? "该批记录时的价格口径较早（未复权），已统一重算。" : "") +
    "T+N 需等后续交易日补齐，未到期显示 —。点任意一行展开当日分时。</p>" +
    // 辩护结论已并进表格「辩护」列，「决策档案」单独折叠在表格下方。
    // 这里只留一个空的站位，pkBearLoad 拿它做兜底提示（正常路径下不写内容）。
    '<div class="pk-arch" id="pkBear" hidden></div>' +
    '<div class="pk-arch" id="pkArch"></div>';
  det.scrollTop = 0;
  if (PK.open) pkChartLoad(PK.open);
  pkBearLoad(b);      // 空头辩护随批次存着，先摆「为什么可能错」再看档案
  pkArchiveLoad(b);   // 决策档案是后取的，晚到不影响上面先看数
}

/* 行内分时图：点一行就地展开，只留一张，避免刷屏 */
async function pkChartLoad(code) {
  const box = $("#pkCh");
  if (!box) return;
  const c = chart("pkCh");
  const q = $("#pkChQ");
  if (q) q.innerHTML = '<span class="pk-na">读取分时…</span>';
  let r;
  try { r = await api("/api/chart?code=" + encodeURIComponent(code) + "&period=intraday"); }
  catch (e) { r = { ok: false, error: String(e) }; }
  // 等待期间用户可能已经点了别的行
  if (PK.open !== code) return;
  if (!r.ok) {
    c.clear();
    if (q) q.innerHTML = '<span class="pk-na">' + esc(r.error || "分时读取失败") + "</span>";
    return;
  }
  if (q) q.innerHTML = mkQuoteHtml(r);
  mkDrawMinute(r, c, true);
  c.resize();
}

function pkSelect(i) {
  const bs = (PK.data && PK.data.batches) || [];
  if (!bs[i]) return;
  PK.idx = i;
  PK.sel = pkKey(bs[i]);
  PK.open = "";   // 换了批次，上一批展开的那只不在这一批里，收起
  PK.bear = null; // 辩护结论按批次缓存，换批必须重取
  PK.bearKey = "";
  const kids = $("#pkList").children;
  for (let j = 0; j < kids.length; j++) kids[j].classList.toggle("on", j === i);
  pkDetail(bs[i]);
  if (kids[i] && kids[i].scrollIntoView) kids[i].scrollIntoView({ block: "nearest" });
  // 窄屏是上下堆叠的：左栏点完要是不滚过去，用户会以为没反应
  if (window.matchMedia("(max-width:980px)").matches) {
    const det = $("#pkDetail");
    if (det) det.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function renderPicks(r) {
  const list = $("#pkList");
  const bs = r.batches || [];
  // 「已可评估」= 至少有一个持有期算出了收益（行情已走过那个窗口）。
  // 和「有新行情」（能算至今涨跌）不是一回事：后者只要求多一天行情，
  // 前者要求走满 5/10/20 个交易日，所以 count 常比 matured 小。
  const matured = bs.filter((b) => Object.keys(b.horizons || {}).length).length;
  const waiting = bs.filter((b) => pkState(b) === "same").length;
  $("#pkSum").innerHTML = bs.length
    ? "共 <b>" + (r.total_batches || bs.length) + "</b> 次选股 · <b>" + (r.total_rows || 0) +
      "</b> 条候选 · <b>" + matured + "</b> 次已可评估" +
      (waiting ? ' · <span class="pk-na">' + waiting + " 次等行情推进</span>" : "") +
      (r.shown && r.shown < r.total_batches ? "（显示最近 " + r.shown + " 次）" : "")
    : "暂无记录";
  if (!bs.length) {
    list.innerHTML = "";
    $("#pkDetail").innerHTML =
      '<div class="pk-empty">还没有选股记录。到「选股工作台」点一次选股，这里就会开始累积。</div>';
    return;
  }
  // 重读后尽量停在原来那一批；找不到（比如被清理）就回到最新
  let idx = bs.findIndex((b) => pkKey(b) === PK.sel);
  if (idx < 0) idx = 0;
  PK.idx = idx;
  PK.sel = pkKey(bs[idx]);
  list.innerHTML = bs.map((b, i) => pkRow(b, i, i === idx)).join("");

  list.onclick = (e) => {
    const row = e.target.closest(".pk-row");
    if (row) pkSelect(Number(row.dataset.i));
  };
  list.onkeydown = (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const n = bs.length;
    const i = Math.max(0, Math.min(n - 1, (PK.idx || 0) + (e.key === "ArrowDown" ? 1 : -1)));
    pkSelect(i);
    if ($("#pkList").children[i]) $("#pkList").children[i].focus();
  };
  const det = $("#pkDetail");
  det.onclick = (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { pkDelete(bs[PK.idx]); return; }
    const s = e.target.closest("[data-sort]");
    if (s) {
      if (PK.sort !== s.dataset.sort) { PK.sort = s.dataset.sort; pkDetail(bs[PK.idx]); }
      return;
    }
    const jump = e.target.closest("[data-jump]");
    if (jump) { openChart(jump.dataset.jump); return; }
    const row = e.target.closest("tr[data-code]");
    if (row) {
      const code = row.dataset.code;
      PK.open = PK.open === code ? "" : code;   // 再点一次收起
      pkDetail(bs[PK.idx]);
    }
  };
  pkDetail(bs[idx]);
}

/* ---------------- 术语解释：气泡 + 术语表 ----------------
   页面只写结论，概念解释统一收在这里，避免正文堆满说明文字。
   新增术语只需在 TERMS 里补一条，正文用 <span class="term" data-term="key"> 包住。 */
const TERM_CATS = [
  ["market", "行情与交易"], ["factor", "因子与指标"],
  ["model", "模型与统计"], ["risk", "组合与风险"],
  ["exec", "仓位与执行"]
];
const TERMS = {
  topn: { n: "TopN", c: "model", d: "按打分排名取前 N 只作为候选。N 越小组合越集中，波动也越大。" },
  factor: { n: "因子", c: "factor", d: "能区分股票未来表现的量化特征，如动量、波动、估值、换手。多个因子合成后才用于排序。" },
  indcap: { n: "单行业上限", c: "risk", d: "同一行业最多入选几只。防止组合押在一个板块上，把行业行情误当成选股能力。" },
  neutral: {
    n: "中性化", c: "factor",
    d: "先按行业（或市值）分组，再在组内做标准化，只比较同类公司。行业中性 = 同行业内比强弱；不中性化则保留行业轮动的影响。"
  },
  model: { n: "模型分", c: "model", d: "LightGBM 从历史数据学「因子 → 未来超额收益」的映射。未训练时用可解释的方向加权基准分，不会伪装成预测。" },
  label: {
    n: "标签", c: "model",
    d: "训练时喂给模型的「正确答案」：这里是未来 N 日超额收益（T+1 收盘到 T+1+N 收盘，减去同期全市场等权收益）。T 日收盘后才打的分数最早 T+1 才能成交，所以不把隔夜跳空算进去。"
  },
  labelrank: {
    n: "横截面分位标签", c: "model",
    d: "把当期收益换成「在一群股票里排第几」（−0.5~0.5），而不是涨了几个点。模型只学谁比谁强，不受牛熊整体涨跌和极端值干扰，排序能力更稳。"
  },
  icir: {
    n: "IC 信息比（IR）", c: "model",
    d: "逐期 RankIC 的均值除以标准差。IC 均值说明「平均有没有用」，IR 说明「稳不稳」：IR 越高，越不是靠某几个季度运气好。一般在 0.3 以上算可用，0.5 以上较好。"
  },
  cross: { n: "横截面", c: "model", d: "同一天把所有股票放在一起横向比较。与之相对的是单只股票的时间序列比较。" },
  funnel: { n: "漏斗", c: "risk", d: "逐层过滤不合格股票（数据缺失、ST、极端风险、流动性、趋势），每层淘汰多少只都会公布。" },
  score: { n: "打分", c: "model", d: "横截面相对强弱，用于排序，不是买卖信号。除模型分外，分数没有绝对单位。" },
  mom: { n: "动量", c: "factor", d: "过去一段时间的涨幅，衡量趋势强弱。20 日动量 = 近 20 个交易日涨幅。" },
  backtest: { n: "后验 / 回测", c: "model", d: "用历史数据重放策略：当时会选到哪些票，之后 5/10/20 个交易日表现如何。" },
  turnover: { n: "换手率", c: "market", d: "成交量占可流通股份的比例，反映交易活跃度。过低难成交，过高往往伴随情绪化炒作。" },
  pe: { n: "PE 市盈率", c: "factor", d: "股价 ÷ 每股收益。TTM 指滚动最近四个季度。同一行业内比较才有意义。", f: "PE = 总市值 ÷ 净利润" },
  pb: { n: "PB 市净率", c: "factor", d: "股价 ÷ 每股净资产。重资产行业（银行、钢铁）常用 PB 估值。", f: "PB = 总市值 ÷ 净资产" },
  sw: { n: "申万一级", c: "market", d: "申万行业分类的第一层，共 33 个行业，是研究里最通用的粗分类。" },
  sharpe: { n: "夏普比率", c: "risk", d: "每承担 1 单位波动换来多少超额收益。越高说明收益质量越好，>1 已算不错。", f: "Sharpe = (年化收益 − 2%) ÷ 年化波动" },
  dd: { n: "最大回撤", c: "risk", d: "净值从历史最高点往下跌的最大幅度，衡量最难受的时候有多难受。" },
  oos: { n: "样本外", c: "model", d: "没参与调参的那段数据。只有样本外也有效，才说明策略不是单纯拟合了历史。" },
  realized: { n: "已实现盈亏", c: "market", d: "卖出后才结算的盈亏，按先进先出（FIFO）配对成本。还持有的浮动盈亏不计入。" },
  ic: {
    n: "IC", c: "model",
    d: "因子值与未来收益的相关系数。0.03 以上通常认为有信息量，0.05 以上算不错；为负说明方向反了。",
    f: "IC = corr(因子, 未来收益)"
  },
  rankic: { n: "RankIC", c: "model", d: "用排名代替原值算的 IC，不受极端值干扰，评估因子时更稳健。" },
  excess: { n: "超额收益", c: "risk", d: "相对基准多赚的部分。基准可以是全市场等权，也可以是同行业，后者更接近纯个股能力（α）。" },
  alpha: { n: "α（阿尔法）", c: "risk", d: "与市场、行业涨跌无关的个股收益。选股追求的正是这部分。" },
  beta: { n: "β（贝塔）", c: "risk", d: "跟随市场或行业涨跌的敏感度。β=1 表示大致同步，行业 β 就是「板块整体在涨」带来的收益。" },
  hhi: {
    n: "HHI 集中度", c: "risk",
    d: "各行业权重平方和。等权分散在 10 个行业约 0.1，全押一个行业为 1.0，越大越集中。",
    f: "HHI = Σ wᵢ²"
  },
  effn: { n: "有效行业数", c: "risk", d: "1 ÷ HHI。比「覆盖了几个行业」更能反映真实分散度：押 10 个行业但九成仓位在一个板块，有效数仍接近 1。" },
  vol: { n: "波动率", c: "factor", d: "收益率的标准差，越大越不稳定。常用近 20 日或 60 日计算。" },
  rsi: { n: "RSI", c: "factor", d: "相对强弱指数，0~100。高于 70 常视为超买、低于 30 视为超卖，属于反转类指标。" },
  macd: { n: "MACD", c: "factor", d: "快慢均线之差及其信号线。柱状图为正表示短期强于长期，金叉 / 死叉是常见的趋势信号。" },
  boll: { n: "布林带", c: "factor", d: "均线上下各加减 2 倍标准差。价格触及上轨常被视为短期过热。" },
  ma: { n: "均线", c: "factor", d: "N 日收盘价的平均值。价格站上 20 日线常被当作短期趋势转强的过滤条件。" },
  st: { n: "ST", c: "risk", d: "被交易所实施特别处理的股票，通常有连续亏损等问题，退市与波动风险显著更高。" },
  liq: { n: "流动性", c: "risk", d: "能不能顺利买卖。成交额和换手率太低时，滑点会吃掉大部分收益。" },
  hfq: { n: "后复权", c: "market", d: "以最早价格为基准向后调整，价格连续，适合计算收益和做回测。看当前价位要用前复权。" },
  t1: { n: "T+1", c: "market", d: "A 股当日买入的股票，下一个交易日才能卖出。回测必须遵守，否则收益会被高估。" },
  slippage: { n: "滑点", c: "market", d: "理想价格与实际成交价的差。挂单越急、流动性越差，滑点越大。" },
  commission: { n: "佣金", c: "market", d: "券商收取的交易费用，常见万 2.5，单笔最低 5 元，买卖双向收取。" },
  stamp: { n: "印花税", c: "market", d: "卖出时按成交金额收取，目前 0.05%，买入不收。" },
  nav: { n: "净值", c: "risk", d: "账户总资产相对初始资金的比值曲线，用来直观比较策略与基准。" },
  benchmark: { n: "基准", c: "risk", d: "用来对照的参照物。本工作台用「同期买入并持有同一只股票」，避免只看绝对收益。" },
  winrate: { n: "胜率", c: "risk", d: "盈利的交易（或评估期数）占比。胜率高不等于赚钱，还要看盈亏比。" },
  plratio: { n: "盈亏比", c: "risk", d: "平均每笔盈利 ÷ 平均每笔亏损。胜率 40% 但盈亏比 3 的策略同样可以赚钱。" },
  fifo: { n: "FIFO", c: "market", d: "先进先出：卖出时按最早买入的那批成本结算盈亏，是券商交割单的常见口径。" },

  /* ---- 行情与交易（补充） ---- */
  realpx: {
    n: "真实价 / 后复权价", c: "market",
    d: "后复权价保证收益连续，但不等于今天的成交价；真实价是账户里能成交的那个数。本系统用后复权算因子与涨跌幅，展示价按真实价换算，两者不要直接相减。"
  },
  limitup: {
    n: "涨跌停", c: "market",
    d: "主板 ±10%、创业板与科创板 ±20%、ST ±5%。回测里涨停开盘不买、跌停开盘不卖，因为排队也成交不了。"
  },
  lot: { n: "整手", c: "market", d: "A 股买入以 100 股为一手。目标持仓按整手取整，买不到一手就标「无法建仓」，不硬凑。" },
  twr: {
    n: "时间加权收益（TWR）", c: "risk",
    d: "剔除入金/出金影响的收益率，用来衡量「策略本身」而不是「你转了多少钱进来」。总资产涨跌里混着转账时，两个数会明显不同。"
  },
  lag: {
    n: "应到交易日 / 滞后", c: "market",
    d: "收盘后 17:45 起算当天为「应到」。数据日期落后于应到日就是滞后；滞后时打分用的是旧价格，结果要打折看。"
  },

  /* ---- 因子与指标（补充） ---- */
  gain: {
    n: "特征重要度（Gain）", c: "model",
    d: "LightGBM 里某个因子在所有分裂中带来的增益总和占比。它说明模型在用谁，不说明这个因子单独有效。"
  },
  pct: {
    n: "因子分位", c: "factor",
    d: "把因子值换成「在全市场排第几」（0~100）。已按因子方向调整：越高越有利，便于不同量纲的因子放在一起看。"
  },
  valband: {
    n: "同行估值区间", c: "factor",
    d: "同行业内 PE/PB 的 25/50/75 分位，乘以当前 EPS/BPS 换算成价格。只回答「按同行估值，现价在什么位置」，不是目标价。"
  },

  /* ---- 模型与统计（补充） ---- */
  debate: {
    n: "多空辩论", c: "model",
    d: "先让模型分别列出多头与空头理由（每条必须带数据证据），再给出证据倾向。目的是逼出反面证据，而不是让模型直接下结论。"
  },
  stance: {
    n: "证据倾向", c: "model",
    d: "辩论后的立场：偏多 / 中性 / 偏空，附带一致性 0~100 与时间周期。一致性低说明两边证据接近，结论不该强。"
  },
  datagap: {
    n: "数据缺口", c: "model",
    d: "分析时明确列出「哪些数据没有」，例如缺资金流水、缺财报。缺口越多的结论越不可靠，这比编一个数字更有用。"
  },

  /* ---- 组合与风险（补充） ---- */
  maxw: { n: "单票上限", c: "risk", d: "单只股票最多占总资产的比例。超限部分只压不上抬，被压下来的是现金。" },
  exposure: { n: "仓位", c: "risk", d: "持仓市值 ÷ 总资产。满仓不等于该满，空仓也是一种仓位，关键要能说清在等什么。" },
  turnovercap: {
    n: "换手上限", c: "risk",
    d: "本次调仓相对当前持仓的最大偏离比例。超过时按比例向当前收缩，避免一次性清仓式调仓带来的费用与滑点。"
  },
  cashfloor: { n: "现金下限", c: "risk", d: "组合里强制保留的现金比例，用来应对补仓、费用与突发赎回。" },

  /* ---- 仓位与执行（新增分类） ---- */
  atr: {
    n: "ATR（真实波幅）", c: "exec",
    d: "近 14 日「真实波幅」的均值，衡量这只票每天正常波动多大。本系统用 ATR 占现价的百分比，方便不同价位的票横向比较。"
  },
  stopref: {
    n: "2×ATR 参考位", c: "exec",
    d: "成本价减去两倍 ATR 得到的价位。跌破它只说明当前波动已超出建仓时的常态，用来提醒核对持有理由，不是止损指令。"
  },
  pivot: {
    n: "Pivot（枢轴位）", c: "exec",
    d: "用近期高低与收盘算出的中心价 P=(H+L+C)/3，以及上下两档 R1/S1。常被当作短期支撑与阻力的参考。"
  },
  riskbudget: {
    n: "风险预算", c: "exec",
    d: "每笔交易允许承担的总资产比例（默认 1%）。单票风险 = 权重 × ATR% × 倍数；同样的仓位，高波动票占用的预算可能是低波动票的数倍。"
  },
  voltarget: {
    n: "波动率倒数权重", c: "exec",
    d: "按 1/近 20 日波动分配权重，波动越大给的钱越少。目标是让每只票对组合波动的贡献接近。"
  },
  riskfixed: {
    n: "固定风险预算", c: "exec",
    d: "先定每笔最多亏多少钱，再按「预算 ÷（ATR×倍数）」反推能买多少。价格波动大的票自动买得少。"
  },
  rp: {
    n: "风险平价", c: "exec",
    d: "迭代调整权重，让每只票对组合总风险的贡献相等。与「按市值分配」相对，避免大市值或高波动票主导组合。"
  },
  hrp: {
    n: "层次风险平价（HRP）", c: "exec",
    d: "先把相关性接近的票聚成簇，再在簇间与簇内递归二分分配风险。比均值-方差优化稳，不需要求协方差矩阵的逆。"
  },
  kelly: {
    n: "分数 Kelly", c: "exec",
    d: "按胜率 p 与盈亏比 b 算最优下注比例 f*=(p·b−q)/b，实战取 0.25 倍左右。f*≤0 表示这套结构没有正优势，应保持低仓而不是加码。",
    f: "f* = (p·b − (1−p)) ÷ b"
  },
  topkdrop: {
    n: "Topk-Drop", c: "exec",
    d: "每天只换 n_drop 只：排名前 topk 的留下，末尾 n_drop 只换成候选里最好的新票。用来压低换手与摩擦成本。"
  },
  planexec: {
    n: "计划-执行偏差", c: "exec",
    d: "目标持仓生成时的计划（买卖谁、多少钱）与之后实际成交的差距。计划外成交占比高，说明计划只是事后记录，对执行没有约束力。"
  },
  difforder: {
    n: "差额单", c: "exec",
    d: "目标持仓减当前持仓得到的「该买多少 / 该卖多少」。它是派生结果，可以随时重算，也可以对同一目标分批执行。"
  }
};
const TERM_CAT_NAME = Object.fromEntries(TERM_CATS);

let _termPop = null, _termPin = false, _termCur = null;
function termPop() {
  if (!_termPop) {
    _termPop = document.createElement("div");
    _termPop.id = "termPop";
    _termPop.setAttribute("role", "tooltip");
    document.body.appendChild(_termPop);
  }
  return _termPop;
}
function showTerm(el) {
  // 三种来源：
  //   1) data-term="键"      → 查 TERMS 术语表（页面里 15 处术语气泡）
  //   2) data-tip="整段说明"  → 直接用，供「原来常驻在页面上、现在收进 ? 的说明」使用
  //   3) data-bear="<json>"  → 空头辩护的结构化内容（结论 + 裁决 + 逐条理由）
  // 全部共用同一个浮层：位置计算、pin、Esc 关闭、滚动隐藏都只有一份实现。
  // 4) data-led → 状态灯专用：每个任务一条可视化进度条（不只是文字）
  if (el.id === "sysLed" && SYSLED.data && SYSLED.data.running) {
    const pop = termPop();
    pop.innerHTML = ledTipHtml(SYSLED.data);
    pop.classList.add("on", "wide", "led-pop");
    placePop(pop, el);
    return;
  }
  const bearRaw = el.dataset.bear;
  if (bearRaw) {
    let d = null;
    try { d = JSON.parse(bearRaw); } catch (e) { d = null; }
    if (!d) return;
    const pop = termPop();
    pop.innerHTML = bearTipHtml(d);
    pop.classList.add("on", "wide", "bear-pop");
    placePop(pop, el);
    return;
  }
  let name = "", cat = "", desc = "", formula = "";
  const raw = el.dataset.tip;
  if (raw) {
    name = el.dataset.tipName || "";
    desc = raw;
  } else {
    const t = TERMS[el.dataset.term];
    if (!t) return;
    name = t.n; cat = TERM_CAT_NAME[t.c] || ""; desc = t.d; formula = t.f || "";
  }
  const pop = termPop();
  pop.classList.remove("bear-pop", "led-pop");
  pop.innerHTML = (name
    ? '<span class="tp-name">' + esc(name) +
      (cat ? '<span class="tp-cat">' + esc(cat) + "</span>" : "") + "</span>"
    : "") + '<div class="tp-desc">' + esc(desc) + "</div>" +
    (formula ? '<div class="tm-formula">' + esc(formula) + "</div>" : "");
  pop.classList.add("on");
  // 长文本（过去的 hint/cap）宽度上限更宽一些，否则一行会排得太碎
  pop.classList.toggle("wide", !!(raw && raw.length > 60));
  placePop(pop, el);
}

/** 把浮层放到目标元素旁边（上下自动翻边、左右不越界） */
function placePop(pop, el) {
  const r = el.getBoundingClientRect();
  const pr = pop.getBoundingClientRect();
  const left = Math.max(8, Math.min(window.innerWidth - pr.width - 8,
    r.left + r.width / 2 - pr.width / 2));
  let top = r.top - pr.height - 8;
  if (top < 8) top = Math.min(window.innerHeight - pr.height - 8, r.bottom + 8);
  pop.style.left = Math.round(left) + "px";
  pop.style.top = Math.round(top) + "px";
  if (_termCur && _termCur !== el) {
    // 同上：状态灯的 .on 是运行态，切换气泡时也不能清掉
    if (_termCur.id !== "sysLed") _termCur.classList.remove("on");
    _termCur.classList.remove("term-open");
  }
  _termCur = el;
  // 状态灯不再加通用 .on（那是蓝环专用），只用 term-open 表达气泡激活
  if (el.id !== "sysLed") el.classList.add("on");
  if (el.id === "sysLed") el.classList.add("term-open");
}

/** 辩护气泡的内容：结论 → 裁决理由 → 逐条否决理由（含触发条件与观察指标） */
function bearTipHtml(d) {
  const meta = BEAR_VERDICT[d.v] || BEAR_VERDICT.thin;
  const head = '<span class="tp-name">' + esc(meta.txt) +
    (d.name ? '<span class="tp-cat">' + esc(d.code + " " + d.name) + "</span>" : "") +
    "</span>";
  const why = d.why
    ? '<p class="bp-why">裁决：' + esc(d.why) + "</p>" : "";
  const items = (d.bears || []).map((y, i) =>
    '<li><span class="bp-n">' + (i + 1) + "</span>" +
    '<span class="bp-p">' + esc(y.point || "") +
    (y.trigger ? '<i>触发：' + esc(y.trigger) + "</i>" : "") +
    (y.watch ? '<i>观察：' + esc(y.watch) + "</i>" : "") +
    "</span></li>").join("");
  const list = items ? '<ul class="bp-list">' + items + "</ul>"
    : '<p class="bp-why">这条记录没有存下否决理由。</p>';
  return head + why + list;
}

/** 状态灯气泡：每个任务一条进度条 + 总进度，鼠标悬停即时显示 */
function ledTipHtml(d) {
  const tasks = d.tasks || [];
  const head = '<span class="tp-name">正在运行 ' + tasks.length + ' 个任务</span>';
  const rows = tasks.map((t) => {
    const pct = (t.pct == null) ? null : Math.max(0, Math.min(100, Math.round(t.pct * 100)));
    const w = pct == null ? 0 : pct;
    const bar = '<div class="led-bar"><i style="width:' + w + '%"></i></div>';
    const label = '<div class="led-row-t"><b>' + esc(t.name) + '</b>' +
      '<span>' + (pct == null ? '计算中' : pct + '%') + '</span></div>';
    const meta = t.detail ? '<div class="led-detail">' + esc(t.detail) + '</div>' : '';
    return '<div class="led-task">' + label + bar + meta + '</div>';
  }).join('');
  const total = (d.overall == null) ? null : Math.max(0, Math.min(100, Math.round(d.overall * 100)));
  const foot = total == null ? '' :
    '<div class="led-total"><span>总进度</span><b>' + total + '%</b></div>';
  return head + rows + foot;
}
function hideTerm(force) {
  if (_termPin && !force) return;
  const pop = termPop();
  pop.classList.remove("on");
  if (_termCur) {
    // 状态灯的 .on 是「运行中=蓝环」，绝不能被气泡关闭清掉（否则鼠标一挪开就闪成绿点）。
    // 其他 .term 元素照旧清 .on。
    if (_termCur.id !== "sysLed") _termCur.classList.remove("on");
    _termCur.classList.remove("term-open");
  }
  _termCur = null;
}
function initTerms() {
  // 事件委托统一处理三类元素：
  //   .term       术语气泡（查 TERMS 表）
  //   .q          「?」说明图标（读 data-tip）
  //   [data-tip]  任何带 data-tip 的元素（行业分布行、缓存状态等），
  //               不再各写一个原生 title —— 原生 title 的样式改不了，
  //               和辩护/状态灯的气泡放在一起就像两套组件。
  const SEL = ".term, .q, [data-tip]";
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest && e.target.closest(SEL);
    if (el && !_termPin) showTerm(el);
  });
  document.addEventListener("mouseout", (e) => {
    const el = e.target.closest && e.target.closest(SEL);
    if (el && !_termPin) hideTerm();
  });
  document.addEventListener("click", (e) => {
    const el = e.target.closest && e.target.closest(SEL);
    if (el) {
      e.preventDefault();
      // 已固定且点的就是它 → 关闭。
      // 注意必须同时把 _termPin 复位：以前只 hideTerm(true) 而没清 pin，
      // 于是 _termCur 被置 null、pin 还留着，下一次点击既进不了关闭分支
      // （_termCur !== el）、又被 mouseover 的 !_termPin 挡住不显示 —— 表现为
      // 「点开了就再也关不掉/打不开」，很难复现但一点就中。
      if (_termPin) {
        _termPin = false;
        hideTerm(true);
        return;
      }
      _termPin = true;
      showTerm(el);
      return;
    }
    _termPin = false;
    hideTerm(true);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { _termPin = false; hideTerm(true); closeTermModal(); closeDrawer(); }
  });
  window.addEventListener("scroll", () => { if (!_termPin) hideTerm(true); }, true);
}
/* ---------------- 顶栏系统状态灯 ----------------
   空闲：绿色小点，表示「系统正常」。
   有任务：变成蓝色圆环，环的长度 = 所有任务的总进度；悬停看每个任务的明细。
   设计意图：任务进度只有这一个入口。其他地方（数据中枢、股票池页）
   不再各画一份进度条 —— 用户只需要知道「有没有在跑、跑了多少」，
   需要细节时悬停就能看到全部任务的分解。 */
const SYSLED = { timer: null, data: null, busy: false };

/* 前端本地任务：因子体检、回测、选股这类「点一下等结果」的同步请求，
   后端 /api/tasks 看不到（请求还没返回），但同样要占住状态灯。
   登记后绿点变转圈，页面不再遮罩、可以继续操作；请求结束立即撤下。 */
const LOCALTASK = new Map();

function sysTaskStart(key, label) {
  LOCALTASK.set(String(key), String(label || key));
  sysLedPaint(SYSLED.data);
}
function sysTaskEnd(key) {
  LOCALTASK.delete(String(key));
  sysLedPaint(SYSLED.data);
  // 立刻对一次后端状态，避免本地任务结束后灯停在转圈
  clearTimeout(SYSLED.timer);
  SYSLED.timer = setTimeout(sysLedPoll, 120);
}

function sysLedPaint(d) {
  const btn = $("#sysLed"), fill = $("#sysLedFill");
  if (!btn || !fill) return;
  const tasks = (d && d.tasks) || [];
  const backend = !!(d && d.running) && tasks.length > 0;
  const local = [...LOCALTASK.values()];
  const running = backend || local.length > 0;

  btn.classList.toggle("on", running);
  btn.classList.toggle("local", local.length > 0);
  // 圆环周长（r=8 → 2πr ≈ 50.27）；用 stroke-dasharray 控制填充比例
  const C = 2 * Math.PI * 8;
  fill.style.strokeDasharray = String(C);
  if (backend) {
    const pct = (d.overall == null) ? 0 : Math.max(0, Math.min(1, Number(d.overall)));
    fill.style.strokeDashoffset = String(C * (1 - pct));
    // 悬停内容走专用进度条气泡（showTerm 里的 data-led 分支），不再塞纯文本
    btn.title = "";
    btn.classList.add("term");
  } else {
    fill.style.strokeDashoffset = String(C);   // 清空圆环
    btn.classList.remove("term");
    if (!local.length) {
      delete btn.dataset.tip;
      delete btn.dataset.tipName;
      const rec = ((d && d.recent) || [])[0];
      btn.title = (rec
        ? ("系统运行正常 · 最近一次：" + (rec.kind || "") + " " +
           (rec.ok ? "成功" : "失败") + " " + String(rec.finished || "").slice(5, 16))
        : "系统运行正常") + " · 点击查看运行日志";
    }
  }
  // 本地任务没有确定进度：绿点转圈 + 悬停列出正在跑的任务
  if (local.length) {
    btn.dataset.tipName = "正在执行";
    btn.dataset.tip = local.join("、");
    btn.title = local.join("、");
  }
}

async function sysLedPoll() {
  if (SYSLED.busy) return;
  SYSLED.busy = true;
  try {
    const d = await api("/api/tasks");
    SYSLED.data = d;
    sysLedPaint(d);
  } catch (e) {
    /* 读不到就保持上一次的样子，不要把灯熄灭误导成「都正常」 */
  } finally {
    SYSLED.busy = false;
  }
  // 有任务时 1.2 秒对一次（进度要跟手），空闲时 6 秒一次就够
  const fast = !!(SYSLED.data && SYSLED.data.running);
  clearTimeout(SYSLED.timer);
  SYSLED.timer = setTimeout(sysLedPoll, fast ? 1200 : 6000);
}

function sysLedStart() {
  const btn = $("#sysLed");
  if (!btn) return;
  btn.addEventListener("click", () => {
    // 状态灯只回答「系统最近在干什么、有没有报错」——直接进运行日志；
    // 任务进度明细在悬停气泡里，不需要再跳「更新记录」。
    goHub("logs");
  });
  sysLedPoll();
}

/* 术语表：独立页面（原来是弹窗）。
   成页而不是弹窗的好处：能常驻搜索、分组浏览、可被浏览器 Ctrl+F 命中，
   也能用 #terms 直接分享/收藏。 */
function termPageRender(q) {
  const box = $("#termPageList");
  if (!box) return;
  const key = String(q == null ? $("#termPageQ")?.value : q || "").trim().toLowerCase();
  const all = Object.entries(TERMS);
  const hit = all.filter(([, t]) =>
    !key || t.n.toLowerCase().includes(key) || (t.d || "").toLowerCase().includes(key));
  const cnt = $("#termCount");
  if (cnt) {
    cnt.textContent = key
      ? "匹配 " + hit.length + " / " + all.length + " 条"
      : "共 " + all.length + " 条";
  }
  if (!hit.length) {
    box.innerHTML = '<p class="hint">没有匹配的术语，试试「夏普」「IC」「复权」。</p>';
    return;
  }
  box.innerHTML = TERM_CATS.map(([ck, cn]) => {
    const items = hit.filter(([, t]) => t.c === ck);
    if (!items.length) return "";
    return '<div class="tm-group"><h4>' + esc(cn) + ' <i class="tm-n">' + items.length + "</i></h4>" +
      items.map(([, t]) =>
        '<div class="tm-item"><b>' + esc(t.n) + "</b><p>" + esc(t.d) + "</p>" +
        (t.f ? '<div class="tm-formula">' + esc(t.f) + "</div>" : "") + "</div>").join("") +
      "</div>";
  }).join("");
}

/* closeTermModal 保留空实现：Esc 关闭浮层时还会调它（术语表已独立成页） */
function closeTermModal() { /* 已无弹窗 */ }

/* ---------------- 关于：整站使用说明 ----------------
   内容放数据里而不是 HTML：目录、搜索、命中计数都要遍历章节，
   加章节只要往 ABOUT_DOC 里加一条，不用同步改三处。 */
const ABOUT_DOC = [
  {
    id: "start", title: "快速开始", tag: "5 分钟",
    blocks: [
      { p: "这是本地运行的 A 股研究台：用公开行情做横截面选股、回测验证、目标持仓计算，并把实盘账户的**只读**数据接进来做复盘。所有计算都在本机完成，行情与档案存在 `data/quant.db`。" },
      { h: "第一次使用" },
      { ul: [
        "打开**数据中枢 → 数据链**，看四段数据（名单 / 日线 / 估值 / 快照）是否都到「应到交易日」；缺哪段就点哪段的刷新。",
        "进**选股工作台**，确认候选范围与 TopN，点「开始选股」，等右上角状态灯转完。",
        "在候选榜点任意一行的名称，跳到**单股分析**，看因子分位、多空辩论与风险参考位。",
        "去**目标持仓**填总资产、选一种权重方式，生成差额单（只算不报）。",
        "有实盘账户时，进**实盘复盘**点「同步账户」拉一次快照；体检与日记从那天起才有数据。"
      ] },
      { note: "本工具**全程不下单**：不引入任何下单或撤单接口。实盘只读取资金、持仓、委托与成交，任何按钮都不会提交委托。" }
    ]
  },
  {
    id: "map", title: "页面地图", tag: "15 页",
    blocks: [
      { p: "左侧导航按用途分五组，每页只做一件事。下面的顺序就是推荐的使用顺序。" },
      { h: "研究" },
      { kv: [
        ["选股工作台", "候选范围 + 参数 → 24 因子 → 模型打分 → 漏斗 → 行业上限 → 候选榜。上方三栏是本期候选与参数、行业分布、T+N 后验；下方左侧候选榜、右侧空头辩护。"],
        ["选股记录", "每期候选按 as_of 存档，可回看当时的分、因子与 LLM 理由；附 60 期归因，看历史上选出来的票后来怎么走。"],
        ["目标持仓", "把候选名单变成「每只买多少股」：7 种权重方式、单票/单行业上限、换手上限、Topk-Drop，最后给差额单与费用估算。"],
        ["单股分析", "一只股票的横截面位置：真实价、模型分与排名、24 个因子分位、多空辩论、五节报告、风险与价位参考、同行估值区间。每次分析自动存档，可删。"]
      ] },
      { h: "验证" },
      { kv: [
        ["回测总览", "T 日收盘信号、T+1 开盘成交，含佣金/印花税/过户费/滑点/涨跌停/整手；给净值、回撤、胜率、盈亏比与分年度表现。"],
        ["行情走势", "单只标的的 K 线与指标叠加，用来核对信号位置，不做预测。"],
        ["因子体检", "单因子 IC/RankIC、分层收益、换手与相关性；判断某个因子是不是已经被榨干，再决定要不要加维度。"],
        ["参数扫描", "对 TopN、持有期、行业上限等做网格，输出参数敏感度，避免只看一组「最好看」的参数。"]
      ] },
      { h: "数据" },
      { kv: [
        ["数据中枢", "8 个标签：数据链、数据源、估值源、模型、更新记录、运行日志、长期配置等。所有刷新、训练、同步的入口都在这里。"],
        ["行业与强度", "申万一级 / 证监会行业归属，行业成员与强度排名；行业缺失的票在选股里会被单独统计。"],
        ["股票池与行情", "自选股清单与本地行情覆盖情况（截至哪天、多少根日线），可单只或批量补行情。"]
      ] },
      { h: "实盘" },
      { kv: [
        ["实盘复盘", "账本曲线、持仓归因、胜率与盈亏比、委托质量、行为体检、风险预算走势、计划-执行偏差。"],
        ["复盘日记", "按交易日一天一篇 Markdown：今日事实 / 过程复盘 / 需要警惕 / 明日检查；支持预览、编辑、整理格式与 AI 撰写。"]
      ] },
      { h: "其他" },
      { kv: [
        ["术语表", "全部术语与口径，按分类分组、可搜索；正文里带虚下划线的词也能悬停直接看。"],
        ["关于", "就是当前这一页：完整使用说明、常见问题与边界。"]
      ] }
    ]
  },
  {
    id: "data", title: "数据与更新", tag: "每天 17:45",
    blocks: [
      { p: "选股依赖四段数据，缺任何一段都会让结果失真。数据中枢的「数据链」把它们分开显示，并各自给出状态与刷新按钮。" },
      { kv: [
        ["全 A 名单", "当天可交易的股票清单（含上市/退市判定）。名单没更新时，新上市的票不会进候选。"],
        ["日线行情", "沪深走 baostock 后复权日线，北交所走腾讯。行情日落后时，打分用的是旧价格，必须刷新。"],
        ["估值", "PE / PB / 总市值 / 换手 / 量比，按数据源顺序降级获取，收盘后才有当日值。"],
        ["横截面快照", "把当天全市场因子算好存一份，选股与单股分析都读它；快照落后会先重建再打分。"]
      ] },
      { h: "什么时候更新" },
      { ul: [
        "**应到交易日**：收盘后 17:45 起算当天；盘中打开时，页面用的是上一交易日的收盘数据，会标注数据日。",
        "**自动班次**：每天 18:10 跑主流水线；如果发现行情滞后，会在同日补跑（有次数上限，避免反复打源）。",
        "**待办**：左侧或数据中枢的待办只列「影响结果」的缺口；点一下直达对应刷新按钮。"
      ] },
      { note: "盘中不要反复点全市场刷新：行情源有频率限制，连续点击会触发冷却，反而更慢。" }
    ]
  },
  {
    id: "screen", title: "选股链路", tag: "24 因子",
    blocks: [
      { p: "一次选股是一条固定链路，每一层的淘汰数量都会公布在漏斗里，方便判断是「没货」还是「被过滤掉了」。" },
      { kv: [
        ["1 候选范围", "全市场 / 指定行业 / 股票池。范围越小，横截面分位越不稳，样本太少时会提示。"],
        ["2 因子", "24 个价量与基本面因子（动量、波动、换手、估值、规模、趋势等），每个都按横截面算分位。"],
        ["3 中性化", "按行业（或市值）分组后组内标准化，只比同类公司；也可以关掉，保留行业轮动的影响。"],
        ["4 模型", "LightGBM 学「因子 → 未来 N 日超额收益」。没训练或过期时用可解释的方向加权基准分，不会伪装成预测。"],
        ["5 漏斗", "逐层剔除数据缺失、ST、极端风险、流动性不足、趋势破坏的票，每层剩多少都列出来。"],
        ["6 行业上限", "同一行业最多入选几只，防止把板块行情当成选股能力。"],
        ["7 候选榜", "最终 TopN。分数是横截面相对排序，不是买卖信号。"],
        ["8 LLM（可选）", "对候选做重排解读，并可为每只票生成空头辩护（≥3 条理由才保留），用于自我反驳。"]
      ] },
      { h: "工作台上方三栏" },
      { ul: [
        "**本期候选与参数**：这一期用了什么范围、TopN、行业上限，样本数是多少。",
        "**行业分布**：候选落在哪些行业、集中度（HHI 与有效行业数）。",
        "**T+N 后验**：这批候选在历史同类参数下的表现，与全市场均值、同行业中位数对照。"
      ] },
      { note: "样本期数少时，后验只报数不下结论。任何「这组参数最好」的判断，都要先看参数扫描里的敏感度。" }
    ]
  },
  {
    id: "backtest", title: "回测与后验", tag: "T+1",
    blocks: [
      { p: "回测不是证明策略能赚钱，而是检验「如果当时真的这么做，会遇到什么」。所以成交假设比收益率更重要。" },
      { kv: [
        ["成交时点", "T 日收盘出信号，T+1 开盘成交。不把信号当天的收盘价当成买入价。"],
        ["涨跌停", "涨停开盘不买、跌停开盘不卖；一字板买不到就是买不到。"],
        ["整手", "按 100 股整手取整，买不到一手的标「无法建仓」而不是硬凑。"],
        ["费用", "佣金（含最低 5 元）、印花税（卖出）、过户费、滑点都计入；费用吃掉多少毛利会单独显示。"],
        ["未建模", "停牌、分红现金流、盘口深度、冲击成本没有建模，实盘通常比回测差。"]
      ] },
      { h: "后验怎么读" },
      { ul: [
        "把当期打分逻辑放回历史每一期重放，TopN 等权持有到第 N 个交易日。",
        "同时给三个对照：全市场等权、同行业中位数、以及「只买不选」的基准。",
        "看的是**超额**是否稳定，而不是某一期涨了多少。"
      ] },
      { note: "参数扫描里最好看的那组，往往只是拟合了这段历史。样本外与滚动验证才是有效性的证据。" }
    ]
  },
  {
    id: "target", title: "目标持仓与仓位算法", tag: "7 种权重",
    blocks: [
      { p: "选股回答「买什么」，目标持仓回答「每只买多少」。输出的是目标持仓与差额单，不是订单。" },
      { kv: [
        ["等权", "每只一样多。最简单，也最容易解释。"],
        ["打分加权", "按分数的横截面 z 值做 softmax，避免最高分那只吃掉全部权重。"],
        ["波动率倒数", "按 1/近 20 日波动分配，高波动票自动减权。"],
        ["固定风险预算", "让每只票的「权重 × ATR% × 倍数」都等于同一笔预算；超限部分留现金，不回补。"],
        ["风险平价", "迭代到各票对组合的风险贡献相等。"],
        ["层次风险平价（HRP）", "单链接聚类后递归二分，数据不足时回退等权。"],
        ["分数 Kelly", "用账本胜率与盈亏比算 f*，默认取 0.25 倍；f*≤0 时回退等权并说明没有正优势。"]
      ] },
      { h: "约束" },
      { ul: [
        "**单票上限**与**单行业上限**只压不上抬：被压下来的部分是现金，不会偷偷塞回别的票。",
        "**换手上限**：目标与当前偏离过大时按比例向当前收缩，避免一次性清仓式调仓。",
        "**Topk-Drop**：设了 topk 与每日换手只数后，每天最多换 n_drop 只，持仓超过 topk 的部分直接卖出。",
        "**最小交易额**：差额小于阈值不动，避免为了 1 手付 5 元最低佣金。"
      ] },
      { p: "每次生成都会存一份**计划**（买卖代码、金额、计划换手）。之后实盘体检会拿计划与实际成交比，算出计划外成交占比 —— 这是「计划对执行有没有约束力」的唯一证据。" }
    ]
  },
  {
    id: "stock", title: "单股分析", tag: "多空辩论",
    blocks: [
      { p: "输入代码或名称即可。它回答的是「这只票在当前横截面里处于什么位置」，不给买卖结论。" },
      { kv: [
        ["概览", "真实价与当日涨跌、模型分与全市场排名、横截面分位、行业内排名、PE/PB/市值、换手/量比、行情根数与复权口径。"],
        ["因子分位", "24 个因子逐个给数值、分组与分位条；已按因子方向调整，越高越有利。"],
        ["多空辩论", "第一轮 LLM 只输出严格 JSON：多头 2-4 条、空头 2-4 条（每条必须带数据证据）、证据倾向与一致性、数据缺口。"],
        ["报告", "第二轮基于辩论写五节 Markdown：结论摘要 / 多空对照 / 模型与因子 / 基本面与估值 / 风险与观察点。"],
        ["风险与价位", "ATR14、20 日区间、Pivot 的 R1/S1、2×ATR 参考位；只作波动参考，不是止损指令。"],
        ["同行估值", "行业内 PE/PB 的 25/50/75 分位 × 当前 EPS/BPS，给出相对估值区间；本机没有财报，不做 DCF。"]
      ] },
      { p: "打分必须走**全市场快照**：模型是横截面语义（排名标签、行业中性按组算），只传一只股票时 z 恒为 0，分数会失真。所以先给当日全市场打分，再取目标那一行。" },
      { p: "LLM 有降级链：未配置、调用失败、或输出不完整（五节不齐，通常是被 token 上限截断）都会回退本地模板，并在页面标注「本地模板」，绝不存一份看起来像 AI 分析的半截稿。" },
      { note: "报告里的「参考位」与「估值区间」都是计算值，不是目标价，也不构成买卖建议。" }
    ]
  },
  {
    id: "real", title: "实盘复盘", tag: "只读",
    blocks: [
      { p: "实盘模块只读取账户数据，用于回答「我实际做了什么、和计划差多少」。" },
      { kv: [
        ["接入", "emtl 只读接入东财：资金、持仓、当日委托、历史成交、资金流水。任何下单/撤单接口都不引入。"],
        ["账本", "每日快照 + 历史成交回流入库，形成资金曲线、持仓归因与已实现盈亏（FIFO）。"],
        ["行为体检", "集中度、单票上限、现金缓冲、亏损集中、胜率与盈亏比、换手与费用、委托质量；只列命中项。"],
        ["风险预算", "每只票的「权重 × ATR% × 倍数」与单笔预算比较，并记录每日使用率走势。"],
        ["计划-执行", "最近一份目标持仓计划 vs 之后的实际成交：计划外成交占比、计划卖出是否执行。"]
      ] },
      { h: "复盘日记" },
      { ul: [
        "按交易日一天一篇 Markdown，正文四节：今日事实、过程复盘、需要警惕、明日检查。",
        "「明日检查」会优先点名具体事项：已跌破 2×ATR 参考位的持仓、计划外成交占比过高的情况。",
        "可以手写、用本地模板、或让 LLM 基于聚合数据撰写；AI 不会覆盖手写稿。"
      ] },
      { note: "体检与日记只评价可观察的行为（集中度、换手、持有期、规则命中），不评价赚没赚钱，也不做心理诊断。" }
    ]
  },
  {
    id: "sources", title: "数据源与估值", tag: "六源降级",
    blocks: [
      { p: "估值（PE/PB/市值）按顺序逐个尝试，第一个「日期达标且覆盖够」的源被采纳；前一个只给到旧日期就继续往下试。" },
      { kv: [
        ["腾讯（主）", "收盘后有当日数据，免账号，字段较全。"],
        ["新浪 / 通达信", "价格兜底源：只提供价格，不提供估值。主源全挂时仍能算真实价与涨跌。"],
        ["Tushare", "唯一能按历史交易日整市场回补的源，有调用频率限制，主要用来补历史缺口。"],
        ["东方财富（备份）", "分页快照，受 token 与风控影响；失败只记 info、冷却 6 小时再试，不打扰待办。"],
        ["baostock", "最后兜底，逐只较慢，只补缺失。"]
      ] },
      { h: "仲裁规则" },
      { ul: [
        "日期必须 ≥ 应到交易日，覆盖率 ≥ 95%，关键字段完整度 ≥ 90%（只算 PB 与总市值），才允许覆盖估值文件。",
        "**价格源永不进仲裁**：只有价格的源只记价格备份，绝不覆盖估值，避免把空的 PE/PB 写进去。",
        "全失败时标记「沿用旧估值」并提示价格通道仍可用，不会假装拿到了当天估值。"
      ] },
      { note: "数据源面板里的状态是结果，运行日志才是过程：哪个源失败、什么原因、下次什么时候重试。" }
    ]
  },
  {
    id: "faq", title: "常见问题", tag: "排查",
    blocks: [
      { kv: [
        ["候选是空的", "先看漏斗每层剩多少：是名单没更新、行情缺失，还是行业上限把票卡掉了。再看候选范围是否过窄。"],
        ["数据一直是昨天的", "盘中就是上一交易日收盘；17:45 之后仍未更新，去数据中枢看是哪一段滞后，点对应刷新。"],
        ["估值大面积缺失", "看数据源面板：主源是否失败、东财是否在 6 小时冷却里；价格源只兜底价格，不会补估值。"],
        ["分数全一样 / 明显失真", "通常是快照落后或只剩单只股票。重建横截面快照后重跑。"],
        ["模型显示过期", "训练超过 35 天或特征版本变化会提示；去数据中枢重新构造训练集并训练。"],
        ["AI 解读变成「本地模板」", "LLM 未配置、调用失败或输出被截断。先点「测试连通」，再重试。"],
        ["计划-执行一直是空的", "还没生成过目标持仓；先在目标持仓页生成一次，之后有成交才会出现对比。"],
        ["风险预算走势只有一天", "这张日表从启用后开始累积，不会回填历史。"],
        ["页面数字和券商不一致", "本系统用后复权价算收益，券商显示的是真实成交价；对比时看百分比而不是绝对价。"]
      ] }
    ]
  },
  {
    id: "rule", title: "口径与边界", tag: "必读",
    blocks: [
      { kv: [
        ["价格口径", "日线**后复权**用于因子与回测，避免除权跳空被当成下跌；页面展示的价按**真实价格**换算。涨跌幅仍按后复权计算。"],
        ["打分口径", "分数是横截面相对排序，不是买卖信号。基准分用因子方向加权 z-score；模型分是预测的未来 N 日超额收益（%）。"],
        ["后验口径", "历史每期重放，TopN 等权持有到第 N 个交易日，与全市场均值、同行业中位数比较；样本少时只报数。"],
        ["实盘口径", "已实现盈亏按 FIFO 配对；总收益与净入金分开（TWR），避免把转账当成收益。"]
      ] },
      { env: true },
      { note: "仅供个人研究与学习，**不构成投资建议**，也不含买卖判断、价位或仓位建议。回测受成本、滑点、区间与过拟合影响，实盘通常显著差于回测；历史数据不代表未来。" }
    ]
  },
  {
    id: "tech", title: "技术选型", tag: "为什么不用",
    blocks: [
      { p: "每个依赖都是因为「它解决了具体问题」，而不是因为它流行。下面记录选了什么、以及为什么没选常见替代品。" },
      { kv: [
        ["数据层", "AKShare 提供字段规范；东财 push2his 直连接管被限流的通道；腾讯行情做跨源容灾；本地 SQLite 支撑断网回测。"],
        ["指标层", "用纯 Python 的 ta + pandas 等价实现，避开 TA-Lib 的 C 库编译。"],
        ["研究层", "吸收 Qlib Alpha158 的因子思路做精简版，不引入其二进制数据格式与学习曲线。"],
        ["回测层", "沿用 backtesting.py 的心智模型，自研事件驱动引擎补上 A 股 T+1、整手、涨跌停与费用规则。"],
        ["未引入", "vn.py（实盘网关）、Qlib 全量 / Lean（数据格式重）、backtrader / 原版 zipline（停更）、freqtrade（加密）、TradingAgents（不可复现）。"]
      ] }
    ]
  }
];

function abEsc(t) { return esc(String(t == null ? "" : t)); }

/* 极简富文本：先转义，再还原 **加粗** 与 `代码`。
   正文里的 <img onerror=...> 只会显示成文字。 */
function abFmt(t) {
  let s = abEsc(t);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  return s;
}

/* 高亮只处理标签外的文本段：直接对整串 split 会把 <b> 里的字母也包进 <mark>。 */
function abHi(t, q) {
  const s = abFmt(t);
  if (!q) return s;
  const k = abEsc(q);
  if (!k) return s;
  return s.split(/(<[^>]+>)/).map((part) =>
    part.charAt(0) === "<" ? part : part.split(k).join("<mark>" + k + "</mark>")
  ).join("");
}

function abText(b) {
  if (b.h) return b.h;
  if (b.p) return b.p;
  if (b.note) return b.note;
  if (b.ul) return b.ul.join(" ");
  if (b.kv) return b.kv.map((x) => x[0] + " " + x[1]).join(" ");
  return "";
}

function abBlock(b, q) {
  if (b.h) return "<h4>" + abHi(b.h, q) + "</h4>";
  if (b.p) return '<p class="ab-p">' + abHi(b.p, q) + "</p>";
  if (b.ul) return '<ul class="ab-ul">' +
    b.ul.map((x) => "<li>" + abHi(x, q) + "</li>").join("") + "</ul>";
  if (b.note) return '<div class="ab-note">' + abHi(b.note, q) + "</div>";
  if (b.kv) return b.kv.map(([k, v]) =>
    '<div class="map-row"><div class="layer">' + abHi(k, q) + "</div>" +
    '<div class="txt">' + abHi(v, q) + "</div></div>").join("");
  if (b.env) return '<div class="map-row"><div class="layer">运行环境</div>' +
    '<div class="txt"><span id="cfgInfo">—</span></div></div>';
  return "";
}

function aboutRender(q) {
  const doc = $("#abDoc"), toc = $("#abToc");
  if (!doc || !toc) return;
  const input = $("#abQ");
  const key = String(q == null ? (input ? input.value : "") : q).trim();
  const kl = key.toLowerCase();

  const view = ABOUT_DOC.map((sec) => {
    const titleHit = !kl || (sec.title + " " + (sec.tag || "")).toLowerCase().includes(kl);
    const blocks = (sec.blocks || []).filter((b) => {
      if (!kl || titleHit) return true;
      return abText(b).toLowerCase().includes(kl);
    });
    return { sec, blocks };
  }).filter((v) => v.blocks.length);

  let n = 0;
  view.forEach((v) => { n += v.blocks.length; });

  toc.innerHTML = view.map((v) =>
    '<a href="javascript:void(0)" data-sec="' + v.sec.id + '">' +
    esc(v.sec.title) + "</a>").join("");
  doc.innerHTML = view.map((v) =>
    '<section class="ab-sec" id="ab-' + v.sec.id + '">' +
    '<div class="ab-sec-h"><h3>' + abHi(v.sec.title, key) + "</h3>" +
    (v.sec.tag ? '<span class="ab-tag">' + esc(v.sec.tag) + "</span>" : "") + "</div>" +
    v.blocks.map((b) => abBlock(b, key)).join("") +
    "</section>").join("");

  const hit = $("#abHit");
  if (hit) {
    hit.textContent = key
      ? "命中 " + view.length + " 节 · " + n + " 条"
      : "共 " + ABOUT_DOC.length + " 节 · " + n + " 条";
  }
  if (!view.length) {
    doc.innerHTML = '<div class="ab-sec"><p class="ab-p">没有匹配的章节，试试「仓位」「估值」「回测」「待办」。</p></div>';
    toc.innerHTML = "";
  }
  // 面板重画会丢掉 updateStatus 写进去的版本信息，这里用缓存补回
  const ci = $("#cfgInfo");
  if (ci && LAST_STATUS) ci.textContent = cfgInfoText(LAST_STATUS);
  abSpy();
}

/* 目录高亮：用「最后一个已滚过顶部 160px 的章节」判断当前位置，
   比 IntersectionObserver 稳（首屏与跳转时不会出现空档）。 */
function abSpy() {
  const links = $$("#abToc a[data-sec]");
  if (!links.length) return;
  const y = window.scrollY + 160;
  let cur = links[0];
  links.forEach((a) => {
    const el = document.getElementById("ab-" + a.dataset.sec);
    if (el && el.getBoundingClientRect().top + window.scrollY <= y) cur = a;
  });
  links.forEach((a) => a.classList.toggle("on", a === cur));
}

/* ---------------- 设置入口：所有参数统一收在数据中枢 ---------------- */
function openDrawer() { goHub("screen"); }
function closeDrawer() {}

/* ---------------- misc ---------------- */
/* ---------------- 行业分类与强度 ---------------- */
const IND = { loaded: false, list: [], cur: "", members: [], data: null };

function indLoad(btn) {
  return withBusy(btn, async () => {
    const r = await api("/api/industry/list");
    if (!r.ok) { toast(r.error || "行业数据读取失败", "err"); return; }
    IND.loaded = true;
    IND.data = r;
    const meta = r.meta || {};
    const lv = meta.levels || {};
    const pill = $("#indSourcePill");
    pill.textContent = meta.source || (r.count ? "已就绪" : "未同步");
    pill.className = "pill " + (r.count ? "yes" : "no");
    $("#indMeta").innerHTML = "主口径 <b>申万一级</b> " + (lv["申万一级"] || 0) +
      " 只 · 证监会行业 " + (lv["证监会"] || 0) + " 只 · 手工覆盖 " + (lv["manual"] || 0) +
      " 只 · 更新于 " + esc(meta.updated || "—");
    $("#indChipCount").innerHTML = "覆盖 <b>" + r.count + "</b> 只";
    $("#indChipIndustry").innerHTML = "<b>" + (r.industries || []).length + "</b> 个行业";
    $("#indChipCached").innerHTML = "有行情 <b>" + r.cached + "</b> 只";
    $("#indChipUpdated").textContent = "强度样本 " + (r.strength_sample || 0) + " 个行业";
    IND.list = r.industries || [];
    renderIndTable(IND.list);
    renderIndChart(IND.list);
  });
}

function renderIndTable(list) {
  const body = $("#indBody");
  $("#indEmpty").style.display = list.length ? "none" : "block";
  body.innerHTML = list.map((x) => {
    const cov = x.count ? Math.round((x.cached / x.count) * 100) : 0;
    const ret = x.ret20 == null
      ? '<span class="hint" style="margin:0">样本不足</span>'
      : '<span class="' + signCls(x.ret20) + '">' + signed(x.ret20) + "</span>" +
        (x.cached < 5 ? ' <span class="hint" style="margin:0">样本少</span>' : "");
    const on = IND.cur === x.industry ? " class='on'" : "";
    return "<tr data-ind='" + esc(x.industry) + "' style='cursor:pointer'" + on + ">" +
      "<td><b>" + esc(x.industry) + "</b></td><td>" + esc(x.level || "—") + "</td>" +
      "<td>" + x.count + "</td><td>" + x.cached + "</td><td>" + ret + "</td>" +
      "<td>" + (x.turn5 == null ? "—" : num(x.turn5, 2)) + "</td><td>" + cov + "%</td></tr>";
  }).join("");
  $$("#indBody tr").forEach((tr) => tr.addEventListener("click", () => {
    IND.cur = tr.dataset.ind;
    $$("#indBody tr").forEach((y) => y.classList.toggle("on", y === tr));
    indMembers(tr.dataset.ind);
  }));
}

function renderIndChart(list) {
  const c = chart("indChart");
  const rows = list.filter((x) => x.ret20 != null).slice(0, 20).reverse();
  if (!rows.length) { c.clear(); return; }
  c.setOption({
    grid: { left: 92, right: 52, top: 12, bottom: 22 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (p) => {
        const row = rows.find((x) => x.industry === p[0].name) || {};
        return p[0].name + "：20 日 " + (p[0].value > 0 ? "+" : "") + p[0].value + "%" +
          "<br/>样本 " + (row.cached || 0) + " 只 / 成分 " + (row.count || 0) + " 只";
      } },
    xAxis: { type: "value", axisLabel: { formatter: "{value}%" },
      splitLine: { lineStyle: { color: "#eef1f5" } } },
    yAxis: { type: "category", data: rows.map((x) => x.industry),
      axisLabel: { fontSize: 11, color: "#525c6b" }, axisTick: { show: false } },
    series: [{
      type: "bar", barMaxWidth: 15,
      data: rows.map((x) => ({
        value: x.ret20,
        itemStyle: { color: x.ret20 >= 0 ? "#d9483f" : "#1f9d55", borderRadius: [0, 3, 3, 0] }
      })),
      label: { show: true, position: "right", fontSize: 10,
        formatter: (p) => (p.value > 0 ? "+" : "") + p.value + "%" }
    }]
  }, true);
}

async function indMembers(name) {
  $("#indMemberTag").textContent = name + " · 读取中…";
  const r = await api("/api/industry/members?name=" + encodeURIComponent(name));
  if (!r.ok) { toast(r.error || "成分股读取失败", "err"); return; }
  IND.members = r.rows || [];
  $("#indMemberTag").textContent = name + " · " + r.count + " 只（有行情 " + r.cached + "）";
  $("#indMemEmpty").style.display = IND.members.length ? "none" : "block";
  $("#indMemBody").innerHTML = IND.members.map((x) =>
    "<tr class='rowlink' data-code='" + esc(String(x.code).padStart(6, "0")) + "' title='点击查看走势'>" +
    "<td>" + x.code + "</td><td>" + esc(x.name || "—") + "</td><td>" +
    (x.price === "" ? "—" : esc(x.price)) + "</td><td class='" +
    (x.pct === "" ? "" : signCls(Number(x.pct))) + "'>" +
    (x.pct === "" ? "—" : signed(Number(x.pct))) + "</td><td>" +
    (x.pe === "" ? "—" : num(x.pe, 1)) + "</td><td>" + (x.pb === "" ? "—" : num(x.pb, 2)) +
    "</td><td>" + (x.mv === "" ? "—" : num(x.mv, 0)) + "</td><td>" +
    (x.turnover === "" || x.turnover == null ? "—" : num(x.turnover, 2)) +
    "</td><td><span class='pill " + (x.cached ? "yes" : "no") + "'>" +
    (x.cached ? "已缓存" : "未缓存") + "</span></td></tr>").join("");
}

async function indAddPool() {
  if (!IND.cur || !IND.members.length) { toast("请先选择一个行业", "err"); return; }
  const cur = await api("/api/watchlist");
  const items = (cur.items || []).map((x) => ({ code: x.code, name: x.name, source: x.source }));
  const seen = new Set(items.map((x) => x.code));
  let added = 0;
  IND.members.forEach((x) => {
    if (!seen.has(x.code)) { seen.add(x.code); items.push({ code: x.code, name: x.name, source: "行业批量" }); added++; }
  });
  if (!added) { toast("该行业成分股都已在股票池中", "ok"); return; }
  const r = await post("/api/watchlist", { action: "replace", items });
  toast(r.ok ? ("已加入 " + added + " 只（" + IND.cur + "）") : (r.error || "加入失败"), r.ok ? "ok" : "err");
  if (r.ok) { loadPool(); }
}

function indSync(btn) {
  return withBusy(btn, async () => {
    const r = await post("/api/screen/industry", { force: true });
    if (!r.ok) { toast("同步失败：" + (r.error || "未知错误"), "err"); return; }
    toast(r.message || "已开始同步行业分类", "ok");
    $("#indSyncHint").textContent = "同步中：申万一级 → 证监会补全，约 40~90 秒…";
    if (IND.timer) clearInterval(IND.timer);
    IND.timer = setInterval(async () => {
      const s = await api("/api/screen/industry");
      if (!s.ok) return;
      const sy = s.sync || {};
      if (sy.progress) $("#indSyncHint").textContent = "同步中：" + sy.progress;
      if (sy.running) return;
      clearInterval(IND.timer); IND.timer = null;
      $("#indSyncHint").textContent = "口径：申万一级为主（研究通用），证监会行业补全新股与北交所。";
      toast("行业分类已更新：" + (s.count || 0) + " 只 / " + (s.industries || 0) + " 个行业", "ok");
      await indLoad(); await screenStatus();
    }, 2000);
  });
}

/* ---------------- 数据中枢：统一配置 / 统一更新 / 更新记录 ---------------- */
const HUB = { data: null, timer: null, tab: "chain" };
const HUB_TABS = ["chain", "screen", "backtest", "sources", "real", "ai", "runs", "logs"];

function hubTab(name) {
  if (HUB_TABS.indexOf(name) < 0) name = "chain";
  HUB.tab = name;
  $$("#hubTabs .hub-tab").forEach((b) => {
    const on = b.dataset.hubtab === name;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  $$(".hub-pane[data-hubtab]").forEach((p) => p.classList.toggle("on", p.dataset.hubtab === name));
  if (name === "screen") screenStatus();
  if (name === "sources") loadAccount();
  if (name === "real") rtCfgFill();
  if (name === "ai") hubAiLoad();
  if (name === "runs") runsLoad(true);
  if (name === "logs") logLoad(true);
}

function goHub(name) {
  switchTab("hub");
  hubTab(name || HUB.tab || "chain");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* 复盘 AI 的模式与自动开关：中枢和实盘页共享同一份 /api/emt/ai/config */
function hubAiPaint(r) {
  r = r || {};
  const modes = r.modes || {};
  rtSegPaint("#hubAiHealth", modes.health || "local");
  rtSegPaint("#hubAiJournal", modes.journal || "local");
  const auto = $("#hubAiAuto"); if (auto) auto.checked = r.auto !== false;
  const llm = r.llm || {};
  const note = $("#hubAiNote");
  if (note) {
    note.textContent = llm.ready
      ? ("LLM 已就绪（" + (llm.model || "—") + "）。AI 模式在收盘日结后自动生成：体检进「实盘复盘」，日记进「复盘日记」。")
      : "LLM 未配置或未启用：切到 AI 模式也会自动退回本地规则，不会把模板文字伪装成 AI 稿。";
  }
}
async function hubAiLoad() {
  const r = await api("/api/emt/ai/config");
  if (r && r.ok !== false) hubAiPaint(r);
}
async function hubAiSet(kind, mode) {
  if (mode !== "local" && mode !== "ai") return;
  const r = await post("/api/emt/ai/config", { [kind]: mode });
  if (!r || !r.ok) { toast((r && r.error) || "切换失败", "err"); return; }
  hubAiPaint(r);
  const ai = RT.ai || {};
  ai.modes = (r.modes) || Object.assign({}, ai.modes, { [kind]: mode });
  ai.llm = r.llm || ai.llm;
  RT.ai = ai;
  if (RT.data && !RT.data.empty) { RT.data.ai = ai; rtAiRender(RT.data); }
  if (mode === "ai" && !(r.llm || {}).ready) {
    toast("LLM 未启用：先在上方「LLM 接口」填端点并启用", "info");
  }
}
async function hubAiAutoSet() {
  const on = !!($("#hubAiAuto") && $("#hubAiAuto").checked);
  const r = await post("/api/emt/ai/config", { auto: on });
  if (!r || !r.ok) { toast((r && r.error) || "保存失败", "err"); return; }
  hubAiPaint(r);
  toast(on ? "已开启：每个交易日收盘后自动生成" : "已关闭收盘自动生成", "ok");
}

function hubKind(k, level) {
  const m = { pipeline: "流水线", quote: "行情增量", spot: "实时快照", universe: "全 A 名单",
    industry: "行业分类", ledger: "实盘同步", valuation: "全市场估值" };
  const base = m[k] || k || "更新";
  if (k === "pipeline" && level) {
    const lv = { auto: "按需", quick: "快照", full: "全流程" }[level] || level;
    return base + " · " + lv;
  }
  return base;
}
function hubTrigger(t) {
  return { manual: "手动", scheduled: "定时", shortcut: "快捷" }[t] || t || "—";
}
function hubMs(ms) {
  ms = Number(ms || 0);
  if (ms < 1000) return ms + "ms";
  if (ms < 60000) return (ms / 1000).toFixed(1) + "s";
  return Math.floor(ms / 60000) + "m" + Math.round((ms % 60000) / 1000) + "s";
}
function hubSet(id, v) {
  const el = $("#" + id);
  if (el && document.activeElement !== el) el.value = v;
}
function hubCheck(id, v) {
  const el = $("#" + id);
  if (el && document.activeElement !== el) el.checked = !!v;
}

function hubChain(r) {
  const body = $("#hubChain"), pill = $("#hubPill"), tip = $("#hubTip");
  if (!body) return;
  if (!r || !r.ok) {
    if (pill) { pill.textContent = "不可用"; pill.className = "pipe-pill warn"; }
    body.innerHTML = '<div class="k">状态</div><div class="v">' + esc((r && r.error) || "读取失败") + "</div>";
    return;
  }
  const run = r.run || {};
  const dataStale = r.data_stale == null ? !!r.stale : !!r.data_stale;
  const modelDue = !!(r.model && r.model.retrain_due);
  if (pill) {
    pill.textContent = run.running ? "运行中"
      : dataStale ? "需更新" : (modelDue ? "模型待重训" : "已是最新");
    pill.className = "pipe-pill " +
      (run.running ? "busy" : ((dataStale || modelDue) ? "warn" : "good"));
  }
  const lag = (n) => (n == null ? "—" : (n > 0 ? "落后 " + n + " 日" : "最新"));
  const qlag = Number(r.quote_lag || 0);
  const lm = r.dataset.label_mode === "rank" ? "分位标签"
    : r.dataset.label_mode === "industry" ? "行业内超额" : "全市场超额";
  const kv = [
    ["行情", pipeDate(r.as_of) + " · 缓存 " + r.quote_cached + " 只",
      r.quote_blocked ? "限流·快照补"
        : r.quote_source && r.quote_source.indexOf("spot") === 0 ? "快照补日线"
        : qlag > 0 ? ("落后 " + qlag + " 日" + (r.expected ? " · 应到 " + pipeDate(r.expected) : ""))
        : (r.quote_stale ? "今日未入库" : "最新")],
    ["快照", (r.snapshot.count || 0) + " 只 · " + pipeDate(r.snapshot.as_of), lag(r.snapshot.lag)],
    ["训练集", (r.dataset.rows || 0) + " 行 · " + pipeDate(r.dataset.date_max), lm + " · " + lag(r.dataset.lag)],
    ["模型", r.model.trained
      ? (r.model.age_days == null ? "已训练" : r.model.age_days + " 天前训练") +
        (r.model.ic == null ? "" : " · RankIC " + scrFmt(r.model.ic, 3)) +
        (r.model.ic_ir == null ? "" : " · IR " + scrFmt(r.model.ic_ir, 2))
      : "未训练", r.model.retrain_due ? "建议重训" : "可用"]
  ];
  // 估值与日历都是「过期了也看不出」的环节，直接列在数据链里：
  // 有几千行估值不代表它是新的，日历到期更会让上面所有「最新」失去依据。
  const vas = r.valuation || {};
  if (vas.configured) {
    kv.push(["估值", vas.last_date ? pipeDate(vas.last_date) : "未拉取",
      vas.stale ? ("落后 " + vas.lag + " 日" + (vas.expected ? " · 应到 " + pipeDate(vas.expected) : ""))
        : "最新"]);
  }
  const cal = r.calendar || {};
  if (cal.covered === false) {
    kv.push(["日历", cal.to ? ("只到 " + pipeDate(cal.to)) : "读不到",
      cal.to ? "时效检查失效" : "请联网刷新"]);
  }
  body.innerHTML = kv.map((p) => '<div class="k">' + p[0] + '</div><div class="v">' +
    esc(p[1]) + ' <span class="pipe-flag">' + esc(p[2]) + "</span></div>").join("");
  const badStep = (run.steps || []).filter((s) => !s.ok)[0];
  if (badStep) {
    body.innerHTML += '<div class="k bad">上次失败</div><div class="v bad">' +
      esc(badStep.name + "：" + (badStep.detail || "未返回原因")) + "</div>";
  }
  // 进度不在这里画：统一由顶栏状态灯显示（蓝环 + 悬停明细）。
  if (tip) {
    const acts = r.actions || [];
    tip.textContent = run.running ? "正在执行，完成后自动刷新"
      : badStep ? ("上次更新失败 · " + badStep.name + "：" + (badStep.detail || "未返回原因"))
      : (acts.length ? ((acts.find((a) => a.level === "warn") || acts[0]).text)
        : "四段数据链都在最新状态");
    tip.classList.toggle("bad", !!badStep);
  }
  clearTimeout(HUB.timer);
  if (run.running) HUB.timer = setTimeout(hubPoll, 2500);
}

/* ---------------- 更新记录：筛选 + 分页（记录不清空，靠翻页看历史） ---------------- */
const RUNS = { kind: "all", trigger: "all", ok: "all", q: "", offset: 0, total: 0, busy: false };

function runsFiltered() {
  return RUNS.kind !== "all" || RUNS.trigger !== "all" || RUNS.ok !== "all" || !!RUNS.q.trim();
}
function runsQuery(limit) {
  const p = new URLSearchParams({ limit: String(limit || 60), offset: String(RUNS.offset) });
  if (RUNS.kind !== "all") p.set("kind", RUNS.kind);
  if (RUNS.trigger !== "all") p.set("trigger", RUNS.trigger);
  if (RUNS.ok !== "all") p.set("ok", RUNS.ok);
  if (RUNS.q.trim()) p.set("q", RUNS.q.trim());
  return p.toString();
}
function runsReadForm() {
  if ($("#runKind")) RUNS.kind = $("#runKind").value || "all";
  if ($("#runTrigger")) RUNS.trigger = $("#runTrigger").value || "all";
  if ($("#runOk")) RUNS.ok = $("#runOk").value || "all";
  if ($("#runQ")) RUNS.q = $("#runQ").value || "";
}
async function runsLoad(reset) {
  if (RUNS.busy) return;
  RUNS.busy = true;
  if (reset) RUNS.offset = 0;
  try {
    const d = await api("/api/data/runs?" + runsQuery(60));
    if (!d || !d.ok) { toast((d && d.error) || "读取更新记录失败", "err"); return; }
    RUNS.total = d.total || 0;
    const rows = d.runs || [];
    if (reset) hubRuns(rows); else hubRunsAppend(rows);
    RUNS.offset += rows.length;
    const t = $("#runTotal");
    if (t) t.textContent = RUNS.total ? ("共 " + RUNS.total + " 条 · 已显示 " + RUNS.offset) : "";
    const more = $("#btnRunMore");
    if (more) more.disabled = !RUNS.total || RUNS.offset >= RUNS.total;
  } finally {
    RUNS.busy = false;
  }
}

/* ---------------- 系统运行日志 ---------------- */
const LOG = { level: "all", src: "all", q: "", offset: 0, limit: 200, total: 0, busy: false };
const LOG_LABEL = { info: "信息", warn: "警告", error: "错误", debug: "调试" };
// 来源中文名：下拉和表格都显示这个，value 仍是原始 key（筛选不改）
const LOG_SRC_NAME = {
  ui: "界面通知", scheduler: "调度器", pipeline: "流水线", valsrc: "估值多源",
  datasource: "数据源", bulkdata: "行情增量", store: "存储", app: "服务",
  api: "接口", emt: "实盘账户", emtlink: "实盘账户", emtai: "AI 复盘",
  journal: "复盘日记", industry: "行业分类", screener: "选股", backtest: "回测",
  llm: "大模型", universe: "股票池", spot: "实时行情", histcache: "行情缓存"
};
function logSrcName(s) { return LOG_SRC_NAME[s] || s || "—"; }

function logQuery(extra) {
  const p = new URLSearchParams();
  if (LOG.level !== "all") p.set("level", LOG.level);
  if (LOG.src !== "all") p.set("src", LOG.src);
  if (LOG.q.trim()) p.set("q", LOG.q.trim());
  if (extra) Object.keys(extra).forEach((k) => p.set(k, extra[k]));
  return p.toString();
}
function logRowHtml(x) {
  const lv = String(x.level || "info").toLowerCase();
  const det = x.detail ? '<div class="log-det" hidden>' + esc(x.detail) + "</div>" : "";
  return '<tr class="log-row" data-logid="' + x.id + '">' +
    "<td>" + esc((x.at || "").slice(5, 19)) + "</td>" +
    '<td><span class="lv ' + esc(lv) + '">' + esc(LOG_LABEL[lv] || lv) + "</span></td>" +
    '<td><span class="log-src' + (x.src === "ui" ? " ui" : "") + '" title="' +
      esc(x.src || "") + '">' + esc(logSrcName(x.src)) + "</span></td>" +
    '<td class="log-msg">' + esc(x.msg || "") +
      (x.act ? '<span class="log-act">' + esc(x.act) + "</span>" : "") + det + "</td></tr>";
}
async function logLoad(reset) {
  if (LOG.busy) return;
  LOG.busy = true;
  if (reset) { LOG.offset = 0; LOG.total = 0; }
  try {
    const d = await api("/api/logs?" + logQuery({ limit: LOG.limit, offset: LOG.offset }));
    if (!d || !d.ok) { toast((d && d.error) || "读取日志失败", "err"); return; }
    LOG.total = d.total || 0;
    const items = d.items || [];
    const tb = $("#logBody");
    if (tb) {
      const html = items.map(logRowHtml).join("");
      if (reset) tb.innerHTML = html; else tb.insertAdjacentHTML("beforeend", html);
    }
    LOG.offset += items.length;
    logFacets(d.facets);
    const empty = $("#logEmpty");
    if (empty) empty.style.display = LOG.offset ? "none" : "";
    const t = $("#logTotal");
    if (t) t.textContent = LOG.total ? ("共 " + LOG.total + " 条 · 已显示 " + LOG.offset) : "";
    const more = $("#btnLogMore");
    if (more) more.disabled = !LOG.total || LOG.offset >= LOG.total;
    const keep = $("#logKeep");
    if (keep && d.keep) {
      keep.textContent = "保留 " + d.keep.days + " 天 / " + Math.round(d.keep.rows / 1000) +
        " 千条，超出自动清理（更新记录不受影响）";
    }
  } finally {
    LOG.busy = false;
  }
}
function logFacets(f) {
  if (!f) return;
  const set = (id, v) => { const el = $("#" + id); if (el) el.textContent = v; };
  const c = f.counts || {}, r = f.recent || {};
  const all = Number(c.info || 0) + Number(c.warn || 0) + Number(c.error || 0);
  set("logCntAll", all);
  set("logCntWarn", Number(c.warn || 0) + Number(c.error || 0));
  set("logCntErr", c.error || 0);
  set("logCntInfo", c.info || 0);
  const pill = $("#logPill");
  if (pill) {
    const bad = Number(r.error || 0);
    const warn = Number(r.warn || 0);
    pill.textContent = bad ? ("近 24h " + bad + " 条错误") : warn ? ("近 24h " + warn + " 条警告") : "近 24h 无异常";
    pill.className = "pill " + (bad ? "bad" : warn ? "todo" : "yes");
  }
  const sel = $("#logSrc");
  if (sel) {
    const cur = LOG.src;
    const opts = ['<option value="all">全部来源</option>'].concat(
      (f.sources || []).map((s) => '<option value="' + esc(s) + '">' + esc(logSrcName(s)) + "</option>"));
    sel.innerHTML = opts.join("");
    sel.value = (f.sources || []).indexOf(cur) >= 0 ? cur : "all";
    if (sel.value !== cur) LOG.src = "all";
  }
}


function hubRunRow(r) {
  const steps = r.steps || [];
  const bad = steps.filter((s) => !s.ok);
  const skip = !bad.length && steps.length && steps.every((s) => s.skip);
  const names = steps.map((s) => s.name).join("、");
  const detail = bad.length ? (bad[0].name + "：" + (bad[0].detail || r.error || "失败"))
    : (steps.length ? (steps[steps.length - 1].detail || "") : (r.error || ""));
  const res = r.ok === null ? '<span class="pipe-pill busy">进行中</span>'
    : skip ? '<span class="pipe-pill">跳过</span>'
    : r.ok ? '<span class="pipe-pill good">成功</span>'
    : '<span class="pipe-pill warn">失败</span>';
  const line = (names ? "<b>" + esc(names) + "</b>" : "") +
    (detail ? '<small class="hub-detail' + (bad.length ? " bad" : "") + '" title="' +
      esc(detail) + '">' + esc(detail) + "</small>" : "");
  return "<tr><td>" + esc((r.started || "").slice(5, 16)) + "</td><td>" + esc(hubKind(r.kind, r.level)) +
    "</td><td>" + esc(hubTrigger(r.trigger)) + "</td><td>" + res + "</td><td>" + hubMs(r.ms) +
    '</td><td class="hub-steps-cell">' + line + "</td></tr>";
}

function hubRuns(runs, ids) {
  ids = ids || {};
  const tb = $("#" + (ids.body || "hubRunsBody")), empty = $("#" + (ids.empty || "hubRunsEmpty"));
  if (!tb) return;
  runs = runs || [];
  if (empty) empty.style.display = runs.length ? "none" : "";
  tb.innerHTML = runs.map(hubRunRow).join("");
}

function hubRunsAppend(runs) {
  const tb = $("#hubRunsBody");
  if (!tb || !runs || !runs.length) return;
  const empty = $("#hubRunsEmpty");
  if (empty) empty.style.display = "none";
  tb.insertAdjacentHTML("beforeend", runs.map(hubRunRow).join(""));
}

function hubSchedInfo(s) {
  const info = $("#hubSchedInfo");
  if (!info || !s || !s.ok) { if (info) info.textContent = "定时任务读取失败"; return; }
  hubCheck("hubSched", s.enabled);
  hubSet("hubTime", s.time || "18:10");
  const tm = $("#hubTime"); if (tm) tm.disabled = !s.enabled;
  const last = s.last || {};
  const lastTxt = last.at ? ("上次 " + last.at.slice(5, 16) + " " +
    (last.ok === false ? "失败" : last.ok === null ? "执行中" : "成功")) : "尚未执行";
  info.textContent = (s.enabled ? (autoDays(s.weekdays) + " " + s.time + " · " + (s.state || "")) : "已关闭") +
    " · " + lastTxt + (s.next_run ? " · 下次 " + s.next_run.slice(5, 16) : "");
  info.classList.toggle("bad", last.ok === false);
}

const VAL_CAP = { snapshot: "快照 · 只给当日", date: "按日 · 可回补历史",
  price: "行情 · 仅价格兜底" };
const VAL_STATE = {
  "正常": ["yes", "正常"], "待收盘": ["todo", "待收盘"], "限频": ["sim", "限频"],
  "失败": ["bad", "失败"], "关闭": ["off", "关闭"], "未使用": ["no", "未使用"]
};

function valErrText(e) {
  const t = String(e || "");
  if (!t) return "没取到";
  if (/频率超限|限频/.test(t)) return "限频，已按提示退避，稍后自动重试";
  if (/未配置 Token/.test(t)) return "未配置 Token";
  return t;
}

function hubValRow(x, total) {
  const l = x.last || {};
  const detail = l.ok
    ? (pipeDate(l.date) + " · " + (l.count || 0) + " 只" +
       (l.cover ? " · 覆盖 " + Math.round(l.cover * 100) + "%" : "") +
       (l.ms ? " · " + (l.ms / 1000).toFixed(1) + " 秒" : ""))
    : (l.at ? (l.at.slice(5, 16) + " " + valErrText(l.error)) : x.fields);
  const shown = detail.length > 96 ? detail.slice(0, 94) + "…" : detail;
  const st = VAL_STATE[x.state] || ["no", x.state || "—"];
  // 东财已降为低频备份：它连不上只是这台机器的网络条件，不算当天的故障
  const badge = x.backup && x.state === "失败" ? ["sim", "备份未通"] : st;
  return '<div class="vsrc' + (x.enabled ? "" : " off") + '" data-key="' + esc(x.key) + '">' +
    '<span class="vsrc-no">' + (x.order || "") + "</span>" +
    '<label class="vsrc-sw" title="参与 / 不参与"><input type="checkbox" data-vschk="' + esc(x.key) + '"' +
      (x.enabled ? " checked" : "") + "><i></i></label>" +
    '<div class="vsrc-main"><b>' + esc(x.name) + "</b>" +
      "<small>" + esc(VAL_CAP[x.cap] || "") + " · " + (x.token ? "需 Token" : "免账号") +
      " · " + esc(x.speed) + (x.backup ? " · 失败冷却 6h" : "") + "</small>" +
      "<em>" + esc(shown) + "</em></div>" +
    '<span class="pill ' + badge[0] + '">' + esc(badge[1]) + "</span>" +
    '<div class="vsrc-ops">' +
      '<button class="btn sec xs" data-vsact="up" data-key="' + esc(x.key) + '"' +
        (x.order <= 1 ? " disabled" : "") + ' title="上移">↑</button>' +
      '<button class="btn sec xs" data-vsact="down" data-key="' + esc(x.key) + '"' +
        (x.order >= total ? " disabled" : "") + ' title="下移">↓</button>' +
      '<button class="btn sec xs" data-vsact="test" data-key="' + esc(x.key) + '">测试</button>' +
    "</div></div>";
}

function hubValRender(v) {
  const box = $("#hubValList");
  if (box && v && v.sources) {
    box.innerHTML = v.sources.map((x) => hubValRow(x, v.sources.length)).join("");
  }
  const st = $("#hubValStatus");
  if (!st || !v) return;
  if (!v.last_date) {
    st.textContent = "还没有估值数据";
    st.classList.remove("warn");
    return;
  }
  const src = (v.sources || []).find((x) => x.key === v.last_source);
  const nm = src ? src.name : (v.last_source === "local" ? "本地缓存" : (v.last_source || "—"));
  let txt = "当前 " + pipeDate(v.last_date) + " · " + (v.last_count || 0) + " 只 · 来源 " + nm;
  txt += v.stale
    ? " · 落后 " + (v.lag == null ? "?" : v.lag) + " 个交易日（应到 " + pipeDate(v.expected) + "）"
    : " · 最新";
  // 现在取不到时，直接说清「几点能取」，别让用户反复点、每点一次看一屏红字
  const pre = v.precheck || {};
  if (pre.until && !pre.can) {
    const mins = Math.max(1, Math.round((pre.wait_sec || 0) / 60));
    txt += " · " + pre.until + " 后自动取" + (mins <= 240 ? "（约 " + mins + " 分钟）" : "");
  }
  st.textContent = txt;
  st.classList.toggle("warn", !!v.stale);
}

function hubSources(d) {
  const s = d.sources || {};
  const sp = $("#hubSrcPill");
  if (sp) { sp.textContent = s.configured ? "已配置" : "未配置"; sp.className = "pill " + (s.configured ? "yes" : "no"); }
  const tk = $("#hubSrcToken");
  if (tk) tk.placeholder = s.configured ? ("已保存 " + s.token_masked + "，留空表示不修改") : "粘贴 Tushare Token（约 40 位）";
  hubValRender(s.valuation);
  const ss = $("#hubSrcStatus");
  if (ss) {
    // Token 状态只说「有没有配、配到哪一步」，估值新不新由上方的来源链负责。
    ss.textContent = s.configured
      ? ("Token " + (s.token_masked || "已保存") + " · 最近拉取 " + (s.last_fetch || "—") +
         " · 走 " + ((s.available || []).join(" / ") || "—"))
      : "未配置 Token：东财 / 腾讯两路仍然可用，只是不能按历史日回补";
    ss.classList.toggle("bad", false);
  }

  const l = d.llm || {};
  const lp = $("#hubLlmPill");
  if (lp) { lp.textContent = l.enabled && l.configured ? "已启用" : (l.configured ? "未启用" : "未配置");
    lp.className = "pill " + (l.enabled && l.configured ? "yes" : "no"); }
  hubSet("hubLlmUrl", l.base_url || "");
  hubSet("hubLlmModel", l.model || "");
  hubSet("hubLlmMaxTokens", l.max_tokens || 0);
  hubCheck("hubLlmEnabled", l.enabled);
  const ln = $("#hubLlmNote");
  if (ln) ln.textContent = l.configured
    ? ("Key " + (l.api_key_masked || "已保存") + " · 推理模型把最大输出 tokens 调到 3000 以上更稳")
    : "可不填；不填时用本地规则生成「一眼理由」，不会伪装成大模型结论。";
}

function hubRender(d) {
  if (!d || !d.ok) { toast((d && d.error) || "数据中枢读取失败", "err"); return; }
  HUB.data = d;
  hubChain(d.freshness);
  hubSchedInfo(d.scheduler);
  hubSet("hubWorkers", d.bulk_workers || 6);
  hubSources(d);
  hubBearPaint(d.llm);
  hubAuxLoad();
  // 首屏先用中枢带回来的 40 条快速铺上，再按筛选条补一次带 total 的分页结果
  hubRuns(d.runs);
  if (!runsFiltered()) runsLoad(true);
}

/* 名单与资金流不在 /api/data/hub 里（各自有独立接口，能单独同步），
   进中枢时并行补一次；任一失败就只让那一张卡显示「读取失败」，不拖垮整页。 */
async function hubAuxLoad() {
  const [l, c] = await Promise.all([api("/api/listing"), api("/api/capital")]);
  if (l && l.ok !== false) hubListingUi(l);
  else if ($("#hubListKv")) $("#hubListKv").innerHTML =
    '<div class="k">状态</div><div class="v">读取失败：' + esc((l && l.error) || "未知错误") + "</div>";
  if (c && c.ok !== false) hubCapitalUi(c);
  else if ($("#hubCapKv")) $("#hubCapKv").innerHTML =
    '<div class="k">状态</div><div class="v">读取失败：' + esc((c && c.error) || "未知错误") + "</div>";
}

async function hubLoad() {
  const d = await api("/api/data/hub");
  hubRender(d);
  return d;
}

async function hubPoll() {
  const d = await api("/api/data/hub");
  if (!d || !d.ok) return;
  HUB.data = d;
  hubChain(d.freshness);
  // 用户正在筛选或翻页时不要把他正在看的结果冲掉
  if (!runsFiltered() && RUNS.offset <= 60) runsLoad(true);
  const run = (d.freshness && d.freshness.run) || {};
  if (!run.running) {
    hubSchedInfo(d.scheduler);
    hubSources(d);
    sideTodoLoad();
    const steps = run.steps || [];
    const bad = steps.filter((s) => !s.ok);
    if (steps.length) {
      toast(bad.length ? ("更新完成，" + bad.length + " 步失败 · " + bad[0].name + "：" + (bad[0].detail || "见记录"))
        : "数据链已更新到最新", bad.length ? "err" : "ok");
    }
  }
}

function hubSelected() {
  const only = [];
  if ($("#hubStepQuote").checked) only.push("quote");
  if ($("#hubStepSnapshot").checked) only.push("snapshot");
  if ($("#hubStepDataset").checked) only.push("dataset");
  if ($("#hubStepModel").checked) only.push("model");
  return only;
}

async function hubRun(mode, btn) {
  const go = async () => {
    let body;
    if (mode === "sel") {
      const only = hubSelected();
      if (!only.length) { toast("至少勾选一段再执行", "err"); return; }
      body = { only };
    } else if (mode === "full") {
      body = { level: "full", quote: true };
    } else {
      body = { level: "auto", quote: $("#hubStepQuote").checked };
    }
    const r = await post("/api/data/run", body);
    if (!r.ok) { toast(r.error || "启动失败", "err"); return; }
    toast(mode === "sel" ? "已开始执行所选段" : "已开始更新数据链", "ok");
    hubPoll();
  };
  if (mode === "full" && btn) { twoStep(btn, "确认重训", go); return; }
  await go();
}

async function hubWorkersSave(btn) {
  await withBusy(btn, async () => {
    const r = await post("/api/data/config", { bulk_workers: Number($("#hubWorkers").value) || 6 });
    toast(r.ok ? ("行情并发已保存：" + r.bulk_workers) : (r.error || "保存失败"), r.ok ? "ok" : "err");
  });
}

async function hubSchedSave() {
  const r = await post("/api/screen/scheduler",
    { enabled: $("#hubSched").checked, time: $("#hubTime").value || "18:10" });
  hubSchedInfo(r);
}

async function hubSrcSave(btn) {
  await withBusy(btn, async () => {
    const t = $("#hubSrcToken").value.trim();
    const r = await post("/api/sources", { token: t || undefined, enabled: true });
    toast(r.ok ? (t ? "Token 已保存（仅存本机）" : "状态已保存") : (r.error || "保存失败"), r.ok ? "ok" : "err");
    $("#hubSrcToken").value = "";
    const s = await api("/api/sources"); if (HUB.data) HUB.data.sources = s; hubSources(HUB.data || {});
  });
}
async function hubSrcTest(btn) {
  await withBusy(btn, async () => {
    const r = await post("/api/sources/test", {});
    toast(r.ok ? (r.message || "连接正常") : ("连接失败：" + (r.error || "未知错误")), r.ok ? "ok" : "err");
  });
}
async function hubSrcClear(btn) {
  await withBusy(btn, async () => {
    const r = await post("/api/sources", { action: "clear" });
    toast(r.ok ? "已清除 Tushare Token" : (r.error || "清除失败"), r.ok ? "ok" : "err");
    if (HUB.data) HUB.data.sources = r; hubSources(HUB.data || {});
  });
}
async function hubValSave(patch) {
  const r = await post("/api/sources", Object.assign({ action: "valsrc" }, patch));
  if (!r || !r.ok) { toast((r && r.error) || "保存失败", "err"); return null; }
  if (HUB.data) HUB.data.sources = r.sources;
  hubSources(HUB.data || {});
  return r;
}

async function hubSrcFetch(btn) {
  await withBusy(btn, async () => {
    sysTaskStart("valsrc", "拉取估值");
    let r;
    try { r = await post("/api/sources/fetch", { force: $("#hubSrcForce").checked }); } finally { sysTaskEnd("valsrc"); }
    if (r && r.blocked) {
      // 没到点：不是错误，是「现在取了也白取」。用提示而不是红字，也别让人以为坏了。
      toast(r.message || "现在还取不到新估值，到点会自动取", "info");
    } else {
      toast(r.ok ? (r.message || "已拉取") : ("拉取失败：" + (r.error || "未知错误")), r.ok ? "ok" : "err");
    }
    const s = await api("/api/sources"); if (HUB.data) HUB.data.sources = s; hubSources(HUB.data || {});
    runsLoad(true);
  });
}

function hubLlmPayload() {
  return {
    enabled: $("#hubLlmEnabled").checked,
    base_url: $("#hubLlmUrl").value.trim(),
    api_key: $("#hubLlmKey").value.trim(),
    model: $("#hubLlmModel").value.trim(),
    max_tokens: Number($("#hubLlmMaxTokens").value) || 0
  };
}
async function hubLlmSave(btn) {
  await withBusy(btn, async () => {
    const r = await post("/api/screen/llm", Object.assign({ action: "configure" }, hubLlmPayload()));
    if (!r.ok) { toast("保存失败：" + (r.error || "未知错误"), "err"); return; }
    $("#hubLlmKey").value = "";
    if (HUB.data) HUB.data.llm = r; hubSources(HUB.data || {});
    SCR.llmReady = !!(r.enabled && r.configured);
    const chip = $("#chipLlm");
    if (chip) {
      chip.textContent = SCR.llmReady ? ("LLM " + (r.model || "—")) : "LLM 本地解读";
      chip.className = "chip " + (SCR.llmReady ? "good" : "");
    }
    toast(r.enabled && r.configured ? ("LLM 已启用并保存（" + (r.model || "—") + "）")
      : "配置已保存（未启用或没填全，暂用本地规则）", "ok");
  });
}
async function hubLlmTest(btn) {
  await withBusy(btn, async () => {
    sysTaskStart("llm-test", "测试 LLM");
    let r;
    try { r = await post("/api/screen/llm", Object.assign({ action: "test" }, hubLlmPayload())); }
    finally { sysTaskEnd("llm-test"); }
    toast(r.ok ? ("连通成功：" + r.model + " · " + r.latency_ms + "ms") : ("连接失败：" + (r.error || "未知错误")),
      r.ok ? "ok" : "err");
  });
}
async function hubLlmClear(btn) {
  await withBusy(btn, async () => {
    const r = await post("/api/screen/llm", { action: "configure", clear_key: true });
    $("#hubLlmKey").value = "";
    if (HUB.data) HUB.data.llm = r; hubSources(HUB.data || {});
    SCR.llmReady = false;
    const chip = $("#chipLlm");
    if (chip) { chip.textContent = "LLM 本地解读"; chip.className = "chip"; }
    toast("已清除本机保存的 API Key", "ok");
  });
}

/* ==========================================================================
   P0-1 上市名单 / P0-3 因子体检 / P1-4 目标持仓 / P1-5 空头辩护 / P2-6 资金流
   --------------------------------------------------------------------------
   这几页的共同点：只读「已经算好的东西」，不在这里重算口径。
   页面要做的是把「为什么这么选」「亏在哪一类」「换个约束会怎样」讲清楚，
   而不是把后端的数字再算一遍 —— 两套算法必然分叉，分叉了就没人信。
   ========================================================================== */

/* ---------------- 数据中枢：上市名单（point-in-time） ---------------- */
function hubListingUi(d) {
  d = d || {};
  const cov = d.coverage || {};
  const pw = d.prewarm || {};
  const pill = $("#hubListPill");
  if (pill) {
    // 用交易日口径（lag<=0 即跟上了最近一个交易日）：周末名单必然是周五的，
    // 拿自然日比会一直显示「待同步」，让人误以为数据断了。
    const hasLag = d.lag !== null && d.lag !== undefined;
    const ok = !!d.updated && (hasLag ? Number(d.lag) <= 0 : !!d.fresh);
    pill.textContent = !d.total ? "未同步" : (ok ? "已是最新" : "待同步");
    pill.className = "pill " + (ok ? "yes" : "no");
  }
  const kv = $("#hubListKv");
  if (kv) {
    const pct = (v) => (v == null ? "—" : (Number(v) * 100).toFixed(1) + "%");
    const rows = [
      ["名单总数", (d.total || 0).toLocaleString("zh-CN") + " 只"],
      ["在市 / 退市", (d.listed || 0) + " / " + (d.delisted || 0)],
      ["名单日期", esc(d.updated || "—")],
      ["退市股有行情", cov.total ? (cov.cached + " / " + cov.total + "（" + pct(cov.cover) + "）") : "—"],
      ["近两年退市", cov.recent_2y ? (cov.recent_cached + " / " + cov.recent_2y + " 只可回放") : "—"],
      ["补全进度", pw.running
        ? ("进行中 " + (pw.done || 0) + " / " + (pw.total || 0) + "（成 " + (pw.ok || 0) + " 败 " + (pw.fail || 0) + "）")
        : (pw.message || "未运行")]
    ];
    kv.innerHTML = rows.map((r) =>
      '<div class="k">' + r[0] + '</div><div class="v">' + r[1] + "</div>").join("");
  }
  const note = $("#hubListNote");
  if (note) {
    if (!d.total) {
      note.textContent = "还没有本地名单：点「同步名单」从 baostock 拉一次全量（约 5 秒，结果缓存到当天）。";
    } else if (cov.recent_missing > 0) {
      note.textContent = "近两年退市的 " + cov.recent_2y + " 只里还有 " + cov.recent_missing +
        " 只没有行情，历史回放仍会漏掉它们 —— 点「补退市行情」串行补，中途可停。" +
        "更早退市的个股接口已查无数据，补不全属正常。";
    } else {
      note.textContent = "近两年退市的 " + cov.recent_2y + " 只已全部有行情，" +
        "回放窗口内的 as-of 过滤是真实生效的（不再是把退市股整批删掉）。";
    }
  }
}

async function hubListSync(btn) {
  const work = async () => {
    const r = await post("/api/listing/sync", { force: true });
    if (!r || !r.ok) { toast("同步失败：" + ((r && r.error) || "未知错误"), "err"); return; }
    toast(r.message || "已开始同步上市名单", "ok");
    // 后端跑在后台线程里：轮询到 running 落下就收工，最多等 18 秒
    for (let i = 0; i < 12; i++) {
      await new Promise((res) => setTimeout(res, 1500));
      const s = await api("/api/listing");
      if (!s || !s.ok) continue;
      hubListingUi(s);
      if (!(s.sync || {}).running && s.total) break;
    }
  };
  return btn ? withBusy(btn, work) : work();
}

async function hubListPrewarm(btn) {
  const work = async () => {
    const r = await post("/api/listing/prewarm", { limit: 0 });
    if (!r || !r.ok) { toast("补全失败：" + ((r && r.error) || "未知错误"), "err"); return; }
    toast(r.message || "已开始补退市股行情（每只约 1 秒，可随时停）", "ok");
    // 补全可能跑十几分钟：只在前 60 秒内跟进度，之后让用户自己回来看
    for (let i = 0; i < 20; i++) {
      await new Promise((res) => setTimeout(res, 3000));
      const s = await api("/api/listing");
      if (!s || !s.ok) continue;
      hubListingUi(s);
      if (!(s.prewarm || {}).running) break;
    }
  };
  return btn ? withBusy(btn, work) : work();
}

/* ---------------- 数据中枢：资金流（P2-6） ---------------- */
function hubCapitalUi(d) {
  d = d || {};
  const st = d.state || {};
  const src = d.sources || [];
  const stale = src.some((x) => x.stale);
  const pill = $("#hubCapPill");
  if (pill) {
    pill.textContent = st.running ? "同步中"
      : (!d.total ? "无数据" : (stale ? "部分落后" : "已同步"));
    pill.className = "pill " + (st.running ? "live" : (d.total && !stale ? "yes" : "no"));
  }
  const kv = $("#hubCapKv");
  if (kv) {
    const rows = src.map((x) => {
      let v;
      if (x.off) v = '<span class="dim">' + esc(x.note || "不可用") + "</span>";
      else if (!x.rows) v = '<span class="dim">无数据</span>';
      else v = (x.rows || 0).toLocaleString("zh-CN") + " 行 · 最新 " + esc(x.last || "—") +
        (x.stale ? ' <span class="pill no">落后 ' + (x.lag || 1) + " 日</span>"
          : (x.allow ? ' <span class="dim">T+1 发布</span>' : ""));
      return '<div class="k">' + esc(x.name) + '</div><div class="v">' + v + "</div>";
    });
    rows.push('<div class="k">覆盖区间</div><div class="v">' +
      (d.total ? (esc(d.start || "—") + " ~ " + esc(d.last || "—")) : "—") + "</div>");
    rows.push('<div class="k">同步进度</div><div class="v">' + (st.running
      ? ((st.done || 0) + " / " + (st.total || 0) + " 日 · " + (st.rows || 0) + " 行")
      : (st.message || "未运行")) + "</div>");
    kv.innerHTML = rows.join("");
  }
  hubCheck("hubCapModel", d.in_model);
  const note = $("#hubCapNote");
  if (note) {
    if (st.error) note.textContent = "上次同步出错：" + st.error;
    else if (!d.total) note.textContent = "还没有资金流数据：点「同步近 25 日」拉一次（约 40 秒 / 25 个交易日）。";
    else note.textContent = "因子：" + ((d.factors || []).map((x) => x.name).join("、") || "—") +
      "。这些因子默认不进模型 —— 先在「因子体检」页看它们的 IC / ICIR 与换手，" +
      "证明有增量信息再打开上面的开关。北向持股自 2024-08 起交易所停止披露，字段保留但取不到数。";
  }
}

async function hubCapSync(btn) {
  const work = async () => {
    const r = await post("/api/capital", { action: "sync", days: 25 });
    if (!r || !r.ok) { toast("同步失败：" + ((r && r.error) || "未知错误"), "err"); return; }
    toast(r.message || "已开始同步资金流（近 25 个交易日）", "ok");
    for (let i = 0; i < 20; i++) {
      await new Promise((res) => setTimeout(res, 3000));
      const s = await api("/api/capital");
      if (!s || !s.ok) continue;
      hubCapitalUi(s);
      if (!(s.state || {}).running) break;
    }
  };
  return btn ? withBusy(btn, work) : work();
}

async function hubCapModelSet() {
  const el = $("#hubCapModel");
  if (!el) return;
  const r = await post("/api/capital", { action: "config", config: { in_model: !!el.checked } });
  if (!r || !r.ok) { toast((r && r.error) || "保存失败", "err"); return; }
  toast(el.checked ? "已允许资金流因子进入模型（记得重训一次）" : "已关闭：资金流因子仅作体检对照", "ok");
}

/* ---------------- 数据中枢：空头辩护 ---------------- */
function hubBearPaint(s) {
  s = s || {};
  const pill = $("#hubBearPill");
  const on = !!s.bear_enabled;
  if (pill) {
    pill.textContent = on ? "已启用" : "未启用";
    pill.className = "pill " + (on ? "yes" : "no");
  }
  hubCheck("hubBearEnabled", on);
  hubSet("hubBearModel", s.bear_model || "");
  hubSet("hubJudgeModel", s.judge_model || "");
  hubSet("hubBearMin", s.bear_min || 3);
  const note = $("#hubBearNote");
  if (note) {
    note.textContent = !s.configured
      ? "还没配 LLM：先在上面填端点 / 模型 / Key，否则开启后也会退回本地、不做否决。"
      : (on
        ? ("辩护走 " + (s.bear_model || s.model || "主模型") + "，裁决走 " +
           (s.judge_model || s.model || "主模型") + "。首次上线前请先跑一次「选股记录 → 离线 A/B」。")
        : "关闭中：候选榜不会出现否决理由，也不会有票被 drop。");
  }
}

async function hubBearSave(btn) {
  const work = async () => {
    const body = {
      action: "configure",
      bear_enabled: !!($("#hubBearEnabled") && $("#hubBearEnabled").checked),
      bear_model: ($("#hubBearModel") && $("#hubBearModel").value.trim()) || "",
      judge_model: ($("#hubJudgeModel") && $("#hubJudgeModel").value.trim()) || "",
      bear_min: Number($("#hubBearMin") && $("#hubBearMin").value) || 3
    };
    const r = await post("/api/screen/llm", body);
    if (!r || !r.ok) { toast("保存失败：" + ((r && r.error) || "未知错误"), "err"); return; }
    hubBearPaint(r);
    bearPaintPill();
    toast(body.bear_enabled ? "空头辩护已开启：下次手动选股后会跑" : "空头辩护已关闭", "ok");
  };
  return btn ? withBusy(btn, work) : work();
}

/* ---------------- 因子体检（P0-3） ---------------- */
const FC = { data: null, sel: "", busy: false };
const FC_UP = "#d3403f", FC_DOWN = "#12996a";   // 涨红跌绿
const FC_AXIS = { color: "#8a939f", fontSize: 10 };

function fcRow(key) {
  const rows = (FC.data && FC.data.rows) || [];
  return rows.find((r) => r.key === key) || null;
}

function fcFmt(v, d) {
  if (v == null || v !== v) return '<span class="dim">—</span>';
  return Number(v).toFixed(d == null ? 3 : d);
}
function fcSigned(v, d) {
  if (v == null || v !== v) return '<span class="dim">—</span>';
  const n = Number(v);
  return '<span class="' + signCls(n) + '">' + (n > 0 ? "+" : "") + n.toFixed(d == null ? 2 : d) + "</span>";
}

/* 十分位迷你柱：一行 10 根，跨零线画，正红负绿。表格里直接看出单调性。 */
function fcSpark(dec) {
  if (!dec || !dec.length) return '<span class="dim">—</span>';
  const max = Math.max.apply(null, dec.map((v) => Math.abs(Number(v) || 0))) || 1e-6;
  const w = 72 / dec.length;
  const bars = dec.map((v, i) => {
    const val = Number(v) || 0;
    const h = Math.max(1.2, Math.abs(val) / max * 8);
    const y = val >= 0 ? 9 - h : 9;
    return '<rect x="' + (i * w + 0.5).toFixed(2) + '" y="' + y.toFixed(2) +
      '" width="' + (w - 1).toFixed(2) + '" height="' + h.toFixed(2) +
      '" fill="' + (val >= 0 ? FC_UP : FC_DOWN) + '"/>';
  }).join("");
  return '<svg class="fc-spark" viewBox="0 0 72 18" width="72" height="18" aria-hidden="true">' +
    '<line x1="0" y1="9" x2="72" y2="9" stroke="currentColor" stroke-opacity=".16"/>' + bars + "</svg>";
}

async function fcLoad(force) {
  if (FC.busy) return;
  FC.busy = true;
  const days = (Number(($("#fcDays") || {}).value) || 120);
  const hz = (Number(($("#fcHorizon") || {}).value) || 5);
  const sum = $("#fcSum");
  if (sum) sum.textContent = "正在逐期计算…首次约 15~45 秒（300 只 × " + days +
    " 期 × 24 因子，含资金流因子）；参数没改会直接读缓存。";
  sysTaskStart("factors", "单因子体检");
  let r;
  try { r = await api("/api/factors?days=" + days + "&horizon=" + hz + "&limit=300&capital=1" + (force ? "&force=1" : "")); }
  catch (e) { r = { ok: false, error: String(e) }; }
  finally { sysTaskEnd("factors"); FC.busy = false; }
  if (!r || !r.ok) {
    if (sum) sum.textContent = "计算失败：" + ((r && r.error) || "未知错误");
    toast("因子体检失败：" + ((r && r.error) || "未知错误"), "err");
    return;
  }
  FC.data = r;
  if (!FC.sel || !fcRow(FC.sel)) {
    // 默认选 ICIR 最高、且覆盖够的因子：进来就有东西看，不用先学会怎么点
    const rows = r.rows || [];
    const ok = rows.filter((x) => x.icir != null && (x.cover || 0) > 0.5 && !x.external);
    FC.sel = (ok[0] || rows[0] || {}).key || "";
  }
  fcRender(r);
}

function fcRender(r) {
  const pill = $("#fcPill");
  if (pill) {
    pill.textContent = r.cached ? "缓存" : "已计算";
    pill.className = "pill " + (r.cached ? "yes" : "live");
  }
  const sum = $("#fcSum");
  if (sum) {
    sum.innerHTML = "<b>" + r.periods + "</b> 期 · <b>" + r.pool + "</b> 只样本 · " +
      esc(r.date_from) + " ~ " + esc(r.date_to) + " · 持有 <b>" + r.horizon + "</b> 日 · " +
      "基准池 " + r.bench_n + " 只 · 因子版本 v" + r.factor_version +
      (r.cached ? ' · <span class="dim">来自磁盘缓存（参数相同即复用），换参数或点「重新计算」才重跑</span>' : "");
  }
  const body = $("#fcBody");
  if (body) {
    body.innerHTML = (r.rows || []).map((x) => {
      const on = x.key === FC.sel;
      const tBad = x.t != null && Math.abs(Number(x.t)) >= 2;
      const acBad = x.autocorr != null && Number(x.autocorr) < 0.5;
      const tvBad = x.turnover != null && Number(x.turnover) > 0.5;
      return '<tr class="fc-row' + (on ? " on" : "") + '" data-fc="' + esc(x.key) + '">' +
        '<td><b>' + esc(x.name) + '</b>' +
          (x.external ? ' <span class="pill no">外部</span>' : "") +
          ((x.n || 0) < 30 || (x.cover || 0) < 0.3 ? ' <span class="pill off" title="期数或覆盖率不足，IC 再高也不可信">样本少</span>' : "") +
          '<i class="fc-k">' + esc(x.key) + "</i></td>" +
        '<td class="fc-g">' + esc(x.group || "—") + "</td>" +
        '<td class="' + signCls(Number(x.ic) || 0) + '">' + fcFmt(x.ic, 3) + "</td>" +
        "<td>" + fcFmt(x.icir, 3) + "</td>" +
        '<td class="' + (tBad ? "fc-hl" : "") + '" title="|t|>2 才算统计显著">' + fcFmt(x.t, 2) + "</td>" +
        "<td>" + (x.pos == null ? '<span class="dim">—</span>' : Number(x.pos).toFixed(0) + "%") + "</td>" +
        '<td class="' + signCls(Number(x.ls) || 0) + '">' + fcSigned(x.ls, 2) + "</td>" +
        '<td class="' + (tvBad ? "fc-hl" : "") + '">' + (x.turnover == null ? '<span class="dim">—</span>'
          : (Number(x.turnover) * 100).toFixed(0) + "%") + "</td>" +
        '<td class="' + (acBad ? "fc-hl" : "") + '">' + fcFmt(x.autocorr, 2) + "</td>" +
        "<td>" + (x.cover == null ? '<span class="dim">—</span>' : (Number(x.cover) * 100).toFixed(0) + "%") + "</td>" +
        "<td>" + fcSpark(x.deciles) + "</td></tr>";
    }).join("");
  }
  const note = $("#fcNote");
  if (note) note.textContent = r.note || "—";
  fcDrawAll();
}

function fcDrawAll() {
  fcDrawDec();
  fcDrawIc();
  fcDrawCorr();
  // 数据是 fetch 回来的，画的时候面板宽度可能还没定下来；下一帧再对一次尺寸
  requestAnimationFrame(() => {
    ["fcDecile", "fcIc", "fcCorr"].forEach((id) => {
      if (S.charts[id]) { try { S.charts[id].resize(); } catch (e) {} }
    });
  });
}

function fcPick(key) {
  FC.sel = key;
  $$("#fcBody .fc-row").forEach((tr) => tr.classList.toggle("on", tr.dataset.fc === key));
  fcDrawDec();
  fcDrawIc();
}

function fcDrawDec() {
  const row = fcRow(FC.sel);
  const lab = $("#fcDecName");
  if (lab) lab.textContent = row ? (row.name + "（" + row.key + "）") : "点表格里的因子切换";
  const c = chart("fcDecile");
  if (!row || !(row.deciles || []).length) { c.clear(); return; }
  c.setOption({
    tooltip: { trigger: "axis",
      formatter: (p) => p[0].axisValue + "：" + (p[0].value == null ? "—" : Number(p[0].value).toFixed(2) + "%") },
    grid: { left: 48, right: 14, top: 18, bottom: 26 },
    xAxis: { type: "category", data: row.deciles.map((_, i) => "D" + (i + 1)),
      axisLabel: FC_AXIS, axisTick: { show: false } },
    yAxis: { type: "value", name: "%", scale: true, nameTextStyle: FC_AXIS,
      axisLabel: FC_AXIS, splitLine: { lineStyle: { color: "#eef1f5" } } },
    series: [{
      type: "bar", barMaxWidth: 30,
      data: row.deciles,
      itemStyle: { color: (p) => (Number(p.value) >= 0 ? FC_UP : FC_DOWN) },
      label: { show: true, position: "top", color: "#525c6b", fontSize: 9,
        formatter: (p) => (p.value == null ? "" : Number(p.value).toFixed(1)) }
    }]
  }, true);
}

function fcDrawIc() {
  const row = fcRow(FC.sel);
  const c = chart("fcIc");
  const s = (row && row.series) || [];
  if (!s.length) { c.clear(); return; }
  const acc = [];
  let run = 0;
  s.forEach((x) => { run += Number(x.v) || 0; acc.push(run); });
  c.setOption({
    tooltip: { trigger: "axis",
      formatter: (p) => p[0].axisValue + "<br/>RankIC " + Number(p[0].value || 0).toFixed(3) +
        (p[1] ? "<br/>累计 " + Number(p[1].value || 0).toFixed(3) : "") },
    legend: { data: ["RankIC", "累计"], top: 0, itemWidth: 12, itemHeight: 6,
      textStyle: { color: "#525c6b", fontSize: 10 } },
    grid: { left: 46, right: 44, top: 26, bottom: 24 },
    xAxis: { type: "category", data: s.map((x) => String(x.d).slice(5)),
      axisLabel: Object.assign({}, FC_AXIS, { interval: Math.max(0, Math.floor(s.length / 8)) }) },
    yAxis: [
      { type: "value", name: "IC", scale: true, nameTextStyle: FC_AXIS, axisLabel: FC_AXIS,
        splitLine: { lineStyle: { color: "#eef1f5" } } },
      { type: "value", name: "累计", scale: true, nameTextStyle: FC_AXIS, axisLabel: FC_AXIS,
        splitLine: { show: false } }
    ],
    series: [
      { name: "RankIC", type: "bar", barMaxWidth: 6,
        data: s.map((x) => Number(x.v)),
        itemStyle: { color: (p) => (Number(p.value) >= 0 ? FC_UP : FC_DOWN) } },
      { name: "累计", type: "line", yAxisIndex: 1, data: acc, showSymbol: false,
        lineStyle: { width: 1.6, color: "#1c5fd8" } }
    ]
  }, true);
}

function fcDrawCorr() {
  const d = FC.data;
  const c = chart("fcCorr");
  if (!d || !d.corr || !(d.corr.matrix || []).length) { c.clear(); return; }
  const keys = d.corr.keys || [];
  const nm = {};
  (d.rows || []).forEach((r) => { nm[r.key] = r.name || r.key; });
  // 因子超过 16 个时用短键名做轴标签：24 个中文名斜排根本读不出来，
  // 全名放 tooltip 和表格里就够了。
  const short = keys.length > 16;
  const labels = keys.map((k) => (short ? k : (nm[k] || k)));
  const data = [];
  (d.corr.matrix || []).forEach((row, i) => {
    (row || []).forEach((v, j) => { if (v != null) data.push([j, i, Number(v)]); });
  });
  const n = keys.length;
  const fs = n > 18 ? 9 : 10;
  c.setOption({
    tooltip: { formatter: (p) => (nm[keys[p.data[1]]] || keys[p.data[1]]) + " × " +
      (nm[keys[p.data[0]]] || keys[p.data[0]]) + "：" + Number(p.data[2]).toFixed(2) },
    grid: { left: 74, right: 24, top: 14, bottom: 84 },
    xAxis: { type: "category", data: labels, splitArea: { show: true },
      axisLabel: { color: FC_AXIS.color, fontSize: fs, rotate: 42, interval: 0 } },
    yAxis: { type: "category", data: labels, splitArea: { show: true },
      axisLabel: { color: FC_AXIS.color, fontSize: fs, interval: 0 } },
    visualMap: { min: -1, max: 1, calculable: true, orient: "horizontal",
      left: "center", bottom: 6, itemWidth: 12, itemHeight: 110, precision: 2,
      inRange: { color: [FC_DOWN, "#f2f6f4", "#ffffff", "#fbe9e8", FC_UP] },
      textStyle: { color: FC_AXIS.color, fontSize: 10 } },
    series: [{ type: "heatmap", data: data,
      itemStyle: { borderColor: "#fff", borderWidth: 1 },
      emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(0,0,0,.2)" } } }]
  }, true);
  c.resize();   // 面板可能刚显示出来，容器宽度是渲染那一刻才准的
}

/* ---------------- 目标持仓（P1-4） ---------------- */
const TG = { data: null };

function tgCfg() {
  const maxW = (Number(($("#tgMaxW") || {}).value) || 5) / 100;
  const indCap = (Number(($("#tgIndCap") || {}).value) || 30) / 100;
  if (!(maxW > 0 && maxW < 1)) return null;
  return {
    mode: ($("#tgMode") || {}).value || "equal",
    max_w: maxW,
    ind_cap: indCap > 0 ? indCap : 1,
    turnover_cap: (Number(($("#tgTurn") || {}).value) || 0) / 100,
    risk_pct: (Number(($("#tgRisk") || {}).value) || 1) / 100,
    atr_mult: Number(($("#tgAtr") || {}).value) || 2,
    topk: Number(($("#tgTopk") || {}).value) || 0,
    n_drop: Number(($("#tgDrop") || {}).value) || 0
  };
}

async function tgFill() {
  const r = await api("/api/portfolio");
  if (!r || !r.ok) return;
  const c = r.config || {};
  if ($("#tgMode")) $("#tgMode").value = c.mode || "equal";
  hubSet("tgMaxW", Math.round((c.max_w || 0.05) * 100));
  hubSet("tgIndCap", Math.round((c.ind_cap || 0.3) * 100));
  hubSet("tgTurn", Math.round((c.turnover_cap || 0.6) * 100));
  hubSet("tgRisk", ((c.risk_pct || 0.01) * 100).toFixed(1));
  hubSet("tgAtr", c.atr_mult || 2);
  hubSet("tgTopk", c.topk || 0);
  hubSet("tgDrop", c.n_drop || 0);
  const cur = r.current || {};
  // 账本里只有几千块时不拿它当默认值：A 股一手 100 股，几千块连一只都买不满，
  // 生成出来会是一屏「无法建仓」，看着像坏了。留空让用户自己填计划投入金额。
  const usable = Number(cur.total || 0) >= 20000;
  if (usable) hubSet("tgCapital", Math.round(cur.total));
  const sum = $("#tgSum");
  if (sum && !TG.data) {
    if (usable) {
      sum.textContent = "账本最近持仓日 " + cur.day + " · " + (cur.positions || 0) +
        " 只 · 总资产 " + fmtMoney(cur.total) + "。正在按最近一次选股生成目标持仓…";
      // 一进来就是空的没意义：直接按「最近一次落盘候选」算一遍，不弹提示
      tgRun(null, false, true);
    } else {
      sum.innerHTML = "<b>先把「总资产」填成你的计划投入金额。</b>" +
        (cur.day ? "账本里最近一笔是 " + esc(cur.day) + "，总资产只有 " + fmtMoney(cur.total) + " 元——" +
          "按一手 100 股算，这个规模连一只 20 元的票都买不满 5%。" : "") +
        "填好之后点「生成目标持仓」，右边会给出每只买多少股、卖多少股和大概的费用。";
    }
  }
}

async function tgRun(btn, useScreen, silent) {
  const work = async () => {
    const cfg = tgCfg();
    if (!cfg) { toast("单票上限要在 1%~99% 之间", "err"); return; }
    const picks = (useScreen && SCR.candidates.length) ? SCR.candidates : null;
    if (!silent) {
      sysTaskStart("portfolio", "计算目标持仓");
      // 让实盘体检也使用同一份风险预算参数；保存失败不阻断生成
      try { await post("/api/portfolio", { action: "config", config: cfg }); } catch (e) {}
    }
    let r;
    try {
      r = await post("/api/portfolio", {
        capital: Number(($("#tgCapital") || {}).value) || 0,
        config: cfg, picks: picks
      });
    } finally { if (!silent) sysTaskEnd("portfolio"); }
    if (!r || !r.ok) {
      if (!silent) toast("生成失败：" + ((r && r.error) || "未知错误"), "err");
      const sum = $("#tgSum");
      if (sum) sum.textContent = "生成失败：" + ((r && r.error) || "未知错误") +
        "（多半是没有候选：先在选股工作台跑一次）";
      return;
    }
    TG.data = r;
    tgRender(r);
    // 口径记下来：下次进来表单还是这套参数，不用每次重填
    post("/api/portfolio", { action: "config", config: cfg });
    if (!silent) {
      toast("目标持仓：" + r.n_target + " 只 · 换手 " + r.turnover + "% · 费用 " +
        fmtMoney(r.fee) + "（" + r.fee_bps + " bp）", "ok");
    }
  };
  return btn ? withBusy(btn, work) : work();
}

function tgRender(r) {
  const sum = $("#tgSum");
  if (sum) {
    const bits = [
      "权重 <b>" + esc(r.mode_label || r.mode || "—") + "</b>",
      "总资产 <b>" + fmtMoney(r.capital) + "</b>",
      "目标 <b>" + r.n_target + "</b> 只",
      "买入 <b>" + fmtMoney(r.buy_amount) + "</b>",
      "卖出 <b>" + fmtMoney(r.sell_amount) + "</b>",
      "费用 <b>" + fmtMoney(r.fee) + "</b>（" + r.fee_bps + " bp）",
      "换手 <b>" + r.turnover + "%</b>",
      "剩余现金 <b>" + fmtMoney(r.cash_left) + "</b>（" + r.cash_pct + "%）"
    ];
    let extra = "";
    // Kelly 回退必须显式说明：否则选了「分数 Kelly」却按等权算，页面看不出差别
    if (r.kelly && r.kelly.available === false) {
      extra = '<p class="note" style="margin:6px 0 0;color:var(--amber)">Kelly 未启用：' +
        esc(r.kelly.reason || "缺少胜率/盈亏比") + "，本次已按等权计算。</p>";
    } else if (r.kelly && r.kelly.available) {
      extra = '<p class="note" style="margin:6px 0 0">Kelly 上限 <b>' +
        num(r.kelly.f * 100, 1) + "%</b>（胜率 " + num(r.kelly.win_rate, 1) +
        "% / 盈亏比 " + num(r.kelly.pl_ratio, 2) + " · 来源 " +
        esc(r.kelly.source || "—") + "）</p>";
    }
    if (r.turnover_trimmed) {
      extra += '<p class="note" style="margin:6px 0 0;color:var(--amber)">换手上限生效：原始换手 ' +
        r.turnover_before_cap + "% 超上限，已按比例向当前持仓收缩（老仓不会被一次性清空）。</p>";
    }
    if (r.max_w_hit) {
      extra += '<p class="note" style="margin:4px 0 0">有 ' + r.max_w_hit +
        " 只触及单票上限 " + Math.round(r.max_w * 100) + "%，超出的权重已回补给其他票。</p>";
    }
    if (r.binding_note) {
      extra += '<p class="note" style="margin:4px 0 0">' + esc(r.binding_note) + "</p>";
    }
    if (r.unbuyable) {
      extra += '<p class="note" style="margin:4px 0 0;color:var(--amber)">有 ' + r.unbuyable +
        " 只的目标金额买不到一手（高价股 × 单票上限 × 总资产三者撞上），已单列在表末。" +
        "要么加大总资产，要么把单票上限调高。</p>";
    }
    const cref = r.current && r.current.day
      ? '<span class="dim"> · 当前持仓取自账本 ' + esc(r.current.day) + "（" + r.current.positions + " 只）</span>"
      : '<span class="dim"> · 没有账本持仓，差额单＝全部买入</span>';
    sum.innerHTML = bits.join(" · ") + cref + extra;
  }

  const fill = (id, empty, list, kind) => {
    const body = $("#" + id);
    if (!body) return;
    body.innerHTML = (list || []).map((x) => {
      // 差额单里股数在 delta 上（卖出是负数），不是 shares/qty —— 这两个字段只有
      // 账本持仓才有，直接读会全是 0。
      const sh = Math.abs(Number(x.delta) || 0) || Number(x.shares || x.qty) || 0;
      const nm = kind === "sell"
        ? '<span class="' + (x.note && x.note.indexOf("T+1") >= 0 ? "fc-hl" : "") + '">' +
          esc(x.note || "可卖") + "</span>"
        : '<span class="dim">—</span>';
      return "<tr><td>" + esc(x.code) + "</td><td>" + esc(x.name || "") + "</td>" +
        '<td class="ta-r">' + sh.toLocaleString("zh-CN") + "</td>" +
        '<td class="ta-r">' + num(x.price, 2) + "</td>" +
        '<td class="ta-r">' + fmtMoney(x.amount || 0) + "</td>" +
        (kind === "sell" ? "<td>" + nm + "</td>" : "") + "</tr>";
    }).join("");
    const emp = $("#" + empty);
    if (emp) emp.style.display = (list || []).length ? "none" : "block";
  };
  fill("tgBuyBody", "tgBuyEmpty", r.buys, "buy");
  fill("tgSellBody", "tgSellEmpty", r.sells, "sell");
  if ($("#tgBuySum")) $("#tgBuySum").textContent = (r.buys || []).length + " 笔 · " + fmtMoney(r.buy_amount);
  if ($("#tgSellSum")) $("#tgSellSum").textContent = (r.sells || []).length + " 笔 · " + fmtMoney(r.sell_amount);

  const body = $("#tgBody");
  if (body) {
    body.innerHTML = (r.rows || []).map((x, i) => {
      const cls = x.action === "买入" ? "buy" : (x.action === "卖出" ? "sell" : "");
      return "<tr><td>" + (i + 1) + "</td><td>" + esc(x.code) + "</td><td>" + esc(x.name || "") + "</td>" +
        '<td class="fc-g">' + esc(x.industry || "—") + "</td>" +
        '<td class="ta-r">' + num(x.weight, 2) + "%</td>" +
        '<td class="ta-r">' + (x.atr_pct == null ? "—" : num(x.atr_pct, 2) + "%") + "</td>" +
        '<td class="ta-r">' + (x.risk_amt == null ? "—" : fmtMoney(x.risk_amt)) + "</td>" +
        '<td class="ta-r">' + (x.cur_qty || 0).toLocaleString("zh-CN") + "</td>" +
        '<td class="ta-r">' + (x.target_qty || 0).toLocaleString("zh-CN") + "</td>" +
        '<td class="' + cls + '">' + esc(x.action || "") + "</td>" +
        '<td class="fc-g">' + esc(x.note || "") + "</td></tr>";
    }).join("");
    const emp = $("#tgEmpty");
    if (emp) emp.style.display = (r.rows || []).length ? "none" : "block";
  }

  const ind = $("#tgIndBody");
  if (ind) {
    // 目标金额为 0 的行业（一手都买不起）不画：一条 0% 的空条只会占地方
    const rows = (r.industries || []).filter((x) => Number(x.weight) > 0);
    const max = Math.max.apply(null, rows.map((x) => Number(x.weight) || 0)) || 1;
    ind.innerHTML = rows.map((x) => {
      const over = r.ind_cap && x.weight / 100 > r.ind_cap + 0.005;
      return '<div class="tg-ind-row" title="' + esc(x.industry) + " · " + fmtMoney(x.amount) + '">' +
        '<span class="tg-ind-k">' + esc(x.industry) + "</span>" +
        '<span class="tg-ind-bar"><i style="width:' + (Number(x.weight) / max * 100).toFixed(1) + "%" +
        (over ? ";background:var(--amber)" : "") + '"></i></span>' +
        '<b class="' + (over ? "fc-hl" : "") + '">' + num(x.weight, 1) + "%</b></div>";
    }).join("") || '<p class="hint">暂无</p>';
  }
}

/* ---------------- 选股记录：归因 / 幸存者 / 漏斗 / A-B ---------------- */
const PKR = { at: 0, data: null, busy: false };

async function pkReviewData(force) {
  if (!force && PKR.data && Date.now() - PKR.at < 600000) return PKR.data;
  if (PKR.busy) return null;
  PKR.busy = true;
  try {
    const r = await api("/api/screen/review?days=60&step=5&topn=10");
    if (r && r.ok) { PKR.data = r; PKR.at = Date.now(); }
    return r;
  } finally { PKR.busy = false; }
}

/* 归因与幸存者偏差都来自同一份 /api/screen/review，一次请求喂两张卡。
   服务端有 15 分钟缓存，但前端也在 10 分钟内复用，避免来回切页反复请求。 */
async function pkAttrLoad(btn) {
  const work = async () => {
    const pill = $("#pkAttrPill");
    if (pill) { pill.textContent = "计算中"; pill.className = "pill off"; }
    const box = $("#pkAttr");
    if (box) box.innerHTML = '<p class="hint">正在重放历史各期…首次约 6~10 秒。</p>';
    const r = await pkReviewData(!!btn);
    if (!r) { if (box) box.innerHTML = '<p class="hint">正在等待上一次请求返回…</p>'; return; }
    pkAttrRender(r);
    pkSurvRender(r);
    if (btn) toast(r.ok ? "归因已更新" : ("归因失败：" + (r.error || "未知错误")),
      r.ok ? "ok" : "err");
  };
  return btn ? withBusy(btn, work) : work();
}

async function pkAttrAuto() {
  if (PKR.data) { pkAttrRender(PKR.data); pkSurvRender(PKR.data); return; }
  const r = await pkReviewData(false);
  if (r && r.ok) { pkAttrRender(r); pkSurvRender(r); }
}

function pkAttrRender(r) {
  const box = $("#pkAttr");
  const pill = $("#pkAttrPill");
  if (!box) return;
  if (!r || !r.ok) {
    box.innerHTML = '<p class="hint">归因失败：' + esc((r && r.error) || "未知错误") + "</p>";
    if (pill) { pill.textContent = "失败"; pill.className = "pill no"; }
    return;
  }
  const attr = r.attribution || {};
  const dims = Object.keys(attr);
  if (pill) {
    pill.textContent = "T+" + (r.attribution_h || "—") + " · " + dims.length + " 维";
    pill.className = "pill " + (dims.length ? "yes" : "no");
  }
  if (!dims.length) {
    box.innerHTML = '<p class="hint">没有可归因的样本：先跑几期选股记录，样本够了才切得出分组。</p>';
    return;
  }
  box.innerHTML = dims.map((k) => {
    const d = attr[k] || {};
    const rows = d.rows || [];
    const worst = rows.reduce((a, b) => (Number(b.avg_excess) < Number(a.avg_excess) ? b : a), rows[0] || {});
    const best = rows.reduce((a, b) => (Number(b.avg_excess) > Number(a.avg_excess) ? b : a), rows[0] || {});
    return '<div class="attr-dim">' +
      '<div class="attr-h"><b>' + esc(d.label || k) + "</b>" +
      (rows.length > 1
        ? '<span class="attr-tip">最好 <i class="pos">' + esc(best.key) + " " + fcSigned(best.avg_excess, 2) +
          '</i> · 最差 <i class="neg">' + esc(worst.key) + " " + fcSigned(worst.avg_excess, 2) + "</i></span>"
        : "") + "</div>" +
      '<table class="attr-t"><thead><tr><th>分组</th><th class="ta-r">样本</th>' +
        '<th class="ta-r">收益</th><th class="ta-r">超额</th><th class="ta-r">胜率</th></tr></thead><tbody>' +
      rows.map((x) =>
        "<tr><td>" + esc(x.key) + "</td>" +
        '<td class="ta-r">' + x.n + "</td>" +
        '<td class="ta-r">' + fcSigned(x.avg_ret, 2) + "</td>" +
        '<td class="ta-r">' + fcSigned(x.avg_excess, 2) + "</td>" +
        '<td class="ta-r">' + num(x.win, 1) + "%</td></tr>").join("") +
      "</tbody></table></div>";
  }).join("");
}

function pkSurvRender(r) {
  const box = $("#pkSurv");
  if (!box) return;
  if (!r || !r.ok) { box.innerHTML = "检查失败：" + esc((r && r.error) || "未知错误"); return; }
  const s = r.survivorship || {};
  const pct = (v) => (v == null ? "—" : (Number(v) * 100).toFixed(1) + "%");
  // 判定只看回放窗口（近两年）：早期退市股接口已查无数据，拿全历史覆盖率当标准
  // 会永远显示「可能虚高」，那个结论没人能改，等于没有结论。
  const rc = (s.recent_cached != null && s.recent_2y)
    ? s.recent_cached / s.recent_2y
    : (s.cover == null ? null : Number(s.cover));
  const bad = rc != null && rc < 0.9;
  const kv = (k, v) => '<div class="k">' + k + '</div><div class="v">' + v + "</div>";
  box.className = "scr-kv surv-kv";
  box.innerHTML = [
    kv("重放样本", (s.pool || 0) + " 只"),
    kv("其中已退市", (s.delisted_in_pool || 0) + " 只"),
    kv("近两年退市有行情", s.recent_2y
      ? ("<b>" + (s.recent_cached || 0) + " / " + s.recent_2y + "</b>（" + pct(rc) + "）")
      : "—"),
    kv("全历史退市覆盖", (s.delisted_cached || 0) + " / " + (s.delisted_total || 0) +
      "（" + pct(s.cover) + "）"),
    kv("窗口未走完", (s.pending_bars || 0) + " 只（记 pending，不计入收益）"),
    kv("结论", bad
      ? '<span class="neg">回放窗口内仍有退市股缺行情，超额可能偏高</span>'
      : '<span class="pos">回放窗口内的退市股已入样，幸存者偏差已堵</span>')
  ].join("") +
    '<p class="note" style="grid-column:1/-1;margin:8px 0 0">' +
    "判定口径：按「那天真实在市」的名单（ipo_date ≤ date &lt; out_date）取样本；" +
    "退市股用末根 bar 结算并留在池里。全历史覆盖不到 100% 是因为更早退市的个股接口已查无数据，" +
    "这部分漏损只能承认，不能假装没有——但它不影响当前回放窗口的结论，所以按近两年覆盖率判定。</p>";
}

async function pkFunnelRun(btn) {
  const work = async () => {
    const box = $("#pkFunnel");
    if (box) box.innerHTML = "正在重放各层淘汰组…约 10~20 秒。";
    let r;
    try { r = await api("/api/review/funnel?days=60&step=5&limit=300"); }
    catch (e) { r = { ok: false, error: String(e) }; }
    if (!r || !r.ok) {
      if (box) box.innerHTML = "计算失败：" + esc((r && r.error) || "未知错误");
      toast("漏斗归因失败：" + ((r && r.error) || "未知错误"), "err");
      return;
    }
    pkFunnelRender(r);
  };
  return btn ? withBusy(btn, work) : work();
}

function pkFunnelRender(r) {
  const box = $("#pkFunnel");
  if (!box) return;
  const rows = r.rows || [];
  if (!rows.length) { box.innerHTML = "没有可归因的样本。"; return; }
  const hs = Array.from(new Set(rows.map((x) => x.h))).sort((a, b) => a - b);
  const h = hs[0];
  const sub = rows.filter((x) => x.h === h);
  const keep = sub.find((x) => x.stage.indexOf("保留") === 0);
  box.className = "scroll";
  box.innerHTML = '<table class="pool-table fc-table"><thead><tr>' +
    "<th>淘汰层</th><th class=\"ta-r\">样本</th><th class=\"ta-r\">平均收益</th>" +
    '<th class="ta-r">超额</th><th class="ta-r">胜率</th><th>判定</th></tr></thead><tbody>' +
    sub.map((x) => {
      const ex = Number(x.avg_excess);
      const beat = keep && x.stage !== keep.stage && ex > Number(keep.avg_excess);
      return "<tr><td>" + esc(x.stage) + "</td>" +
        '<td class="ta-r">' + x.n + "</td>" +
        '<td class="ta-r">' + fcSigned(x.avg_ret, 2) + "</td>" +
        '<td class="ta-r">' + fcSigned(x.avg_excess, 2) + "</td>" +
        '<td class="ta-r">' + num(x.win, 1) + "%</td>" +
        "<td>" + (beat ? '<span class="pill no">可能误杀</span>' : '<span class="dim">—</span>') + "</td></tr>";
    }).join("") + "</tbody></table>" +
    '<p class="note" style="margin:8px 0 0">T+' + h + " · " + r.periods + " 期 / 样本池 " +
    r.pool + " 只。" + esc(r.note || "") + "</p>";
}

async function pkAbRun(btn) {
  const work = async () => {
    const box = $("#pkAb");
    if (box) box.innerHTML = "正在统计被否决组与保留组的 T+N…";
    let r;
    try { r = await api("/api/review/llm-ab?limit=30"); }
    catch (e) { r = { ok: false, error: String(e) }; }
    if (!r || !r.ok) {
      if (box) box.innerHTML = "计算失败：" + esc((r && r.error) || "未知错误");
      toast("A/B 失败：" + ((r && r.error) || "未知错误"), "err");
      return;
    }
    pkAbRender(r, "pkAb");
  };
  return btn ? withBusy(btn, work) : work();
}

function pkAbRender(r, boxId) {
  const box = $("#" + boxId);
  if (!box) return;
  const sum = r.summary || [];
  if (!sum.length) {
    box.className = "hint";
    box.innerHTML = "还没有可比的批次：需要至少一批「开启空头辩护后落的选股记录」，且该批已到期。" +
      "开启方式见「数据中枢 → AI 服务 → 空头辩护」。";
    return;
  }
  box.className = "";
  box.innerHTML = sum.map((s) => {
    const ok = Number(s.delta) > 0;
    const cls = s.verdict.indexOf("有效") === 0 ? "yes"
      : (s.verdict.indexOf("无效") === 0 ? "no" : "off");
    return '<div class="ab-line"><span class="pill ' + cls + '">' + esc(s.verdict) + "</span>" +
      "<b>T+" + s.h + "</b> 保留 " + fcSigned(s.keep_ret, 2) +
      " / 否决 " + fcSigned(s.drop_ret, 2) +
      " · delta " + fcSigned(s.delta, 2) +
      " · 命中 " + num(s.hit, 0) + "%（" + s.batches + " 批）" +
      (ok ? "" : ' <span class="neg">否决在亏钱</span>') + "</div>";
  }).join("") + '<p class="note" style="margin:8px 0 0">' + esc(r.note || "") + "</p>";
}

/* ---------------- 选股工作台：空头辩护 ---------------- */

function bearPaintPill() {
  const pill = $("#bearPill");
  if (!pill) return;
  const d = BEAR.data || {};
  if (d.ok === false) { pill.textContent = "调用失败"; pill.className = "pill no"; return; }
  if (!d.mode) { pill.textContent = "未运行"; pill.className = "pill no"; return; }
  if (d.mode === "off") { pill.textContent = "未启用"; pill.className = "pill no"; return; }
  pill.textContent = d.mode === "llm"
    ? ("已裁决：留 " + d.kept + " / 否决 " + d.dropped)
    : "本地降级";
  pill.className = "pill " + (d.mode === "llm" ? (d.dropped ? "no" : "yes") : "off");
}

function bearRender(r) {
  const box = $("#bearBody");
  if (!box) return;
  // bearBody 现在是 <p class="note bear-inline">（原来是一整个卡片的容器）。
  // 所有分支都只写纯文本 / <span>，不能再塞 <p>，否则会生成嵌套的 <p> 被浏览器拆开。
  // 标签读的是 BEAR.data，渲染时顺手对齐：否则换条路径调用会出现
  // 「内容已经画出来、右上角还写未运行」的矛盾状态
  if (r) BEAR.data = r;
  box.className = "note bear-inline";
  if (!r) {
    box.innerHTML = '<span class="dim">未运行 · 到「数据中枢 → AI 服务」勾选空头辩护并填模型即可</span>';
    bearPaintPill();
    return;
  }
  if (r.ok === false) {
    box.className = "note bear-inline bad";
    box.innerHTML = "调用失败：" + esc(r.error || "未知错误");
    bearPaintPill();
    return;
  }
  if (r.mode === "off") {
    box.innerHTML = '<span class="dim">' + esc(r.message || r.note || "空头辩护未启用") + "</span>";
    bearPaintPill();
    return;
  }
  if (r.mode === "local") {
    box.className = "note bear-inline bad";
    box.innerHTML = esc(r.warning || "LLM 不可用，本批未做否决");
    bearPaintPill();
    return;
  }
  const rows = r.rows || [];
  const withBear = rows.filter((x) => (x.bears || []).length);
  const dropped = Number(r.dropped || 0);
  // 结论已经并进表格「辩护」列，这里只留一行状态：
  // 谁是辩护模型、谁裁决、否决了几只、有没有部分批次超时。
  const bits = [
    (r.restored ? "本批存档" : "刚完成"),
    "辩护 " + (r.model || "—"),
    "裁决 " + (r.judge_model || "—"),
    rows.length + " 只",
    "否决 " + dropped + " 只",
  ];
  box.className = "note bear-inline" + (r.warning ? " bad" : "");
  box.innerHTML = '<span class="dim">' + esc(bits.join(" · ")) + "</span>" +
    (r.warning ? " ⚠ " + esc(r.warning) : "") +
    ' <span class="dim">结论见表格「辩护」列，悬停看完整理由</span>';
  bearPaintPill();
  renderScrRows();      // 让「辩护」列立刻反映这一次的裁决
}

async function bearRun(btn) {
  if (BEAR.busy) return;
  const cand = SCR.candidates || [];
  if (!cand.length) { toast("先跑一次选股，再对候选做空头辩护", "info"); return; }
  BEAR.busy = true;
  const work = async () => {
    const box = $("#bearBody");
    if (box) box.innerHTML = '<p class="hint">正在为 ' + cand.length +
      " 只候选找否决理由…按 4 只一批调用，10 只约 40 秒；某批超时会自动拆半重试。</p>";
    sysTaskStart("bear", "空头辩护");
    let r;
    // 带上批次标识：后端会把这份辩护随该批候选一起存库，选股记录里能原样翻出来
    const rec = (SCR.last && SCR.last.record) || {};
    try {
      r = await post("/api/screen/bear", {
        candidates: cand, as_of: (SCR.last || {}).as_of || "", judge: true,
        batch: { as_of: (SCR.last || {}).as_of || "", recorded_at: rec.recorded_at || "" }
      });
    } finally { sysTaskEnd("bear"); BEAR.busy = false; }
    BEAR.data = r;
    bearRender(r);
    if (r && r.ok && r.mode === "llm") {
      toast("空头辩护完成：否决 " + (r.dropped || 0) + " 只" +
        (r.saved ? "，已随本期记录保存（重跑选股才生效）" : "（名单未自动修改，重跑选股才生效）"),
        r.dropped ? "info" : "ok");
    } else if (r && r.ok === false) {
      toast("辩护失败：" + (r.error || "未知错误"), "err");
    }
  };
  return btn ? withBusy(btn, work) : work();
}

/* 首屏静默出数不烧 token：把这一批已经存过的辩护取回来，候选榜与辩护成对出现，
   而不是刷新一次页面辩护就空了 */
async function bearRestore() {
  const last = SCR.last || {};
  const q = new URLSearchParams({ as_of: last.as_of || "" });
  const rec = (last.record || {}).recorded_at || "";
  if (rec) q.set("recorded_at", rec);
  if (!last.as_of) { if (!BEAR.data) bearRender(null); return; }
  let r;
  try { r = await api("/api/screen/bear?" + q.toString()); } catch (e) { r = null; }
  // 存的那份可能是另一个数据日的：和当前候选对不上就别硬贴上去
  const codes = new Set((SCR.candidates || []).map((x) => x.code));
  const hit = (r && r.rows || []).filter((x) => codes.has(x.code)).length;
  if (!r || !r.ok || !r.saved || (codes.size && !hit)) {
    if (!BEAR.data) bearRender(null);
    return;
  }
  bearRender({ ...r, restored: true });
}

async function bearAb(btn) {
  const work = async () => {
    const box = $("#bearAbBody");
    if (box) box.innerHTML = '<p class="hint">正在统计…</p>';
    let r;
    try { r = await api("/api/review/llm-ab?limit=30"); }
    catch (e) { r = { ok: false, error: String(e) }; }
    if (!r || !r.ok) {
      if (box) box.innerHTML = '<p class="note">计算失败：' + esc((r && r.error) || "未知错误") + "</p>";
      return;
    }
    pkAbRender(r, "bearAbBody");
  };
  return btn ? withBusy(btn, work) : work();
}

/* ---------------- 选股记录的空头辩护（随批次存库） ---------------- */
/* 读的是那一批存下来的辩护原文，不重新调模型：记录页只回看，不该烧 token */
async function pkBearLoad(batch) {
  const box = $("#pkBear");
  if (!box) return;
  box.innerHTML = "";
  if (!batch) { PK.bear = null; PK.bearKey = ""; return; }
  const key = pkKey(batch);

  // 同一批次已经取过就不重复请求、更不重复重绘。
  // 之前只要 pkDetail 一跑就会再调进来一次，而这条路径末尾又会 pkDetail，
  // 形成「点开一行 → 整个详情重建 → 展开态被冲掉」的循环 ——
  // 表现就是用户说的「点了有时候展不开」。
  if (PK.bearKey === key && PK.bear) return;

  const q = new URLSearchParams({ as_of: batch.as_of || "",
                                  recorded_at: batch.recorded_at || "" });
  let r;
  try { r = await api("/api/screen/bear?" + q.toString()); } catch (e) { return; }
  if (PK.sel !== pkKey(batch)) return;      // 已经切到别的批次了
  if (!r || !r.ok || !r.saved) {
    PK.bear = null;
    PK.bearKey = key;
    return;
  }
  const rows = r.rows || [];

  // 辩护结论并进「辩护」列：每只票一个 chip，悬停看完整理由（见 bearCellHtml）。
  // 这里把结论按 code 存进 PK.bear，并就地刷新那一列，**不重建整张表**。
  const map = {};
  rows.forEach((x) => {
    map[String(x.code)] = {
      verdict: x.verdict,
      enough: x.enough,
      bears: x.bears || [],
      judge_reason: x.judge_reason || "",
    };
  });
  PK.bear = map;
  PK.bearKey = key;

  // 只替换表格里的辩护单元格，保留展开行、滚动位置与图表实例
  const tbl = $("#pkDetail table");
  if (tbl) {
    const lines = tbl.querySelectorAll("tbody tr.pk-line");
    lines.forEach((tr) => {
      const code = tr.getAttribute("data-code") || "";
      const cell = tr.querySelector("td.pk-bear-cell");
      if (cell) cell.outerHTML = bearCellHtml(map[code] || null);
    });
    return;
  }
  const drop = rows.filter((x) => x.verdict === "drop");
  box.innerHTML = '<p class="bear-none">' + (drop.length
    ? "本批有 " + drop.length + " 只被否决，已在表格「辩护」列标出。"
    : "本批没有被否决的票 —— 辩护全部通过，结论见表格「辩护」列。") + "</p>";
}

/* 空头辩护的运行态：data 是最近一次辩护结果（含裁决），busy 防重复提交 */
const BEAR = { data: null, busy: false };

/* ---------------- 空头辩护：结论 chip（表格内用） ---------------- */
/* 辩护原本是候选榜下方一整块卡片墙（20 只 × 4 条理由 ≈ 2800px）。
   真正决定名单的只有「否决 / 保留 / 理由不足」这一个结论，理由本身是
   按需查阅的细节 —— 所以收进表格的一列：平时只显示 chip，悬停看全文。
   这一列在「选股工作台」和「选股记录」两处共用，保证同一批数据两处看到的一致。 */
const BEAR_VERDICT = {
  drop: { txt: "否决", cls: "no" },
  keep: { txt: "保留", cls: "yes" },
  thin: { txt: "理由不足", cls: "off" },
};
function bearVerdictOf(x) {
  if (!x) return null;
  if (x.verdict === "drop") return "drop";
  if (x.verdict === "keep" || x.enough) return "keep";
  if (x.verdict) return "thin";
  return null;
}
/** 塞进 HTML 属性值的 JSON：转义引号与尖括号，但保留 JSON 结构。
    esc() 会把 " 换成 _（那是给纯文本用的），直接用在 JSON 上会把结构毁掉。 */
function attrJson(obj) {
  return JSON.stringify(obj)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function bearCellHtml(x) {
  const v = bearVerdictOf(x);
  if (!v) return '<td class="pk-bear-cell"><span class="pk-na">—</span></td>';
  const meta = BEAR_VERDICT[v];
  // 悬停内容走 data-bear（结构化 JSON），由 showTerm 渲染成浮层。
  // 原先用原生 title：浏览器有约 1 秒的强制延迟，且换行/缩进不可控 ——
  // 辩护是「逐条理由 + 触发条件 + 观察指标」的结构化内容，原生 tooltip 撑不起来。
  // 现在与 ? 说明图标共用同一个浮层（位置、pin、Esc、滚动隐藏只有一份实现）。
  const payload = {
    v: v,
    code: x.code || "",
    name: x.name || "",
    why: x.judge_reason || "",
    bears: (x.bears || []).slice(0, 4).map((y) => ({
      point: String(y.point || ""),
      trigger: String(y.trigger || ""),
      watch: String(y.watch || ""),
    })),
  };
  return '<td class="pk-bear-cell"><span class="pill ' + meta.cls + ' pk-bear-chip term"' +
    ' data-bear="' + attrJson(payload) + '"' +
    ' tabindex="0" role="button" aria-label="' + esc(meta.txt + "：" + (x.name || x.code || "")) +
    '">' + meta.txt + "</span></td>";
}

/* ---------------- 选股记录的决策档案（P1-5） ---------------- */
async function pkArchiveLoad(batch) {
  const box = $("#pkArch");
  if (!box || !batch) return;
  const r = await api("/api/picks/archive?as_of=" + encodeURIComponent(batch.as_of || "") + "&limit=1");
  const it = r && r.ok ? (r.items || [])[0] : null;
  if (!it) {
    box.innerHTML = "";
    return;
  }
  const p = it.payload || {};
  const picks = p.picks || [];
  const dropped = (p.dropped || []).length;
  const bears = picks.filter((x) => (x.bears || []).length);
  const llm = p.llm || null;
  // 档案是「事后查证」用的，不是每次都要读的内容 —— 整个收进 details，
  // 和上面空头辩护保持同一种交互（点击展开），避免两块各自撑高页面。
  box.innerHTML = '<details class="bear-fold pk-arch-fold">' +
    '<summary>决策档案 · 这一批选了 ' + picks.length + " 只，漏斗淘汰 " + dropped + " 只" +
    (llm ? "，LLM 否决 " + ((llm.dropped == null) ? "—" : llm.dropped) + " 只" : "") +
    "</summary>" +
    '<div style="padding:9px">' +
    '<p class="pk-arch-h" style="margin:0 0 6px"><b>决策档案</b>' +
    '<span class="dim">落盘于 ' + esc(it.at || "") + " · 档案 #" + it.id + "</span></p>" +
    (bears.length
      ? '<ul class="bear-list">' + bears.slice(0, 8).map((x) =>
        "<li><b>" + esc(x.code) + " " + esc(x.name) + "</b>：" +
        esc(((x.bears || [])[0] || {}).point || "") + "</li>").join("") + "</ul>"
      : '<p class="bear-none" style="margin:0">这一批没有记录否决理由。</p>') +
    "</div></details>";
}

const TAB_TITLES = {
  screen: "选股工作台", stock: "单股分析", picks: "选股记录", target: "目标持仓",
  overview: "回测总览", kline: "行情走势", factors: "因子体检", scan: "参数扫描",
  hub: "数据中枢", ind: "行业与强度",
  pool: "股票池与行情", real: "实盘复盘", journal: "复盘日记",
  terms: "术语表", about: "关于"
};
const TAB_GROUPS = {
  screen: "研究", stock: "研究", picks: "研究", target: "研究",
  overview: "验证", kline: "验证", factors: "验证", scan: "验证",
  hub: "数据", ind: "数据", pool: "数据", real: "实盘", journal: "实盘",
  terms: "其他", about: "其他"
};
/* 分组键：决定顶栏状态标签与回测参数条是否出现。
   行情走势归在「验证」组里方便就近查看，但它不依赖回测参数，所以单独给 market 键，不弹参数条。
   目标持仓复用 picks 键：它读的就是选股结果，顶栏该显示同一份状态。 */
const TAB_GROUP_KEY = {
  screen: "screen", stock: "screen", picks: "picks", target: "picks",
  overview: "verify", kline: "market", factors: "data", scan: "verify",
  hub: "data", ind: "data", pool: "data", real: "real", journal: "real",
  terms: "other", about: "other"
};
const POOL_LOADED = { done: false };
const PK = { loaded: false, data: null, idx: 0, sel: "", sort: "rank", open: "",
             bear: null, bearKey: "" };
function ensureTabData(name) {
  if (name === "hub") { hubLoad(); hubTab(HUB.tab); }
  if (name === "stock" && !STK.loaded) stkLoad();
  if (name === "ind" && !IND.loaded) indLoad();
  if (name === "terms") termPageRender("");   // 每次进页面都按当前关键字重画一次
  if (name === "about") aboutRender();        // 目录与搜索按当前输入重建
  if (name === "picks") {
    picksLoad();      // 记录会随每次选股变化，进页面就重读
    // 归因要重放 60 期，等记录先铺上去再补，别让两张卡抢同一个渲染时机
    setTimeout(pkAttrAuto, 600);
  }
  if (name === "factors" && !FC.data) fcLoad();
  if (name === "target") tgFill();
  if (name === "pool" && !POOL_LOADED.done) {
    POOL_LOADED.done = true;
    loadAccount(); uniLoad(); bulkStatus(); uniRunsLoad();
  }
  if (name === "kline") {
    // 行情页独立取数：默认跟随回测标的，用户手动输入过就尊重用户的选择
    if (!MK.code || !MK.manual) MK.code = mkCode(($("#code").value || "")) || MK.code || "600000";
    mkIndicesLoad();
    mkHistRender();
    // 分时 / 五日是盘中数据，每次进入都拉一次（后端有 15s 缓存兜底）
    mkLoad(null, { force: MK.period === "intraday" || MK.period === "five" });
  }
  if (name === "overview") {
    // 参数一字未改就直接复用上一次结果，不再每次切页都重跑。
    // 扫描页只要热力图，不需要完整回测（K 线/月度/成交明细都在总览），
    // 所以这里不再把 scan 一起算进来 —— 少跑一次全量回测。
    if (!S.last || S.fp !== btFingerprint()) runBacktest();
  }
  if (name === "scan") scanCtxPaint();
  // 实盘账本会随每次同步/历史拉取变化，进页面就重读；单次 30~80ms，不值得做失效判断
  if (name === "real") rtLoad();
  if (name === "journal") jnLoad();
}
function syncSideCollapse() {
  const d = $("#sideCollapse");
  if (d) d.open = window.innerWidth > 860;
}
function switchTab(name) {
  $$(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("on", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  $$(".panel").forEach((p) => p.classList.toggle("on", p.dataset.panel === name));
  document.body.dataset.page = name;
  document.body.dataset.group = TAB_GROUP_KEY[name] || "other";
  const h = $("#pageTitle");
  if (h && TAB_TITLES[name]) h.textContent = TAB_TITLES[name];
  const g = $("#crumbGroup");
  if (g) g.textContent = TAB_GROUPS[name] || "";
  closeDrawer();
  mountBtBar(name);
  // 回测参数条只在验证组出现，隐藏时把图表尺寸补回来
  setTimeout(() => resizeIn('[data-panel="' + name + '"]'), 30);
  ensureTabData(name);
  // 切页时顺手对一次待办（限频 60 秒）：数据链面板是每次进来重拉的，
  // 待办若一直吃页面加载时那份旧状态，就会出现「面板说需更新、待办里没有」。
  if (Date.now() - (SIDE_TODO.at || 0) > 60000) sideTodoLoad();
}

/* 回测参数条只有一个（输入框、参数、状态都在里面），总览页挂进左列当「运行设置」，
   其它页归位到页顶横条。移动节点而不是复制一份，值和事件都跟着走。 */
function mountBtBar(name) {
  const bt = $("#btBar"), home = $("#btSlot"), side = $("#ovSide");
  if (!bt) return;
  if (name === "overview" && side) side.appendChild(bt);
  else if (home && bt.parentNode !== home) home.appendChild(bt);
}
function exportTrades() {
  if (!S.last || !S.last.trades.length) return;
  const head = "日期,方向,成交价,股数,成交金额,手续费,盈亏,收益率(%)\n";
  const body = S.last.trades.map((t) => [t.date, t.side, t.price, t.shares, t.amount, t.fee,
    t.pnl === null ? "" : t.pnl, t.pnl_pct === null ? "" : t.pnl_pct].join(",")).join("\n");
  const blob = new Blob(["\ufeff" + head + body], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "trades_" + S.last.code + "_" + S.last.strategy + ".csv";
  a.click(); URL.revokeObjectURL(a.href);
  toast("交易明细已导出", "ok");
}
async function refreshData() {
  sysTaskStart("refresh-quote", "刷新行情缓存");
  const r = await post("/api/backtest", payload({ refresh: true }));
  sysTaskEnd("refresh-quote");
  if (!r.ok) { toast(r.error || "刷新失败", "err"); return; }
  S.last = r; S.fp = btFingerprint(); renderBacktest(r);
  toast("行情已刷新：" + r.source, "ok");
  updateStatus();
}
async function clearCache() {
  const r = await post("/api/cache", { action: "clear" });
  toast(r.ok ? "已清除 " + r.cleared + " 根日线缓存（SQLite）" : (r.error || "清除失败"), r.ok ? "ok" : "err");
  updateStatus();
}
/* ---------------- 侧栏「今日待办」：只提示该更新的数据，点击直达 ---------------- */
const SIDE_TODO = { busy: false, at: 0, timer: null };
// 待办只是「检查结果」：后台增量、定时任务、手动刷新都会让结论变，页面却一直挂着
// 加载时那一份，于是数据链面板说「需更新」、待办里是空的。两分钟对一次 + 回到
// 前台时立刻补一次，就够了；没有这些钩子时只能靠用户自己点「今日待办」重查。
function sideTodoWatch() {
  if (SIDE_TODO.timer) return;
  SIDE_TODO.timer = setInterval(() => {
    if (document.visibilityState !== "hidden") sideTodoLoad();
  }, 120000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && Date.now() - (SIDE_TODO.at || 0) > 20000) {
      sideTodoLoad();
    }
  });
}
/* 跳转后给「该点的按钮」套一圈呼吸光环 + 流光描边 + 悬浮「点这里」气泡 */
let SPOT_TIMER = null, SPOT_TIP = null, SPOT_MOVE = null;
function spotClear() {
  $$(".spot").forEach((el) => el.classList.remove("spot"));
  if (SPOT_TIP) { SPOT_TIP.remove(); SPOT_TIP = null; }
  if (SPOT_TIMER) { clearTimeout(SPOT_TIMER); SPOT_TIMER = null; }
  if (SPOT_MOVE) { window.removeEventListener("scroll", SPOT_MOVE); SPOT_MOVE = null; }
}
function spotPlace(el, tip) {
  const r = el.getBoundingClientRect();
  if (!r.width && !r.height) return false;
  const below = r.top < 56;
  tip.style.left = Math.round(r.left + r.width / 2) + "px";
  tip.style.top = Math.round(below ? r.bottom + 10 : r.top - 8) + "px";
  tip.classList.toggle("below", below);
  return true;
}
function spotlight(el, text) {
  if (!el || el.offsetParent === null) { if (el) el.scrollIntoView({ block: "center" }); return; }
  spotClear();
  el.classList.add("spot");
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  const tip = document.createElement("div");
  tip.className = "spot-tip";
  tip.textContent = text || "点这里";
  document.body.appendChild(tip);
  SPOT_TIP = tip;
  spotPlace(el, tip);
  SPOT_MOVE = () => { if (SPOT_TIP) spotPlace(el, SPOT_TIP); };
  window.addEventListener("scroll", SPOT_MOVE, { passive: true });
  window.addEventListener("resize", SPOT_MOVE, { passive: true });
  SPOT_TIMER = setTimeout(spotClear, 4800);
}
function sideTodoGo(it) {
  if (it.hub) goHub(it.hub); else switchTab(it.go);
  navDrawer(false);   // 待办在窄屏抽屉里，跳转后要收起，否则挡住目标页
  if (it.tab) {
    const t = $('.ptab[data-ptab="' + it.tab + '"]');
    if (t && !t.classList.contains("on")) t.click();
  }
  try { history.replaceState(null, "", "#" + it.go); } catch (err) { /* file:// 忽略 */ }
  if (it.drawer) openDrawer();
  const target = it.spot ? $(it.spot) : null;
  if (target) setTimeout(() => spotlight(target, it.tip), 320);
}
function sideTodoRender(items) {
  const list = $("#sfTodoList"), cnt = $("#sfTodoCount");
  if (!list) return;
  if (!items.length) {
    list.innerHTML = '<p class="sf-empty">今天的数据都已更新，暂无待办。</p>';
    if (cnt) { cnt.textContent = "0"; cnt.className = "sf-count good"; }
    return;
  }
  // 只有 info 时别用橙色：那会让「补个资金流」看起来跟「行情断了」一样严重
  const nWarn = items.filter((x) => x.level !== "info").length;
  if (cnt) {
    cnt.textContent = String(items.length);
    cnt.className = "sf-count " + (nWarn ? "warn" : "info");
  }
  list.innerHTML = items.map((it, i) =>
    '<button class="sf-item ' + (it.level || "warn") + '" data-i="' + i + '">' +
    '<span class="sf-dot"></span><span>' + esc(it.text) + "</span></button>").join("");
  list.onclick = (e) => {
    const b = e.target.closest(".sf-item[data-i]");
    if (!b) return;
    const it = items[Number(b.dataset.i)];
    if (it && it.go) sideTodoGo(it);
  };
}
async function sideTodoLoad(force) {
  const list = $("#sfTodoList");
  // busy 只防并发，不防「前后脚」重入：init 先调一次、screenStatus 紧接着又调一次，
  // 于是 data/hub 与 universe 各请求两轮。SIDE_TODO.at 本来就是「上次检查时间」，
  // 复用它做 60 秒节流（和切页/回前台的重查窗口一致）。
  // force=true 用于用户手点「今日待办」——那必须真的重查。
  if (!list || SIDE_TODO.busy) return;
  if (!force && Date.now() - (SIDE_TODO.at || 0) < 60000) return;
  SIDE_TODO.busy = true;
  SIDE_TODO.at = Date.now();
  try {
  const items = [];
  let hub = null, uni = null;
  try { hub = await api("/api/data/hub"); } catch (e) { hub = null; }
  try { uni = await api("/api/universe?limit=1"); } catch (e) { uni = null; }
  const hubOk = !!(hub && hub.ok), uniOk = !!(uni && uni.ok);
  if (!hubOk && !uniOk) {
    list.innerHTML = '<p class="sf-empty">状态读取失败，点上方「今日待办」重试。</p>';
    const c0 = $("#sfTodoCount");
    if (c0) { c0.textContent = "—"; c0.className = "sf-count"; }
    return;
  }
  if (!hubOk) {
    items.push({ level: "info", text: "数据链状态读取失败，待办可能不全；去数据中枢重查一次",
      go: "hub", hub: "chain" });
  }
  if (hubOk) {
    const f = hub.freshness || {};
    if (f.ok === false) {
      items.push({ level: "warn", text: f.error || "还没有行情缓存，先去补行情",
        go: "pool", tab: "all", spot: "#btnBulkInc", tip: "先补行情" });
    } else {
      const ACT = {
        quote: { go: "pool", tab: "all", spot: "#btnBulkInc", tip: "点这里增量更新", name: "行情" },
        cover: { go: "pool", tab: "all", spot: "#btnBulkInc", tip: "点这里增量更新", name: "行情覆盖" },
        snapshot: { go: "hub", hub: "screen", spot: "#btnScrSnapshot", tip: "重建快照", name: "快照" },
        dataset: { go: "hub", hub: "screen", spot: "#btnScrDataset", tip: "重建训练集", name: "训练集" },
        model: { go: "hub", hub: "screen", spot: "#btnScrTrain", tip: "重训模型", name: "模型" },
        // 日历到期会让时效检查失效：跳到数据链，一键更新会顺带联网刷新日历
        calendar: { go: "hub", hub: "chain", spot: "#btnHubAuto", tip: "点这里刷新日历", name: "交易日历" },
        // 估值去数据源页：那里能看到四路源的顺序、开关和各自上次结果
        valuation: { go: "hub", hub: "sources", spot: "#btnHubSrcFetch", tip: "拉取估值", name: "估值" },
        // 2026-11 新增的三块数据都在「数据链」面板里，按钮就在对应卡片上
        listing: { go: "hub", hub: "chain", spot: "#btnListSync", tip: "同步名单", name: "上市名单" },
        listing_prewarm: { go: "hub", hub: "chain", spot: "#btnListPre", tip: "补退市股行情", name: "退市股行情" },
        capital: { go: "hub", hub: "chain", spot: "#btnCapSync", tip: "同步资金流", name: "资金流" },
        // 全 A 名单 / 实时价是股票池页的两个按钮：以前它们过期只在页面上悄悄
        // 显示旧数据，待办里看不到，于是没人点
        universe: { go: "pool", tab: "all", spot: "#btnUniRefresh", tip: "刷新名单", name: "全 A 名单" },
        spot: { go: "pool", tab: "all", spot: "#btnUniSpot", tip: "刷新实时价", name: "实时价" }
      };
      (f.actions || []).forEach((a) => {
        // 默认只报 warn：info 里多数是「正常落后」的说明（如训练集尾部等标签），
        // 放进待办只会让列表永远清不空。维护类的新待办用 todo:true 显式要位置。
        if (a.level !== "warn" && !a.todo) return;
        items.push({ level: a.level === "warn" ? "warn" : "info", text: a.text,
          ...(ACT[a.key] || { go: "hub", hub: "chain" }) });
      });
      // 数据链容器头顶的「需更新」就是这些 auto 级 warn 汇总出来的。分成几条各自
      // 跳不同页面时，用户在数据中枢看到「需更新」却翻不到对应的那一条，所以两项
      // 以上再补一条汇总，直接落到「更新到最新」。
      const autoWarn = (f.actions || []).filter((a) => a.level === "warn" && a.auto);
      if (autoWarn.length > 1) {
        items.unshift({ level: "warn",
          text: "数据链有 " + autoWarn.length + " 项需要更新（" +
            autoWarn.map((a) => (ACT[a.key] && ACT[a.key].name) || a.key).join("、") +
            "），点这里一次跑完",
          go: "hub", hub: "chain", spot: "#btnHubAuto", tip: "一键更新到最新" });
      }
      if (f.quote_blocked) {
        items.push({ level: "warn", text: "行情数据源被限流/不可用，稍后重试增量更新",
          go: "hub", hub: "chain", spot: "#btnHubAuto", tip: "从这里重试" });
      }
      if (f.run && f.run.running) {
        items.push({ level: "info", text: "数据更新正在执行，完成后这里会自动刷新",
          go: "hub", hub: "runs" });
      }
    }
    const s = hub.sources || {};
    if (s.configured && !s.rows) {
      items.push({ level: "info", text: "Tushare 已配置，但还没拉过全市场估值（PE/PB/市值）",
        go: "hub", hub: "sources", spot: "#btnHubSrcFetch", tip: "拉取估值" });
    }
    // 实盘账本：今天该日结却还没同步（多半是晚上没开电脑，自动日结没触发）。
    // 资产快照只能取当下，漏的这天补不回来，所以只提醒「现在补今天」。
    const e = hub.emt || {};
    if (e.configured && e.eod && e.eod.pending) {
      items.push({ level: "warn",
        text: "实盘账本今天还没同步（" + (e.eod.note || "缺少今日快照") + "），点「同步账户」补一份",
        go: "real", spot: "#btnRtSync", tip: "同步账户" });
    }
  }
  if (!hubOk && uniOk) {
    // 名单 / 实时价 / 行情覆盖的判断统一在后端 freshness 里做（与数据链容器同一
    // 份结论，见 pipeline.freshness）。只有中枢接口读不到时才在这里兜一次，
    // 两处各写一套「今天算不算新」正是以前容器提示、待办却不提示的根源。
    if (!uni.fresh) {
      items.push({ level: "warn", text: "全 A 名单停在 " + (uni.updated || "—") +
        "（落后 " + (uni.lag == null ? "若干" : uni.lag) + " 个交易日），点「刷新名单」",
        go: "pool", tab: "all", spot: "#btnUniRefresh", tip: "刷新名单" });
    }
    const total = Number(uni.total || 0), cached = Number(uni.cached || 0);
    if (total && cached / total < 0.9) {
      items.push({ level: "warn", text: "行情只覆盖 " + (cached / total * 100).toFixed(1) +
        "%（" + cached + "/" + total + "），样本偏少，建议补全行情",
        go: "pool", tab: "all", spot: "#btnBulkFull", tip: "全量补全" });
    }
  }
  const ind = (SCR.status && SCR.status.data && SCR.status.data.industry) || null;
  if (ind) {
    if (!ind.count) {
      items.push({ level: "warn", text: "还没有行业分类，行业约束与中性化都用不了",
        go: "ind", spot: "#btnIndSync", tip: "同步分类" });
    } else if (ind.stale) {
      items.push({ level: "warn", text: "行业分类已 30 天未更新，建议每季度同步一次（约 40~90 秒）",
        go: "ind", spot: "#btnIndSync", tip: "同步分类" });
    }
  }
  sideTodoRender(items);
  SIDE_TODO.at = Date.now();
  } finally {
    SIDE_TODO.busy = false;
  }
}
let LAST_STATUS = null;      // 关于页每次重画都会丢掉 #cfgInfo，用它把版本信息填回去
function cfgInfoText(r) {
  const c = (r && r.config) || {};
  return "Python " + (r.python || "—") + " · 服务 127.0.0.1:" + (r.port || 8765) +
    " · 配置 " + ((c.capital || 200000) / 10000) + " 万初始资金 / 佣金 " +
    ((c.commission || 0.00025) * 1000) + "‰ / 滑点 " + ((c.slippage || 0.0005) * 1000) + "‰";
}
async function updateStatus(preloaded) {
  const r = preloaded || await api("/api/status");
  if (!r.ok) return;
  LAST_STATUS = r;
  $("#chipAk").textContent = "AKShare " + (r.akshare || "—");
  $("#chipAk").className = "chip " + (r.akshare_broken ? "warn" : "good");
  $("#chipCache").innerHTML = "缓存 <b>" + (r.cache_count || 0) + "</b> 只";
  // 运行环境那行现在在「关于 → 口径与边界」里由 ABOUT_DOC 渲染，
  // 首屏时该面板还没画出来，取不到就跳过；进关于页时 aboutRender 会补上。
  const ci = $("#cfgInfo");
  if (ci) ci.textContent = cfgInfoText(r);
}
/* 窄屏导航抽屉：只负责开合状态。桌面端按钮不显示，调用也只会改个 class。 */
function navDrawer(open) {
  document.body.classList.toggle("nav-open", !!open);
  const t = $("#navToggle");
  if (t) t.setAttribute("aria-expanded", open ? "true" : "false");
}

function bind() {
  const navToggle = $("#navToggle"), navScrim = $("#navScrim");
  const navOpen = navDrawer;
  if (navToggle) {
    navToggle.addEventListener("click", () => {
      navOpen(!document.body.classList.contains("nav-open"));
    });
  }
  if (navScrim) navScrim.addEventListener("click", () => navOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && document.body.classList.contains("nav-open")) navOpen(false);
  });
  window.addEventListener("resize", () => {
    // 旋屏或拖宽窗口后抽屉必须收起，否则桌面布局会留一块被锁的滚动
    if (window.innerWidth > 860) navOpen(false);
  });

  // 顶部条只有 54px，放不下「今日待办」：窄屏把它挪进抽屉，宽屏再挪回侧栏底部。
  // 只移动 DOM、不改内容，两个尺寸下的事件绑定（按 id 取）都不受影响。
  const sideFoot = document.querySelector(".side-foot");
  const navEl = $("#nav"), sideEl = document.querySelector(".sidebar");
  const placeSideFoot = () => {
    if (!sideFoot || !navEl || !sideEl) return;
    const narrow = window.innerWidth <= 860;
    const inNav = sideFoot.parentElement === navEl;
    if (narrow && !inNav) navEl.appendChild(sideFoot);
    else if (!narrow && inNav) sideEl.appendChild(sideFoot);
  };
  placeSideFoot();
  window.addEventListener("resize", placeSideFoot);

  $$(".tab").forEach((t) => {
    t.setAttribute("role", "tab");
    t.id = "tab-" + t.dataset.tab;
    t.setAttribute("aria-controls", "panel-" + t.dataset.tab);
    t.setAttribute("aria-selected", t.classList.contains("on") ? "true" : "false");
    t.addEventListener("click", () => {
      switchTab(t.dataset.tab);
      navOpen(false);   // 选完就收起，手机上不用再点一次遮罩
      try { history.replaceState(null, "", "#" + t.dataset.tab); } catch (e) { /* file:// 下忽略 */ }
    });
  });
  $$(".panel").forEach((p) => {
    p.setAttribute("role", "tabpanel");
    p.id = "panel-" + p.dataset.panel;
    p.setAttribute("aria-labelledby", "tab-" + p.dataset.panel);
    p.setAttribute("aria-label", TAB_TITLES[p.dataset.panel] || p.dataset.panel);
  });
  $$(".chart").forEach((c) => {
    c.setAttribute("role", "img");
    c.setAttribute("aria-label", "策略图表：" + c.id);
  });
  $("#btnScrRun").addEventListener("click", (e) => screenRun(false, e.currentTarget));
  $("#btnPkReload").addEventListener("click", () => picksLoad(true));
  // 行情走势：周期切换 + 代码输入
  $$("#mkTabs .mk-tab").forEach((t) => {
    t.addEventListener("click", () => {
      if (MK.period === t.dataset.period) return;
      mkLoad(t.dataset.period, { force: true });
      try { localStorage.setItem("quantdesk_mk_period", t.dataset.period); } catch (e) {}
    });
  });
  $("#btnMkLoad").addEventListener("click", () => {
    MK.manual = true;
    MK.key = "";
    mkLoad(null, { force: true });
  });
  // 行业成分股：点任意一行直接看走势
  $("#indMemBody").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-code]");
    if (tr) openChart(tr.dataset.code);
  });
  $("#mkCode").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    MK.manual = true;
    MK.key = "";
    mkLoad(null, { force: true });
  });
  // 查看记录：点名字看走势，点 × 只删这一条
  $("#mkHistList").addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { mkHistRemove(del.dataset.del); return; }
    const chip = e.target.closest(".mk-hchip[data-code]");
    if (chip) openChart(chip.dataset.code);
  });
  $("#mkHistList").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const chip = e.target.closest(".mk-hchip[data-code]");
    if (chip && e.target === chip) { e.preventDefault(); openChart(chip.dataset.code); }
  });
  $("#btnMkHistClear").addEventListener("click", (e) => {
    twoStep(e.currentTarget, "确认清空", () => { mkHistSave([]); mkHistRender(); });
  });
  // 「历史截止日」提示可点：一键清空日期过滤，回到最新行情
  $("#scrAsOfTag").addEventListener("click", () => {
    if (!$("#scrAsOfTag").classList.contains("warn")) return;
    $("#scrAsOfInput").value = "";
    screenRun(false);
  });
  $("#btnScrSnapshot").addEventListener("click", (e) => screenSnapshot(e.currentTarget));
  if ($("#btnPipeAuto")) $("#btnPipeAuto").addEventListener("click", (e) => pipeRun("auto", e.currentTarget));
  if ($("#btnPipeFull")) $("#btnPipeFull").addEventListener("click", (e) => pipeRun("full", e.currentTarget));
  // 定时任务：勾选即开关，改时刻保存后立刻按新时刻排下一次
  if ($("#autoSched")) {
    $("#autoSched").addEventListener("change", (e) => {
      autoSave({ enabled: e.currentTarget.checked },
        e.currentTarget.checked ? "已开启：每个交易日收盘后自动更新" : "已关闭自动更新");
    });
  }
  if ($("#autoTime")) {
    $("#autoTime").addEventListener("change", (e) => {
      const v = (e.currentTarget.value || "").slice(0, 5);
      if (!/^\d{2}:\d{2}$/.test(v)) return;
      autoSave({ time: v }, "自动更新时间改为 " + v);
    });
  }
  if ($("#chipFresh")) $("#chipFresh").addEventListener("click", () => goHub("chain"));
  // 数据中枢：分类标签 + 页内跳转
  const hubTabs = $("#hubTabs");
  if (hubTabs) hubTabs.addEventListener("click", (e) => {
    const b = e.target.closest(".hub-tab");
    if (b) hubTab(b.dataset.hubtab);
  });
  $$("[data-hubgo]").forEach((b) => b.addEventListener("click", () => goHub(b.dataset.hubgo)));
  if ($("#btnHubReload")) $("#btnHubReload").addEventListener("click", () => hubLoad());
  if ($("#btnHubAuto")) $("#btnHubAuto").addEventListener("click", (e) => hubRun("auto", e.currentTarget));
  if ($("#btnHubSel")) $("#btnHubSel").addEventListener("click", (e) => hubRun("sel", e.currentTarget));
  if ($("#btnHubFull")) $("#btnHubFull").addEventListener("click", (e) => hubRun("full", e.currentTarget));
  if ($("#btnHubWorkers")) $("#btnHubWorkers").addEventListener("click", (e) => hubWorkersSave(e.currentTarget));
  if ($("#hubSched")) $("#hubSched").addEventListener("change", hubSchedSave);
  if ($("#hubTime")) $("#hubTime").addEventListener("change", hubSchedSave);
  if ($("#btnHubRuns")) $("#btnHubRuns").addEventListener("click", () => runsLoad(true));

  // 更新记录：筛选 + 翻页（记录全部保留，靠分页看历史）
  const runApply = () => { runsReadForm(); runsLoad(true); };
  if ($("#btnRunSearch")) $("#btnRunSearch").addEventListener("click", runApply);
  ["#runKind", "#runTrigger", "#runOk"].forEach((s) => {
    if ($(s)) $(s).addEventListener("change", runApply);
  });
  if ($("#runQ")) $("#runQ").addEventListener("keydown", (e) => { if (e.key === "Enter") runApply(); });
  if ($("#btnRunMore")) $("#btnRunMore").addEventListener("click", () => runsLoad(false));

  // 运行日志：级别 / 来源 / 搜索 / 翻页 / 导出 / 清理 / 自动刷新
  const logApply = () => {
    if ($("#logSrc")) LOG.src = $("#logSrc").value || "all";
    if ($("#logQ")) LOG.q = $("#logQ").value || "";
    logLoad(true);
  };
  if ($("#logLevels")) $("#logLevels").addEventListener("click", (e) => {
    const b = e.target.closest("[data-loglt]");
    if (!b) return;
    LOG.level = b.dataset.loglt;
    $$("#logLevels .chip").forEach((x) => x.classList.toggle("on", x === b));
    logLoad(true);
  });
  if ($("#logSrc")) $("#logSrc").addEventListener("change", logApply);
  if ($("#btnLogSearch")) $("#btnLogSearch").addEventListener("click", logApply);
  if ($("#logQ")) $("#logQ").addEventListener("keydown", (e) => { if (e.key === "Enter") logApply(); });
  if ($("#btnLogMore")) $("#btnLogMore").addEventListener("click", () => logLoad(false));
  if ($("#btnLogExport")) $("#btnLogExport").addEventListener("click", () => {
    window.open("/api/logs?" + logQuery({ export: "1", limit: "5000" }), "_blank");
  });
  // 界面通知回流开关：默认开，嫌吵可以关掉（只影响前端上报，不影响后端日志）
  if ($("#logToast")) {
    const cb = $("#logToast");
    cb.checked = TOAST_LOG.on;
    cb.addEventListener("change", () => {
      TOAST_LOG.on = !!cb.checked;
      try { localStorage.setItem("quantdesk_logtoast", TOAST_LOG.on ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
      toast(TOAST_LOG.on ? "以后右上角的通知都会记进运行日志" : "已停止记录界面通知", "info");
    });
  }
  if ($("#btnLogClear")) $("#btnLogClear").addEventListener("click", (e) => {
    twoStep(e.currentTarget, "确认清理", async () => {
      const r = await post("/api/logs", { action: "prune" });
      toast(r.ok ? ("已清理 " + (r.removed || 0) + " 条") : (r.error || "清理失败"),
        r.ok ? "ok" : "err");
      logLoad(true);
    });
  });
  // 点行展开/收起详情（异常栈、原始错误）
  if ($("#logBody")) $("#logBody").addEventListener("click", (e) => {
    const tr = e.target.closest(".log-row");
    if (!tr) return;
    const det = tr.querySelector(".log-det");
    if (!det) return;
    det.hidden = !det.hidden;
    tr.classList.toggle("open", !det.hidden);
  });
  // 自动刷新只在「日志页 + 没筛选」时跟，别打断正在看的内容
  setInterval(() => {
    const on = $("#logAuto");
    // HUB.tab 停在 logs 后不会复位，必须同时判断当前页面，否则切到别的页
    // 仍每 30 秒拉 200 条日志并重绘一个看不见的表格。
    if (!on || !on.checked || HUB.tab !== "logs") return;
    if (document.body.dataset.page !== "hub") return;
    if (LOG.level !== "all" || LOG.src !== "all" || LOG.q.trim()) return;
    if (LOG.offset <= LOG.limit) logLoad(true);
  }, 30000);
  if ($("#btnHubSrcSave")) $("#btnHubSrcSave").addEventListener("click", (e) => hubSrcSave(e.currentTarget));
  if ($("#btnHubSrcTest")) $("#btnHubSrcTest").addEventListener("click", (e) => hubSrcTest(e.currentTarget));
  if ($("#btnHubSrcClear")) $("#btnHubSrcClear").addEventListener("click", (e) => hubSrcClear(e.currentTarget));
  if ($("#btnHubSrcFetch")) $("#btnHubSrcFetch").addEventListener("click", (e) => hubSrcFetch(e.currentTarget));
  const vl = $("#hubValList");
  if (vl) {
    vl.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-vsact]");
      if (!b) return;
      const key = b.dataset.key, act = b.dataset.vsact;
      if (act === "test") {
        await withBusy(b, async () => {
          const r = await post("/api/sources/test", { source: key });
          toast(r.ok ? (r.message || "可用") : (key + " 不可用：" + (r.error || "无数据")), r.ok ? "ok" : "err");
        });
        return;
      }
      const v = (HUB.data && HUB.data.sources && HUB.data.sources.valuation) || null;
      if (!v) return;
      const order = (v.sources || []).map((x) => x.key);
      const i = order.indexOf(key);
      const j = act === "up" ? i - 1 : i + 1;
      if (i < 0 || j < 0 || j >= order.length) return;
      [order[i], order[j]] = [order[j], order[i]];
      await hubValSave({ order });
    });
    vl.addEventListener("change", async (e) => {
      const c = e.target.closest("input[data-vschk]");
      if (!c) return;
      const en = {}; en[c.dataset.vschk] = c.checked;
      await hubValSave({ enabled: en });
    });
  }
  if ($("#btnHubLlmSave")) $("#btnHubLlmSave").addEventListener("click", (e) => hubLlmSave(e.currentTarget));
  if ($("#btnHubLlmTest")) $("#btnHubLlmTest").addEventListener("click", (e) => hubLlmTest(e.currentTarget));
  if ($("#btnHubLlmClear")) $("#btnHubLlmClear").addEventListener("click", (e) => hubLlmClear(e.currentTarget));
  const hubHealth = $("#hubAiHealth"), hubJournal = $("#hubAiJournal");
  if (hubHealth) hubHealth.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (b) hubAiSet("health", b.dataset.mode);
  });
  if (hubJournal) hubJournal.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (b) hubAiSet("journal", b.dataset.mode);
  });
  if ($("#hubAiAuto")) $("#hubAiAuto").addEventListener("change", hubAiAutoSet);
  // 上市名单 / 资金流 / 空头辩护（P0-1 · P2-6 · P1-5）
  if ($("#btnListSync")) $("#btnListSync").addEventListener("click", (e) => hubListSync(e.currentTarget));
  if ($("#btnListPre")) $("#btnListPre").addEventListener("click", (e) => hubListPrewarm(e.currentTarget));
  if ($("#btnCapSync")) $("#btnCapSync").addEventListener("click", (e) => hubCapSync(e.currentTarget));
  if ($("#hubCapModel")) $("#hubCapModel").addEventListener("change", hubCapModelSet);
  if ($("#btnHubBearSave")) $("#btnHubBearSave").addEventListener("click", (e) => hubBearSave(e.currentTarget));
  // 因子体检
  if ($("#btnFcRun")) $("#btnFcRun").addEventListener("click", () => fcLoad(true));
  ["fcDays", "fcHorizon"].forEach((id) => {
    if ($("#" + id)) $("#" + id).addEventListener("change", () => fcLoad(false));
  });
  if ($("#fcBody")) $("#fcBody").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-fc]");
    if (tr) fcPick(tr.dataset.fc);
  });
  // 目标持仓：一个读服务端「最近一次落盘」，一个优先用工作台当前候选
  if ($("#btnTgLoad")) $("#btnTgLoad").addEventListener("click", (e) => tgRun(e.currentTarget, false));
  if ($("#btnTgRun")) $("#btnTgRun").addEventListener("click", (e) => tgRun(e.currentTarget, true));
  // 选股记录的四张分析卡
  if ($("#btnPkAttr")) $("#btnPkAttr").addEventListener("click", (e) => pkAttrLoad(e.currentTarget));
  if ($("#btnPkSurv")) $("#btnPkSurv").addEventListener("click", (e) => pkAttrLoad(e.currentTarget));
  if ($("#btnPkFunnel")) $("#btnPkFunnel").addEventListener("click", (e) => pkFunnelRun(e.currentTarget));
  if ($("#btnPkAb")) $("#btnPkAb").addEventListener("click", (e) => pkAbRun(e.currentTarget));
  // 选股页的空头辩护
  if ($("#btnBearRun")) $("#btnBearRun").addEventListener("click", (e) => bearRun(e.currentTarget));
  if ($("#btnBearAb")) $("#btnBearAb").addEventListener("click", (e) => bearAb(e.currentTarget));
  $("#btnScrDataset").addEventListener("click", (e) => screenDataset(e.currentTarget));
  $("#btnScrTrain").addEventListener("click", (e) => screenTrain(e.currentTarget));
  $("#btnScrLlm").addEventListener("click", (e) => screenLlm(e.currentTarget));
  $("#btnScrReview").addEventListener("click", (e) => screenReview(e.currentTarget, false));
  $("#btnScrIndustry").addEventListener("click", (e) => screenIndustrySync(e.currentTarget));
  $("#btnScrExport").addEventListener("click", screenExport);
  $("#btnScrEmptyRun").addEventListener("click", (e) => screenRun(false, e.currentTarget));
  $("#btnScrEmptyPrewarm").addEventListener("click", () => switchTab("pool"));
  $("#btnSfTodoRefresh").addEventListener("click", sideTodoLoad);
  // 指数条只在行情页活着：30 秒一次，切到别的页就停
  setInterval(() => {
    if (!document.hidden && document.body.dataset.page === "kline") mkIndicesLoad();
  }, 30000);
  // 设置入口：原位置只留按钮，点开统一跳到数据中枢对应分类
  if ($("#btnScrSettings")) $("#btnScrSettings").addEventListener("click", () => goHub("screen"));
  if ($("#btnBtCost")) $("#btnBtCost").addEventListener("click", () => goHub("backtest"));
  if ($("#btnPoolToHub")) $("#btnPoolToHub").addEventListener("click", () => goHub("sources"));
  // 更多条件
  $("#btnScrMore").addEventListener("click", (e) => {
    const box = $("#scrMore"), b = e.currentTarget;
    const open = box.hidden;
    box.hidden = !open;
    b.textContent = open ? "收起条件" : "更多条件";
    b.setAttribute("aria-expanded", open ? "true" : "false");
  });
  // 术语表
  if ($("#btnTermsTop")) $("#btnTermsTop").addEventListener("click", () => switchTab("terms"));
  if ($("#termPageQ")) $("#termPageQ").addEventListener("input", (e) => termPageRender(e.target.value));
  // 关于页：搜索 + 目录跳转 + 滚动高亮
  if ($("#abQ")) {
    $("#abQ").addEventListener("input", (e) => aboutRender(e.target.value));
    $("#abQ").addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      e.target.value = "";
      aboutRender("");
    });
  }
  if ($("#abToc")) $("#abToc").addEventListener("click", (e) => {
    const a = e.target.closest("a[data-sec]");
    if (!a) return;
    e.preventDefault();
    const el = document.getElementById("ab-" + a.dataset.sec);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  window.addEventListener("scroll", abSpy, { passive: true });
  sysLedStart();
  $("#scrDataHint").addEventListener("click", (e) => {
    if (e.target.dataset.act === "preheat") { e.preventDefault(); switchTab("pool"); }
  });
  SCR_PREF_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", scrSavePrefs);
  });
  $("#scrBody").addEventListener("click", (e) => {
    const op = e.target.closest("button[data-act]");
    if (op) {
      if (op.dataset.act === "backtest") {
        $("#code").value = op.dataset.code;
        switchTab("overview");   // 按需回测：代码变了会自动重算，无需再显式调用
      } else if (op.dataset.act === "chart") {
        openChart(op.dataset.code);
      } else if (op.dataset.act === "pooladd") {
        screenPoolAdd(op.dataset.code, op.dataset.name);
      }
      return;
    }
    const el = e.target.closest(".codelink");
    if (el) toggleScrDetail(el.dataset.code);
  });
  $("#strategy").addEventListener("change", renderParams);
  $("#stockSel").addEventListener("change", (e) => { $("#code").value = e.target.value; });
  $("#btnRun").addEventListener("click", () => runBacktest());
  $("#btnRefresh").addEventListener("click", refreshData);
  $("#btnClearCache").addEventListener("click", clearCache);
  $("#btnScan").addEventListener("click", runScan);
  $("#btnExport").addEventListener("click", exportTrades);
  $("#linkPool").addEventListener("click", (e) => { e.preventDefault(); switchTab("pool"); loadPool(); });
  $("#btnPoolAdd").addEventListener("click", poolAdd);
  const ptabs = $(".ptabs");
  if (ptabs) ptabs.addEventListener("click", (e) => {
    const b = e.target.closest(".ptab");
    if (!b) return;
    const k = b.dataset.ptab;
    $$(".ptabs .ptab").forEach((x) => {
      const on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-selected", on ? "true" : "false");
    });
    $$(".ptab-panel").forEach((p) => p.classList.toggle("on", p.dataset.ptabbody === k));
    if (k === "mine") loadPool();
    if (k === "all") {
      if (!UNI.total) uniLoad();
      uniRunsLoad();
    }
  });
  $("#poolCode").addEventListener("keydown", (e) => { if (e.key === "Enter") poolAdd(); });
  $("#btnPoolExportTxt").addEventListener("click", () => poolExport("txt"));
  $("#btnPoolExportJson").addEventListener("click", () => poolExport("json"));
  $("#btnPoolClearSamples").addEventListener("click", (e) => {
    twoStep(e.currentTarget, "确认清除", async () => {
      const r = await post("/api/watchlist", { action: "remove_source", source: "初始示例" });
      afterPoolChange(r, r.ok ? ("已清除 " + (r.removed || 0) + " 只初始示例股票") : "");
    });
  });
  $("#btnPoolClear").addEventListener("click", (e) => {
    twoStep(e.currentTarget, "确认清空", async () => {
      afterPoolChange(await post("/api/watchlist", { action: "clear" }), "股票池已清空");
    });
  });
  $("#poolBody").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) {
      const tr = e.target.closest("tr[data-code]");
      if (tr) openChart(tr.dataset.code);
      return;
    }
    const act = b.dataset.act, code = b.dataset.code;
    const run = async () => {
      const r = await post("/api/watchlist", {
        action: act, code: code, dir: b.dataset.dir || ""
      });
      let msg = "";
      if (r.ok && act === "remove") msg = "已移除 " + code + "（行情缓存保留）";
      if (r.ok && act === "default") msg = "已设为默认 " + code + "，下次打开自动选中";
      afterPoolChange(r, msg);
    };
    if (act === "remove") twoStep(b, "确认删除", run);
    else run();
  });
  $("#btnAcctSave").addEventListener("click", acctSave);
  $("#btnAcctClear").addEventListener("click", acctClear);
  $("#btnAcctSync").addEventListener("click", acctSync);
  $("#acctKey").addEventListener("keydown", (e) => { if (e.key === "Enter") acctSave(); });
  $("#btnUniSearch").addEventListener("click", () => { UNI.offset = 0; UNI.checked.clear(); uniLoad(); });
  $("#uniQ").addEventListener("keydown", (e) => { if (e.key === "Enter") { UNI.offset = 0; uniLoad(); } });
  $("#uniMarket").addEventListener("change", () => { UNI.offset = 0; uniLoad(); });
  $("#uniCached").addEventListener("change", () => { UNI.offset = 0; uniLoad(); });
  $("#btnUniRefresh").addEventListener("click", uniRefresh);
  $("#btnUniSpot").addEventListener("click", uniSpot);
  $("#btnUniRuns").addEventListener("click", uniRunsLoad);
  $("#btnUniAdd").addEventListener("click", () => uniApply("merge", false));
  $("#btnUniReplace").addEventListener("click", () => uniApply("replace", false));
  $("#btnUniAddChecked").addEventListener("click", () => uniApply("merge", true));
  $("#btnUniPrev").addEventListener("click", () => { UNI.offset = Math.max(0, UNI.offset - UNI.limit); uniLoad(); });
  $("#btnUniNext").addEventListener("click", () => {
    if (UNI.offset + UNI.limit < UNI.filtered) { UNI.offset += UNI.limit; uniLoad(); }
  });
  $("#uniBody").addEventListener("click", (e) => {
    // 勾选框和按钮保持原行为，其余位置点行 → 看走势
    if (e.target.closest("input, button, a, label")) return;
    const tr = e.target.closest("tr[data-code]");
    if (tr) openChart(tr.dataset.code);
  });
  $("#uniBody").addEventListener("change", (e) => {
    const c = e.target;
    if (!c.classList.contains("uniCk")) return;
    if (c.checked) UNI.checked.add(c.dataset.code);
    else UNI.checked.delete(c.dataset.code);
  });
  $("#uniCheckAll").addEventListener("change", (e) => {
    $$("#uniBody .uniCk").forEach((c) => {
      c.checked = e.target.checked;
      if (e.target.checked) UNI.checked.add(c.dataset.code);
      else UNI.checked.delete(c.dataset.code);
    });
  });
  $("#btnBulkInc").addEventListener("click", () => bulkStart("incremental"));
  $("#btnBulkFull").addEventListener("click", bulkAskFull);
  $("#btnBulkYes").addEventListener("click", () => {
    $("#bulkConfirm").hidden = true;
    bulkStart("full");
  });
  $("#btnBulkNo").addEventListener("click", () => { $("#bulkConfirm").hidden = true; });
  $("#btnBulkStop").addEventListener("click", async () => {
    const r = await post("/api/bulk", { action: "stop" });
    toast(r.message || "已发送停止信号", "ok"); bulkStatus();
  });
  if ($("#chipBulk")) $("#chipBulk").addEventListener("click", () => {
    switchTab("pool");
    const tab = $('.ptab[data-ptab="all"]');
    if (tab && !tab.classList.contains("on")) tab.click();
    const card = $("#bulkCard");
    if (card) setTimeout(() => card.scrollIntoView({ behavior: "smooth", block: "center" }), 80);
  });
  $("#btnIndSync").addEventListener("click", (e) => indSync(e.currentTarget));
  $("#btnIndReload").addEventListener("click", (e) => indLoad(e.currentTarget));
  $("#btnIndAddPool").addEventListener("click", indAddPool);
  // 实盘复盘
  $("#btnRtSync").addEventListener("click", rtSync);
  $("#btnRtHist").addEventListener("click", () => {
    goHub("real");
    setTimeout(() => {
      const p = $("#rtHistFrom");
      if (p && p.scrollIntoView) p.scrollIntoView({ block: "center" });
    }, 60);
  });
  $("#btnRtPull").addEventListener("click", rtPullHistory);
  $("#btnRtProbe").addEventListener("click", rtProbe);
  $("#btnRtCfg").addEventListener("click", rtCfgOpen);
  $("#btnRtSave").addEventListener("click", rtCfgSave);
  $("#btnRtLogout").addEventListener("click", rtLogout);
  // 单股分析：输入代码回车即分析；历史可点开回看、单条删除或清空该股
  $("#btnStkAnalyze").addEventListener("click", stkAnalyze);
  $("#stkCode").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); stkAnalyze(); }
  });
  // 输入完整代码后失焦就先出画像（不发 LLM），用户不必等「分析」才知道长什么样
  $("#stkCode").addEventListener("blur", () => {
    const v = ($("#stkCode").value || "").trim();
    if (/^\d{6}$/.test(v) && (!STK.detail || STK.code !== v)) stkDetail(v, true);
  });
  $("#stkHist").addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { stkDelete(Number(del.dataset.del)); return; }
    const open = e.target.closest("[data-id]");
    if (open) stkOpen(Number(open.dataset.id));
  });
  $("#btnStkClear").addEventListener("click", stkClear);
  // 复盘日记本：按天存档，编辑区未保存时切日期/离开页面都要先确认
  $("#btnJnNew").addEventListener("click", jnNew);
  $("#btnJnSave").addEventListener("click", jnSave);
  $("#btnJnDelete").addEventListener("click", jnDelete);
  $("#btnJnFormat").addEventListener("click", jnFormat);
  $("#jnView").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    JN.view = b.dataset.v || "preview";
    jnPaint();
    if (JN.view === "edit") $("#jnText").focus();
  });
  $("#jnDay").addEventListener("change", (e) => jnOpen(e.target.value));
  $("#jnList").addEventListener("click", (e) => {
    const b = e.target.closest(".jn-row");
    if (b) jnOpen(b.dataset.day);
  });
  $("#jnText").addEventListener("input", () => {
    JN.dirty = ($("#jnText").value || "") !== JN.text;
    jnPaint();
  });
  $("#jnText").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "s") {
      e.preventDefault();
      jnSave();
    }
  });
  window.addEventListener("beforeunload", (e) => {
    if (!JN.dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });
  $("#rtHealthMode").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (b) rtSetMode("health", b.dataset.mode);
  });
  $("#btnRtHealthGen").addEventListener("click", () => rtGenerate("health", false));
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-llm-cfg]");
    if (!b) return;
    e.preventDefault();
    goHub("ai");
    setTimeout(() => {
      const f = $("#hubLlmUrl");
      if (f) { f.scrollIntoView({ block: "center" }); f.focus(); }
    }, 80);
  });
  const autoChk = $("#rtAutoSync"), autoTime = $("#rtAutoTime");
  const pushAuto = async () => {
    const r = await post("/api/emt/config", {
      auto_sync: !!(autoChk && autoChk.checked),
      auto_time: (autoTime && autoTime.value) || "15:35"
    });
    const m = $("#rtAutoMsg");
    if (m) m.textContent = (r && r.ok) ? ("已保存：" + (r.auto_sync ? "每个交易日 " +
      r.auto_time + " 自动同步" : "已关闭自动同步")) : ("保存失败：" + ((r && r.error) || ""));
    toast(r && r.ok ? "自动同步设置已保存" : "保存失败", (r && r.ok) ? "ok" : "err");
  };
  if (autoChk) autoChk.addEventListener("change", pushAuto);
  if (autoTime) autoTime.addEventListener("change", pushAuto);

  const rfChk = $("#rtAutoRefresh"), rfMin = $("#rtRefreshMin");
  const pushRefresh = async () => {
    const r = await post("/api/emt/config", {
      auto_refresh: !!(rfChk && rfChk.checked),
      refresh_min: Number((rfMin && rfMin.value) || 5) || 5
    });
    if (r && r.ok) {
      rtRefreshPaint(r);
      toast(r.auto_refresh ? ("已开启盘中自动刷新：每 " + r.refresh_min + " 分钟一次")
        : "已关闭盘中自动刷新", "ok");
    } else {
      toast("保存失败：" + ((r && r.error) || "未知原因"), "err");
    }
  };
  if (rfChk) rfChk.addEventListener("change", pushRefresh);
  if (rfMin) rfMin.addEventListener("change", pushRefresh);

  // 账户页每 30 秒看一次状态：只轮询本地 /api/emt/status，不碰券商网关；
  // 发现后端盘中刷新写入了新快照，再重读账本，避免页面上的数字停在打开那一刻。
  if (!RT.timer) {
    RT.timer = setInterval(async () => {
      if (document.hidden || document.body.dataset.page !== "real") return;
      const st = await api("/api/emt/status");
      if (!st || st.ok === false) return;
      rtPaintStatus(st);
      rtRefreshPaint(st);
      if (st.last_sync && RT.syncSeen && st.last_sync !== RT.syncSeen && !RT.busy) {
        await rtLoad();
      } else if (!RT.syncSeen) {
        RT.syncSeen = st.last_sync || "";
      }
    }, 30000);
  }
  $("#btnApply").addEventListener("click", () => {
    if (!S.best) return;
    $("#strategy").value = "ma_cross"; renderParams();
    const fi = $('#paramBox input[data-k="fast"]'), si = $('#paramBox input[data-k="slow"]');
    if (fi) fi.value = S.best.fast; if (si) si.value = S.best.slow;
    // 只切页：ensureTabData 会按参数指纹发现变了并自动重跑。
    // 以前这里再显式调一次 runBacktest，等于一次点击发两个回测请求。
    switchTab("overview");
  });
  window.addEventListener("resize", () => {
    Object.values(S.charts).forEach((c) => c.resize());
    syncSideCollapse();
  });
}

async function init() {
  $("#end").value = todayStr();
  $("#start").value = "2022-01-01";
  scrLoadPrefs();
  if (!$("#scrTrainStart").value) $("#scrTrainStart").value = "2022-01-01";
  if (!$("#scrTrainEnd").value) $("#scrTrainEnd").value = todayStr();
  // 首屏要发十几路请求。以前是串行 await，总耗时等于各段相加；
  // 互不依赖的请求并行发出，首屏感知快一个量级。
  // 只有「渲染顺序有依赖」的才保留 await：先 status（决定模型/LLM 胶囊），
  // 再 screenStatus（依赖它设的标志），最后 screenRun。
  const stP = api("/api/status");
  const poolP = loadPool(true);      // 首次打开强制用股票池里「设为默认」的那只
  bind(); renderParams();
  initTerms();            // 术语气泡 + 术语表：正文只留结论，解释按需展开
  bulkStatus();           // 有下载任务在跑时，任何页签都能看到顶部徽标与进度
  sideTodoLoad();         // 左下角待办先加载，等 screenStatus 回来会补上行业口径
  sideTodoWatch();        // 每 2 分钟 / 回到前台时对一次，避免待办停在旧结论
  // 注意：不要在这里调 pipeLoad() —— screenStatus() 内部已经调了，
  // 两处都调会让 freshness/hub/universe 各多请求一轮（实测 2 次变 4 次）。
  const hashTab = (location.hash || "").replace("#", "");
  if (hashTab && TAB_TITLES[hashTab] && hashTab !== "screen") switchTab(hashTab);  // 支持 #ind 直链
  // #hub-sources 这种直链：进中枢并直接切到对应标签（数据源页要能单独分享/截图）
  const hubDeep = /^hub-(chain|screen|backtest|sources|real|ai|logs)$/.exec(hashTab);
  if (hubDeep) { switchTab("hub"); hubTab(hubDeep[1]); }

  const st = await stP;
  if (st.ok) {
    const c = st.config || {};
    $("#capital").value = c.capital || 200000;
    $("#commission").value = num((c.commission || 0.00025) * 1000, 3);
    $("#stamp_tax").value = num((c.stamp_tax || 0.0005) * 1000, 3);
    $("#slippage").value = num((c.slippage || 0.0005) * 1000, 3);
  }
  await poolP;
  updateStatus(st);       // 复用首次 /api/status 响应，不再二次请求
  await screenStatus();   // 三层状态 / 行业覆盖度 / 模块清单
  // 首屏优先恢复上一次落盘的候选（几十毫秒），不重建全市场快照。
  // 数据是否过期由「重新选股」的彩虹流光提示；只有完全没有落盘记录时才自动算一次，
  // 避免新用户打开是空表。
  const restored = await screenResume();
  if (!restored) await screenRun(true);
  // 后验要重放 200 只股票的历史各期（服务端首次约 6 秒），排到首屏渲染之后再跑，
  // 否则会和候选榜抢同一份 CPU，反而让「打开就卡住」。之后再点开是秒回（服务端有缓存）。
  setTimeout(() => screenReview(null, true), 2500);
}

document.addEventListener("DOMContentLoaded", init);
