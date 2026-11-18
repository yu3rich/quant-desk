"""chartdata.py — 行情看图数据层。

对外只做一件事：给定股票代码和周期，返回可直接绘图的序列。

周期口径（与常见行情软件一致）：
  - intraday 分时：当日逐分钟价格 + 均价 + 分钟量，横轴固定为 09:30~11:30 / 13:00~15:00
  - five    五日：最近 5 个交易日的连续分时，横轴按天拼接
  - day/week/month：蜡烛图，前复权（最新价 = 实时价，与分时同口径）

数据全部走腾讯（web.ifzq.gtimg.cn），与回测用的 baostock 后复权缓存相互独立：
回测要做收益连乘，必须后复权；看图要看着像真的，必须前复权。两套口径不混用。
"""

from __future__ import annotations

import time

import requests

HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
}
BASE = "https://web.ifzq.gtimg.cn"

# 内存缓存：盘中反复切周期不重复打接口
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = {"intraday": 15, "five": 45, "day": 180, "week": 300, "month": 600}

PERIODS = ("intraday", "five", "day", "week", "month")


def key_of(code: str) -> str:
    """'600000' / 'sh600000' / '600000.SH' → 'sh600000'。"""
    c = str(code or "").strip().lower().replace(" ", "")
    for pre in ("sh", "sz", "bj"):
        if c.startswith(pre):
            c = c[len(pre):]
    c = c.split(".")[0]
    c = "".join(ch for ch in c if ch.isdigit())
    if len(c) != 6:
        raise ValueError("股票代码需为 6 位数字")
    if c[0] == "9":
        if c[:2] == "92":        # 920/921 为北交所新代码段
            return "bj" + c
        return "sh" + c          # 900 沪 B
    if c[0] in ("4", "8"):
        return "bj" + c          # 北交所
    if c[0] in ("5", "6"):
        return "sh" + c          # 6 主板、688 科创板、5 沪基金
    return "sz" + c              # 000/002/200/300/301


