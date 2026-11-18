# -*- coding: utf-8 -*-
"""行情缓存的「尾部读取」加速层

问题：data/cache 下 5300 个后复权日线 CSV，单只 216 KB / 2150 行。
而绝大多数计算只要最近一段 —— 因子最长窗口 61 个交易日，行业强度只要最后 21 行。
每次 pd.read_csv 读全表再取末尾，等于为了最后 100 行去解析 2100 行，
选股 1500 只 = 8 秒、行业强度 5200 只 = 13 秒，页面就卡在这里。

做法：seek 到文件末尾只读最后 n 行（表头单独从前几字节取），
解析行数少一个数量级，也不占内存、不占磁盘（C 盘只剩 1 GB，落 npz 缓存这条路走不通）。

口径必须与 screener.load_history 完全一致，否则因子会悄悄变化：
清洗规则集中在 _clean()，两边共用。
"""
from __future__ import annotations

import datetime as dt
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import numpy as np

BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / "data" / "cache"

# 与 screener.load_history 保持一致的数值列
COLS = ("open", "high", "low", "close", "volume", "amount", "pct", "turnover")
# 单行约 97 字节，留 1.3 倍余量；n 越大越安全，但尾部块也越大
ROW_BYTES = 130


def path_of(code: str) -> Path:
    return CACHE_DIR / (str(code).zfill(6) + ".csv")


def _clean(d: pd.DataFrame) -> pd.DataFrame:
    """与 screener.load_history 前半段完全相同的清洗。"""
    d = d.copy()
    d["date"] = d["date"].astype(str).str.slice(0, 10)
    for c in COLS:
        if c not in d.columns:
            d[c] = 0.0
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[(d["close"] > 0) & (d["open"] > 0)].sort_values("date")
    return d.drop_duplicates("date").reset_index(drop=True)


def _drop_unclosed(d: pd.DataFrame) -> pd.DataFrame:
    """盘中（15:00 前）当日未收盘 K 线必须剔除，否则因子含未完成 bar。"""
    if dt.datetime.now().hour < 15:
        day = dt.date.today().isoformat()
        d = d[d["date"] < day]
    return d


def _raw_tail(fp: Path, n: int) -> tuple[list[str], list[list[str]]] | None:
    """读 CSV 尾部 n 行原始文本行：先取表头，再 seek 到末尾取数据。

    pandas 的 read_csv 即使只喂 30 行也有 1.5 ms 的固定开销（解析器初始化、
    dtype 推断、BlockManager 组装），5200 只就白花 8 秒 —— 这才是卡顿的真凶，
    不是行数多。这里直接 csv 切行，30 行约 0.25 ms。
    """
    fp = Path(fp)
    if not fp.exists():
        return None
    try:
        with open(fp, "rb") as f:
            head = f.readline().decode("utf-8-sig", errors="replace").rstrip("\r\n")
            f.seek(0, 2)
            size = f.tell()
            back = min(size, n * ROW_BYTES + 512)
            f.seek(max(0, size - back))
            raw = f.read()
        txt = raw.decode("utf-8-sig", errors="replace")
        lines = txt.splitlines()
        if back < size and lines:
            lines = lines[1:]                 # 首行多半是被截断的半行
        return head.split(","), [ln.split(",") for ln in lines[-n:]]
    except Exception:
        return None


