# -*- coding: utf-8 -*-
"""
QuantDesk - 本地 A 股量化工作台核心引擎

整合思路：
  主项目 AKShare（数据规范 / 复权口径 / A 股覆盖）
  + 直连通道容灾（替换 AKShare 单点限流风险）
  + 本地 SQLite 行情库（离线能力，data/quant.db）
  + ta 技术指标 / Qlib Alpha158 精简因子（替换 TA-Lib 编译依赖与 Qlib 专有二进制格式）
  + 自研事件驱动回测（吸收 backtesting.py 的简洁心智模型，补上它缺失的 A 股规则）
  + 向量化参数网格（吸收 vectorbt 快速扫描思路，规避 numba / Python 3.13 兼容风险）
  + QuantStats 口径绩效指标（自算，规避 yfinance 等无关依赖）
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
import urllib.request
from pathlib import Path

import qdpaths

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"

COLS = ["date", "open", "high", "low", "close", "volume", "amount", "pct", "turnover"]

# AKShare 熔断：失败不再「永久禁用」，而是冷却一段时间后再试，
# 避免「一只股票偶发失败 → 整个进程所有股票都不再走 AKShare」的自伤式降级。
_AK_COOLDOWN = 300.0                 # 单位秒；限流按官方口径通常 5 分钟恢复
_AK = {"fails": 0, "until": 0.0}
_AKSHARE_BROKEN = False              # 兼容 /api/status：仅表示「当前处于冷却中」


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(patch: dict) -> dict:
    """合并写回 config.json（只覆盖传入的顶层键，其余如 stocks 原样保留）。"""
    cur = load_config()
    cur.update({k: v for k, v in (patch or {}).items()})
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return cur


def stock_name(code: str, cfg: dict | None = None) -> str:
    """名称优先取用户自定义股票池 / 本地名称缓存，配置文件仅作为历史兜底。"""
    code = str(code).zfill(6)
    try:
        import watchlist
        n = watchlist.stock_name(code)
        if n and n != code:
            return n
    except Exception:
        pass
    for s in (cfg or {}).get("stocks", []):
        if str(s.get("code")) == str(code):
            return s.get("name", code)
    return code


# --------------------------------------------------------------------------
# 数据层：AKShare 主通道 / 直连容灾 / 本地缓存
# --------------------------------------------------------------------------

def _secid(code: str) -> str:
    code = str(code).zfill(6)
    # 北交所（含 920 新代码段）在东财归 0. 市场；沪市 5/6/9 归 1.
    if code.startswith(("43", "83", "87", "92")):
        return "0." + code
    return ("1." if code[0] in ("5", "6", "9") else "0.") + code


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["date"] = d["date"].astype(str).str.slice(0, 10)
    for c in COLS:
        if c not in d.columns:
            d[c] = 0.0
        if c != "date":
            d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[COLS].dropna(subset=["open", "high", "low", "close"])
    d = d[(d["close"] > 0) & (d["open"] > 0)]
    d = d.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return d


def _fetch_akshare(code: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    raw = ak.stock_zh_a_hist(symbol=code, period="daily",
                             start_date=start, end_date=end, adjust="qfq")
    if raw is None or len(raw) == 0:
        raise RuntimeError("AKShare 返回空数据")
    ren = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
           "最低": "low", "成交量": "volume", "成交额": "amount",
           "涨跌幅": "pct", "换手率": "turnover"}
    d = raw.rename(columns=ren).copy()
    return _normalize(d)


def _fetch_direct(code: str, start: str, end: str) -> pd.DataFrame:
    """东方财富 push2his 直连：与 AKShare 同源不同端点，用于绕开 push2 限流。"""
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        "?fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt=1&secid={_secid(code)}&beg={start}&end={end}"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://quote.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        obj = json.loads(r.read().decode("utf-8"))
    klines = ((obj.get("data") or {}).get("klines") or [])
    if not klines:
        raise RuntimeError("直连通道返回空数据")
    rows = []
    for k in klines:
        p = k.split(",")
        if len(p) < 11:
            continue
        rows.append({"date": p[0], "open": p[1], "close": p[2],
                     "high": p[3], "low": p[4], "volume": p[5],
                     "amount": p[6], "pct": p[8], "turnover": p[10]})
    return _normalize(pd.DataFrame(rows))


def _fetch_tencent(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯行情：与东方财富完全独立的第二数据源。单段上限约 700 条，按 2 年分段拼接。"""
    import time
    import requests
    code = str(code).zfill(6)
    prefix = "sh" if code[0] in ("5", "6", "9") else ("bj" if code[0] in ("4", "8") else "sz")
    key = prefix + code
    s = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    y1, y2 = int(start[:4]), int(end[:4])
    segs, y = [], y1
    while y <= y2:
        a = s if y == y1 else f"{y}-01-01"
        b = e if y + 1 >= y2 else f"{y + 1}-12-31"
        segs.append((a, b))
        y += 2
    hdr = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    rows = []
    for a, b in segs:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={key},day,{a},{b},800,qfq")
        try:
            r = requests.get(url, headers=hdr, timeout=20)
            dd = ((r.json().get("data") or {}).get(key) or {})
            k = dd.get("qfqday") or dd.get("day") or []
            for p in k:
                if len(p) < 6:
                    continue
                rows.append({"date": p[0], "open": p[1], "close": p[2],
                             "high": p[3], "low": p[4], "volume": p[5],
                             "amount": 0.0, "pct": 0.0, "turnover": 0.0})
        except Exception:
            continue
        time.sleep(0.12)
    if not rows:
        raise RuntimeError("腾讯行情返回空数据")
    d = _normalize(pd.DataFrame(rows))
    d["pct"] = (d["close"].pct_change() * 100).fillna(0.0)
    return d


