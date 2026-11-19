/* QuantDesk frontend - no build step, plain JS + local ECharts */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const S = { charts: {}, last: null, best: null };

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

/* ---------------- backtest ---------------- */
async function runBacktest(extra) {
  const b = payload(extra);
  loading(true, "正在获取行情并回测 " + b.code + " …");
  $("#btnRun").disabled = true;
  const r = await post("/api/backtest", b);
  $("#btnRun").disabled = false;
  loading(false);
  if (!r.ok) { toast(r.error || "回测失败", "err"); return; }
  S.last = r;
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
async function runScan() {
  const b = payload();
  const fast = $("#scanFast").value.split(",").map((x) => Number(x.trim())).filter((x) => x > 0);
  const slow = $("#scanSlow").value.split(",").map((x) => Number(x.trim())).filter((x) => x > 0);
  if (!fast.length || !slow.length) { toast("参数网格格式有误", "err"); return; }
  if (fast.length * slow.length > 120) { toast("网格最多 120 组，请减少参数个数", "err"); return; }
  loading(true, "正在扫描 " + fast.length * slow.length + " 组参数…");
  $("#btnScan").disabled = true;
  const r = await post("/api/scan", {
    code: b.code, start: b.start, end: b.end,
    capital: b.capital, commission: b.commission,
    stamp_tax: b.stamp_tax, slippage: b.slippage,
    grid: { fast: fast, slow: slow }
  });
  $("#btnScan").disabled = false;
  loading(false);
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
   统一由 /api/screen/run 的基准分模式承接，后端 /api/factors 与 quant.factor_rank 一并删除。 */

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
    '</td><td><span class="pill ' + (s.cached ? "yes" : "no") + '">' + (s.cached ? "已缓存" : "未缓存") + "</span></td>" +
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
  loading(true, "正在向东方财富官方接口同步自选股…");
  const r = await post("/api/account/sync", { mode: $("#acctMode").value });
  loading(false);
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
  $("#uniMeta").innerHTML = "全市场 <b>" + r.total + "</b> 只 · 已缓存行情 <b>" + r.cached +
    "</b> 只 · 名单更新 " + r.updated + " · 当前筛选 " + r.filtered + " 只" +
    (r.fresh ? "" : ' <span style="color:var(--amber)">（非当日，建议刷新）</span>');
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
    loading(true, "正在加入 " + codes.length + " 只…");
    const r = await post("/api/watchlist", { action: "import", mode: mode, text: rows.join("\n") });
    loading(false);
    if (!r.ok) { toast(r.error || "加入失败", "err"); return; }
    UNI.checked.clear();
    await loadPool(); updateStatus(); await uniLoad();
    toast("已加入 " + r.added + " 只，股票池共 " + r.total + " 只", "ok");
    return;
  }
  const doApply = async () => {
    const p = uniParams();
    loading(true, "正在写入股票池…");
    const r = await post("/api/universe/apply", { mode: mode, q: p.q, market: p.market,
                                                 cached_only: $("#uniCached").checked, cap: 6000 });
    loading(false);
    if (!r.ok) { toast(r.error || "操作失败", "err"); return; }
    await loadPool(); updateStatus(); await uniLoad();
    toast("已" + (mode === "replace" ? "替换" : "加入") + "，股票池现有 " + r.count + " 只", "ok");
  };
  if (mode === "replace") return twoStep($("#btnUniReplace"), "确认替换", doApply);
  return doApply();
}
async function uniRefresh() {
  twoStep($("#btnUniRefresh"), "确认刷新", async () => {
    loading(true, "正在从公开行情源拉取全 A 股名单…");
    const r = await post("/api/universe/refresh", {});
    loading(false);
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
  const prog = $("#bulkProg"), chip = $("#chipBulk");
  const total = s.total || 0, done = s.done || 0;
  const pct = total ? Math.min(100, Math.round(done / total * 100)) : 0;
  const active = !!s.running;
  const hasRun = total > 0 && (active || done > 0);

  if (prog) {
    prog.hidden = !hasRun;
    prog.classList.toggle("run", active);
    prog.classList.toggle("done", !active);
    if (hasRun) {
      const stopped = !active && (s.message || "").indexOf("停止") >= 0;
      $("#bpTitle").textContent = (s.mode === "full" ? "全量补全" : "增量更新") +
        (active ? " · 进行中" : (stopped ? " · 已停止" : " · 已完成"));
      $("#bpPct").textContent = pct + "%";
      $("#bpFill").style.width = pct + "%";
      const parts = [
        "进度 " + done + " / " + total + " 只",
        "成功 " + (s.ok_count || 0), "跳过 " + (s.skip || 0), "失败 " + (s.fail || 0),
        "新增 " + (s.rows || 0) + " 行",
      ];
      if (s.workers > 1) parts.push(s.workers + " 进程");
      if (active && s.rate > 0) parts.push("约 " + s.rate.toFixed(2) + " 只/秒");
      if (active && s.eta > 0) parts.push("剩余约 " + fmtDur(s.eta));
      if (active && s.last) parts.push("当前 " + s.last);
      let meta = parts.join(" · ");
      if (!active && s.last_err) meta += " · " + s.last_err;
      $("#bpMeta").textContent = meta;
    }
  }
  if (chip) {
    chip.hidden = !active;
    if (active) chip.textContent = "行情下载 " + pct + "%";
  }
  const el = $("#bulkStatus");
  if (el) {
    el.textContent = (active ? "任务运行中，切到别的页签进度也不会丢（顶部有徽标）"
      : (s.message || "空闲")) +
      " · 已缓存 " + (s.cached_total || 0) + " / " + (s.universe_total || 0) +
      " 只（" + (s.cover || 0) + "%）";
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
  loading(true, "正在刷新全市场实时价（腾讯批量）…");
  const r = await post("/api/spot", { action: "refresh" });
  loading(false);
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
  journalShown: "", ai: null, aiBusy: { health: false, journal: false }, aiTried: {} };

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

function rtJournalPaint(ai, b) {
  const ta = $("#rtJournal");
  const j = ai.journal || {};
  const modes = ai.modes || {};
  const mode = modes.journal || "local";
  const localText = ai.local_journal || "";

  // 盘中刷新会周期性重读账本：只有内容还停在“上次载入的已保存版本”时才覆盖，
  // 用户改了但没保存的草稿不能被自动刷新冲掉。
  if (ta) {
    const cur = ta.value || "", prev = RT.journalShown || "";
    if (cur === prev) {
      let next = j.text || "";
      if (!next && mode === "local") next = localText;
      ta.value = next;
      RT.journalShown = j.text || "";
    }
  }

  const note = $("#rtJNote");
  if (note) {
    let html = "";
    if (mode === "local") {
      if (j.src === "ai") {
        html = "<b>本地模板</b> · 当前显示的是已入库 AI 稿；" +
          "点「生成本地模板」会用本地规则覆盖并保存。";
      } else if (j.src === "manual") {
        html = "<b>本地模板</b> · 当前是手写稿，本地模板不会自动覆盖；" +
          "要替换请点「生成本地模板」后确认。";
      } else if (j.src === "local") {
        html = "<b>本地模板</b> · 已入库，只复述账本事实与规则命中，不走网络。";
      } else {
        html = "<b>本地模板</b> · 当前是未保存草稿，点「保存日记」才会入库。";
      }
    } else if (j.src === "ai") {
      html = "<b>AI 撰写</b>" + (j.model ? " · " + esc(j.model) : "") +
        (j.at ? " · " + esc(j.at) : "") +
        " · 已入库，可直接编辑后保存为手写稿。";
    } else if (j.src === "local") {
      html = "<b>AI 撰写</b> · 当前是本地模板稿；点「AI 生成」用模型重写。";
    } else if (j.src === "manual") {
      html = "<b>手写日记</b> · " + esc(j.at || "") +
        " · AI 不会自动覆盖；要替换请点「AI 生成」并确认。";
    } else if (!(ai.llm || {}).ready) {
      html = '<span class="err">LLM 未启用或未配置</span>，日记暂用本地模板。' + rtLlmLink();
    } else {
      html = "<b>AI 撰写</b> · 尚未生成；收盘日结后会自动生成，也可以点「AI 生成」。";
    }
    note.innerHTML = html;
  }

  const btn = $("#btnRtGenJ");
  if (btn) {
    btn.textContent = mode === "local" ? "生成本地模板" : (j.src === "ai" ? "重新生成" : "AI 生成");
    btn.disabled = RT.aiBusy.journal;
  }
  const jd = $("#rtJDay");
  if (jd) {
    jd.textContent = b.date ? "账本最近交易日 " + b.date + (b.at ? " · 快照 " + b.at : "")
      : "等待第一个账本日";
  }
}

function rtAiRender(b) {
  const ai = b.ai || {};
  RT.ai = ai;
  const modes = ai.modes || { journal: "local", health: "local" };
  rtSegPaint("#rtHealthMode", modes.health || "local");
  rtSegPaint("#rtJMode", modes.journal || "local");

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

  rtJournalPaint(ai, b);
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
    const jsrc = (ai.journal || {}).src || "";
    if (modes.journal === "ai" && jsrc !== "ai" && jsrc !== "manual" &&
        !RT.aiBusy.journal && !RT.aiTried[day + "journal"]) {
      RT.aiTried[day + "journal"] = true;
      setTimeout(() => rtGenerate("journal", true), 700);
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
  rtSegPaint("#hubAiJournal", m2.journal || "local");
  if (mode === "ai") {
    if ((r.llm || {}).ready) {
      if (kind === "health" && !ai.health) rtGenerate("health", true);
      const jsrc = (ai.journal || {}).src || "";
      if (kind === "journal" && jsrc !== "ai" && jsrc !== "manual") rtGenerate("journal", true);
    } else {
      toast("LLM 未启用：先到「数据中枢 → AI 服务」填端点并启用", "info");
    }
  }
}

async function rtGenerate(kind, auto) {
  if (!RT.data || RT.data.empty) { toast("还没有账本，先同步一次", "err"); return; }
  if (RT.aiBusy[kind]) return;
  const ai = RT.ai || {};
  const modes = ai.modes || {};
  const mode = modes[kind] || "local";
  const btn = kind === "health" ? $("#btnRtHealthGen") : $("#btnRtGenJ");
  const oldText = btn ? btn.textContent : "";
  let force = false;

  if (kind === "journal") {
    const j = ai.journal || {};
    const ta = $("#rtJournal");
    const dirty = ta && (ta.value || "") !== (RT.journalShown || "");
    const need = dirty || j.src === "manual";
    if (need) {
      const ok = window.confirm("生成会覆盖账本里的当前日记" +
        (dirty ? "（包括未保存的编辑）" : "") + "，继续吗？");
      if (!ok) return;
      force = true;
    }
  }

  RT.aiBusy[kind] = true;
  if (btn) { btn.disabled = true; btn.textContent = "生成中…"; }
  try {
    const r = await post("/api/emt/ai/generate", { kind: kind, day: (RT.data.date || ""), force: force });
    if (!r || !r.ok) {
      toast((r && r.error) || "生成失败", "err");
      return;
    }
    if (r.busy) { toast(r.error || "正在生成，请稍候", "info"); return; }

    if (kind === "health") {
      if (r.mode === "ai" && (r.items || []).length) {
        ai.health = { items: r.items, summary: r.summary || "", data_note: r.data_note || "",
          model: r.model || "", at: r.at || "" };
        toast(auto ? "AI 体检已生成" : "AI 体检完成", "ok");
      } else {
        ai.health = null;
        toast(r.warning || "AI 不可用，已回退本地规则", auto ? "info" : "info");
      }
    } else {
      if (r.mode === "manual" || (r.warning && r.saved === false && r.conflict)) {
        toast(r.warning || "已有手写日记，未覆盖", "info");
      } else if (r.text) {
        const ta = $("#rtJournal");
        if (r.saved === false && r.mode !== "local") {
          toast(r.warning || "AI 稿未覆盖手写日记", "info");
        } else {
          if (ta) ta.value = r.text;
          ai.journal = { text: r.text, src: r.mode, at: r.at || "", model: r.model || "" };
          if (RT.data) RT.data.journal = r.text;
          RT.journalShown = r.text;
          toast(r.mode === "ai" ? (auto ? "AI 日记已生成" : "AI 日记已更新")
            : "本地模板已生成", "ok");
        }
      }
    }
    if (RT.data) RT.data.ai = ai;
  } catch (e) {
    toast("生成失败：" + e, "err");
  } finally {
    RT.aiBusy[kind] = false;
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
  loading(true, "正在登录东财并拉取资金与持仓…");
  const r = await post("/api/emt/sync", {});
  loading(false);
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
  loading(true, "正在按月拉取历史数据，首次可能要一两分钟…");
  const r = await post("/api/emt/history", { start: start, end: end, size: 500, what: what });
  loading(false);
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
function rtCfgClose() {}
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
async function rtSaveJournal() {
  const day = (RT.data && RT.data.date) || "";
  if (!day) { toast("还没有账本日，先同步一次", "err"); return; }
  const text = $("#rtJournal").value || "";
  const r = await post("/api/emt/journal", { day: day, text: text });
  if (!r || !r.ok) { toast((r && r.error) || "保存失败", "err"); return; }
  const m = $("#rtJMsg");
  if (m) m.textContent = "已保存到 " + day + "（手写）";
  if (RT.data) {
    RT.data.journal = text;
    if (RT.data.ai) {
      RT.data.ai.journal = { text: text, src: "manual", at: r.journal_at || "", model: "" };
    }
  }
  RT.journalShown = text;
  rtAiRender(RT.data || {});
  toast("日记已保存", "ok");
}

/* ---------------- 复盘日记：一天一篇 Markdown ---------------- */
const JN = {
  loaded: false, entries: [], day: "", text: "", src: "", at: "", dir: "",
  dirty: false, busy: false, loading: false
};
const JN_SRC_LABEL = { manual: "手写", ai: "AI 稿", local: "本地模板" };

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
  const chars = $("#jnChars");
  if (chars) chars.textContent = ($("#jnText").value || "").length.toLocaleString("zh-CN") + " 字";
  const dirty = $("#jnDirty");
  if (dirty) dirty.textContent = JN.dirty ? "未保存" : "";
  const save = $("#btnJnSave");
  if (save) { save.disabled = JN.busy; save.textContent = JN.busy ? "保存中…" : "保存"; }
  const del = $("#btnJnDelete");
  if (del) del.disabled = JN.busy || !exists;
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
  $("#jnDay").value = today;
  $("#jnText").value = "";
  jnRenderList();
  jnPaint();
  $("#jnText").focus();
}
function goJournal(day) {
  if (day && /^\d{4}-\d{2}-\d{2}$/.test(String(day))) JN.day = String(day).slice(0, 10);
  switchTab("journal");
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
function scrLoadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(SCR_PREF_KEY) || "{}");
    SCR_PREF_IDS.forEach((id) => {
      const el = document.getElementById(id);
      if (!el || p[id] == null) return;
      if (el.type === "checkbox") el.checked = !!p[id];
      else el.value = p[id];
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
    topn: scrNum("#scrTopN", 10),
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
    max_symbols: scrNum("#scrMaxSymbols", 300),
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
  const lm = r.dataset.label_mode === "rank" ? "分位标签"
    : r.dataset.label_mode === "industry" ? "行业内超额" : "全市场超额";
  const kv = [
    ["行情", pipeDate(r.as_of) + " · 缓存 " + r.quote_cached + " 只",
      r.quote_blocked ? "限流·快照补"
        : r.quote_source && r.quote_source.indexOf("spot") === 0 ? "快照补日线"
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
  sideTodoLoad();      // 左下角待办：行情/快照/训练集/模型/行业缺什么
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
  return '<tr class="scr-detail-row" data-detail="' + x.code + '"><td colspan="10">' +
    '<div class="scr-detail-grid">' + bits +
    "<div>板块：<b>" + esc(x.market || "—") + "</b></div>" +
    "<div>行业：<b>" + esc(x.industry || "待补齐") + "</b></div>" +
    "<div>行业内排名：<b>" + esc(rk) + "</b></div>" +
    "<div>基准分：<b>" + scrFmt(x.base_score, 3) + "</b></div>" +
    "<div>数据日：<b>" + esc(x.date || "—") + "</b></div></div>" +
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
  $("#scrBody").innerHTML = rows.map((x) => {
    const detail = SCR.detail.has(x.code) ? scrDetailHtml(x) : "";
    const pctCell = x.pct == null ? "—" : signed(x.pct);
    return "<tr><td><span class='rank'>" + (x.rank_llm || x.rank) + "</span></td>" +
      '<td><button type="button" class="codelink" data-code="' + x.code +
      '" aria-label="展开 ' + esc(x.name) + ' 的因子详情">' + x.code + "</button></td>" +
      "<td>" + esc(x.name) + "</td>" + indCell(x) +
      "<td title=\"" + (x.close_real != null && Math.abs(x.close_real - x.close) > 0.01
        ? "真实价；后复权口径 " + scrFmt(x.close)
        : "选股日收盘价") + "\">" + scrFmt(x.close_real == null ? x.close : x.close_real) + "</td>" +
      '<td class="' + (x.pct == null ? "" : signCls(x.pct)) + '">' + pctCell + "</td>" +
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
      loading(true, "正在构建横截面并打分…");
      $("#scrSampleN").textContent = "…";
      $("#scrHeadline").textContent = "正在用本地行情生成横截面候选…";
    }
    const p = scrRunParams();
    if (silent) {
      p.reuse_snapshot = true;   // 首屏优先复用；后端缺快照时自动构建
      // 首屏自动出数是「展示用」的，不能写进选股记录：否则每刷新一次页面就把当期的
      // 历史记录覆盖成这次自动跑的结果，用户手动选的 20 只被悄悄换成 10 只。
      p.record = false;
    }
    let r;
    try { r = await post("/api/screen/run", p); }
    finally { if (!silent) loading(false); }
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
    // 复用当期截面时说明一句：数字是现算的，只是没重新扫全市场原始 K 线
    if (r.snapshot_reused) tg.push("复用本期截面");
    tagEl.textContent = tg.join(" · ");
    tagEl.classList.toggle("warn", !!r.is_historical);
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
    loading(true, "正在生成横截面快照…");
    let r;
    try { r = await post("/api/screen/snapshot", { scope: p.scope, as_of: p.as_of, limit: p.limit }); }
    finally { loading(false); }
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
    loading(true, "正在构造多期横截面训练集…");
    let r;
    try {
      r = await post("/api/screen/dataset", {
        start: d.start, end: d.end, horizon: p.horizon, step: p.step,
        max_symbols: p.max_symbols, scope: p.scope,
        neutral: neutralForTrain, label_mode: p.label_mode
      });
    } finally { loading(false); }
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
    loading(true, "正在训练 LightGBM（时序切分验证）…");
    let r;
    try { r = await post("/api/screen/train", {}); }
    finally { loading(false); }
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
  // 恢复上次重排：数据日和候选集完全一致才套用，分数/理由取存档，价格用本次最新候选
  try {
    const s = await post("/api/screen/llm", { action: "last" });
    const sv = s && s.saved;
    if (!sv || !sv.items || sv.as_of !== asOf || !SCR.candidates.length) return;
    const codes = new Set(SCR.candidates.map((x) => x.code));
    if (sv.items.length !== codes.size || !sv.items.every((x) => codes.has(x.code))) return;
    const m = new Map(sv.items.map((x) => [x.code, x]));
    const rows = SCR.candidates.map((x) => Object.assign({}, x, m.get(x.code)));
    rows.sort((a, b) => (a.rank_llm || 999) - (b.rank_llm || 999));
    SCR.llmRows = rows; SCR.llmMode = sv.mode;
    renderScrRows();
    const isLlm = sv.mode === "llm";
    scrLlmBadge((isLlm ? "LLM · " + (sv.model || "") : "本地规则解释") + " · 已恢复", isLlm);
    $("#scrScoreNote").textContent = (SCR.noteBase || "") + " · " +
      (isLlm ? "LLM 重排" : "本地规则解读") + "（" + esc(String(sv.saved_at || "").slice(5, 16)) + " 存档）";
  } catch (e) { /* 恢复失败不影响候选展示 */ }
}
async function screenLlm(btn) {
  if (!SCR.candidates.length) { toast("请先点「重新选股」生成候选", "err"); return; }
  await withBusy(btn, async () => {
    if (btn) btn.textContent = "解读中…";
    loading(true, "正在生成候选解读 / 重排…");
    let r;
    try {
      r = await post("/api/screen/llm", {
        action: "rerank", candidates: SCR.candidates,
        as_of: (SCR.last && SCR.last.as_of) || "", horizon: scrNum("#scrHorizon", 10)
      });
    } finally { loading(false); }
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
  if (!box) return;
  const stages = (fn && fn.stages) || [];
  if (!stages.length) { box.innerHTML = ""; return; }
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
  const capNote = (ind && ind.note) ? ind.note : "";
  const dist = (ind && ind.counts) ? Object.keys(ind.counts).length : 0;
  box.innerHTML = '<div class="scr-fn-track">' + arrows.join("") + "</div>" +
    '<p class="scr-fn-note">' + esc((fn && fn.dropped ? ("共剔除 " + fn.dropped + " 只 · ") : "") +
      capNote + (dist ? (" · 覆盖 " + dist + " 个行业") : "")) + "</p>";
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
  const maxW = Math.max.apply(null, rows.map((r) => r.weight).concat([1]));
  const body = rows.map((r) => {
    const bw = r.base_weight;
    const bm = (bw == null || maxW <= 0) ? "" :
      '<span class="bm" style="left:' + Math.min(100, bw / maxW * 100).toFixed(1) +
      '%" title="全市场基线 ' + scrFmt(bw, 1) + '%"></span>';
    return '<div class="scr-exp-row"><span class="nm">' + esc(r.industry) + "</span>" +
      '<span class="tr"><span class="fi" style="width:' + (r.weight / maxW * 100).toFixed(1) + '%"></span>' +
      bm + "</span>" +
      '<span class="vl">' + scrFmt(r.weight, 1) + "%</span>" +
      '<span class="bs">基线 ' + (bw == null ? "—" : scrFmt(bw, 1) + "%") + "</span></div>";
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
  if (hint) hint.textContent = "橙线 = 全市场基线";
}

function screenReview(btn, silent) {
  const work = async () => {
    const box = $("#scrReview");
    if (!silent) box.innerHTML = '<p class="note" style="margin:0">正在重放历史各期…</p>';
    let r;
    try { r = await api("/api/screen/review?days=60&step=5&topn=" + scrNum("#scrTopN", 10)); }
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
function pkPending(b) { return !b.last_date || b.last_date <= b.as_of; }
function pkKey(b) { return b.as_of + "|" + b.recorded_at; }

/* 左栏一行＝一次选股。做成对齐的窄表而不是卡片：垂直扫读时数字能直接上下比。 */
function pkRow(b, i, on) {
  const hz = b.horizons || {};
  const pending = pkPending(b);
  const side = pending ? "" : (b.avg_chg > 0 ? " up" : b.avg_chg < 0 ? " down" : "");
  const dataDay = String(b.as_of || "").slice(0, 10);
  const recAt = String(b.recorded_at || "");
  const tip = "数据日 " + dataDay + " · 选股日 " + recAt + " · " + b.count + " 只";
  return '<button type="button" class="pk-row' + (on ? " on" : "") + side + '" data-i="' + i +
    '" title="' + esc(tip) + '">' +
    '<span class="pk-c1">' + esc(dataDay.slice(5)) + "</span>" +
    '<span class="pk-c2">' + esc(recAt ? recAt.slice(5, 10) : "—") + "</span>" +
    '<span class="pk-c3">' + b.count + "</span>" +
    '<span class="pk-c4">' + (pending ? '<span class="pk-na">待更新</span>' : pkPct(b.avg_chg)) + "</span>" +
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
  const pending = pkPending(b);
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
  const hzK = (h, name) => {
    const v = hz[String(h)];
    if (!v) return kpi(name, '<span class="pk-na">—</span>', "未到期");
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
    const tr = '<tr class="rowlink pk-line' + (open ? " on" : "") + '" data-code="' + esc(x.code) +
      '" data-name="' + esc(x.name || "") + '"><td class="pk-rk">' +
      (x.rank == null ? '<span class="pk-na">—</span>' : x.rank) + "</td>" +
      '<td><button class="pk-link" title="展开当日分时">' + esc(x.code) + "</button></td>" +
      "<td>" + esc(x.name) + "</td>" +
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
    return tr + '<tr class="pk-cr"><td colspan="11"><div class="pk-ch">' +
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
      ? '<p class="pk-bars-none">这批记录后还没有新行情，涨跌与 T+N 都停在记录日 —— 等下一个交易日收盘后再来看。</p>'
      : '<div class="pk-bars" aria-hidden="true">' + bars + "</div>") +
    '<div class="pk-tbl"><table><thead><tr>' +
    '<th class="pk-rk">#</th><th>代码</th><th>名称</th><th class="pk-r">涨跌</th><th class="pk-opt3">行业</th>' +
    '<th class="pk-r pk-opt">分数</th><th class="pk-r pk-opt">记录价</th><th class="pk-r pk-opt2">最新价</th>' +
    '<th class="pk-r">T+5</th><th class="pk-r">T+10</th><th class="pk-r">T+20</th>' +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>" +
    '<p class="pk-foot">价格按本地缓存的后复权序列计算，与行情软件显示的真实价可能不同。' +
    (b.legacy ? "该批记录时的价格口径较早（未复权），已统一重算。" : "") +
    "T+N 需等后续交易日补齐，未到期显示 —。点任意一行展开当日分时。</p>";
  det.scrollTop = 0;
  if (PK.open) pkChartLoad(PK.open);
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
  const matured = bs.filter((b) => Object.keys(b.horizons || {}).length).length;
  $("#pkSum").innerHTML = bs.length
    ? "共 <b>" + (r.total_batches || bs.length) + "</b> 次选股 · <b>" + (r.total_rows || 0) +
      "</b> 条候选 · <b>" + matured + "</b> 次已可评估" +
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
  ["model", "模型与统计"], ["risk", "组合与风险"]
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
    d: "训练时喂给模型的「正确答案」。本系统用未来 N 日收益：从 T+1 收盘算到 T+1+N 收盘 —— T 日收盘后才打的分数，最早 T+1 才能成交，所以不把隔夜跳空算进去。"
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
  label: { n: "标签", c: "model", d: "训练时让模型预测的目标。这里是未来 N 日收益减去同期基准，即超额收益。" },
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
  fifo: { n: "FIFO", c: "market", d: "先进先出：卖出时按最早买入的那批成本结算盈亏，是券商交割单的常见口径。" }
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
  const t = TERMS[el.dataset.term];
  if (!t) return;
  const pop = termPop();
  pop.innerHTML = '<span class="tp-name">' + esc(t.n) +
    '<span class="tp-cat">' + esc(TERM_CAT_NAME[t.c] || "") + "</span></span>" +
    esc(t.d) + (t.f ? '<div class="tm-formula">' + esc(t.f) + "</div>" : "");
  pop.classList.add("on");
  const r = el.getBoundingClientRect();
  const pr = pop.getBoundingClientRect();
  const left = Math.max(8, Math.min(window.innerWidth - pr.width - 8,
    r.left + r.width / 2 - pr.width / 2));
  let top = r.top - pr.height - 8;
  if (top < 8) top = Math.min(window.innerHeight - pr.height - 8, r.bottom + 8);
  pop.style.left = Math.round(left) + "px";
  pop.style.top = Math.round(top) + "px";
  if (_termCur && _termCur !== el) _termCur.classList.remove("on");
  _termCur = el;
  el.classList.add("on");
}
function hideTerm(force) {
  if (_termPin && !force) return;
  const pop = termPop();
  pop.classList.remove("on");
  if (_termCur) _termCur.classList.remove("on");
  _termCur = null;
}
function initTerms() {
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest && e.target.closest(".term");
    if (el && !_termPin) showTerm(el);
  });
  document.addEventListener("mouseout", (e) => {
    const el = e.target.closest && e.target.closest(".term");
    if (el && !_termPin) hideTerm();
  });
  document.addEventListener("click", (e) => {
    const el = e.target.closest && e.target.closest(".term");
    if (el) {
      e.preventDefault();
      if (_termCur === el && _termPin) { _termPin = false; hideTerm(true); return; }
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
function openTermModal() {
  $("#termMask").hidden = false;
  $("#termModal").hidden = false;
  $("#termQ").value = "";
  renderTermList("");
  setTimeout(() => $("#termQ").focus(), 40);
}
function closeTermModal() {
  const m = $("#termModal");
  if (!m || m.hidden) return;
  m.hidden = true;
  $("#termMask").hidden = true;
}
function renderTermList(q) {
  const key = String(q || "").trim().toLowerCase();
  const hit = Object.entries(TERMS).filter(([, t]) =>
    !key || t.n.toLowerCase().includes(key) || (t.d || "").toLowerCase().includes(key));
  if (!hit.length) { $("#termList").innerHTML = '<p class="hint">没有匹配的术语。</p>'; return; }
  $("#termList").innerHTML = TERM_CATS.map(([ck, cn]) => {
    const items = hit.filter(([, t]) => t.c === ck);
    if (!items.length) return "";
    return '<div class="tm-group"><h4>' + esc(cn) + "</h4>" + items.map(([, t]) =>
      '<div class="tm-item"><b>' + esc(t.n) + "</b><p>" + esc(t.d) + "</p>" +
      (t.f ? '<div class="tm-formula">' + esc(t.f) + "</div>" : "") + "</div>").join("") + "</div>";
  }).join("");
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
const HUB_TABS = ["chain", "screen", "backtest", "sources", "real", "ai", "runs"];

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
  if (name === "runs") api("/api/data/runs?limit=60").then((r) => hubRuns((r || {}).runs));
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
      ? ("LLM 已就绪（" + (llm.model || "—") + "）。AI 模式在收盘日结后自动生成，也可在实盘复盘页手动触发。")
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
  const lm = r.dataset.label_mode === "rank" ? "分位标签"
    : r.dataset.label_mode === "industry" ? "行业内超额" : "全市场超额";
  const kv = [
    ["行情", pipeDate(r.as_of) + " · 缓存 " + r.quote_cached + " 只",
      r.quote_blocked ? "限流·快照补"
        : r.quote_source && r.quote_source.indexOf("spot") === 0 ? "快照补日线"
        : (r.quote_stale ? "今日未入库" : "最新")],
    ["快照", (r.snapshot.count || 0) + " 只 · " + pipeDate(r.snapshot.as_of), lag(r.snapshot.lag)],
    ["训练集", (r.dataset.rows || 0) + " 行 · " + pipeDate(r.dataset.date_max), lm + " · " + lag(r.dataset.lag)],
    ["模型", r.model.trained
      ? (r.model.age_days == null ? "已训练" : r.model.age_days + " 天前训练") +
        (r.model.ic == null ? "" : " · RankIC " + scrFmt(r.model.ic, 3)) +
        (r.model.ic_ir == null ? "" : " · IR " + scrFmt(r.model.ic_ir, 2))
      : "未训练", r.model.retrain_due ? "建议重训" : "可用"]
  ];
  body.innerHTML = kv.map((p) => '<div class="k">' + p[0] + '</div><div class="v">' +
    esc(p[1]) + ' <span class="pipe-flag">' + esc(p[2]) + "</span></div>").join("");
  const badStep = (run.steps || []).filter((s) => !s.ok)[0];
  if (badStep) {
    body.innerHTML += '<div class="k bad">上次失败</div><div class="v bad">' +
      esc(badStep.name + "：" + (badStep.detail || "未返回原因")) + "</div>";
  }
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

function hubRuns(runs, ids) {
  ids = ids || {};
  const tb = $("#" + (ids.body || "hubRunsBody")), empty = $("#" + (ids.empty || "hubRunsEmpty"));
  if (!tb) return;
  runs = runs || [];
  if (empty) empty.style.display = runs.length ? "none" : "";
  tb.innerHTML = runs.map((r) => {
    const steps = r.steps || [];
    const bad = steps.filter((s) => !s.ok);
    const names = steps.map((s) => s.name).join("、");
    const detail = bad.length ? (bad[0].name + "：" + (bad[0].detail || r.error || "失败"))
      : (steps.length ? (steps[steps.length - 1].detail || "") : (r.error || ""));
    const res = r.ok === null ? '<span class="pipe-pill busy">进行中</span>'
      : r.ok ? '<span class="pipe-pill good">成功</span>'
      : '<span class="pipe-pill warn">失败</span>';
    const line = (names ? "<b>" + esc(names) + "</b>" : "") +
      (detail ? '<small class="hub-detail' + (bad.length ? " bad" : "") + '">' + esc(detail) + "</small>" : "");
    return "<tr><td>" + esc((r.started || "").slice(5, 16)) + "</td><td>" + esc(hubKind(r.kind, r.level)) +
      "</td><td>" + esc(hubTrigger(r.trigger)) + "</td><td>" + res + "</td><td>" + hubMs(r.ms) +
      '</td><td class="hub-steps-cell">' + line + "</td></tr>";
  }).join("");
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

function hubSources(d) {
  const s = d.sources || {};
  const sp = $("#hubSrcPill");
  if (sp) { sp.textContent = s.configured ? "已配置" : "未配置"; sp.className = "pill " + (s.configured ? "yes" : "no"); }
  const tk = $("#hubSrcToken");
  if (tk) tk.placeholder = s.configured ? ("已保存 " + s.token_masked + "，留空表示不修改") : "粘贴 Tushare Token（约 40 位）";
  const ss = $("#hubSrcStatus");
  if (ss) ss.textContent = s.configured
    ? ("估值 " + (s.rows || 0) + " 只 · 交易日 " + (s.last_date || "—") + " · 拉取于 " + (s.last_fetch || "—"))
    : "未配置 Token；填了才会拉 PE/PB/市值";

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
  hubRuns(d.runs);
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
  hubRuns(d.runs);
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
async function hubSrcFetch(btn) {
  await withBusy(btn, async () => {
    loading(true, "正在拉取全市场估值（daily_basic，一次请求）…");
    let r;
    try { r = await post("/api/sources/fetch", { force: $("#hubSrcForce").checked }); } finally { loading(false); }
    toast(r.ok ? (r.message || "已拉取") : ("拉取失败：" + (r.error || "未知错误")), r.ok ? "ok" : "err");
    const s = await api("/api/sources"); if (HUB.data) HUB.data.sources = s; hubSources(HUB.data || {});
    hubRuns((await api("/api/data/runs?limit=40")).runs);
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
    loading(true, "正在测试 LLM 端点连通性…");
    let r;
    try { r = await post("/api/screen/llm", Object.assign({ action: "test" }, hubLlmPayload())); }
    finally { loading(false); }
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

const TAB_TITLES = {
  screen: "选股工作台", picks: "选股记录", overview: "回测总览",
  kline: "行情走势", scan: "参数扫描",
  hub: "数据中枢", ind: "行业与强度",
  pool: "股票池与行情", real: "实盘复盘", journal: "复盘日记", about: "关于"
};
const TAB_GROUPS = {
  screen: "研究", picks: "研究", overview: "验证", kline: "验证", scan: "验证",
  hub: "数据", ind: "数据", pool: "数据", real: "实盘", journal: "实盘", about: "其他"
};
/* 分组键：决定顶栏状态标签与回测参数条是否出现。
   行情走势归在「验证」组里方便就近查看，但它不依赖回测参数，所以单独给 market 键，不弹参数条。 */
const TAB_GROUP_KEY = {
  screen: "screen", picks: "picks", overview: "verify", kline: "market", scan: "verify",
  hub: "data", ind: "data", pool: "data", real: "real", journal: "real", about: "other"
};
const POOL_LOADED = { done: false };
const PK = { loaded: false, data: null, idx: 0, sel: "", sort: "rank", open: "" };
function ensureTabData(name) {
  if (name === "hub") { hubLoad(); hubTab(HUB.tab); }
  if (name === "ind" && !IND.loaded) indLoad();
  if (name === "picks") picksLoad();      // 记录会随每次选股变化，进页面就重读
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
  if (name === "overview" || name === "scan") {
    // 只在首次进入、或标的被改变时才回测；选股页不再预先计算隐藏图表
    const code = ($("#code").value || "").trim();
    if (!S.last || S.last.code !== code) runBacktest();
  }
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
  loading(true, "正在强制刷新行情缓存…");
  const r = await post("/api/backtest", payload({ refresh: true }));
  loading(false);
  if (!r.ok) { toast(r.error || "刷新失败", "err"); return; }
  S.last = r; renderBacktest(r);
  toast("行情已刷新：" + r.source, "ok");
  updateStatus();
}
async function clearCache() {
  const r = await post("/api/cache", { action: "clear" });
  toast(r.ok ? "已清除 " + r.cleared + " 个缓存文件" : (r.error || "清除失败"), r.ok ? "ok" : "err");
  updateStatus();
}
/* ---------------- 侧栏「今日待办」：只提示该更新的数据，点击直达 ---------------- */
const SIDE_TODO = { busy: false };
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
  if (cnt) { cnt.textContent = String(items.length); cnt.className = "sf-count warn"; }
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
async function sideTodoLoad() {
  const list = $("#sfTodoList");
  if (!list || SIDE_TODO.busy) return;
  SIDE_TODO.busy = true;
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
  if (hubOk) {
    const f = hub.freshness || {};
    if (f.ok === false) {
      items.push({ level: "warn", text: f.error || "还没有行情缓存，先去补行情",
        go: "pool", tab: "all", spot: "#btnBulkInc", tip: "先补行情" });
    } else {
      const ACT = {
        quote: { go: "pool", tab: "all", spot: "#btnBulkInc", tip: "点这里增量更新" },
        snapshot: { go: "hub", hub: "screen", spot: "#btnScrSnapshot", tip: "重建快照" },
        dataset: { go: "hub", hub: "screen", spot: "#btnScrDataset", tip: "重建训练集" },
        model: { go: "hub", hub: "screen", spot: "#btnScrTrain", tip: "重训模型" }
      };
      (f.actions || []).forEach((a) => {
        if (a.level !== "warn") return;
        items.push({ level: "warn", text: a.text, ...(ACT[a.key] || { go: "screen" }) });
      });
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
  }
  if (uniOk) {
    // 周末/节假日本来就不会有新名单；用「最新行情日」而不是日历今天判断，
    // 否则周六周日、长假期间会一直挂着「名单不是今天更新的」。
    const dayDigits = (v) => String(v || "").replace(/[^0-9]/g, "").slice(0, 8);
    const dataDay = dayDigits(((hub && hub.freshness) || {}).as_of);
    const uniDay = dayDigits(uni.updated);
    const listBehind = !uni.fresh && !(dataDay && uniDay && uniDay >= dataDay);
    if (listBehind) {
      items.push({ level: "warn", text: "全 A 名单不是今天更新的（" + (uni.updated || "—") + "），点「刷新名单」",
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
  } finally {
    SIDE_TODO.busy = false;
  }
}
async function updateStatus(preloaded) {
  const r = preloaded || await api("/api/status");
  if (!r.ok) return;
  const c = r.config || {};
  $("#chipAk").textContent = "AKShare " + (r.akshare || "—");
  $("#chipAk").className = "chip " + (r.akshare_broken ? "warn" : "good");
  $("#chipCache").innerHTML = "缓存 <b>" + (r.cache_count || 0) + "</b> 只";
  $("#cfgInfo").textContent = "Python " + (r.python || "—") + " · 服务 127.0.0.1:" + (r.port || 8765) +
    " · 配置 " + ((c.capital || 200000) / 10000) + " 万初始资金 / 佣金 " +
    ((c.commission || 0.00025) * 1000) + "‰ / 滑点 " + ((c.slippage || 0.0005) * 1000) + "‰";
}
function bind() {
  $$(".tab").forEach((t) => {
    t.setAttribute("role", "tab");
    t.id = "tab-" + t.dataset.tab;
    t.setAttribute("aria-controls", "panel-" + t.dataset.tab);
    t.setAttribute("aria-selected", t.classList.contains("on") ? "true" : "false");
    t.addEventListener("click", () => {
      switchTab(t.dataset.tab);
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
  if ($("#btnHubRuns")) $("#btnHubRuns").addEventListener("click", async () => {
    hubRuns((await api("/api/data/runs?limit=60")).runs);
  });
  if ($("#btnHubSrcSave")) $("#btnHubSrcSave").addEventListener("click", (e) => hubSrcSave(e.currentTarget));
  if ($("#btnHubSrcTest")) $("#btnHubSrcTest").addEventListener("click", (e) => hubSrcTest(e.currentTarget));
  if ($("#btnHubSrcClear")) $("#btnHubSrcClear").addEventListener("click", (e) => hubSrcClear(e.currentTarget));
  if ($("#btnHubSrcFetch")) $("#btnHubSrcFetch").addEventListener("click", (e) => hubSrcFetch(e.currentTarget));
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
  $("#btnTermsTop").addEventListener("click", openTermModal);
  $("#btnTermClose").addEventListener("click", closeTermModal);
  $("#termMask").addEventListener("click", closeTermModal);
  $("#termQ").addEventListener("input", (e) => renderTermList(e.target.value));
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
  $("#chipBulk").addEventListener("click", () => {
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
  $("#btnRtSaveJ").addEventListener("click", rtSaveJournal);
  $("#btnRtJBook").addEventListener("click", () => goJournal((RT.data && RT.data.date) || ""));
  // 复盘日记本：按天存档，编辑区未保存时切日期/离开页面都要先确认
  $("#btnJnNew").addEventListener("click", jnNew);
  $("#btnJnSave").addEventListener("click", jnSave);
  $("#btnJnDelete").addEventListener("click", jnDelete);
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
  $("#rtJMode").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (b) rtSetMode("journal", b.dataset.mode);
  });
  $("#btnRtHealthGen").addEventListener("click", () => rtGenerate("health", false));
  $("#btnRtGenJ").addEventListener("click", () => rtGenerate("journal", false));
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
    switchTab("overview"); runBacktest();
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
  const st = await api("/api/status");
  if (st.ok) {
    const c = st.config || {};
    $("#capital").value = c.capital || 200000;
    $("#commission").value = num((c.commission || 0.00025) * 1000, 3);
    $("#stamp_tax").value = num((c.stamp_tax || 0.0005) * 1000, 3);
    $("#slippage").value = num((c.slippage || 0.0005) * 1000, 3);
  }
  await loadPool(true);   // 首次打开强制用股票池里「设为默认」的那只
  bind(); renderParams();
  initTerms();            // 术语气泡 + 术语表：正文只留结论，解释按需展开
  bulkStatus();           // 有下载任务在跑时，任何页签都能看到顶部徽标与进度
  sideTodoLoad();         // 左下角待办先加载，等 screenStatus 回来会补上行业口径
  const hashTab = (location.hash || "").replace("#", "");
  if (hashTab && TAB_TITLES[hashTab] && hashTab !== "screen") switchTab(hashTab);  // 支持 #ind 直链
  updateStatus(st);       // 复用首次 /api/status 响应，不再二次请求
  await screenStatus();   // 三层状态 / 行业覆盖度 / 模块清单
  await screenRun(true);  // 打开即出候选，首屏不再是一张空表
  // 后验要重放 200 只股票的历史各期（服务端首次约 6 秒），排到首屏渲染之后再跑，
  // 否则会和候选榜抢同一份 CPU，反而让「打开就卡住」。之后再点开是秒回（服务端有缓存）。
  setTimeout(() => screenReview(null, true), 2500);
}
document.addEventListener("DOMContentLoaded", init);