def ohlcv(code: str, n: int = 200) -> dict | None:
    """最近 n 根日线，直接给 numpy 数组（已过 close>0 过滤、按日期升序）。

    返回 {date: list[str], o,h,l,c,v,a: np.ndarray, t: np.ndarray}；读不到返回 None。
    给 last_features / 行业强度这类「只要数值」的场景用，绕开 DataFrame。
    """
    got = _raw_tail(path_of(code), n)
    if not got:
        return None
    head, rows = got
    try:
        idx = {k: head.index(k) for k in
               ("date", "open", "high", "low", "close", "volume", "amount", "turnover")}
    except ValueError:
        return None
    need = max(idx.values()) + 1
    KEYS = ("open", "high", "low", "close", "volume", "amount", "turnover")
    pos = [idx[k] for k in KEYS]

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return float("nan")          # 空字段 / "nan" 文本：pandas 读成 NaN，这里也一样

    dates, vals = [], []
    for r in rows:
        if len(r) < need:
            continue
        try:
            v = [float(r[p]) for p in pos]
        except (TypeError, ValueError):
            # 个别字段为空是常态（早期数据缺 amount），不能因此丢掉整行 ——
            # pandas 会保留该行并把那一列置 NaN，丢掉就会少一天 K 线。
            v = [_f(r[p]) for p in pos]
        dates.append(r[idx["date"]][:10])
        vals.append(v)
    if not vals:
        return None
    arr = np.asarray(vals, dtype="f8")
    out = {"date": dates, "o": arr[:, 0], "h": arr[:, 1], "l": arr[:, 2],
           "c": arr[:, 3], "v": arr[:, 4], "a": arr[:, 5], "t": arr[:, 6]}
    # 顺序必须与 screener.load_history 一致：先剔无效价（close/open<=0），
    # 再按日期排序、同日取第一条。反过来的话，同一天里 close=0 的脏行会先把
    # 正常行挤掉，再被过滤，等于凭空少一天数据 —— 实测会让 MACD 差 1 个点。
    keep = (out["c"] > 0) & (out["o"] > 0)
    if not keep.all():
        keep = np.asarray(keep, dtype=bool)
        out = {k: (v if k == "date" else v[keep]) for k, v in out.items()}
        out["date"] = [d for d, k in zip(dates, keep) if k]
        dates = out["date"]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        order = sorted(range(len(dates)), key=lambda i: dates[i])
        seen, keep_idx = set(), []
        for i in order:
            if dates[i] in seen:
                continue
            seen.add(dates[i])
            keep_idx.append(i)
        out = {k: (v if k == "date" else v[keep_idx]) for k, v in out.items()}
        out["date"] = [dates[i] for i in keep_idx]
    out = _drop_unclosed_arr(out)
    return out


def _drop_unclosed_arr(a: dict) -> dict:
    """盘中当日未收盘 bar 必须剔除，否则因子含未完成 K 线。"""
    if dt.datetime.now().hour >= 15 or not a["date"]:
        return a
    day = dt.date.today().isoformat()
    keep = [i for i, d in enumerate(a["date"]) if d < day]
    if len(keep) == len(a["date"]):
        return a
    return {k: ([v[i] for i in keep] if k == "date" else v[keep]) for k, v in a.items()}


def tail_frame(code: str, n: int = 200) -> pd.DataFrame | None:
    """只读 CSV 最后 n 行（自动补上表头）。文件太短时读全表。"""
    fp = path_of(code)
    if not fp.exists():
        return None
    try:
        with open(fp, "rb") as f:
            head = f.readline().decode("utf-8-sig", errors="replace").strip()
            f.seek(0, 2)
            size = f.tell()
            back = min(size, n * ROW_BYTES + 512)
            f.seek(max(0, size - back))
            raw = f.read()
        txt = raw.decode("utf-8-sig", errors="replace")
        lines = txt.splitlines()
        if back < size and lines:
            lines = lines[1:]                 # 首行多半是被截断的半行
        if len(lines) < n:                    # 尾部块没吃满，说明文件本身短，退回全量
            return _clean(pd.read_csv(fp, dtype={"date": str}))
        text = head + "\n" + "\n".join(lines[-n:])
        d = pd.read_csv(io.StringIO(text), dtype={"date": str})
    except Exception:
        try:
            d = pd.read_csv(fp, dtype={"date": str})
        except Exception:
            return None
    return _clean(d)


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
    """并行批量取尾部日线。磁盘随机读 + pandas 解析都会释放 GIL，线程有效。"""
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
    """倒数第 back 根已收盘 bar 的关键字段（行业强度这种只要一两个数就够）。"""
    d = tail_frame(code, max(8, back + 4))
    if d is None or len(d) < back:
        return None
    d = _drop_unclosed(d)
    if len(d) < back:
        return None
    r = d.iloc[-back]
    try:
        return {"date": str(r["date"]), "close": float(r["close"]),
                "turnover": float(r["turnover"]) if pd.notna(r["turnover"]) else 0.0}
    except Exception:
        return None


def invalidate():
    """CSV 被重写后无需清理：本模块不缓存数据，每次都按文件现状读。"""
    return True