def fetch_history(code: str, cfg: dict | None = None) -> tuple[pd.DataFrame, str]:
    """拉全量历史并写入缓存，返回 (DataFrame, 通道名)。"""
    global _AKSHARE_BROKEN
    cfg = cfg or load_config()
    code = str(code).strip().zfill(6)
    start = str(cfg.get("cache_start", "20180101")).replace("-", "")
    end = dt.date.today().strftime("%Y%m%d")
    errors = []

    if time.time() >= _AK["until"]:
        try:
            d = _fetch_akshare(code, start, end)
            if len(d) > 20:
                _AK["fails"] = 0
                _AK["until"] = 0.0
                _AKSHARE_BROKEN = False
                _save_cache(code, d, "AKShare", "qfq")
                return d, "AKShare"
            raise RuntimeError("有效行数不足 20")
        except Exception as e:
            errors.append("AKShare: " + str(e)[:90])
            _AK["fails"] += 1
            _AK["until"] = time.time() + _AK_COOLDOWN
            _AKSHARE_BROKEN = True

    try:
        d = _fetch_direct(code, start, end)
        if len(d) > 20:
            _save_cache(code, d, "东方财富直连", "qfq")
            return d, "东方财富直连"
        raise RuntimeError("有效行数不足 20")
    except Exception as e:
        errors.append("直连: " + str(e)[:90])

    try:
        d = _fetch_tencent(code, start, end)
        if len(d) > 20:
            _save_cache(code, d, "腾讯行情", "qfq")
            return d, "腾讯行情"
        raise RuntimeError("有效行数不足 20")
    except Exception as e:
        errors.append("腾讯: " + str(e)[:90])

    cached = _read_cache(code)
    if cached is not None and len(cached) > 20:
        return cached, "本地缓存（离线）"
    raise RuntimeError("行情获取失败 → " + " | ".join(errors))


def _save_cache(code: str, d: pd.DataFrame, source: str, basis: str = "qfq"):
    import store
    store.bars_write(code, d, source, basis)


def _read_cache(code: str) -> pd.DataFrame | None:
    import store
    d = store.bars_frame(code)
    if d is None:
        return None
    try:
        return _normalize(d)
    except Exception:
        return None


def cache_info(code: str) -> dict | None:
    import store
    m = store.meta_of(code)
    return m or None


def _ref_price(code: str) -> float | None:
    """从全 A 名单取最新真实价，用于把后复权序列等比回缩到真实量级。"""
    try:
        import csv
        p = qdpaths.data_path("universe.csv")
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("code") == code:
                    v = float(r.get("price"))
                    return v if v > 0 else None
    except Exception:
        pass
    return None


def _rescale_hfq(code: str, d: pd.DataFrame) -> pd.DataFrame:
    """后复权价格整体被放大，按名单最新真实价等比回缩。

    只缩放 OHLC 四个价格列，收益率 / 均线偏离 / 量比等因子不受影响，
    但整手资金门槛（100 股）恢复成真实量级，避免回测误判「资金不足」。
    """
    if d is None or d.empty:
        return d
    ref, last = _ref_price(code), float(d["close"].iloc[-1])
    if not ref or last <= 0:
        return d
    k = ref / last
    out = d.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = out[c] * k
    return out


