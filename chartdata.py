"""chartdata.py — 行情看图数据层。

对外只做一件事：给定股票代码和周期，返回可直接绘图的序列。

周期口径（与常见行情软件一致）：
  - intraday 分时：当日逐分钟价格 + 均价 + 分钟量，横轴固定为 09:30~11:30 / 13:00~15:00
  - five    五日：最近 5 个交易日的连续分时，横轴按天拼接
  - day/week/month：蜡烛图，前复权（最新价 = 实时价，与分时同口径）

分时/五日/指数走腾讯（web.ifzq.gtimg.cn）；K 线走腾讯前复权，失败时依次退到
腾讯备域名与新浪日线合成（见 KLINE_SRC）。与回测用的 baostock 后复权缓存相互独立：
回测要做收益连乘，必须后复权；看图要看着像真的，必须前复权。两套口径不混用。
"""

from __future__ import annotations

import datetime as dt
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


def invalidate() -> None:
    """行情落库后清空图表缓存（store.bars_write/bars_append 会调）。

    只按 TTL 失效不够：补完当日 K 线后立刻看图，日线缓存最长 180 秒
    还是旧的，用户会以为「更新没生效」。
    """
    _CACHE.clear()


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


# --------------------------------------------------------------------------
# K 线多源：腾讯（前复权）→ 腾讯备域名（前复权）→ 新浪日线本地合成（不复权）
# --------------------------------------------------------------------------
# 2026-11-26 起 web.ifzq.gtimg.cn/appstock/app/fqkline/get 被腾讯 WAF 拦成
# HTTP 501（跳 waf.tencent.com），日/周/月图全挂。同域名的 newfqkline 路径和
# ifzq.gtimg.cn 老路径都还正常，所以优先换路径而不是换供应商 —— 口径（前复权、
# 成交量单位「手」、日期格式）完全一致，不用改任何解析逻辑。
# 百度股市通（finance.pae.baidu.com）实测对本机 IP 直接 403 "hit risk"，
# 参数补全、带 Cookie、带浏览器指纹都无效，属于 IP/设备级风控，服务端直连绕不过，
# 因此没有接进来：加一个必然失败的源只会让每次切周期多等一次超时。
KLINE_SRC = (
    ("腾讯", "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"),
    ("腾讯备线", "https://ifzq.gtimg.cn/appstock/app/fqkline/get"),
)
SINA_HDR = {**HDR, "Referer": "https://finance.sina.com.cn/"}


def _brief(e) -> str:
    """requests 的异常文本里塞了整条 URL，报给用户时只留结论。"""
    s = str(e or "")
    if "501" in s or "Not Implemented" in s:
        return "HTTP 501（被 WAF 拦截）"
    if "403" in s:
        return "HTTP 403（被风控）"
    return s.split(" for url:")[0][:80] or s[:80]


def _tencent_kline(url: str, key: str, period: str, n: int) -> tuple[list, dict]:
    d = _get("%s?param=%s,%s,,,%d,qfq" % (url, key, period, n), timeout=15)
    node = d.get(key) or {}
    rows = node.get("qfq" + period) or node.get(period) or []
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
    return bars, _qt_meta(node.get("qt") or d.get("qt"), key)


def _agg_bars(bars: list, period: str) -> list:
    """日线聚合成周/月：开=首日开、收=末日收、高低取极值、量求和。

    周按 ISO 周分组（跨年那一周不会被切成两段），月按年月。
    """
    out, cur = [], None
    for b in bars:
        ds = b["date"]
        if period == "month":
            gk = ds[:7]
        else:
            try:
                y, w, _ = dt.date(int(ds[:4]), int(ds[5:7]), int(ds[8:10])).isocalendar()
                gk = "%04d-W%02d" % (y, w)
            except Exception:
                gk = ds[:7]
        if cur is None or cur["_k"] != gk:
            if cur:
                out.append(cur)
            cur = {"_k": gk, "date": ds, "open": b["open"], "close": b["close"],
                   "high": b["high"], "low": b["low"], "volume": b["volume"]}
        else:
            cur["date"] = ds
            cur["close"] = b["close"]
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["volume"] += b["volume"]
    if cur:
        out.append(cur)
    for x in out:
        x.pop("_k", None)
    return out


def _sina_kline(key: str, period: str, n: int) -> tuple[list, dict]:
    """新浪日线 → 需要的根数。只有日线接口，周/月在这里聚合。

    成交量新浪给的是「股」，腾讯给的是「手」，统一除以 100。
    """
    need = {"day": n + 5, "week": n * 5 + 30, "month": n * 22 + 40}[period]
    need = min(max(need, 300), 4800)
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (key, need))
    r = requests.get(url, headers=SINA_HDR, timeout=20)
    r.raise_for_status()
    arr = r.json()
    if not isinstance(arr, list) or not arr:
        raise RuntimeError("返回空数据")
    bars = []
    for x in arr:
        try:
            bars.append({"date": str(x["day"])[:10], "open": float(x["open"]),
                         "close": float(x["close"]), "high": float(x["high"]),
                         "low": float(x["low"]), "volume": float(x["volume"]) / 100.0})
        except Exception:
            continue
    if period != "day":
        bars = _agg_bars(bars, period)
    bars = bars[-n:]
    pre = bars[-2]["close"] if len(bars) > 1 else None
    return bars, {"name": "", "pre_close": pre}


