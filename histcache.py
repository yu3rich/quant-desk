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

    返回 {date: list[str], o,h,l,c,v,a: np.ndarray, t: np.ndarray}；读不到返回 None。
    """
    rows = store.bars_tail(code, n)
    if not rows:
        return None
    dates = [r[0] for r in rows]
    arr = np.asarray([[np.nan if v is None else v for v in r[1:]] for r in rows], dtype="f8")
    out = {"date": dates, "o": arr[:, 0], "h": arr[:, 1], "l": arr[:, 2],
           "c": arr[:, 3], "v": arr[:, 4], "a": arr[:, 5], "t": arr[:, 6]}
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
    try:
        return {"date": str(r[0]), "close": float(r[4]),
                "turnover": float(r[8]) if r[8] is not None else 0.0}
    except Exception:
        return None


def invalidate():
    """数据库即读即见（WAL），无需清理缓存。"""
    return True