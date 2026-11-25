# -*- coding: utf-8 -*-
"""单股风险与价位：ATR、波动率、区间位置、相对估值区间。

口径：
- 全部基于本地后复权日线（与因子、回测一致）；
- 对外只给 `atr_pct`（ATR 占现价百分比）和换算后的价位，避免高送转后
  后复权的绝对金额与真实价不可比；
- 相对估值只用当期 PE/PB 的行业分位，不做 DCF —— 本机没有财报数据，
  编一个 DCF 只会得到看起来精确的假数字。
"""
from __future__ import annotations

import math

import datasource
import industry
import store

MIN_BARS = 30


def _bars(code, n=160):
    try:
        return store.bars_frame(code, n)
    except Exception:
        return None


def atr_pct(df, n=14):
    """ATR14 占最新收盘的百分比，返回 (pct, abs)。"""
    if df is None or len(df) < n + 2:
        return None, None
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = (h - l).abs().combine((h - pc).abs(), max).combine((l - pc).abs(), max)
    a = float(tr.tail(n).mean())
    last = float(c.iloc[-1])
    if not last or not math.isfinite(a) or a <= 0:
        return None, None
    return round(a / last * 100, 3), round(a, 4)


def vol_pct(df, n=20):
    """近 n 日收益率标准差（%），衡量日波动而不是年化。"""
    if df is None or len(df) < n + 2:
        return None
    ret = df["close"].pct_change().tail(n).dropna()
    if len(ret) < max(5, n // 2):
        return None
    v = float(ret.std() * 100)
    return round(v, 3) if math.isfinite(v) else None


def levels(code: str, real_price=None, hfq_close=None) -> dict:
    """ATR、20/60 日高低、Pivot R1/S1，以及 2×ATR 的参考回撤。

    给了 real_price 与 hfq_close 时，把所有绝对价位换算成真实价；
    否则返回后复权价，调用方自己乘换算因子。
    """
    df = _bars(code)
    if df is None or len(df) < MIN_BARS:
        return {}
    last = df.iloc[-1]
    close = float(last["close"])
    a_pct, a_abs = atr_pct(df)
    hi20 = float(df["high"].tail(20).max())
    lo20 = float(df["low"].tail(20).min())
    hi60 = float(df["high"].tail(60).max())
    lo60 = float(df["low"].tail(60).min())
    # 用 20 日区间做 pivot，比单日高低点稳，不会被一根长上影线带偏
    p = (hi20 + lo20 + close) / 3.0
    r1, s1 = 2 * p - lo20, 2 * p - hi20
    factor = 1.0
    try:
        if real_price and hfq_close and float(hfq_close) > 0:
            factor = float(real_price) / float(hfq_close)
    except (TypeError, ValueError):
        factor = 1.0

    def px(v):
        return round(float(v) * factor, 2)

    return {
        "atr_pct": a_pct,
        "atr": round(a_abs * factor, 2) if a_abs is not None else None,
        "vol20_pct": vol_pct(df),
        "high20": px(hi20), "low20": px(lo20),
        "high60": px(hi60), "low60": px(lo60),
        "dist_high20_pct": round((close / hi20 - 1) * 100, 2) if hi20 else None,
        "dist_low20_pct": round((close / lo20 - 1) * 100, 2) if lo20 else None,
        "pivot": px(p), "r1": px(r1), "s1": px(s1),
        # 2×ATR 只作为「波动参考位」：跌破它说明当日波动已超出常态，不是止损指令
        "stop_ref": px(close - 2 * a_abs) if a_abs else None,
        "stop_ref_pct": round(-2 * a_pct, 2) if a_pct else None,
        "bars": int(len(df)),
        "price_factor": round(factor, 6),
    }


def valuation(code: str, price, basic: dict | None = None,
              imap: dict | None = None) -> dict:
    """行业内 PE/PB 分位 × 当前 EPS/BPS，给出相对估值区间。

    这不是目标价：它只回答「按同行估值水平，当前价对应什么位置」。
    `basic` / `imap` 由批量调用方传入，避免逐只重复读全市场表。
    """
    try:
        price = float(price)
    except (TypeError, ValueError):
        return {}
    if price <= 0:
        return {}
    code = str(code).zfill(6)
    basic = basic if basic is not None else datasource.load_basic()
    if imap is None:
        try:
            imap = industry.load_map()
        except Exception:
            imap = {}
    ind = imap.get(code, "")
    me = basic.get(code) or {}

    def _f(v):
        try:
            x = float(v)
            return x if math.isfinite(x) and x > 0 else None
        except (TypeError, ValueError):
            return None

    pe, pb = _f(me.get("pe")), _f(me.get("pb"))
    # 行业缺失时不能把全市场当同行：那会把「高估值行业」的票判成便宜，
    # 而字段名和文案还写着「行业内分位」，属于静默算错。直接如实返回不可用。
    if not ind:
        return {"industry": "", "peers": 0, "pe": pe, "pb": pb,
                "eps": round(price / pe, 4) if pe else None,
                "bps": round(price / pb, 4) if pb else None,
                "pe_band": None, "pb_band": None,
                "price_band_pe": None, "price_band_pb": None,
                "reason": "no_industry",
                "note": "行业归属缺失，同行分位不可用（不拿全市场代替）。"}
    peers = []
    for c, v in basic.items():
        if c == code or (ind and imap.get(c) != ind):
            continue
        peers.append(v)

    def _band(key, cap):
        # 剔除极端值：同行里 PE 几百倍的亏损反转股会把 75 分位拉到没有意义
        vals = sorted(x for x in (_f(v.get(key)) for v in peers) if x and x <= cap)
        n = len(vals)
        if n < 10:
            return None
        def q(p):
            return vals[min(n - 1, max(0, int(round(p * (n - 1)))))]
        return [round(q(.25), 2), round(q(.50), 2), round(q(.75), 2)]

    pe_band, pb_band = _band("pe", 100.0), _band("pb", 20.0)
    eps = round(price / pe, 4) if pe else None
    bps = round(price / pb, 4) if pb else None

    def _price_band(mults, base):
        if not mults or not base:
            return None
        return [round(m * base, 2) for m in mults]

    return {
        "industry": ind,
        "peers": len(peers),
        "pe": pe, "pb": pb, "eps": eps, "bps": bps,
        "pe_band": pe_band, "pb_band": pb_band,
        "price_band_pe": _price_band(pe_band, eps),
        "price_band_pb": _price_band(pb_band, bps),
        "method": "行业内 PE/PB 的 25/50/75 分位 × 当前 EPS/BPS；只作相对估值参考",
        "note": "DCF 需要财报数据，本机未接入，未计算。",
    }


def batch(codes) -> dict:
    """批量算风险指标，供组合权重使用；单只失败返回空 dict，不拖垮整批。"""
    out = {}
    for c in codes or []:
        code = str(c).zfill(6)
        try:
            out[code] = levels(code)
        except Exception:
            out[code] = {}
    return out


def valuation_batch(pairs) -> dict:
    """一次 load_basic / load_map 供多只复用。

    体检要同时看十几只持仓的成本 vs 同行分位，逐只调用会把 5000 行的
    基础表重复读十几遍；这里只读一次，再逐只套 valuation()。
    """
    out = {}
    try:
        basic = datasource.load_basic()
    except Exception:
        return out
    if not basic:
        return out
    try:
        imap = industry.load_map()
    except Exception:
        imap = {}
    for code, price in pairs or []:
        c = str(code).zfill(6)
        try:
            out[c] = valuation(c, price, basic, imap)
        except Exception:
            out[c] = {}
    return out
