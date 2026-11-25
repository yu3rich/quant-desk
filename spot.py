# -*- coding: utf-8 -*-
"""
spot.py — 全市场实时快照（腾讯批量行情，绕开东财 push2 限流）

为什么单独一层：
  - 东财 push2 对批量请求会限流（已实测触发），而名单/复权行情都一度依赖它
  - 腾讯 qt.gtimg.cn 支持一次批量查询多只，单只返回 现价/涨跌幅/开高低/成交额/换手率
  - 结果落 data/spot.csv，供「全 A 股名单」叠加最新价，不改变历史缓存的复权口径

对外：
  refresh(codes=None)   批量刷新并落盘，返回 {ok,count,updated}
  load()                读取最近一次快照 {code: {...}}
  status()              快照时间 / 条数 / 是否新鲜
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import datetime as dt
import json
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(parents=True, exist_ok=True)
SPOT_CSV = DATA / "spot.csv"
SPOT_META = DATA / "spot.json"
UNIVERSE = DATA / "universe.csv"
FIELDS = ["code", "name", "price", "pct", "open", "high", "low",
          "volume", "amount", "turnover", "time"]
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def _prefix(code: str) -> str:
    from bulkdata import market_kind
    return {"sh": "sh", "sz": "sz", "bj": "bj"}.get(market_kind(code), "")


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fetch_chunk(keys: list[str]) -> dict[str, dict]:
    url = "https://qt.gtimg.cn/q=" + ",".join(keys)
    r = __import__("requests").get(url, headers=HEADERS, timeout=10)
    txt = r.content.decode("gbk", errors="ignore")
    out: dict[str, dict] = {}
    for seg in txt.split(";"):
        seg = seg.strip()
        if not seg.startswith("v_"):
            continue
        try:
            _, val = seg.split("=", 1)
            p = val.strip().strip('"').split("~")
            if len(p) < 39:
                continue
            code = p[2].strip()
            if len(code) != 6:
                continue
            out[code] = {
                "code": code, "name": p[1].strip(), "price": p[3], "pct": p[32],
                "prev_close": p[4],
                "open": p[5], "high": p[33], "low": p[34],
                "volume": p[36], "amount": p[37], "turnover": p[38], "time": p[30],
            }
        except Exception:
            continue
    return out


def _codes() -> list[str]:
    if not UNIVERSE.exists():
        return []
    try:
        with UNIVERSE.open("r", encoding="utf-8-sig", newline="") as f:
            return [r["code"] for r in csv.DictReader(f) if r.get("code")]
    except Exception:
        return []


def refresh(codes: list[str] | None = None, workers: int = 4, merge: bool = False) -> dict:
    """刷新快照。merge=True 时保留未参与本次刷新的旧行（只补一部分代码时用）。"""
    codes = codes or _codes()
    keys = [f"{_prefix(c)}{c}" for c in codes if _prefix(c)]
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {"ok": False, "error": "没有可刷新的股票代码"}
    got: dict[str, dict] = {}
    try:
        with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = [ex.submit(_fetch_chunk, ch) for ch in _chunks(keys, 60)]
            for fu in cf.as_completed(futs):
                try:
                    got.update(fu.result())
                except Exception:
                    continue
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    if not got:
        return {"ok": False, "error": "腾讯行情返回为空，可能网络被拦截，稍后重试"}
    rows = list(got.values())
    if merge:
        # 只刷新了部分代码时，别把全市场快照覆盖成几条
        rows = [v for k, v in load().items() if k not in got] + rows
    rows.sort(key=lambda x: x["code"])
    with SPOT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SPOT_META.write_text(json.dumps({"updated": now, "count": len(rows),
                                     "source": "腾讯行情"}, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "count": len(rows), "updated": now,
            "up": sum(1 for r in rows if _f(r["pct"]) > 0),
            "down": sum(1 for r in rows if _f(r["pct"]) < 0)}


def _f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def prev_closes(codes: list[str]) -> dict[str, dict]:
    """批量取「真实昨收 / 现价 / 行情日期」。

    腾讯字段 4 = 昨收（真实价，未复权），字段 3 = 现价，字段 30 = 行情时间
    （yyyymmddHHMMSS）。拿这三个数就能把本地后复权价换算回真实价：
    复权因子 = 真实昨收 ÷ 后复权收盘（同一交易日），两次除权之间因子不变。
    返回 {code: {"prev_close": float, "price": float, "qdate": "yyyymmdd"}}。
    """
    keys = [f"{_prefix(c)}{c}" for c in codes if _prefix(c)]
    keys = list(dict.fromkeys(keys))
    out: dict[str, dict] = {}
    if not keys:
        return out
    try:
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(_fetch_chunk, ch) for ch in _chunks(keys, 60)]
            for fu in cf.as_completed(futs):
                try:
                    got = fu.result()
                except Exception:
                    continue
                for code, r in got.items():
                    pc, px, qt = _f(r.get("prev_close")), _f(r.get("price")), str(r.get("time") or "")
                    if pc <= 0 and px <= 0:
                        continue
                    out[code] = {"prev_close": pc if pc > 0 else None,
                                 "price": px if px > 0 else None,
                                 "qdate": qt[:8] if len(qt) >= 8 else ""}
    except Exception:
        return out
    return out


def real_price(hfq_close: float, hfq_last: float, last_date: str, q: dict | None) -> float | None:
    """把某日的后复权收盘换算成该日的真实成交价。

    hfq_last / last_date 是本地缓存的最新一根已收盘 bar；q 是 prev_closes 的一条。
    行情日期等于缓存最新日时用现价（收盘后现价≈定盘价），否则用昨收
    （昨收正好是「缓存最新日」的真实收盘 —— 盘中昨收不会变成当天）。
    两次除权之间因子恒定，因此对更早的历史日期同样成立。
    """
    if not q or not hfq_close or not hfq_last or hfq_last <= 0:
        return None
    ref = None
    qd = str(q.get("qdate") or "")
    ld = str(last_date or "").replace("-", "")
    if qd and ld and qd == ld:
        ref = q.get("price")
    if not ref:
        ref = q.get("prev_close")
    if not ref or ref <= 0:
        return None
    try:
        return round(float(hfq_close) * float(ref) / float(hfq_last), 2)
    except Exception:
        return None


def load() -> dict[str, dict]:
    if not SPOT_CSV.exists():
        return {}
    try:
        with SPOT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
            return {r["code"]: r for r in csv.DictReader(f) if r.get("code")}
    except Exception:
        return {}


def status() -> dict:
    meta = {}
    if SPOT_META.exists():
        try:
            meta = json.loads(SPOT_META.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    fresh = False
    if meta.get("updated"):
        try:
            t = dt.datetime.strptime(meta["updated"], "%Y-%m-%d %H:%M:%S")
            fresh = (dt.datetime.now() - t).total_seconds() < 6 * 3600
        except Exception:
            fresh = False
    return {"ok": True, "updated": meta.get("updated", ""), "count": meta.get("count", 0),
            "fresh": fresh, "file": str(SPOT_CSV)}
