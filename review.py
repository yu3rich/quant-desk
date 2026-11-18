# -*- coding: utf-8 -*-
"""
QuantDesk 选股框架 · T+N 后验闭环

两件事：
  1) scorecard()：把「当前这套打分逻辑」放回历史每一期重放一遍，看 T+5/10/20 的真实结果
     —— 用本地缓存日线自己算前瞻收益，不依赖任何外部评估服务
  2) track_record()：把每次真实选股落盘的候选（data/screen/picks.csv）在到期后回看

口径说明（写进界面，避免误读）：
  - 收益 = 该股在评估日收盘 → 第 h 个交易日后收盘的涨跌幅（前复权，本地缓存）
  - 超额 = TopN 等权平均收益 − 同一天「所有可评估股票」的等权平均收益
  - 行业超额 = TopN 等权平均收益 − 同一天「同行业全部可评估股票」的等权平均收益
    两个基准都要看：全市场超额里含行业轮动，行业超额才是纯个股 α
  - 胜率 = TopN 中收益 > 0 的比例；跑赢率 = 收益高于同日全样本均值的比例
  - 样本期数不足时只报数不下结论
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

import screener

BASE = Path(__file__).resolve().parent
SCREEN_DIR = BASE / "data" / "screen"
PICKS_CSV = SCREEN_DIR / "picks.csv"
HORIZONS = (5, 10, 20)

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
    """行情缓存最新交易日；只读文件尾部，够快才敢放进缓存 key 里。"""
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


def _fwd(panel: dict, date: str, h: int) -> dict[str, float]:
    """某一天、所有股票的第 h 个交易日收益（%）。"""
    out = {}
    for code, f in panel.items():
        hit = f.index[f["date"] == date]
        if len(hit) == 0:
            continue
        i = int(hit[0])
        if i + h >= len(f):
            continue
        c0, c1 = float(f["close"].iloc[i]), float(f["close"].iloc[i + h])
        if c0 > 0 and c1 > 0:
            out[code] = (c1 / c0 - 1) * 100
    return out


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
            "h": h, "periods": n, "picks": int(sum(len(r.get("codes") or []) for r in rows if r.get("h%d" % h))),
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


def scorecard(days: int = 60, step: int = 5, topn: int = 10,
              horizons=HORIZONS, limit: int = 200) -> dict:
    """历史重放：每 step 个交易日取一期，用当前打分逻辑选 TopN，回看 T+N。"""
    days = max(10, min(int(days), 400))
    step = max(1, min(int(step), 30))
    topn = max(1, min(int(topn), 50))
    codes = screener.resolve_scope("cached", limit)
    if not codes:
        return {"ok": False, "error": "没有本地行情缓存，无法做后验回看"}
    panel = _panel(codes)
    if len(panel) < 5:
        return {"ok": False, "error": "可评估股票不足（<5 只），请先预热更多行情"}
    maxh = max(horizons)

    # 评估日：所有股票日期的交集里，剔除末尾不足 maxh 根 bar 的部分
    date_sets = [set(f["date"]) for f in panel.values()]
    common = sorted(set.intersection(*date_sets)) if date_sets else []
    if len(common) < maxh + 5:
        common = sorted({d for f in panel.values() for d in f["date"]})
    usable = common[:-maxh] if len(common) > maxh else common
    marks = usable[-days:] if len(usable) > days else usable
    marks = marks[::step] if step > 1 else marks
    if not marks:
        return {"ok": False, "error": "可回看的交易日不足，请预热更长历史"}

    rows, engine = [], "baseline"
    for date in marks:
        recs = []
        for code, f in panel.items():
            hit = f.index[f["date"] == date]
            if len(hit) == 0:
                continue
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
            fwd = _fwd(panel, date, h)
            if not fwd:
                continue
            uni = float(np.mean(list(fwd.values())))
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
                              "cover": len(got)}
        rows.append(row)

    if not rows:
        return {"ok": False, "error": "回看样本为空：本地缓存历史太短"}
    stats = _agg(rows, horizons)
    return {
        "ok": True, "engine": engine, "topn": topn, "step": step,
        "periods": len(rows), "universe": len(panel),
        "date_from": rows[0]["date"], "date_to": rows[-1]["date"],
        "horizons": stats,
        "recent": rows[-3:],
        "note": "口径：TopN 等权持有到第 N 个交易日，超额基准是同日「全部可评估股票」等权均值；"
                "样本 %d 期，只作链路验证与方向验证，不作收益承诺。" % len(rows),
    }


def track_record(horizons=HORIZONS, limit: int = 400) -> dict:
    """已落盘候选的到期回看（真实选股记录，不是重放）。"""
    if not PICKS_CSV.exists():
        return {"ok": True, "rows": 0, "periods": 0, "horizons": [],
                "note": "还没有落盘的候选；跑一次「一键选股」就会开始记录。"}
    try:
        p = pd.read_csv(PICKS_CSV, dtype={"code": str, "as_of": str})
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    if p.empty:
        return {"ok": True, "rows": 0, "periods": 0, "horizons": [], "note": "记录为空。"}
    p["code"] = p["code"].astype(str).str.zfill(6)
    dates = sorted(p["as_of"].astype(str).unique())[-limit:]
    panel = _panel_close(sorted(p["code"].unique().tolist()))
    rows = []
    for date in dates:
        sub = p[p["as_of"].astype(str) == date]
        codes = sub["code"].tolist()
        row = {"date": date, "codes": codes}
        for h in horizons:
            fwd = _fwd(panel, date, h)
            got = [fwd[c] for c in codes if c in fwd]
            if not got:
                continue
            uni = float(np.mean(list(fwd.values()))) if fwd else 0.0
            mean_ret = float(np.mean(got))
            row["h%d" % h] = {"ret": round(mean_ret, 3), "universe": round(uni, 3),
                              "excess": round(mean_ret - uni, 3), "cover": len(got)}
        rows.append(row)
    matured = [r for r in rows if any("h%d" % h in r for h in horizons)]
    return {
        "ok": True, "rows": int(len(p)), "periods": len(rows),
        "matured_periods": len(matured), "dates": dates[-5:],
        "horizons": _agg(matured, horizons) if matured else [],
        "note": "这里是真实落盘的候选在到期后的表现；期数为 0 说明记录还没走完 T+N，属正常。",
    }


def picks_log(limit: int = 40) -> dict:
    """选股记录明细：一次选股＝一个批次（as_of + recorded_at）。

    同一交易日可能选过多次（改了 TopN 或参数），按批次拆开展示，既不丢历史，
    也不会让「一期多批」的候选混成一锅。每批给出记录价 → 最新价的表现与 T+N 后验。
    """
    if not PICKS_CSV.exists():
        return {"ok": True, "batches": [], "total_rows": 0, "total_batches": 0,
                "note": "还没有选股记录；在工作台跑一次选股就会开始记录。"}
    # 结果只随「记录文件」和「最新行情日」变化：这两者没变就直接返回上次结果，
    # 打开记录页不必每次重算一遍（0.9 秒 × 每次点开）。
    key = None
    try:
        st = PICKS_CSV.stat()
        key = (st.st_mtime_ns, st.st_size, int(limit), _latest_day())
        if _LOG_CACHE["key"] == key and _LOG_CACHE["data"] is not None:
            return _LOG_CACHE["data"]
    except Exception:
        key = None
    try:
        p = pd.read_csv(PICKS_CSV, dtype={"code": str, "as_of": str})
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
            fwd = _fwd(panel, k["as_of"], h)
            fwd_cache[h] = fwd
            got = [fwd[c] for c in sub["code"] if c in fwd]
            if not got:
                continue
            uni = float(np.mean(list(fwd.values()))) if fwd else 0.0
            mean_ret = float(np.mean(got))
            hz[str(h)] = {"ret": round(mean_ret, 3), "universe": round(uni, 3),
                          "excess": round(mean_ret - uni, 3), "cover": len(got)}
        item["horizons"] = hz

        rows, chgs, last_dates = [], [], []
        for _, r in sub.iterrows():
            code = str(r["code"])
            f = panel.get(code)
            last = float(f["close"].iloc[-1]) if f is not None and len(f) else None
            last_date = str(f["date"].iloc[-1])[:10] if f is not None and len(f) else ""
            if last_date:
                last_dates.append(last_date)
            # 记录价必须和最新价同口径：picks.csv 里早期批次存的是真实价（未复权），
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
            rows.append({
                "code": code, "name": r.get("name") or code,
                "industry": r.get("industry") or "",
                "rank": None if pd.isna(r["_rank"]) else int(r["_rank"]),
                "score": sc,
                "rec_close": rec, "last_close": last, "chg": chg,
                "legacy_basis": drift,
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
              "note": "每条记录＝一次选股（数据日 + 记录时间）。涨跌＝记录价 → 本地缓存最新收盘价。"}
    if key:
        _LOG_CACHE.update(key=key, data=result)
    return result
