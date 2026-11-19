# -*- coding: utf-8 -*-
"""
QuantDesk · 行业中性化与行业暴露

为什么要有这一层
----------------
行业的用处不止「单行业最多选 N 只」这种事后截断。GitHub 上成熟的 A 股多因子项目
都把行业放进**计算**里，主流做法基本一致：

  · Qlib（微软）        CSZScoreNorm / CSRankNorm 做横截面标准化，配合行业分组做组内标准化
  · A-share-quant       fill_na → winsorize → z-score → industry neutral → market cap neutral
  · zer0factor          截面去极值、标准化，以及行业 / 市值中性化
  · aq-quant-trading    行业 + 市值中性化（FWL 定理），再按中性化后的 RankIC 定因子方向

不做中性化的直接后果：动量因子会大量押注当期最热的行业，模型/基准分学到的是
「行业轮动 β」，一旦风格切换就连本带利还回去。中性化之后，因子只在同行业内部
比较相对强弱，选出来的才是「个股 α」。

本模块提供三个口径
------------------
1) industry  行业内去均值 + 行业内标准差标准化
             （等价于对行业哑变量做横截面回归取残差，即 FWL 定理最简单的特例）
             组内样本 < MIN_GROUP 时退回全市场 z —— 小行业用自己的噪声做分母会放大假信号
2) industry+size  在上面基础上再对 log 总市值做一次横截面回归取残差
             需要 Tushare 估值数据（total_mv）；覆盖率不足 60% 时自动降级并说明原因
3) none      原样全市场 z（旧口径，保留用于对照）

另外提供
--------
intra_rank(df, key)      行业内百分位（0~1）——「这只票在同行业里排第几」
exposure(picked, base)   入选组合的行业权重 vs 全市场基线 + HHI 集中度
size_available()         市值中性化当前是否可用、覆盖率多少
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MIN_GROUP = 5          # 组内少于 5 只不做组内标准化，避免小行业噪声被放大
WINSOR = 3.0           # 标准化后按 ±3σ 截尾
SIZE_MIN_COVER = 0.60  # 市值覆盖率低于此值不启用市值中性化
MODES = ("industry", "industry+size", "none")


def _ind_series(df: pd.DataFrame, col: str = "industry") -> pd.Series:
    """行业列清洗：NaN / 'nan' / None → 空字符串，空值不参与分组（走全市场口径）。"""
    if col not in df.columns:
        return pd.Series([""] * len(df), index=df.index)
    s = df[col].astype(str).str.strip()
    return s.where(~s.str.lower().isin(("", "nan", "none", "null")), "")


def zscore(s: pd.Series, clip: float = WINSOR) -> pd.Series:
    """横截面去极值 + z 标准化。标准差退化时返回 0（中性），不留 NaN 给下游。"""
    x = pd.to_numeric(s, errors="coerce")
    sd = x.std(ddof=0)
    if not np.isfinite(sd) or sd < 1e-9:
        return pd.Series(0.0, index=x.index)
    return ((x - x.mean()) / sd).clip(-clip, clip)


def neutral_z(df: pd.DataFrame, keys: list[str], mode: str = "industry",
              skip: set[str] | None = None, industry_col: str = "industry") -> pd.DataFrame:
    """对 keys 里的每个因子做横截面中性化，返回同形状的 z 值表（NaN 已填 0）。

    skip 里的因子不做行业内去均值：像「行业 20 日动能」本身就是组均值，
    被组内去均值会直接归零，必须原样保留全市场口径。
    """
    mode = mode if mode in MODES else "industry"
    skip = set(skip or ())
    ind = _ind_series(df, industry_col)
    grouped = ind.ne("")
    out = pd.DataFrame(index=df.index)

    for k in keys:
        if k not in df.columns:
            out[k] = 0.0
            continue
        x = pd.to_numeric(df[k], errors="coerce")
        if mode == "none" or k in skip:
            out[k] = zscore(x).fillna(0.0)
            continue

        g = x.groupby(ind)
        mu = g.transform("mean")
        sd = g.transform("std")
        cnt = g.transform("count")
        z = (x - mu) / sd
        # 行业为空 / 组内样本太少 / 组内方差退化 → 退回全市场 z（不丢样本，也不制造假信号）
        bad = (~grouped) | (cnt < MIN_GROUP) | (~np.isfinite(sd)) | (sd < 1e-9)
        z = z.where(~bad, zscore(x))
        out[k] = z.clip(-WINSOR, WINSOR).fillna(0.0)

    if mode == "industry+size":
        out = _size_neutralize(df, out)
    return out


def _size_neutralize(df: pd.DataFrame, z: pd.DataFrame) -> pd.DataFrame:
    """对 log 总市值做逐列截面回归取残差。数据不够就原样返回（宁可不做，也不猜）。

    注意：这里只看当前 DataFrame 是否有足够的 total_mv，不查「当前估值快照」。
    训练集可以从历史市值文件里带 total_mv，推理快照用当期估值，二者都能走通。
    """
    if "total_mv" not in df.columns:
        return z
    mv = pd.to_numeric(df["total_mv"], errors="coerce")
    ok = mv.notna() & (mv > 0)
    if int(ok.sum()) < 10:
        return z
    s = np.log(mv[ok])
    s = (s - s.mean()) / (s.std(ddof=0) or 1.0)
    out = z.copy()
    for k in z.columns:
        x = z[k][ok]
        var = float(s.var(ddof=0))
        if var < 1e-12:
            continue
        beta = float(((x - x.mean()) * (s - s.mean())).mean()) / var
        out.loc[ok, k] = x - beta * s
    return out


def size_available() -> dict:
    """市值中性化是否可用：当期估值看 daily_basic，训练看历史市值累积。"""
    try:
        import datasource
        basic = datasource.load_basic() or {}
        hist = datasource.mv_history_status()
    except Exception as e:
        return {"available": False, "cover": 0, "total": 0, "history_days": 0,
                "reason": "读取估值数据失败：%s" % str(e)[:60]}
    try:
        import industry
        total = len(industry.load_map())
    except Exception:
        total = 0
    cover = sum(1 for v in basic.values() if v.get("total_mv") not in ("", None))
    ratio = cover / total if total else 0.0
    history_days = int((hist or {}).get("days") or 0)
    return {"available": bool(total and (ratio >= SIZE_MIN_COVER or history_days >= 20)),
            "cover": cover, "total": total, "ratio": round(ratio * 100, 1),
            "history_days": history_days,
            "reason": "" if (ratio >= SIZE_MIN_COVER or history_days >= 20) else
                      "未拉取估值数据或覆盖不足（%d/%d），市值中性化自动跳过" % (cover, total)}


def intra_rank(df: pd.DataFrame, key: str = "score", industry_col: str = "industry") -> pd.Series:
    """行业内百分位 0~1（越大越强）。组内不足 3 只时退回全市场百分位；
    行业未知的样本返回 NaN —— 给它算一个「全市场百分位」会被误读成「行业内第一」。"""
    if key not in df.columns:
        return pd.Series([np.nan] * len(df), index=df.index)
    x = pd.to_numeric(df[key], errors="coerce")
    ind = _ind_series(df, industry_col)
    cnt = x.groupby(ind).transform("count")
    r_in = x.groupby(ind).rank(pct=True)
    r_all = x.rank(pct=True)
    return r_in.where(cnt >= 3, r_all).where(ind.ne(""), np.nan)


def exposure(picked: list[dict], base: pd.DataFrame | None = None) -> dict:
    """入选组合的行业暴露：权重、相对全市场基线的偏离、HHI 集中度。

    HHI = Σw²，等权 10 只分散在 10 个行业时 = 0.1，全押一个行业 = 1.0；
    有效行业数 = 1/HHI，比「覆盖了几个行业」更能看出是不是真的分散。
    """
    n = len(picked)
    if not n:
        return {"ok": False, "rows": [], "hhi": None, "effective_n": None,
                "industries": 0, "note": "没有入选标的"}
    counts: dict[str, int] = {}
    for r in picked:
        k = str(r.get("industry") or "").strip() or "未分类"
        counts[k] = counts.get(k, 0) + 1
    base_counts: dict[str, int] = {}
    base_total = 0
    if base is not None and len(base) and "industry" in base.columns:
        s = _ind_series(base).replace("", "未分类")
        base_counts = s.value_counts().to_dict()
        base_total = int(len(s))

    rows = []
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        w = v / n
        bw = (base_counts.get(k, 0) / base_total) if base_total else None
        rows.append({"industry": k, "n": v, "weight": round(w * 100, 1),
                     "base_weight": None if bw is None else round(bw * 100, 1),
                     "gap": None if bw is None else round((w - bw) * 100, 1)})
    hhi = sum((v / n) ** 2 for v in counts.values())
    top = rows[0]
    return {
        "ok": True, "n": n, "industries": len(counts), "unclassified": counts.get("未分类", 0),
        "hhi": round(hhi, 3), "effective_n": round(1.0 / hhi, 1) if hhi > 0 else None,
        "max_weight": top["weight"], "max_industry": top["industry"],
        "base_total": base_total, "rows": rows[:12], "rows_more": max(0, len(rows) - 12),
        "note": "HHI %.3f ≈ 有效行业数 %.1f 个；基线为本次横截面样本的行业权重" % (hhi, 1.0 / hhi if hhi else 0),
    }


def describe(mode: str = "industry") -> str:
    """给界面用的一句话口径说明，避免用户不知道分数被怎么处理过。"""
    return {
        "industry": "行业内标准化：每个因子先减去所属行业均值再除以行业标准差，剥离行业 β，只比同行业内的相对强弱",
        "industry+size": "行业 + 市值中性化：行业内标准化后再对 log 市值回归取残差，同时剥离行业与大小盘风格",
        "none": "原始横截面标准化：不剥离行业与风格暴露，保留行业轮动带来的收益，同时承担风格切换的风险",
    }.get(mode, "")