def get_quotes(code: str, start: str = "20180101", end: str | None = None,
               refresh: bool = False, cfg: dict | None = None):
    """对外数据入口：优先本地缓存（沪深为后复权，回缩到真实量级后使用）。

    - 有缓存且不强制刷新：直接读，不联网
    - 缺缓存 / 强制刷新 / 超过 7 天未更新：沪深走 baostock 全量或增量，其余走旧通道
    - 后复权缓存统一等比回缩，单只回测的整手资金判断保持真实
    """
    cfg = cfg or load_config()
    code = str(code).strip().zfill(6)
    end = (end or dt.date.today().strftime("%Y%m%d")).replace("-", "")
    start = str(start).replace("-", "")
    meta = cache_info(code) or {}
    full = _read_cache(code)
    stale = bool(full is not None and meta.get("last_date", "").replace("-", "") <
                 (dt.date.today() - dt.timedelta(days=7)).strftime("%Y%m%d"))
    if full is None or refresh or stale:
        try:
            import bulkdata
            busy = bool(bulkdata._STATE.get("running"))
            if bulkdata.to_bs_code(code) and not busy:
                mode = "incremental" if full is not None else "full"
                bulkdata._run([code], mode)
                full = _read_cache(code)
                meta = cache_info(code) or {}
            elif full is None:
                # 批量任务在跑时不抢 baostock 连接，回测缺数据走旧通道兜底
                full, _src = fetch_history(code, cfg)
                meta = cache_info(code) or {}
        except Exception:
            if full is None:
                full, _src = fetch_history(code, cfg)
                meta = cache_info(code) or {}
    if full is None or len(full) == 0:
        raise RuntimeError("行情获取失败：本地无缓存且下载未成功，请稍后重试")

    if (meta.get("basis") or "qfq") == "hfq":
        full = _rescale_hfq(code, full)
    source = (meta.get("source") or "本地缓存")

    s_key = full["date"].str.replace("-", "", regex=False)
    d = full[(s_key >= start) & (s_key <= end)].reset_index(drop=True)
    # 盘中排除当日未收盘 K 线：未完成的 bar 会让「收盘信号」变成前视数据
    if len(d) and dt.datetime.now().hour < 15:
        if str(d["date"].iloc[-1]).replace("-", "")[:8] == dt.date.today().strftime("%Y%m%d"):
            d = d.iloc[:-1].reset_index(drop=True)
    if len(d) == 0:
        raise RuntimeError("所选区间内无行情数据，请调整起止日期")
    return d, source


# --------------------------------------------------------------------------
# 指标层：ta 指标 + Qlib Alpha158 精简因子
# --------------------------------------------------------------------------

def _z(s: pd.Series, w: int = 60) -> pd.Series:
    m = s.rolling(w, min_periods=30).mean()
    sd = s.rolling(w, min_periods=30).std().replace(0, np.nan)
    return (s - m) / sd