def _get(url: str, timeout: int = 12) -> dict:
    r = requests.get(url, headers=HDR, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if d.get("code") not in (0, None):
        raise RuntimeError("行情接口返回异常：%s" % (d.get("msg") or d.get("code")))
    return d.get("data") or {}


def _cache_get(k: str):
    hit = _CACHE.get(k)
    if hit and time.time() - hit[0] < _TTL.get(k.split(":")[0], 60):
        return hit[1]
    return None


def _cache_put(k: str, v: dict) -> dict:
    # 控制体积，避免长期运行无限增长
    if len(_CACHE) > 400:
        for old in list(_CACHE)[:200]:
            _CACHE.pop(old, None)
    _CACHE[k] = (time.time(), v)
    return v


def _slots() -> list[str]:
    """A 股分时时间轴：09:30~11:30 + 13:00~15:00，共 241 个分钟点。"""
    out = []
    for m in range(9 * 60 + 30, 11 * 60 + 31):
        out.append("%02d%02d" % (m // 60, m % 60))
    for m in range(13 * 60, 15 * 60 + 1):
        out.append("%02d%02d" % (m // 60, m % 60))
    return out


SLOTS = _slots()


def _hhmm(t: str) -> str:
    return "%s:%s" % (t[:2], t[2:])


def _qt_meta(qt: dict, key: str) -> dict:
    """qt 数组里取名称与昨收（分时涨跌幅基准）。"""
    arr = (qt or {}).get(key) or []
    if len(arr) < 5:
        return {"name": "", "pre_close": None}
    try:
        pre = float(arr[4]) if arr[4] not in ("", None) else None
    except Exception:
        pre = None
    return {"name": str(arr[1] or ""), "pre_close": pre}


def _parse_minutes(rows: list) -> dict[str, tuple[float, float, float]]:
    """'0930 10.78 9402 10135356.00' → {t: (price, cum_vol_手, cum_amt_元)}

    腾讯在 15:00 之后还会把收盘集合竞价/盘后固定价格交易铺成 15:06~15:30 的时间戳，
    这些点价格等于收盘价、属于重复展示，统一丢弃（收盘价由 15:00 那一点承担）。
    """
    out = {}
    for s in rows or []:
        p = str(s).split()
        if len(p) < 3 or p[0] > "1500":
            continue
        try:
            out[p[0]] = (float(p[1]), float(p[2]), float(p[3]) if len(p) > 3 else 0.0)
        except Exception:
            continue
    return out


def _fill_day(parsed: dict[str, tuple[float, float, float]]) -> tuple[list, list, list]:
    """把当日已成交的点铺到固定时间轴上：价格、均价、每分钟量（手）。"""
    price, avg, vol = [], [], []
    last_t = max(parsed) if parsed else ""
    prev_p, prev_v, prev_a = None, 0.0, 0.0
    for t in SLOTS:
        if t in parsed:
            p, cv, ca = parsed[t]
            d = max(cv - prev_v, 0.0)
            prev_v, prev_a = cv, ca
            prev_p = p
            price.append(round(p, 3))
            vol.append(round(d, 0))
            avg.append(round(ca / (cv * 100), 4) if cv > 0 and ca > 0 else None)
        elif last_t and t > last_t:
            price.append(None)          # 还没到的时间点，不画
            avg.append(None)
            vol.append(None)
        else:
            price.append(prev_p)        # 该分钟无成交：价格延续，量为 0
            vol.append(0.0)
            avg.append(avg[-1] if avg else None)
    return price, avg, vol


def intraday(code: str) -> dict:
    key = key_of(code)
    hit = _cache_get("intraday:" + key)
    if hit:
        return hit
    d = _get("%s/appstock/app/minute/query?code=%s" % (BASE, key))
    node = d.get(key) or {}
    body = node.get("data") or {}
    rows = body.get("data") or []
    meta = _qt_meta(node.get("qt"), key)
    parsed = _parse_minutes(rows)
    price, avg, vol = _fill_day(parsed)
    pre = meta["pre_close"]
    last = next((p for p in reversed(price) if p is not None), None)
    out = {
        "ok": True, "code": key[2:], "market": key[:2], "name": meta["name"],
        "period": "intraday", "date": body.get("date") or "",
        "pre_close": pre, "last": last,
        "pct": round((last / pre - 1) * 100, 2) if (last and pre) else None,
        "axis": [_hhmm(t) for t in SLOTS],
        "price": price, "avg": avg, "vol": vol,
    }
    return _cache_put("intraday:" + key, out)


def five_day(code: str) -> dict:
    key = key_of(code)
    hit = _cache_get("five:" + key)
    if hit:
        return hit
    d = _get("%s/appstock/app/day/query?code=%s" % (BASE, key))
    node = d.get(key) or {}
    days = node.get("data")
    if isinstance(days, dict):
        days = days.get("data")
    days = days or []
    days = sorted(days, key=lambda x: str(x.get("date") or ""))   # 接口返回是倒序，统一成时间正序
    meta = _qt_meta(node.get("qt"), key)

    axis, price, avg, vol = [], [], [], []
    for day in days:
        parsed = _parse_minutes(day.get("data") or [])
        p, a, v = _fill_day(parsed)
        for i, t in enumerate(SLOTS):
            axis.append(_hhmm(t))
            price.append(p[i])
            avg.append(a[i])
            vol.append(v[i])
    # 每天一个刻度标签
    marks = []
    for i, day in enumerate(days):
        ds = str(day.get("date") or "")
        marks.append({"label": "%s/%s" % (ds[4:6], ds[6:8]) if len(ds) == 8 else ds,
                      "index": i * len(SLOTS)})

    last = next((p for p in reversed(price) if p is not None), None)
    first = next((p for p in price if p is not None), None)
    pre = meta["pre_close"]
    out = {
        "ok": True, "code": key[2:], "market": key[:2], "name": meta["name"],
        "period": "five", "date": (str(days[-1].get("date")) if days else ""),
        # 五日图没有「首日昨收」可用，基准取首日开盘价，图例里写明口径
        "pre_close": first, "last": last,
        "pct": round((last / first - 1) * 100, 2) if (last and first) else None,
        "axis": axis, "price": price, "avg": avg, "vol": vol, "marks": marks,
        "days": len(days),
        "base_note": "区间涨幅以首日开盘价为基准",
    }
    return _cache_put("five:" + key, out)


def kline(code: str, period: str = "day") -> dict:
    period = period if period in ("day", "week", "month") else "day"
    key = key_of(code)
    ck = "%s:%s" % (period, key)
    hit = _cache_get(ck)
    if hit:
        return hit
    n = {"day": 620, "week": 320, "month": 200}[period]
    d = _get("%s/appstock/app/fqkline/get?param=%s,%s,,,%d,qfq" % (BASE, key, period, n),
             timeout=15)
    node = d.get(key) or {}
    rows = node.get("qfq" + period) or node.get(period) or []
    meta = _qt_meta(node.get("qt") or d.get("qt"), key)

    bars = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            o, c, h, l, v = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        except Exception:
            continue
        bars.append({"date": str(r[0]), "open": o, "close": c, "high": h, "low": l,
                     "volume": v})
    if not bars:
        raise RuntimeError("该周期没有行情数据（可能是新股或长期停牌）")

    # 涨跌幅按前收算，首根没有前收则为 0
    for i, b in enumerate(bars):
        b["pct"] = round((b["close"] / bars[i - 1]["close"] - 1) * 100, 2) if i else 0.0

    out = {
        "ok": True, "code": key[2:], "market": key[:2], "name": meta["name"] or "",
        "period": period, "date": bars[-1]["date"],
        "pre_close": meta["pre_close"], "last": bars[-1]["close"],
        "pct": bars[-1]["pct"], "bars": bars, "count": len(bars),
        "advice": "前复权，最新价与实时行情一致",
    }
    return _cache_put(ck, out)


def get(code: str, period: str = "day") -> dict:
    period = str(period or "day").lower()
    if period in ("min", "1min", "today", "fenshi", "分时"):
        period = "intraday"
    if period in ("5day", "fiveday", "五日"):
        period = "five"
    if period in ("d", "日"):
        period = "day"
    if period in ("w", "周"):
        period = "week"
    if period in ("m", "月"):
        period = "month"
    if period not in PERIODS:
        period = "day"
    try:
        if period == "intraday":
            return intraday(code)
        if period == "five":
            return five_day(code)
        return kline(code, period)
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "code": str(code), "period": period}