def kline(code: str, period: str = "day") -> dict:
    period = period if period in ("day", "week", "month") else "day"
    key = key_of(code)
    ck = "%s:%s" % (period, key)
    hit = _cache_get(ck)
    if hit:
        return hit
    n = {"day": 620, "week": 320, "month": 200}[period]
    bars, meta, src, adjust, errs = [], {}, "", "qfq", []
    for name, url in KLINE_SRC:
        try:
            b, m = _tencent_kline(url, key, period, n)
            if b:
                bars, meta, src = b, m, name
                break
        except Exception as e:                       # noqa: BLE001
            errs.append("%s：%s" % (name, _brief(e)))
    if not bars:
        # 腾讯两条线都挂时用新浪日线自己合成周/月：多一个完全独立的第三方源，
        # 代价是不复权（除权日会有跳空），所以只在前面全失败时用，并在返回里写明。
        try:
            b, m = _sina_kline(key, period, n)
            if b:
                bars, meta, src, adjust = b, m, "新浪", "none"
        except Exception as e:                       # noqa: BLE001
            errs.append("新浪：%s" % _brief(e))
    if not bars:
        raise RuntimeError("K 线接口都不可用（%s）" % "；".join(errs))

    # 涨跌幅按前收算，首根没有前收则为 0
    for i, b in enumerate(bars):
        b["pct"] = round((b["close"] / bars[i - 1]["close"] - 1) * 100, 2) if i else 0.0

    out = {
        "ok": True, "code": key[2:], "market": key[:2], "name": meta["name"] or "",
        "period": period, "date": bars[-1]["date"],
        "pre_close": meta["pre_close"], "last": bars[-1]["close"],
        "pct": bars[-1]["pct"], "bars": bars, "count": len(bars),
        "source": src, "adjust": adjust,
        "advice": "前复权，最新价与实时行情一致" if adjust == "qfq" else "备用源不复权，除权日会有跳空",
    }
    return _cache_put(ck, out)


# --------------------------------------------------------------------------
# 大盘指数条：一次请求拿五个指数，盘中 8 秒缓存
# --------------------------------------------------------------------------

INDEX_SYMBOLS = (
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("bj899050", "北证50"),
    ("sh000688", "科创50"),
)
INDEX_TTL = 8
_INDEX_CACHE: dict = {"at": 0.0, "data": None}
# 指数卡右侧迷你走势：单独缓存 60 秒，避免 8 秒一次的报价刷新反复打 5 个分时接口
_SPARK_TTL = 60
_SPARK_CACHE: dict[str, tuple[float, list]] = {}


def _spark_points(key: str, limit: int = 48) -> list[float]:
    hit = _SPARK_CACHE.get(key)
    if hit and time.time() - hit[0] < _SPARK_TTL:
        return hit[1]
    try:
        d = _get("%s/appstock/app/minute/query?code=%s" % (BASE, key), timeout=8)
        node = d.get(key) or {}
        rows = ((node.get("data") or {}).get("data")) or []
        parsed = _parse_minutes(rows)
        pts = [round(v[0], 3) for _, v in sorted(parsed.items())]
        # 241 个分钟点塞进 64px 的小图没有意义，等距抽到 48 个
        if len(pts) > limit:
            step = (len(pts) - 1) / float(limit - 1)
            pts = [pts[int(round(i * step))] for i in range(limit)]
        _SPARK_CACHE[key] = (time.time(), pts)
        return pts
    except Exception:
        return hit[1] if hit else []


def _idx_time(raw: str) -> str:
    """'20261119100115' → '10:01:15'。"""
    s = str(raw or "")
    return "%s:%s:%s" % (s[8:10], s[10:12], s[12:14]) if len(s) >= 14 else ""


def indices(ttl: int = INDEX_TTL) -> dict:
    """五个主要指数的最新点位与涨跌幅。失败时返回上一次成功结果（如有）。"""
    now = time.time()
    hit = _INDEX_CACHE.get("data")
    if hit and now - float(_INDEX_CACHE.get("at") or 0) < ttl:
        return hit
    q = ",".join(code for code, _ in INDEX_SYMBOLS)
    try:
        r = requests.get("https://qt.gtimg.cn/q=" + q, headers=HDR, timeout=8)
        r.raise_for_status()
        r.encoding = "gbk"
        text = r.text
    except Exception as e:
        if hit:
            return hit
        return {"ok": False, "error": ("指数行情读取失败：%s" % str(e)[:120])}

    raw = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        try:
            name, body = line.split("=", 1)
            raw[name[2:]] = body.strip().strip('"').split("~")
        except Exception:
            continue

    def f(p, i):
        try:
            return float(p[i])
        except Exception:
            return None

    out = []
    for code, label in INDEX_SYMBOLS:
        p = raw.get(code) or []
        if len(p) < 35:
            continue
        price = f(p, 3)
        if price is None:
            continue
        pre = f(p, 4)
        pct = f(p, 32)
        if pct is None and pre:
            pct = round((price / pre - 1) * 100, 2)
        out.append({"code": code, "name": p[1] or label,
                    "price": price, "pre_close": pre,
                    "change": f(p, 31), "pct": pct,
                    "high": f(p, 33), "low": f(p, 34),
                    "time": _idx_time(p[30] if len(p) > 30 else "")})
    if not out:
        if hit:
            return hit
        return {"ok": False, "error": "指数行情暂时没有返回数据"}
    # 右侧迷你走势：5 个分时接口并行走，首次约几百毫秒，之后 60 秒内直接复用
    sparks = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        codes = [x["code"] for x in out]
        with ThreadPoolExecutor(max_workers=len(codes)) as ex:
            sparks = dict(zip(codes, ex.map(_spark_points, codes)))
    except Exception:
        sparks = {}
    for x in out:
        x["spark"] = sparks.get(x["code"]) or []
    data = {"ok": True, "list": out, "at": time.strftime("%H:%M:%S")}
    _INDEX_CACHE.update(at=now, data=data)
    return data


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