def add_indicators(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    fast, slow = int(p.get("fast", 10)), int(p.get("slow", 30))
    d["ma_fast"] = c.rolling(fast, min_periods=fast).mean()
    d["ma_slow"] = c.rolling(slow, min_periods=slow).mean()

    # ta 库（纯 Python 指标，替换 TA-Lib 的 C 编译依赖）；失败则用等价 pandas 实现
    try:
        from ta.momentum import RSIIndicator
        from ta.trend import MACD
        d["rsi"] = RSIIndicator(close=c, window=int(p.get("rsi_n", 14))).rsi()
        m = MACD(close=c)
        d["macd"], d["macd_signal"] = m.macd(), m.macd_signal()
    except Exception:
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        d["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
        d["macd"] = e12 - e26
        d["macd_signal"] = d["macd"].ewm(span=9, adjust=False).mean()

    mid = c.rolling(20, min_periods=20).mean()
    sd = c.rolling(20, min_periods=20).std()
    d["boll_mid"], d["boll_up"], d["boll_dn"] = mid, mid + 2 * sd, mid - 2 * sd

    ret = c.pct_change()
    ret5, ret20 = c.pct_change(5), c.pct_change(20)
    vol20 = ret.rolling(20, min_periods=20).std()
    vol_ratio = v.rolling(5, min_periods=5).mean() / v.rolling(20, min_periods=20).mean().replace(0, np.nan)
    ma_dev = (c - mid) / mid
    # Alpha158 精简版：动量 / 短期反转 / 低波 / 量能 / 均线偏离，时序 z-score 加权
    d["factor"] = (
        0.30 * _z(ret20) - 0.15 * _z(ret5) - 0.15 * _z(vol20)
        + 0.15 * _z(vol_ratio) + 0.25 * _z(ma_dev)
    ).fillna(0.0)
    d["ret20"] = ret20
    d["vol20"] = vol20
    d["ma_dev"] = ma_dev
    return d


def build_signals(d: pd.DataFrame, strategy: str, p: dict) -> np.ndarray:
    n = len(d)
    sig = np.zeros(n, dtype=int)
    if strategy in ("ma_cross", "macd"):
        if strategy == "ma_cross":
            a, b = d["ma_fast"].to_numpy(), d["ma_slow"].to_numpy()
        else:
            a, b = d["macd"].to_numpy(), d["macd_signal"].to_numpy()
        with np.errstate(invalid="ignore"):
            raw = (a > b).astype(int)
        raw[np.isnan(a) | np.isnan(b)] = 0
        return raw
    if strategy == "rsi":
        lo, hi = float(p.get("lower", 30)), float(p.get("upper", 70))
        state = 0
        for i, r in enumerate(d["rsi"].to_numpy()):
            if np.isnan(r):
                sig[i] = 0
                continue
            if r < lo:
                state = 1
            elif r > hi:
                state = 0
            sig[i] = state
        return sig
    if strategy == "boll":
        state = 0
        c = d["close"].to_numpy()
        up, mid, dn = d["boll_up"].to_numpy(), d["boll_mid"].to_numpy(), d["boll_dn"].to_numpy()
        for i in range(n):
            if np.isnan(up[i]):
                sig[i] = 0
                continue
            if c[i] > up[i]:
                state = 1
            elif c[i] < mid[i] or c[i] < dn[i]:
                state = 0
            sig[i] = state
        return sig
    if strategy == "factor":
        th = float(p.get("threshold", 0.3))
        state = 0
        for i, f in enumerate(d["factor"].to_numpy()):
            if f > th:
                state = 1
            elif f < -th:
                state = 0
            sig[i] = state
        return sig
    raise RuntimeError("未知策略: " + str(strategy))


# --------------------------------------------------------------------------
# 回测引擎：事件驱动 + A 股规则
#   信号 T 日收盘产生 -> T+1 开盘成交（避免前视偏差）
#   T+1 卖出限制 / 100 股一手 / 涨跌停不可成交 / 佣金+印花税+过户费 / 滑点
# --------------------------------------------------------------------------

DEFAULT_PARAMS = {"fast": 10, "slow": 30, "rsi_n": 14,
                  "lower": 30, "upper": 70, "threshold": 0.3}


def _limit_pct(code: str, name: str, cfg: dict) -> float:
    """按板块取涨跌停幅度：主板 10%、创业板/科创板 20%、北交所 30%、ST 5%。

    回测只有 OHLC，用开盘涨幅判断能不能成交，阈值必须和这只票的真实限幅一致：
    固定 10% 会把创业板 +15% 的开盘当成「没涨停」照样买入，也会把 ST 的 +6%
    误判成涨停而错过。
    """
    c = str(code or "").strip()
    if name and "ST" in str(name).upper():
        return 0.05
    if c[:3] in ("300", "301", "688", "689"):
        return 0.20
    if c[:1] in ("4", "8"):
        return 0.30
    try:
        return float(cfg.get("limit_pct", 0.10))
    except (TypeError, ValueError):
        return 0.10


def _downside_dev(ret, rf_daily: float = 0.02 / 252) -> float:
    """下行偏差：sqrt(mean(min(r−MAR, 0)²)) × √252。

    这是 Sortino 的分母。旧写法用「负收益子集的标准差」，只有一个负收益日时
    std(ddof=1) 是 NaN、两个等幅负收益日时是 0，指标直接变成 0.0。
    """
    import numpy as _np
    r = _np.asarray(ret, dtype=float)
    if not r.size:
        return 0.0
    down = _np.minimum(r - rf_daily, 0.0)
    return float(math.sqrt(float((down ** 2).mean())) * math.sqrt(252))


def run_backtest(df: pd.DataFrame, strategy: str = "ma_cross",
                 params: dict | None = None, cfg: dict | None = None,
                 capital: float | None = None, light: bool = False,
                 code: str = "", name: str = "") -> dict:
    cfg = cfg or load_config()
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    d = add_indicators(df.copy(), p)
    if len(d) < 40:
        raise RuntimeError("数据不足：至少需要 40 个交易日")
    sig = build_signals(d, strategy, p)

    capital = float(capital or cfg.get("capital", 100000))
    comm = float(cfg.get("commission", 0.00025))
    min_comm = float(cfg.get("min_commission", 5))
    tax = float(cfg.get("stamp_tax", 0.0005))
    transfer = float(cfg.get("transfer_fee", 0.00001))
    slip = float(cfg.get("slippage", 0.0005))
    limit_pct = _limit_pct(code, name, cfg)
    lot = 100

    dates = d["date"].tolist()
    opens, closes = d["open"].to_numpy(float), d["close"].to_numpy(float)
    cash, shares, avg_cost = capital, 0, 0.0
    buy_i, pending, prev_close = -10 ** 9, None, np.nan
    blocked_capital, buy_attempts = 0, 0
    equity, bench_curve, positions, trades, marks = [], [], [], [], []

    # 基准：首日开盘买入并持有
    bpx = float(opens[0])
    b_shares = int(capital / (bpx * (1 + comm + transfer)) // lot) * lot
    b_cash = capital - b_shares * bpx * (1 + comm + transfer)

    for i in range(len(d)):
        o, c = float(opens[i]), float(closes[i])
        gap_up = gap_dn = False
        if not np.isnan(prev_close) and prev_close > 0:
            r = o / prev_close - 1
            gap_up = r >= limit_pct - 0.003
            gap_dn = r <= -(limit_pct - 0.003)

        if pending == "buy" and shares == 0 and not gap_up:
            buy_attempts += 1
            px = o * (1 + slip)
            qty = int(cash / (px * (1 + comm + transfer)) // lot) * lot
            if qty > 0:
                amt = qty * px
                fee = max(amt * comm, min_comm) + amt * transfer
                if amt + fee <= cash:
                    cash -= amt + fee
                    shares, avg_cost, buy_i = qty, px + fee / qty, i
                    trades.append({"date": dates[i], "side": "买入", "price": round(px, 2),
                                   "shares": int(qty), "amount": round(amt, 2),
                                   "fee": round(fee, 2), "pnl": None, "pnl_pct": None})
                    marks.append({"date": dates[i], "type": "buy", "price": round(px, 2)})
                else:
                    blocked_capital += 1
            else:
                blocked_capital += 1
        elif pending == "sell" and shares > 0 and not gap_dn and i > buy_i:
            px = o * (1 - slip)
            amt = shares * px
            fee = max(amt * comm, min_comm) + amt * tax + amt * transfer
            cash += amt - fee
            pnl = (px - avg_cost) * shares - fee
            trades.append({"date": dates[i], "side": "卖出", "price": round(px, 2),
                           "shares": int(shares), "amount": round(amt, 2),
                           "fee": round(fee, 2), "pnl": round(pnl, 2),
                           "pnl_pct": round(pnl / max((avg_cost * shares), 1) * 100, 2)})
            marks.append({"date": dates[i], "type": "sell", "price": round(px, 2)})
            shares, avg_cost = 0, 0.0
        pending = None

        eq = cash + shares * c
        equity.append(float(eq))
        bench_curve.append(float(b_cash + b_shares * c))
        positions.append(1 if shares > 0 else 0)

        target = int(sig[i])
        if target == 1 and shares == 0:
            pending = "buy"
        elif target == 0 and shares > 0:
            pending = "sell"
        prev_close = c

    idx = pd.to_datetime(dates)
    eq_s = pd.Series(equity, index=idx)
    ret = eq_s.pct_change().fillna(0.0)
    n = max(len(eq_s), 1)
    final = float(eq_s.iloc[-1])
    total = final / capital - 1
    ann = (final / capital) ** (252 / n) - 1 if final > 0 else -1
    vol = float(ret.std() * math.sqrt(252))
    downside = _downside_dev(ret.to_numpy(dtype=float))
    sharpe = (ann - 0.02) / vol if vol > 0 else 0.0
    sortino = (ann - 0.02) / downside if downside > 0 else 0.0
    dd = eq_s / eq_s.cummax() - 1
    mdd = float(dd.min())
    calmar = ann / abs(mdd) if mdd < 0 else 0.0

    closed = [t for t in trades if t["side"] == "卖出"]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    losses = [t for t in closed if (t["pnl"] or 0) <= 0]
    win_rate = len(wins) / len(closed) if closed else 0.0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0
    bench_total = float(bench_curve[-1]) / capital - 1 if bench_curve else 0.0

    metrics = {
        "total": round(total * 100, 2), "annual": round(ann * 100, 2),
        "sharpe": round(sharpe, 2), "sortino": round(sortino, 2),
        "max_drawdown": round(mdd * 100, 2), "calmar": round(calmar, 2),
        "volatility": round(vol * 100, 2), "win_rate": round(win_rate * 100, 2),
        "trades": int(len(closed)), "pl_ratio": round(pl_ratio, 2),
        "benchmark": round(bench_total * 100, 2),
        "excess": round((total - bench_total) * 100, 2),
        "final_equity": round(final, 2), "capital": round(capital, 2),
    }

    warnings = []
    if not closed:
        if blocked_capital or (buy_attempts and b_shares == 0):
            warnings.append("资金不足：本股票 1 手（100 股）约需 %.0f 元，高于初始资金 %.0f 元；请在左侧提高初始资金，或换一只低价股票。"
                            % (float(opens[0]) * lot, capital))
        elif buy_attempts == 0:
            warnings.append("该区间内策略未产生买入信号，可放宽参数或延长回测区间。")
        elif shares > 0:
            warnings.append("策略期末仍持仓，区间内没有平仓交易，收益按浮动盈亏计入净值。")
        else:
            warnings.append("该区间内无平仓交易，请检查策略参数与回测区间。")
    if b_shares == 0:
        warnings.append("基准「买入持有」同样受整手资金门槛限制，基准收益为 0，仅供参照。")

    out = {"ok": True, "strategy": strategy, "params": p,
           "metrics": metrics, "warnings": warnings}
    if light:
        return out

    monthly = []
    prev = capital
    for per, grp in eq_s.groupby(eq_s.index.to_period("M")):
        val = float(grp.iloc[-1])
        monthly.append({"month": str(per), "ret": round((val / prev - 1) * 100, 2)})
        prev = val

    out.update({
        "curve": [{"date": dates[i], "equity": round(equity[i], 2),
                   "benchmark": round(bench_curve[i], 2),
                   "drawdown": round(float(dd.iloc[i]) * 100, 2),
                   "position": positions[i]} for i in range(len(d))],
        "monthly": monthly,
        "trades": trades,
        "marks": marks,
        "quotes": [{"date": dates[i],
                    "o": round(float(d["open"].iloc[i]), 2),
                    "h": round(float(d["high"].iloc[i]), 2),
                    "l": round(float(d["low"].iloc[i]), 2),
                    "c": round(float(closes[i]), 2),
                    "v": round(float(d["volume"].iloc[i]), 0),
                    "ma_fast": _n(d["ma_fast"].iloc[i]),
                    "ma_slow": _n(d["ma_slow"].iloc[i])} for i in range(len(d))],
    })
    return out


def _n(x, nd=2):
    try:
        return None if pd.isna(x) else round(float(x), nd)
    except Exception:
        return None


def scan_params(df: pd.DataFrame, grid: dict, cfg: dict | None = None) -> dict:
    """向量化思路的参数网格：小步快跑，返回 Sharpe / 收益 / 回撤 三维矩阵。"""
    cfg = cfg or load_config()
    fasts = [int(x) for x in grid.get("fast", [5, 10, 15, 20, 25, 30])]
    slows = [int(x) for x in grid.get("slow", [30, 45, 60, 80, 100, 120])]
    cells = []
    for f in fasts:
        for s in slows:
            if f >= s:
                cells.append({"fast": f, "slow": s, "sharpe": None,
                              "total": None, "max_drawdown": None, "valid": False})
                continue
            try:
                r = run_backtest(df, "ma_cross", {"fast": f, "slow": s}, cfg, light=True)
                m = r["metrics"]
                cells.append({"fast": f, "slow": s, "sharpe": m["sharpe"],
                              "total": m["total"], "max_drawdown": m["max_drawdown"],
                              "valid": True})
            except Exception:
                cells.append({"fast": f, "slow": s, "sharpe": None,
                              "total": None, "max_drawdown": None, "valid": False})
    return {"ok": True, "fasts": fasts, "slows": slows, "cells": cells,
            "count": len([c for c in cells if c["valid"]])}


# 横截面打分已统一由 screener.py + mlmodel.py 提供（16 因子 / 模型分 / 基准分），
# 此处不再保留旧的「单因子榜单」实现，避免两套口径并存。
