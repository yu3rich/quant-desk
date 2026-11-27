# -*- coding: utf-8 -*-
"""
QuantDesk · 单因子体检（factor tear sheet）

为什么要有这一页：
  24 个因子喂给 LightGBM，最后只看到模型级 RankIC 0.1178。这个数字回答不了
  三个真正影响收益的问题：
    1) 哪个因子在衰减？—— 需要 IC 的时序，不是均值
    2) 因子之间是否在重复计数？—— 需要相关矩阵
    3) 按这个因子选股，换手有多高？—— 需要分位换手率
  第 3 点最要命：A 股月频选股，若 Top 分位每期换掉 80%，双边成本 0.1%/期，
  一年 12 期就是 1.2%，足以吃掉一半超额。所以「秩自相关 < 0.5 的因子必须配换手上限」。

指标口径：
  IC       每期截面 spearman(因子值, 未来 h 日收益)；不取绝对值，方向由符号体现
  ICIR     IC 均值 / IC 标准差（按日频算，不年化）；t = ICIR × sqrt(期数)
  正 IC 率  IC > 0 的期数占比
  十分位    每期按因子分 10 组，取各组未来收益均值，再看跨期平均
  多空      D10 − D1（每期先算再平均）
  分位换手  相邻两期 Top 10% 成分的变动比例（1 − 重叠率）
  秩自相关  相邻两期因子横截面秩的相关系数，衡量信号衰减速度
  覆盖率    该因子非空比例（行业类因子在北交所/新股上天然缺失）

只读：本模块不写库、不改打分链路，纯统计。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

import listing
import review
import screener
import store

from paths import BASE, DATA
CACHE_PATH = BASE / "data" / "screen" / "factor_checkup.json"
DECILES = 10


# 口径/排序/文案一改，旧缓存就不能再用 —— 否则页面会显示上一版的结论，
# 而且用户完全看不出来。改逻辑时把这个数字 +1。
CACHE_VERSION = 2


def _cache_key(days: int, step: int, horizon: int, limit: int,
               with_cap: bool = True) -> tuple:
    """缓存维度里必须带 with_cap：带不带资金流因子是两套结果，
    共用一份缓存会让关掉资金流时仍然返回那 6 行。"""
    cap = 1 if with_cap else 0
    try:
        return (store.data_day(), int(days), int(step), int(horizon), int(limit),
                screener.FEATURE_VERSION, CACHE_VERSION, cap)
    except Exception:
        return ("", int(days), int(step), int(horizon), int(limit), 0, CACHE_VERSION, cap)


def _sig(key) -> str:
    return "|".join(str(x) for x in key)


# 缓存槽位：120/60/20 期来回切是最常见的用法，只留一份等于每次切回去都重算。
MAX_SLOTS = 5


def _read_store() -> dict:
    """读缓存文件，顺手把旧的单条格式迁成多槽格式。"""
    if not CACHE_PATH.exists():
        return {"slots": {}}
    try:
        d = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"slots": {}}
    if isinstance(d.get("slots"), dict):
        return d
    if d.get("key") and d.get("data"):
        return {"slots": {_sig(d["key"]): {"key": d["key"], "at": d.get("at", ""),
                                           "data": d["data"]}}}
    return {"slots": {}}


def _load_cache(key) -> dict | None:
    hit = (_read_store().get("slots") or {}).get(_sig(key))
    return (hit or {}).get("data")


def _save_cache(key, data: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        st = _read_store()
        slots = st.setdefault("slots", {})
        slots[_sig(key)] = {"key": list(key),
                            "at": dt.datetime.now().isoformat(timespec="seconds"),
                            "data": data}
        if len(slots) > MAX_SLOTS:
            keep = sorted(slots.items(), key=lambda kv: kv[1].get("at", ""))[-MAX_SLOTS:]
            st["slots"] = dict(keep)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


def _build(days: int, step: int, horizon: int, limit: int) -> tuple[pd.DataFrame, dict]:
    """把逐期横截面摊成一张长表：date / code / 各因子 / fwd。"""
    pool = review._replay_pool(limit)
    if not pool:
        raise RuntimeError("没有本地行情缓存")
    panel = review._panel(pool)
    if len(panel) < 20:
        raise RuntimeError("可评估股票不足（%d 只），请先预热更多行情" % len(panel))
    bench, bench_n = review._bench_panel()
    cal = review._calendar(bench)
    if len(cal) < horizon + 10:
        raise RuntimeError("缓存历史太短")
    usable = cal[:-(horizon + 1)]
    marks = usable[-days:] if len(usable) > days else usable
    marks = marks[::step] if step > 1 else marks

    rows = []
    for date in marks:
        fwd, _st = review._fwd(panel, date, horizon, cal=cal)
        if len(fwd) < 20:
            continue
        for code, f in panel.items():
            if code not in fwd or not listing.alive(code, date):
                continue
            hit = f.index[f["date"] == date]
            if len(hit) == 0:
                continue
            rec = f.iloc[int(hit[0])]
            item = {"date": date, "code": code, "fwd": fwd[code]}
            for k in screener.FEATURE_KEYS:
                v = rec.get(k)
                item[k] = float(v) if isinstance(v, (int, float, np.floating)) and v == v else np.nan
            rows.append(item)
    if not rows:
        raise RuntimeError("可回看的交易日不足")
    return pd.DataFrame(rows), {"bench_n": int(bench_n), "pool": len(panel),
                                "marks": len(marks)}


def _num(df: pd.DataFrame, k: str) -> pd.Series:
    """统一转数值。资金流因子拼表后容易退化成 object（None 与 NaN 混在一起），
    object 列既不能 qcut 也不能 nlargest，这一步必须显式做。"""
    return pd.to_numeric(df[k], errors="coerce")


def _decile_table(df: pd.DataFrame, keys: list[str]) -> dict[str, list[float]]:
    """每期按因子分 10 组，返回各组跨期平均收益。"""
    out = {k: [] for k in keys}
    for k in keys:
        acc = [[] for _ in range(DECILES)]
        for _, sub in df.groupby("date"):
            v = _num(sub, k)
            ok = v.notna()
            if ok.sum() < DECILES * 2:
                continue
            try:
                b = pd.qcut(v[ok], DECILES, labels=False, duplicates="drop")
            except Exception:
                continue
            if b.nunique() < DECILES // 2:
                continue
            f = sub.loc[ok, "fwd"]
            for g in range(int(b.max()) + 1):
                m = b == g
                if m.any():
                    # 分箱数少于 10 时，按比例映射到最后 10 个桶位，避免错位
                    pos = int(round(g * (DECILES - 1) / max(1, int(b.max()))))
                    acc[pos].append(float(f[m].mean()))
        out[k] = [round(float(np.mean(a)), 3) if a else None for a in acc]
    return out


def _top_turnover(df: pd.DataFrame, keys: list[str], frac: float = 0.1) -> dict[str, float]:
    """相邻两期 Top 分位的成分变动比例 —— 衡量「这个因子要求你换多少仓」。"""
    out = {}
    for k in keys:
        prev, chg = None, []
        for _, sub in df.groupby("date"):
            v = _num(sub, k)
            ok = v.notna()
            n = max(2, int(round(ok.sum() * frac)))
            if ok.sum() < 10:
                prev = None
                continue
            cur = set(sub.loc[ok].assign(_v=v[ok]).nlargest(n, "_v")["code"])
            if prev is not None:
                chg.append(1 - len(cur & prev) / max(1, len(cur)))
            prev = cur
        out[k] = round(float(np.mean(chg)), 3) if chg else None
    return out


def _autocorr(df: pd.DataFrame, keys: list[str]) -> dict[str, float]:
    """相邻两期因子横截面秩的相关系数：越低说明信号衰减越快。"""
    out = {}
    dates = sorted(df["date"].unique())
    if len(dates) < 3:
        return {k: None for k in keys}
    by_date = {d: sub.set_index("code") for d, sub in df.groupby("date")}
    for k in keys:
        vals = []
        for a, b in zip(dates[:-1], dates[1:]):
            x = pd.to_numeric(by_date[a][k], errors="coerce")
            y = pd.to_numeric(by_date[b][k], errors="coerce")
            common = x.index.intersection(y.index)
            if len(common) < 20:
                continue
            xr = x.loc[common].rank()
            yr = y.loc[common].rank()
            c = xr.corr(yr)
            if c == c:
                vals.append(float(c))
        out[k] = round(float(np.mean(vals)), 3) if vals else None
    return out


def _ic_table(df: pd.DataFrame, keys: list[str]) -> dict[str, dict]:
    """逐期截面 RankIC 及其汇总。

    用「先 rank 再皮尔逊」算 spearman：比 scipy 依赖少，且能一次算完 24 列
    （corrwith 是向量化的），120 期 × 24 因子在 1 秒内跑完。
    """
    series: dict[str, list[tuple[str, float]]] = {k: [] for k in keys}
    for date, sub in df.groupby("date"):
        if len(sub) < 30:
            continue
        r = sub[keys + ["fwd"]].rank()
        cors = r[keys].corrwith(r["fwd"])
        for k in keys:
            v = cors.get(k)
            if v == v:
                series[k].append((date, float(v)))
    out = {}
    for k in keys:
        arr = np.array([v for _, v in series[k]], dtype=float)
        if len(arr) < 3:
            out[k] = {"n": len(arr), "ic": None, "icir": None, "t": None,
                      "pos": None, "series": []}
            continue
        mean, std = float(arr.mean()), float(arr.std(ddof=1))
        icir = mean / std if std > 1e-9 else 0.0
        out[k] = {"n": int(len(arr)), "ic": round(mean, 4),
                  "icir": round(icir, 3),
                  "t": round(icir * np.sqrt(len(arr)), 2),
                  "pos": round(float((arr > 0).mean()) * 100, 1),
                  "ic_std": round(std, 4),
                  "series": [{"d": d, "v": round(v, 4)} for d, v in series[k]]}
    return out


def _monthly(series: list[dict]) -> list[dict]:
    acc: dict[str, list[float]] = {}
    for x in series:
        m = str(x["d"])[:7]
        acc.setdefault(m, []).append(x["v"])
    return [{"m": m, "ic": round(float(np.mean(v)), 4), "n": len(v)}
            for m, v in sorted(acc.items())]


def _corr_matrix(df: pd.DataFrame, keys: list[str]) -> dict:
    """因子相关矩阵（两两 spearman，跨期平均）。

    单期相关在样本少时噪声大，所以先每期算、再对期数取平均；
    相关性 >0.8 的组合基本可以认为在重复计数同一个信号。
    """
    dates = sorted(df["date"].unique())
    mats = []
    for d in dates[-60:]:
        sub = df[df["date"] == d][keys]
        if len(sub) < 30:
            continue
        r = sub.rank()
        mats.append(r.corr().to_numpy())
    if not mats:
        return {"keys": keys, "matrix": []}
    stack = np.stack(mats)
    ok = np.isfinite(stack)
    cnt = ok.sum(axis=0)
    total = np.where(ok, stack, 0.0).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        m = np.where(cnt > 0, total / np.maximum(cnt, 1), np.nan)
    return {"keys": keys, "matrix": [[None if x != x else round(float(x), 3) for x in row]
                                     for row in m],
            "periods": len(mats)}


def checkup(days: int = 120, step: int = 1, horizon: int = 5, limit: int = 300,
            force: bool = False, extra: list[dict] | None = None,
            with_cap: bool = True) -> dict:
    """单因子体检主入口。结果按（数据日/参数/因子版本/是否含资金流）缓存，页面秒开。

    资金流那 6 个因子要逐日 as-of 拼表，实测 9~10 秒，是主表计算的几十倍。
    它必须放在缓存命中判断**之后**算：放在路由里预计算的话，缓存再快也白搭
    —— 每次进页面仍然先白跑 10 秒，然后把结果丢掉。
    """
    days = max(20, min(int(days), 400))
    step = max(1, min(int(step), 20))
    horizon = int(horizon) if int(horizon) in (1, 5, 10, 20) else 5
    limit = max(50, min(int(limit), 2000))
    key = _cache_key(days, step, horizon, limit, with_cap)
    if not force:
        got = _load_cache(key)
        if got:
            got["cached"] = True
            return got

    try:
        df, meta = _build(days, step, horizon, limit)
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}

    keys = [k for k in screener.FEATURE_KEYS if k in df.columns]
    extra = list(extra or [])
    if with_cap and not extra:
        extra = _capital_rows(limit, days, step, horizon)
    ics = _ic_table(df, keys)
    dec = _decile_table(df, keys)
    turn = _top_turnover(df, keys)
    auto = _autocorr(df, keys)
    cov = {k: round(float(df[k].notna().mean()), 3) for k in keys}

    rows = []
    for k in keys:
        f = next((x for x in screener.FEATURES if x["key"] == k), {})
        ic = ics.get(k) or {}
        d = dec.get(k) or []
        ls = None
        if d and d[-1] is not None and d[0] is not None:
            ls = round(float(d[-1] - d[0]), 3)
        rows.append({
            "key": k, "name": f.get("name", k), "group": f.get("group", ""),
            "desc": f.get("desc", ""), "model_only": bool(f.get("model_only")),
            "ic": ic.get("ic"), "icir": ic.get("icir"), "t": ic.get("t"),
            "pos": ic.get("pos"), "n": ic.get("n"),
            "deciles": d, "ls": ls,
            "turnover": turn.get(k), "autocorr": auto.get(k),
            "cover": cov.get(k),
            "monthly": _monthly(ic.get("series") or []),
            "series": (ic.get("series") or [])[-120:],
        })

    # 外部资金流因子（P2-6）：单独一组，不进模型，只作为对照
    if extra:
        for x in extra:
            rows.append(x)

    # 排序要防「小样本假冠军」：只有 16 期、覆盖率 20% 的因子很容易刷出 IC 0.4 的假象，
    # 把它顶在第一位等于骗人。样本不足的（n<30 或覆盖<30%）一律排到后面。
    def _rank_key(r: dict):
        thin = (r.get("n") or 0) < 30 or (r.get("cover") or 0) < 0.3
        return (thin, r.get("icir") is None, -(r.get("icir") or -99))

    rows.sort(key=_rank_key)
    data = {
        "ok": True, "horizon": horizon, "days": days, "step": step,
        "periods": int(df["date"].nunique()), "pool": meta["pool"],
        "bench_n": meta["bench_n"], "factor_version": screener.FEATURE_VERSION,
        "date_from": str(df["date"].min()), "date_to": str(df["date"].max()),
        "rows": rows,
        "corr": _corr_matrix(df, keys),
        "note": "IC 为逐期截面 spearman，单位是相关系数（0.03 已属可用）；"
                "ICIR = IC 均值 / 标准差，t = ICIR×√期数，|t|>2 才算统计显著；"
                "分位换手是 Top10% 成分相邻两期的变动比例，换手越高越吃成本；"
                "秩自相关 <0.5 的因子必须配换手上限，否则超额只是纸面。"
                "期数少于 30 或覆盖率低于 30% 的因子排在末尾——它们能刷出很高的 IC，"
                "但那是小样本的假象，不能当结论。本页只做统计，不改打分链路。",
        "cached": False,
    }
    _save_cache(key, data)
    return data


def quick(limit: int = 150, days: int = 40) -> dict:
    """轻量版（首屏用）：样本小、期数少，只求快。资金流因子不参与。"""
    return checkup(days=days, step=2, horizon=5, limit=limit, with_cap=False)


# --------------------------------------------------------------------------
# 外部资金流因子（P2-6）：不进模型，只作对照
# --------------------------------------------------------------------------

def _build_capital(days: int, step: int, horizon: int, limit: int) -> pd.DataFrame:
    """把资金流因子也摊成同一张长表（date/code/fwd/cap_*），指标口径完全一致。

    严格 as-of：每个日期只调用 capital.factor_frame(codes, date)，
    内部只用 date 及之前的记录，不会出现「用今天的两融余额回填上个月」。
    """
    import capital
    pool = review._replay_pool(limit)
    if not pool:
        return pd.DataFrame()
    panel = review._panel(pool)
    bench, _n = review._bench_panel()
    cal = review._calendar(bench)
    usable = cal[:-(horizon + 1)]
    marks = usable[-days:] if len(usable) > days else usable
    marks = marks[::step] if step > 1 else marks
    frames = []
    for date in marks:
        fwd, _st = review._fwd(panel, date, horizon, cal=cal)
        codes = [c for c in panel if c in fwd and listing.alive(c, date)]
        if len(codes) < 20:
            continue
        f = capital.factor_frame(codes, date)
        if f is None or f.empty:
            continue
        f = f.assign(date=date, fwd=[fwd[c] for c in f["code"]])
        frames.append(f)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _capital_rows(limit: int = 300, days: int = 120, step: int = 1,
                  horizon: int = 5) -> list[dict]:
    """资金流因子的体检行（与主表同结构，group=资金流）。数据不足时返回空表。"""
    try:
        import capital
        # 只想知道「有没有数据」，用轻量版（0.01s）；status() 要逐字段扫表（0.3s）
        if not capital.fresh().get("total"):
            return []
        df = _build_capital(days, step, horizon, limit)
        if df.empty:
            return []
        keys = [x["key"] for x in capital.CAP_FACTORS if x["key"] in df.columns]
        if not keys:
            return []
        ics = _ic_table(df, keys)
        dec = _decile_table(df, keys)
        turn = _top_turnover(df, keys)
        auto = _autocorr(df, keys)
        cov = {k: round(float(df[k].notna().mean()), 3) for k in keys}
        meta = {x["key"]: x for x in capital.factor_meta()}
        out = []
        for k in keys:
            m = meta.get(k, {})
            ic = ics.get(k) or {}
            d = dec.get(k) or []
            ls = (round(float(d[-1] - d[0]), 3)
                  if d and d[-1] is not None and d[0] is not None else None)
            out.append({"key": k, "name": m.get("name", k), "group": "资金流",
                        "desc": m.get("desc", ""), "model_only": True,
                        "ic": ic.get("ic"), "icir": ic.get("icir"), "t": ic.get("t"),
                        "pos": ic.get("pos"), "n": ic.get("n"),
                        "deciles": d, "ls": ls, "turnover": turn.get(k),
                        "autocorr": auto.get(k), "cover": cov.get(k),
                        "monthly": _monthly(ic.get("series") or []),
                        "series": (ic.get("series") or [])[-120:],
                        "external": True})
        return out
    except Exception:
        return []
