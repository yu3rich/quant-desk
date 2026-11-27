# -*- coding: utf-8 -*-
"""行情读取层（SQLite 版）

对外 API 与旧 CSV 版完全一致：ohlcv / tail_frame / recent / bulk_* / last_bar。
底层改为 store.bars_tail —— 主键 (code,date) 聚簇索引上的范围扫描，
「取最近 n 根」的耗时与该股历史总量无关，也不再需要 seek 文件尾的字节估算。

口径与 screener.load_history 一致：先剔无效价（close/open<=0，SQL WHERE 已做），
按日期升序、主键保证无重复；盘中（15:00 前）当日未收盘 bar 仍要剔除。
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import numpy as np

import store

COLS = store.COLS


def _drop_unclosed(d: pd.DataFrame) -> pd.DataFrame:
    """盘中（15:00 前）当日未收盘 K 线必须剔除，否则因子含未完成 bar。"""
    if dt.datetime.now().hour < 15:
        day = dt.date.today().isoformat()
        d = d[d["date"] < day]
    return d


def ohlcv(code: str, n: int = 200) -> dict | None:
    """最近 n 根有效日线，直接给 numpy 数组（升序）。

    返回 {date: list[str], o,h,l,c,v,a,t: np.ndarray}；读不到返回 None。

    列对齐只在这里发生一次，且**按列名映射**而不是写下标：
    以前是 `"t": arr[:, 6]`，而 store.COLS 去掉 date 后 arr[:,6] 是 pct、
    turnover 在 arr[:,7] —— 取错一列不会报错，只会让全市场快照的「换手率」
    实际变成当日涨跌幅，而模型最依赖的恰好是这个特征（重要性 0.20）。
    改成先建 列名→下标 的映射，新增/调整列时要么自动跟上、要么 KeyError 炸出来。
    """
    rows = store.bars_tail(code, n)
    if not rows:
        return None
    dates = [r[0] for r in rows]
    arr = np.asarray([[np.nan if v is None else v for v in r[1:]] for r in rows], dtype="f8")
    # store.COLS 含 date，bars_tail 每行是 (date, open, high, low, close, volume, amount, pct, turnover)
    # 去掉 date 后与 arr 的列一一对应
    names = [c for c in store.COLS if c != "date"]
    if arr.shape[1] != len(names):
        raise RuntimeError("bars 列数变化（%d 列 vs 期望 %d），请检查 store.COLS 与 SQL"
                           % (arr.shape[1], len(names)))
    idx = {name: i for i, name in enumerate(names)}
    out = {
        "date": dates,
        "o": arr[:, idx["open"]], "h": arr[:, idx["high"]],
        "l": arr[:, idx["low"]], "c": arr[:, idx["close"]],
        "v": arr[:, idx["volume"]], "a": arr[:, idx["amount"]],
        "t": arr[:, idx["turnover"]],
    }
    return _drop_unclosed_arr(out)


def _drop_unclosed_arr(a: dict) -> dict:
    if dt.datetime.now().hour >= 15 or not a["date"]:
        return a
    day = dt.date.today().isoformat()
    keep = [i for i, d in enumerate(a["date"]) if d < day]
    if len(keep) == len(a["date"]):
        return a
    return {k: ([v[i] for i in keep] if k == "date" else v[keep]) for k, v in a.items()}


def tail_frame(code: str, n: int = 200) -> pd.DataFrame | None:
    """最近 n 根有效日线（DataFrame，已清洗）。"""
    return store.bars_frame(code, n)


def recent(code: str, n: int = 200) -> pd.DataFrame | None:
    """最近 n 根已收盘日线（已清洗）。"""
    d = tail_frame(code, n)
    return None if d is None else _drop_unclosed(d).reset_index(drop=True)


def _one_ohlcv(args):
    code, n = args
    try:
        return code, ohlcv(code, n)
    except Exception:
        return code, None


def bulk_ohlcv(codes, n: int = 30, workers: int = 6) -> dict:
    """并行批量取尾部数组（行业强度这类「只要几个数」的场景用）。"""
    codes = list(codes)
    if not codes:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(codes))) as ex:
        for code, a in ex.map(_one_ohlcv, [(c, n) for c in codes]):
            out[code] = a
    return out


def _one(args):
    code, n = args
    try:
        return code, recent(code, n)
    except Exception:
        return code, None


def bulk_recent(codes, n: int = 200, workers: int = 6) -> dict:
    codes = list(codes)
    if not codes:
        return {}
    if len(codes) == 1:
        c = codes[0]
        return {c: _one((c, n))[1]}
    out = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(codes))) as ex:
        for code, d in ex.map(_one, [(c, n) for c in codes]):
            out[code] = d
    return out


def _col_index() -> dict:
    """store.COLS -> 下标。与 ohlcv 共用同一份映射，避免两处各写一套下标。"""
    return {name: i for i, name in enumerate(store.COLS)}


def last_bar(code: str, back: int = 1) -> dict | None:
    """倒数第 back 根已收盘 bar 的关键字段。"""
    rows = store.bars_tail(code, max(8, back + 4))
    if not rows:
        return None
    if dt.datetime.now().hour < 15:
        day = dt.date.today().isoformat()
        rows = [r for r in rows if r[0] < day]
    if len(rows) < back:
        return None
    r = rows[-back]
    ix = _col_index()
    try:
        t = r[ix["turnover"]]
        return {"date": str(r[ix["date"]]), "close": float(r[ix["close"]]),
                "turnover": float(t) if t is not None else 0.0}
    except Exception:
        return None


def invalidate():
    """兼容旧调用点：真正要清的是 chartdata 的图表缓存（TTL 180 秒）。

    数据库本身即读即见（WAL），这里只负责把下游图表缓存一起作废。
    """
    try:
        import chartdata
        chartdata.invalidate()
    except Exception:
        pass
    return True