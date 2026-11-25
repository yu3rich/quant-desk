# -*- coding: utf-8 -*-
"""
QuantDesk 选股框架 · T+N 后验闭环

两件事：
  1) scorecard()：把「当前这套打分逻辑」放回历史每一期重放一遍，看 T+5/10/20 的真实结果
     —— 用本地缓存日线自己算前瞻收益，不依赖任何外部评估服务
  2) track_record()：把每次真实选股落盘的候选（store.picks）在到期后回看

口径（写进界面，避免误读）：
  - 收益 = T 日收盘打分 → T+1 收盘等权建仓 → 第 N 个交易日收盘（本地缓存后复权）
  - 结算四条规则（对齐 TradingAgents settlement 的严谨性）：
      1. 建仓在 T 之后第一个交易日收盘；该日停牌买不到 → 不计入（no_entry）
      2. 必须走满 h 个交易日才结算；不足 → pending，不算半截收益
      3. 停牌日无 bar → 按目标日之前最后一根 bar 的收盘价 mark-to-last（不偷看复牌价）
      4. 退市 → 用末根 bar 结算并标 delisted（这部分亏损必须留在样本里，否则就是幸存者偏差）
  - 超额 = TopN 等权平均收益 − 同一天「同期参与评估股票」的等权平均收益
  - 行业超额 = TopN 等权平均收益 − 同一天「同行业可评估股票」的等权平均收益
  - 幸存者偏差：股票池按 listing.as_of 过滤，历史上已退市的票只要本地有行情就参与回放
  - 胜率 = TopN 中收益 > 0 的比例；跑赢率 = 收益高于同日全样本均值的比例
  - 样本期数不足时只报数不下结论
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

import listing
import screener
import store

BASE = Path(__file__).resolve().parent
HORIZONS = (5, 10, 20)
MAX_ENTRY_GAP = 10          # 建仓日停牌超过这么多个交易日，视为买不到

try:
    import industry as _industry
    IND_MAP: dict[str, str] = _industry.load_map()
except Exception:
    IND_MAP = {}


def _ind_bench(fwd: dict[str, float], picks: pd.DataFrame) -> float | None:
    """入选股票所属行业的当日等权收益均值（每个行业先算均值，再对入选行业取平均）。"""
    if not IND_MAP or "industry" not in picks.columns:
        return None
    by_ind: dict[str, list[float]] = {}
    for code, r in fwd.items():
        ind = IND_MAP.get(str(code).zfill(6))
        if ind:
            by_ind.setdefault(ind, []).append(r)
    vals = []
    for ind in picks["industry"].astype(str).unique():
        if ind and ind in by_ind:
            vals.append(float(np.mean(by_ind[ind])))
    return float(np.mean(vals)) if vals else None


def _panel(codes: list[str], as_of: str | None = None) -> dict:
    """一次性读入所有缓存股票的特征与收盘价，按日期建索引，供逐期重放。"""
    panel = {}
    for code in codes:
        d = screener.load_history(code, as_of)
        if d is None or len(d) < 100:
            continue
        f = screener.feature_frame(d)
        f = f.dropna(subset=["mom20", "vol20"])
        if f.empty:
            continue
        f = f.reset_index(drop=True)
        f["_i"] = range(len(f))                 # 该股在自身序列中的位置 → 用于算 T+h
        panel[code] = f
    return panel


def _score_rows(df: pd.DataFrame) -> tuple[pd.Series, str]:
    """优先用已训练模型打分；未训练则用可复现的方向加权基准分。"""
    try:
        import mlmodel
        pred = mlmodel.predict_frame(df)
        if pred is not None:
            return pd.Series(pred, index=df.index), mlmodel._meta().get("engine", "lightgbm")
    except Exception:
        pass
    return screener.base_scores(df), "baseline"


_LOG_CACHE = {"key": None, "data": None}


def _latest_day() -> str:
    """行情缓存的数据日（多数股票到齐的那天）；取不到才退回读单只票尾部。"""
    try:
        d = store.data_day()
        if d:
            return d
    except Exception:
        pass
    try:
        import histcache
        return (histcache.last_bar("600000") or {}).get("date", "")[:10]
    except Exception:
        return ""


def _panel_close(codes: list[str], n: int = 700) -> dict:
    """只带 date/close 的轻量面板。

    选股记录只需要「某日收盘 → 之后第 h 日收盘」的比值，不需要 24 个因子；
    旧版复用 _panel 会把每只股票的特征矩阵都算一遍（20 ms/只，200 只 4 秒），
    而这里读 CSV 尾部就够，0.3 ms/只。
    """
    import histcache
    got = histcache.bulk_ohlcv(codes, n, workers=6)
    out = {}
    for c in codes:
        a = got.get(c)
        if not a or len(a["c"]) < 2:
            continue
        out[c] = pd.DataFrame({"date": list(a["date"]), "close": a["c"]})
    return out


_BENCH = {"key": None, "panel": {}, "n": 0, "cal": []}


def _bench_panel(limit: int = 800) -> tuple[dict, int]:
    """全市场等权基准用的宽基面板（等间隔抽样约 800 只）。

    落盘记录只有几十只候选，若拿「候选自己」当基准算超额，等于自己跟自己比，
    永远接近 0。基准必须来自候选之外的全市场横截面；启动时建一次、按最新行情日缓存。
    """
    key = (_latest_day(), int(limit))
    if _BENCH["key"] == key and _BENCH["panel"]:
        return _BENCH["panel"], int(_BENCH["n"])
    codes = screener.resolve_scope("cached", limit)
    panel = _panel_close(codes)
    cal = sorted({d for f in panel.values() for d in f["date"]})
    _BENCH.update(key=key, panel=panel, n=len(panel), cal=cal)
    return panel, len(panel)


def _calendar(bench: dict | None = None) -> list[str]:
    """交易日历：用基准面板的日期并集，而不是各股日期交集。

    交集口径在引入退市股后会塌掉——2009 年退市的票没有 2024 年的 bar，
    交集直接变成空集，回放窗口会莫名其妙退到十几年前。并集取尾部才是对的。
    """
    if _BENCH.get("cal"):
        return list(_BENCH["cal"])
    src = bench if bench is not None else _BENCH.get("panel") or {}
    return sorted({d for f in src.values() for d in f["date"]})


def _dates_np(f: pd.DataFrame) -> np.ndarray:
    a = f.attrs.get("_dn")
    if a is None:
        a = f["date"].astype(str).str.slice(0, 10).to_numpy(dtype="U10")
        f.attrs["_dn"] = a
    return a


def _cal_pos(cal: list[str], date: str) -> int:
    """日期 → 日历下标；找不到返回 -1（停牌日/非交易日不该出现在重放里）。"""
    i = int(np.searchsorted(np.asarray(cal, dtype="U10"), str(date)[:10], "left"))
    if i < len(cal) and cal[i] == str(date)[:10]:
        return i
    return -1


def _fwd(panel: dict, date: str, h: int, entry_lag: int = 1,
         cal: list[str] | None = None) -> tuple[dict[str, float], dict]:
    """某一天选出的股票，按 T+entry_lag 收盘建仓、持有 h 个交易日的收益（%）。

    与训练标签保持一致：T 日收盘后才打分，最早只能 T+1 收盘成交；
    从 T 日收盘直接算会把买不到的 T→T+1 隔夜跳空算成收益。

    返回 (code → 收益%, 统计)。统计里分开记：
      pending   窗口还没走完（数据不够），不写半截结果
      no_entry  建仓日停牌且 10 个交易日内没复牌，买不到
      suspended 目标日停牌，按最后一根 bar 估值
      delisted  目标日之前已退市，用末根 bar 结算（不能剔除，否则重演幸存者偏差）
    """
    d0 = str(date)[:10]
    stat = {"pending": 0, "no_entry": 0, "suspended": 0, "delisted": 0}
    out: dict[str, float] = {}
    cal = cal or _calendar()
    pos = _cal_pos(cal, d0)
    if pos < 0 or pos + entry_lag + h >= len(cal):
        stat["pending"] = len(panel)
        return out, stat
    entry_date = cal[pos + entry_lag]
    target_date = cal[pos + entry_lag + h]

    for code, f in panel.items():
        dates = _dates_np(f)
        i = int(np.searchsorted(dates, entry_date, "left"))
        if i >= len(dates):
            stat["no_entry"] += 1
            continue
        if dates[i] != entry_date:
            # 建仓日停牌：往后找复牌首日，隔太久就当买不到（真实组合也建不了仓）
            gap = _cal_pos(cal, str(dates[i]))
            if gap < 0 or gap - (pos + entry_lag) > MAX_ENTRY_GAP:
                stat["no_entry"] += 1
                continue
        j = int(np.searchsorted(dates, target_date, "right")) - 1
        if j < i:
            stat["pending"] += 1
            continue
        c0 = float(f["close"].iloc[i])
        c1 = float(f["close"].iloc[j])
        if not (c0 > 0 and c1 > 0):
            stat["pending"] += 1
            continue
        exit_date = str(dates[j])
        if exit_date != target_date:
            od = str(listing.info(code).get("out_date") or "")[:10]
            if od and exit_date >= od:
                stat["delisted"] += 1
            else:
                stat["suspended"] += 1
        out[code] = (c1 / c0 - 1) * 100
    return out, stat


def _agg(rows: list[dict], horizons=HORIZONS) -> list[dict]:
    """把逐期明细聚合成每个 horizon 的一张表。"""
    out = []
    for h in horizons:
        rets, excs, inds, wins, beats, per_period = [], [], [], [], [], []
        for r in rows:
            v = r.get("h%d" % h)
            if not v or v.get("ret") is None:
                continue
            rets.append(v["ret"])
            excs.append(v["excess"])
            if v.get("ind_excess") is not None:
                inds.append(v["ind_excess"])
            wins.append(1 if v["ret"] > 0 else 0)
            beats.append(1 if v["excess"] > 0 else 0)
            per_period.append(v["ret"])
        n = len(rets)
        out.append({
            "h": h, "periods": n,
            "picks": int(sum(len(r.get("codes") or []) for r in rows if r.get("h%d" % h))),
            "avg_ret": round(float(np.mean(rets)), 3) if n else None,
            "avg_excess": round(float(np.mean(excs)), 3) if n else None,
            "avg_ind_excess": round(float(np.mean(inds)), 3) if inds else None,
            "ind_periods": len(inds),
            "win_rate": round(float(np.mean(wins)) * 100, 1) if n else None,
            "beat_rate": round(float(np.mean(beats)) * 100, 1) if n else None,
            "best": round(float(np.max(per_period)), 2) if n else None,
            "worst": round(float(np.min(per_period)), 2) if n else None,
            "cum": round(float(np.sum(rets)), 2) if n else None,
        })
    return out


# --------------------------------------------------------------------------
# 归因：回答「哪一类票在亏」
# --------------------------------------------------------------------------

_GROUPS = 5          # 分位组数（五分位）


def _qcut_labels(vals: pd.Series, groups: int = _GROUPS) -> pd.Series:
    """截面分位标签。用 labels=False 再自己映射，避免 duplicates='drop' 时
    分箱数少于 labels 长度直接抛异常（小样本横截面很容易踩到）。"""
    try:
        b = pd.qcut(vals, groups, labels=False, duplicates="drop")
        return pd.Series([None if pd.isna(x) else "Q%d" % (int(x) + 1) for x in b],
                         index=vals.index, dtype=object)
    except Exception:
        return pd.Series([None] * len(vals), index=vals.index, dtype=object)


def _group_stat(items: list[dict], key: str, groups: int = _GROUPS) -> list[dict]:
    """按某一维度分组统计收益与超额。

    items 里每条是 {date, ret, excess, <key>}。数值维度按每期截面分位切组
    （而不是全样本切），否则牛熊不同阶段的绝对值不可比。
    """
    if not items:
        return []
    df = pd.DataFrame(items)
    if key not in df.columns:
        return []
    cats = []
    if pd.api.types.is_numeric_dtype(df[key]):
        df["_g"] = None
        for _, sub in df.groupby("date"):
            v = pd.to_numeric(sub[key], errors="coerce")
            ok = v.notna()
            # 每期只有 TopN 只（默认 10），门槛设成 5 组×3 会永远不达标；
            # 只要够切 5 组就切，组内样本少时看 n 列自己判断可信度。
            if ok.sum() < max(groups, 8):
                continue
            df.loc[sub.index[ok], "_g"] = _qcut_labels(v[ok], groups)
        cats = [f"Q{i + 1}" for i in range(groups)]
    else:
        df["_g"] = df[key].astype(str).replace({"": "未分类", "nan": "未分类",
                                                "None": "未分类"})
        cats = [x for x in df["_g"].value_counts().index[:12]]
    rows = []
    for g in cats:
        sub = df[df["_g"] == g]
        if sub.empty:
            continue
        r = sub["ret"].astype(float)
        e = sub["excess"].astype(float)
        rows.append({"key": str(g), "n": int(len(sub)),
                     "avg_ret": round(float(r.mean()), 3),
                     "avg_excess": round(float(e.mean()), 3),
                     "win": round(float((r > 0).mean()) * 100, 1)})
    return rows


def _attribute(items: list[dict], dims: dict[str, str]) -> dict:
    """items: [{date, ret, excess, industry, vol20, turnover, mom20, close, stage, llm}]"""
    out = {}
    for k, label in dims.items():
        rows = _group_stat(items, k)
        if rows:
            out[k] = {"label": label, "rows": rows}
    return out


ATTR_DIMS = {
    "industry": "行业",
    "vol20": "20日波动（五分位）",
    "turnover": "换手率（五分位）",
    "mom20": "20日动量（五分位）",
    "close": "股价（五分位）",
    "stage": "漏斗层（被哪层淘汰）",
    "llm": "LLM 裁决",
}


FUNNEL_LAYERS = [("quality", "数据完整性"), ("st", "ST 与退市"),
                 ("extreme", "极端风险"), ("liquidity", "流动性"), ("trend", "趋势")]


def _survivors(df: pd.DataFrame, key: str, cfg: dict | None) -> set[str]:
    """只开第 key 层，返回通过该层之后仍在的代码集合。

    funnel 是纯函数，代价只是几次布尔运算，所以定位「谁被哪层淘汰」
    直接逐层跑一遍最省事，也不用改动线上链路的返回结构。
    """
    c = {**screener.FUNNEL_DEFAULT, **(cfg or {})}
    for k, _ in FUNNEL_LAYERS:
        c[k] = False
    c[key] = True
    try:
        kept, _ = screener.funnel(df, c)
        return set(kept["code"].astype(str))
    except Exception:
        return set(df["code"].astype(str))


def funnel_check(days: int = 60, step: int = 5, horizons=(5, 10), limit: int = 300,
                 cfg: dict | None = None) -> dict:
    """被漏斗淘汰的票，事后是不是反而涨得好？

    只读缓存、不改线上链路：同一批横截面上逐层定位淘汰归属，
    再把「各层淘汰组」与「保留组」的 T+N 收益放在一起比。
    如果某层淘汰组长期跑赢保留组，说明那层的阈值在误杀。
    """
    days = max(10, min(int(days), 250))
    step = max(1, min(int(step), 20))
    pool = _replay_pool(limit)
    if not pool:
        return {"ok": False, "error": "没有本地行情缓存"}
    panel = _panel(pool)
    bench, bench_n = _bench_panel()
    cal = _calendar(bench)
    if len(cal) < max(horizons) + 10:
        return {"ok": False, "error": "缓存历史太短，无法比较"}
    usable = cal[:-(max(horizons) + 1)]
    marks = usable[-days:] if len(usable) > days else usable
    marks = marks[::step] if step > 1 else marks

    buckets: dict[str, list[dict]] = {}
    for date in marks:
        recs = []
        for code, f in panel.items():
            if not listing.alive(code, date):
                continue
            hit = f.index[f["date"] == date]
            if len(hit) == 0:
                continue
            rec = f.iloc[int(hit[0])].to_dict()
            rec["code"] = code
            recs.append(rec)
        if len(recs) < 20:
            continue
        df = pd.DataFrame(recs)
        df["code"] = df["code"].astype(str)
        stage_of: dict[str, str] = {}
        survivors = set(df["code"])
        for key, name in FUNNEL_LAYERS:
            if not df["code"].isin(survivors).any():
                break
            keep = _survivors(df[df["code"].isin(survivors)], key, cfg)
            for c in survivors - keep:
                stage_of.setdefault(c, name)
            survivors &= keep
        for c in survivors:
            stage_of[c] = "保留（可进入排序）"

        for h in horizons:
            fwd, _s1 = _fwd(panel, date, h, cal=cal)
            bfwd, _s2 = _fwd(bench, date, h, cal=cal)
            if not fwd or not bfwd:
                continue
            uni = float(np.mean(list(bfwd.values())))
            for code, ret in fwd.items():
                if code not in stage_of:
                    continue
                buckets.setdefault(stage_of[code], []).append(
                    {"date": date, "ret": ret, "excess": ret - uni, "h": h})
    rows = []
    for stage, items in buckets.items():
        for h in horizons:
            sub = [x for x in items if x["h"] == h]
            if not sub:
                continue
            r = [x["ret"] for x in sub]
            e = [x["excess"] for x in sub]
            rows.append({"stage": stage, "h": h, "n": len(sub),
                         "avg_ret": round(float(np.mean(r)), 3),
                         "avg_excess": round(float(np.mean(e)), 3),
                         "win": round(float(np.mean([1 if x > 0 else 0 for x in r])) * 100, 1)})
    order = ["保留（可进入排序）"] + [n for _, n in FUNNEL_LAYERS]
    rows.sort(key=lambda x: (x["h"], order.index(x["stage"]) if x["stage"] in order else 99))
    return {"ok": True, "rows": rows, "periods": len(marks), "pool": len(panel),
            "bench_n": int(bench_n),
            "note": "按「被哪一层淘汰」分组看事后 T+N 收益；若某层淘汰组长期跑赢保留组，"
                    "说明该层阈值可能在误杀，应放宽。淘汰归属按层序优先（先被淘汰的归先那层）。"}


def _replay_pool(limit: int = 200, include_delisted: bool = True) -> list[str]:
    """回放用的股票池。

    关键差异：旧口径走 screener.resolve_scope("cached")，它会用「当前全 A 名单」
    过滤缓存，把所有历史上退市的票直接排除 —— 这就是幸存者偏差。
    这里改成「所有已缓存个股」，退市与否交给 listing.alive() 按日期判断。

    另外，退市股必须优先保留而不是跟着抽样：limit=200 时等间隔抽样几乎必然
    把 18 只退市票全部漏掉，那 as-of 过滤就白做了。所以先用退市票占位，
    剩余名额再给在市股票抽样。
    """
    import universe
    cached = sorted(store.cached_codes())
    codes = [c for c in cached if universe.market_of(c) != "其他"]
    if not include_delisted:
        return screener.resolve_scope("cached", limit)
    try:
        # 只认明确标注 status=0 的退市股。名单里查不到（北交所等 baostock 不覆盖）
        # 的代码必须按「在市」处理，否则会被当成退市票优先塞进池子，
        # 抽样名额全被这些票吃掉，回放样本反而更偏。
        dead = [c for c in codes if str(listing.info(c).get("status") or "") == "0"]
    except Exception:
        dead = []
    if not dead:
        return screener._sample_even(codes, int(limit) if limit else 0)
    live = [c for c in codes if c not in set(dead)]
    room = int(limit) - len(dead) if limit else 0
    picked = dead + (screener._sample_even(live, room) if room > 0 else
                     ([] if limit else live))
    return sorted(picked)


def scorecard(days: int = 60, step: int = 5, topn: int = 10,
              horizons=HORIZONS, limit: int = 200) -> dict:
    """历史重放：每 step 个交易日取一期，用当前打分逻辑选 TopN，回看 T+N。"""
    days = max(10, min(int(days), 400))
    step = max(1, min(int(step), 30))
    topn = max(1, min(int(topn), 50))
    codes = _replay_pool(limit)
    if not codes:
        return {"ok": False, "error": "没有本地行情缓存，无法做后验回看"}
    panel = _panel(codes)
    if len(panel) < 5:
        return {"ok": False, "error": "可评估股票不足（<5 只），请先预热更多行情"}
    # 超额必须对「候选之外」的宽基算，否则 200 只重放样本里挑 Top10，
    # 基准会随打分口径一起漂移，超额天然接近 0。这里复用全市场等权面板。
    bench, bench_n = _bench_panel()
    cal = _calendar(bench)
    maxh = max(horizons) + 1
    if len(cal) < maxh + 10:
        return {"ok": False, "error": "可回看的交易日不足，请预热更长历史"}
    usable = cal[:-maxh]                      # 末尾留够 T+1+h
    marks = usable[-days:] if len(usable) > days else usable
    marks = marks[::step] if step > 1 else marks
    if not marks:
        return {"ok": False, "error": "可回看的交易日不足，请预热更长历史"}

    rows, engine = [], "baseline"
    dim_items: dict[int, list[dict]] = {h: [] for h in horizons}
    delisted_hit: set[str] = set()
    pending = 0
    for date in marks:
        recs = []
        for code, f in panel.items():
            if not listing.alive(code, date):
                continue
            hit = f.index[f["date"] == date]
            if len(hit) == 0:
                continue
            if str(listing.info(code).get("out_date") or "")[:10]:
                delisted_hit.add(code)
            rec = f.iloc[int(hit[0])].to_dict()
            rec["code"] = code
            rec["date"] = date
            recs.append(rec)
        if len(recs) < 5:
            continue
        xdf = pd.DataFrame(recs)
        # 行业列：重放打分要与线上「一键选股」同口径（行业内标准化需要行业归属）
        try:
            xdf["industry"] = [IND_MAP.get(str(c).zfill(6), "") for c in xdf["code"]]
        except Exception:
            xdf["industry"] = ""
        try:
            sc, eng = _score_rows(xdf)
            engine = eng
        except Exception:
            sc = screener.base_scores(xdf)
        xdf = xdf.assign(score=pd.to_numeric(sc, errors="coerce")).dropna(subset=["score"])
        xdf = xdf.sort_values("score", ascending=False).head(topn).reset_index(drop=True)

        row = {"date": date, "codes": [str(c) for c in xdf["code"]],
               "industries": int(xdf["industry"].replace("", "未分类").nunique()),
               "avg_score": round(float(xdf["score"].mean()), 3)}
        for h in horizons:
            fwd, fst = _fwd(panel, date, h, cal=cal)
            pending += int(fst.get("pending", 0))
            bfwd, _bs = _fwd(bench, date, h, cal=cal)
            if not fwd or not bfwd:
                continue
            uni = float(np.mean(list(bfwd.values())))
            got = [fwd[c] for c in xdf["code"] if c in fwd]
            if not got:
                continue
            mean_ret = float(np.mean(got))
            # 行业基准：同日同行业全部可评估股票的等权收益（用来区分「行业 β」与「个股 α」）
            ind_mean = _ind_bench(fwd, xdf)
            row["h%d" % h] = {"ret": round(mean_ret, 3),
                              "universe": round(uni, 3),
                              "excess": round(mean_ret - uni, 3),
                              "ind_excess": None if ind_mean is None else round(mean_ret - ind_mean, 3),
                              "industries": row["industries"],
                              "cover": len(got),
                              "delisted": int(fst.get("delisted", 0)),
                              "suspended": int(fst.get("suspended", 0))}
            for _, x in xdf.iterrows():
                c = str(x["code"])
                if c not in fwd:
                    continue
                dim_items[h].append({
                    "date": date, "code": c, "ret": fwd[c],
                    "excess": fwd[c] - (ind_mean if ind_mean is not None else uni),
                    "industry": str(x.get("industry") or "未分类") or "未分类",
                    "vol20": x.get("vol20"), "turnover": x.get("turnover"),
                    "mom20": x.get("mom20"), "close": x.get("close"),
                })
        rows.append(row)

    if not rows:
        return {"ok": False, "error": "回看样本为空：本地缓存历史太短"}
    stats = _agg(rows, horizons)
    # 「当前模型」重放历史时，模型训练已经看过这些日期（训练集覆盖到验证期之前），
    # 这一段属于样本内回放，指标天然偏乐观；必须明说，别让人当成可交易业绩。
    sample = "out_of_sample"
    sample_note = ""
    if engine != "baseline":
        try:
            import mlmodel
            vs = str(mlmodel._meta().get("valid_start") or "").replace("-", "")[:8]
            d0 = str(rows[0]["date"]).replace("-", "")[:8]
            if vs and d0 and d0 < vs:
                sample = "in_sample"
                sample_note = ("　注意：本次回放起点 %s 早于模型验证起点 %s，模型训练时已见过这段历史，"
                               "属于样本内回放，指标偏乐观；真实表现请看「落盘记录」的 T+N。"
                               % (rows[0]["date"], vs))
        except Exception:
            pass
    try:
        cov = listing.coverage()
    except Exception:
        cov = {}
    attr = _attribute(dim_items.get(max(horizons), []), ATTR_DIMS)
    return {
        "ok": True, "engine": engine, "topn": topn, "step": step,
        "periods": len(rows), "universe": len(panel), "bench_n": int(bench_n),
        "date_from": rows[0]["date"], "date_to": rows[-1]["date"],
        "horizons": stats,
        "recent": rows[-3:],
        "sample": sample,
        "attribution": attr,
        "attribution_h": max(horizons),
        "survivorship": {
            "pool": len(panel), "delisted_in_pool": len(delisted_hit),
            "delisted_cached": cov.get("cached"), "delisted_total": cov.get("total"),
            "cover": cov.get("cover"), "pending_bars": pending,
            # 全历史覆盖率没意义（2005 年退市的票接口早查无此股），
            # 判断「回放可信不可信」要看回放窗口覆盖到的那两年。
            "recent_2y": cov.get("recent_2y"), "recent_cached": cov.get("recent_cached"),
            "recent_missing": cov.get("recent_missing"),
        },
        "note": "口径：T 日收盘打分、T+1 收盘等权建仓，持有 N 个交易日到 T+1+N；"
                "停牌按最后一根 bar 估值、退市按末根 bar 结算（留在样本里），"
                "窗口未走完记为 pending 不计入；"
                "超额基准是同日全市场等权（抽样 %d 只），不是参与重放的 %d 只样本；"
                "样本 %d 期，只作链路验证与方向验证，不作收益承诺。%s"
                % (bench_n, len(panel), len(rows), sample_note),
    }


def track_record(horizons=HORIZONS, limit: int = 400) -> dict:
    """已落盘候选的到期回看（真实选股记录，不是重放）。"""
    if store.picks_count() == 0:
        return {"ok": True, "rows": 0, "periods": 0, "horizons": [],
                "note": "还没有落盘的候选；跑一次「一键选股」就会开始记录。"}
    try:
        p = store.picks_frame()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    if p.empty:
        return {"ok": True, "rows": 0, "periods": 0, "horizons": [], "note": "记录为空。"}
    p["code"] = p["code"].astype(str).str.zfill(6)
    p["as_of"] = p["as_of"].astype(str)
    p["recorded_at"] = p["recorded_at"].astype(str)
    keys = (p[["as_of", "recorded_at"]].drop_duplicates()
            .sort_values(["as_of", "recorded_at"]).tail(int(limit)))
    panel = _panel_close(sorted(p["code"].unique().tolist()))
    bench, bench_n = _bench_panel()
    cal = _calendar(bench)
    meta = _picks_meta_map()
    rows = []
    dim_items: dict[int, list[dict]] = {h: [] for h in horizons}
    for _, k in keys.iterrows():
        date = str(k["as_of"])
        # 同一数据日可能有多次选股实验，按批次分开统计，不能合成一期。
        sub = p[(p["as_of"] == date) & (p["recorded_at"] == str(k["recorded_at"]))]
        codes = sub["code"].tolist()
        row = {"date": date, "recorded_at": str(k["recorded_at"]), "codes": codes}
        for h in horizons:
            fwd, _st = _fwd(panel, date, h, cal=cal)
            bfwd, _bs = _fwd(bench, date, h, cal=cal)
            got = [fwd[c] for c in codes if c in fwd]
            if not got or not bfwd:
                continue
            uni = float(np.mean(list(bfwd.values())))
            mean_ret = float(np.mean(got))
            row["h%d" % h] = {"ret": round(mean_ret, 3), "universe": round(uni, 3),
                              "excess": round(mean_ret - uni, 3), "cover": len(got)}
            for _, x in sub.iterrows():
                c = str(x["code"])
                if c not in fwd:
                    continue
                m = meta.get((date, str(k["recorded_at"]), c)) or {}
                dim_items[h].append({
                    "date": date, "code": c, "ret": fwd[c],
                    "excess": fwd[c] - uni,
                    "industry": str(x.get("industry") or "未分类") or "未分类",
                    "vol20": m.get("vol20"), "turnover": m.get("turnover"),
                    "mom20": m.get("mom20"), "close": m.get("close"),
                    "stage": m.get("funnel_stage"), "llm": m.get("verdict"),
                })
        rows.append(row)
    matured = [r for r in rows if any("h%d" % h in r for h in horizons)]
    dates = sorted({r["date"] for r in rows})
    return {
        "ok": True, "rows": int(len(p)), "periods": len(rows),
        "matured_periods": len(matured), "dates": dates[-5:],
        "bench_n": int(bench_n),
        "horizons": _agg(matured, horizons) if matured else [],
        "attribution": _attribute(dim_items.get(max(horizons), []), ATTR_DIMS),
        "attribution_h": max(horizons),
        "note": "这里是真实落盘的候选在到期后的表现（T+1 收盘建仓口径）；"
                "同一数据日的多次选股按批次分别统计；"
                "超额基准是同日全市场等权（抽样 %d 只），不是候选自己；"
                "期数为 0 说明记录还没走完 T+N，属正常。" % bench_n,
    }


def _picks_meta_map() -> dict:
    """(as_of, recorded_at, code) → 落盘时记下的因子/漏斗/LLM 信息。"""
    try:
        return store.picks_meta_map()
    except Exception:
        return {}


def picks_log(limit: int = 40) -> dict:
    """选股记录明细：一次选股＝一个批次（as_of + recorded_at）。

    同一数据日可以跑多次（改 TopN / 换参数 / 不同打分口径），每次都作为独立批次
    保留，按记录时间排开展示。T+N 后验也按批次计算，不会把几次实验混成一个均值。
    """
    if store.picks_count() == 0:
        return {"ok": True, "batches": [], "total_rows": 0, "total_batches": 0,
                "note": "还没有选股记录；在工作台跑一次选股就会开始记录。"}
    # 结果只随「记录内容」和「最新行情日」变化：这两者没变就直接返回上次结果，
    # 打开记录页不必每次重算一遍（0.9 秒 × 每次点开）。
    key = None
    try:
        key = (*store.picks_version(), int(limit), _latest_day())
        if _LOG_CACHE["key"] == key and _LOG_CACHE["data"] is not None:
            return _LOG_CACHE["data"]
    except Exception:
        key = None
    try:
        p = store.picks_frame()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    if p.empty:
        return {"ok": True, "batches": [], "total_rows": 0, "total_batches": 0,
                "note": "记录为空。"}
    p["code"] = p["code"].astype(str).str.zfill(6)
    p["as_of"] = p["as_of"].astype(str)
    p["recorded_at"] = p["recorded_at"].astype(str)
    p["_rank"] = (pd.to_numeric(p["rank"], errors="coerce")
                  if "rank" in p.columns else np.nan)
    panel = _panel_close(sorted(p["code"].unique().tolist()))
    bench, bench_n = _bench_panel()
    cal = _calendar(bench)
    meta = _picks_meta_map()
    # 真实价换算：腾讯昨收/现价 ÷ 后复权最新收盘 = 复权因子。
    # 展示价用真实价（和券商 App 对得上），涨跌幅仍按后复权同口径相除，不受影响。
    try:
        import spot
        qmap = spot.prev_closes(p["code"].unique().tolist())
    except Exception:
        spot = None
        qmap = {}

    keys = (p[["as_of", "recorded_at"]].drop_duplicates()
            .sort_values(["as_of", "recorded_at"], ascending=False))
    total_batches = int(len(keys))
    out = []
    for _, k in keys.head(int(limit)).iterrows():
        sub = p[(p["as_of"] == k["as_of"]) & (p["recorded_at"] == k["recorded_at"])]
        sub = sub.sort_values(["_rank", "code"], na_position="last")
        item = {"as_of": k["as_of"], "recorded_at": k["recorded_at"], "count": int(len(sub))}

        hz, fwd_cache = {}, {}
        for h in HORIZONS:
            fwd, fst = _fwd(panel, k["as_of"], h, cal=cal)
            fwd_cache[h] = fwd
            bfwd, _bs = _fwd(bench, k["as_of"], h, cal=cal)
            got = [fwd[c] for c in sub["code"] if c in fwd]
            if not got or not bfwd:
                continue
            uni = float(np.mean(list(bfwd.values())))
            mean_ret = float(np.mean(got))
            hz[str(h)] = {"ret": round(mean_ret, 3), "universe": round(uni, 3),
                          "excess": round(mean_ret - uni, 3), "cover": len(got),
                          # 同一批里没算出收益的：窗口没走完 / 建仓日停牌买不到
                          "unpriced": int(sum(1 for c in sub["code"] if c not in fwd))}
        item["horizons"] = hz

        rows, chgs, last_dates = [], [], []
        for _, r in sub.iterrows():
            code = str(r["code"])
            f = panel.get(code)
            last = float(f["close"].iloc[-1]) if f is not None and len(f) else None
            last_date = str(f["date"].iloc[-1])[:10] if f is not None and len(f) else ""
            if last_date:
                last_dates.append(last_date)
            # 记录价必须和最新价同口径：picks 里早期批次存的是真实价（未复权），
            # 与后复权序列直接相除会算出 +300% 这种假涨幅，所以统一按缓存重取当日收盘。
            rec = None
            if f is not None and len(f):
                hit = f.index[f["date"] == k["as_of"]]
                if len(hit):
                    rec = float(f["close"].iloc[int(hit[0])])
            raw = None
            try:
                raw = float(r.get("close"))
            except Exception:
                raw = None
            drift = bool(rec and raw and abs(raw / rec - 1) > 0.2)   # 旧口径批次
            chg = round((last / rec - 1) * 100, 2) if (last and rec and rec > 0) else None
            if chg is not None:
                chgs.append(chg)
            try:
                sc = float(r.get("score"))
                sc = None if sc != sc else round(sc, 3)
            except Exception:
                sc = None
            m = meta.get((str(k["as_of"]), str(k["recorded_at"]), code)) or {}
            rows.append({
                "code": code, "name": r.get("name") or code,
                "industry": r.get("industry") or "",
                "rank": None if pd.isna(r["_rank"]) else int(r["_rank"]),
                "score": sc,
                "rec_close": rec, "last_close": last, "chg": chg,
                "rec_real": (spot.real_price(rec, last, last_date, qmap.get(code))
                             if spot else None),
                "last_real": (spot.real_price(last, last, last_date, qmap.get(code))
                              if spot else None),
                "legacy_basis": drift,
                "funnel_stage": m.get("funnel_stage") or "",
                "verdict": m.get("verdict") or "",
                "bear": m.get("bear_case") or [],
                "llm_reason": m.get("llm_reason") or "",
                "llm_delta": m.get("llm_delta"),
                "h": {str(h): round(fwd_cache[h][code], 2) for h in HORIZONS
                      if code in fwd_cache[h]}
            })
        item["picks"] = rows
        item["avg_chg"] = round(float(np.mean(chgs)), 2) if chgs else None
        item["win"] = int(sum(1 for x in chgs if x > 0))
        item["scored"] = len(chgs)
        item["last_date"] = max(last_dates) if last_dates else ""
        item["legacy"] = bool(rows) and sum(1 for x in rows if x["legacy_basis"]) > len(rows) / 2
        out.append(item)

    result = {"ok": True, "batches": out, "total_rows": int(len(p)),
              "total_batches": total_batches, "shown": len(out),
              "bench_n": int(bench_n),
              "note": "每条记录＝一次选股（数据日 + 记录时间），同一数据日多次选股会分开展示；"
                      "价格已换算成真实价（未复权）；涨跌幅按后复权同口径计算；"
                      "T+N 超额基准是同日全市场等权（抽样 %d 只）。" % bench_n}
    if key:
        _LOG_CACHE.update(key=key, data=result)
    return result


# --------------------------------------------------------------------------
# LLM 空头辩护的离线 A/B
# --------------------------------------------------------------------------

def llm_ab(limit: int = 20, horizons=(5, 10)) -> dict:
    """用真实落盘记录检验「LLM 否决」到底拦得对不对。

    比较对象：同一批次里被判 drop 的票 vs 留下的票，各自的 T+N 收益。
    判据：如果 drop 组的收益长期不低于 keep 组，说明否决没有信息量（甚至在瞎拦），
    必须停用；只有 drop 组显著更差，才说明这个过滤器在创造价值。

    注意：这不是「LLM 说得有没有道理」，而是「拦得对不对」——两件事不能互相替代。
    """
    try:
        archives = store.archive_list(limit=max(1, min(int(limit), 200)))
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}
    batches = []
    for a in archives:
        pl = a.get("payload") or {}
        picks = [p for p in (pl.get("picks") or []) if p.get("code")]
        if not picks:
            continue
        has_verdict = [p for p in picks
                       if str(p.get("verdict") or "") in ("keep", "drop")]
        if not has_verdict:
            continue
        batches.append((a, picks))
    if not batches:
        return {"ok": True, "batches": [], "summary": [], "rows": 0,
                "note": "还没有带裁决的决策档案。开启「空头辩护」跑一次选股后，"
                        "这里会自动积累可用于 A/B 的样本。"}

    all_codes = sorted({str(p["code"]).zfill(6) for _, ps in batches for p in ps})
    panel = _panel_close(all_codes)
    bench, bench_n = _bench_panel()
    cal = _calendar(bench)
    out = []
    for a, picks in batches:
        as_of = str(a.get("as_of") or "")[:10]
        keep = [str(p["code"]).zfill(6) for p in picks
                if str(p.get("verdict") or "keep") != "drop"]
        drop = [str(p["code"]).zfill(6) for p in picks
                if str(p.get("verdict") or "") == "drop"]
        row = {"as_of": as_of, "at": a.get("at"), "keep_n": len(keep), "drop_n": len(drop)}
        for h in horizons:
            fwd, _st = _fwd(panel, as_of, h, cal=cal)
            bfwd, _bs = _fwd(bench, as_of, h, cal=cal)
            k = [fwd[c] for c in keep if c in fwd]
            d = [fwd[c] for c in drop if c in fwd]
            if not k or not d:
                continue
            uni = float(np.mean(list(bfwd.values()))) if bfwd else 0.0
            row["h%d" % h] = {"keep_ret": round(float(np.mean(k)), 3),
                              "drop_ret": round(float(np.mean(d)), 3),
                              "keep_excess": round(float(np.mean(k)) - uni, 3),
                              "drop_excess": round(float(np.mean(d)) - uni, 3),
                              "delta": round(float(np.mean(k) - np.mean(d)), 3),
                              "n": [len(k), len(d)]}
        if any("h%d" % h in row for h in horizons):
            out.append(row)

    summary = []
    for h in horizons:
        rows = [r["h%d" % h] for r in out if "h%d" % h in r]
        if not rows:
            continue
        k = float(np.mean([x["keep_ret"] for x in rows]))
        d = float(np.mean([x["drop_ret"] for x in rows]))
        correct = sum(1 for x in rows if x["delta"] > 0)
        hit = correct / len(rows) * 100
        if len(rows) < 5:
            verdict = "样本不足以下结论"
        elif k - d > 0.3 and hit >= 60:
            verdict = "有效：否决组确实更差"
        elif k - d <= 0:
            verdict = "倾向无效：被否决的反而更好，建议停用"
        else:
            verdict = "方向正确但差距不足"
        summary.append({"h": h, "batches": len(rows),
                        "keep_ret": round(k, 3), "drop_ret": round(d, 3),
                        "delta": round(k - d, 3), "hit": round(hit, 1),
                        "verdict": verdict})
    return {"ok": True, "batches": out, "summary": summary, "rows": len(out),
            "bench_n": int(bench_n),
            "note": "delta = keep 组收益 − drop 组收益。为正说明「否决」拦掉的确实更差，"
                    "过滤器有价值；为负说明在瞎拦，应停用。样本少于 5 期不要下结论。"
                    "口径与 T+N 后验一致（T+1 收盘建仓）。"}
