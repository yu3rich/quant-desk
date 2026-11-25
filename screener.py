# -*- coding: utf-8 -*-
"""
QuantDesk 选股框架 · 数据层（Stage 1）

职责：把「单只股票时序回测」升级为「横截面选股」所需的三件东西
  1) 全市场横截面快照 snapshot：每个交易日、每只股票一行特征（推理用，绝不含未来数据）
  2) 训练数据集 dataset：多期横截面特征 + 未来 N 日超额收益标签（仅训练用）
  3) 统一的因子定义 / 方向 / 标准化口径，供模型层与展示层共用

设计边界（本期只做框架）：
  - 漏斗过滤、行业约束、T+N 后验闭环：仅预留接口与状态位，本轮不实现
  - 全部基于 store.bars（SQLite）的本地后复权日线，不新增高频联网请求
  - 训练标签使用未来数据，但严格限制在 dataset 内；snapshot 永远只用到截止日
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

import histcache


def _atomic_csv(df, path) -> None:
    """先写临时文件再原子替换。

    直接 to_csv 是「先截断、再逐行写」，同进程的 Flask 请求线程在这几十毫秒
    里读到的是半截文件：要么解析失败（页面瞬间显示「无快照」），要么正好截在
    行边界上，被当成一份残缺的候选表静默使用。os.replace 在 Windows 上也是原子的。
    """
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
SCREEN_DIR = DATA / "screen"
SCREEN_DIR.mkdir(parents=True, exist_ok=True)
UNIVERSE_PATH = DATA / "universe.csv"
SNAPSHOT_CSV = SCREEN_DIR / "snapshot.csv"
SNAPSHOT_META = SCREEN_DIR / "snapshot.json"
DATASET_CSV = SCREEN_DIR / "dataset.csv"
DATASET_META = SCREEN_DIR / "dataset.json"

# --------------------------------------------------------------------------
# 因子定义：Qlib Alpha158 精简版思路（动量 / 反转 / 波动 / 量能 / 形态 / 位置）
# direction 只用于「无模型时的基准分」，LightGBM 会自己学方向与交互
#
# 行业维度分两层：
#   1) 处理层：所有「个股因子」在打分前做行业内标准化（neutral.py），剥离行业 β
#   2) 因子层：额外提供 2 个行业类因子（行业动能 / 行业内相对动量）喂给模型
#      这两列不参与基准分合成 —— 因为 mom20 ≡ ind_rel20 + ind_mom20 是恒等式，
#      三个方向权重一起加会把动量的权重放大三倍
# --------------------------------------------------------------------------
FEATURES: list[dict] = [
    {"key": "ret1", "name": "1日收益", "unit": "%", "direction": -0.05, "group": "反转"},
    {"key": "mom5", "name": "5日动量", "unit": "%", "direction": -0.05, "group": "动量"},
    {"key": "mom20", "name": "20日动量", "unit": "%", "direction": 0.25, "group": "动量"},
    {"key": "mom60", "name": "60日动量", "unit": "%", "direction": 0.10, "group": "动量"},
    {"key": "dist_ma60", "name": "偏离60日线", "unit": "%", "direction": 0.05, "group": "趋势"},
    {"key": "ma_dev", "name": "偏离20日线", "unit": "%", "direction": 0.05, "group": "趋势"},
    {"key": "macd_hist", "name": "MACD柱", "unit": "%", "direction": 0.10, "group": "趋势"},
    {"key": "rsi14", "name": "RSI14", "unit": "", "direction": 0.05, "group": "形态"},
    {"key": "boll_pos", "name": "布林位置", "unit": "", "direction": -0.05, "group": "形态"},
    {"key": "dd20", "name": "距20日高点", "unit": "%", "direction": 0.05, "group": "形态"},
    {"key": "vol20", "name": "20日波动", "unit": "%", "direction": -0.10, "group": "风险"},
    {"key": "vol60", "name": "60日波动", "unit": "%", "direction": -0.05, "group": "风险"},
    {"key": "amp20", "name": "20日振幅", "unit": "%", "direction": -0.05, "group": "风险"},
    {"key": "vol_ratio", "name": "量比(5/20)", "unit": "", "direction": 0.10, "group": "量能"},
    {"key": "amount_ratio", "name": "额比(5/20)", "unit": "", "direction": 0.05, "group": "量能"},
    {"key": "turnover", "name": "换手率", "unit": "%", "direction": -0.05, "group": "量能"},
    # K 线形态组：对齐 Qlib Alpha158 的 KMID / KLEN / KSFT 等
    # direction=0 表示不参与基准分权重合成（避免与 ret1 重复计数），只由模型自己学
    {"key": "kmid", "name": "实体中点", "unit": "%", "direction": 0.0,
     "group": "形态", "desc": "(收盘−开盘)/开盘，当日实体的方向与强度"},
    {"key": "klen", "name": "实体振幅", "unit": "%", "direction": 0.0,
     "group": "形态", "desc": "(最高−最低)/开盘，单日振幅相对开盘价"},
    {"key": "ksft", "name": "收盘位置", "unit": "", "direction": 0.0,
     "group": "形态", "desc": "(收盘−开盘)/(最高−最低)，收盘在当日区间中的位置，衡量尾盘强弱"},
    {"key": "rsv20", "name": "RSV20", "unit": "", "direction": 0.0,
     "group": "形态", "desc": "收盘在近 20 日高低区间中的位置，0 贴最低、1 贴最高"},
    {"key": "cntp20", "name": "20日上涨占比", "unit": "", "direction": 0.0,
     "group": "动量", "desc": "近 20 个交易日中收涨的天数占比，衡量上涨的持续性而非幅度"},
    {"key": "corr20", "name": "20日量价相关", "unit": "", "direction": 0.0,
     "group": "量能", "desc": "收盘价与成交量的 20 日相关系数，衡量放量上涨/缩量上涨的量价配合"},
    # 行业类因子：只喂模型，不进基准分（model_only）
    {"key": "ind_mom20", "name": "行业20日动能", "unit": "%", "direction": 0.0,
     "group": "行业", "model_only": True,
     "desc": "所属行业当日等权 20 日涨幅，衡量行业顺风/逆风"},
    {"key": "ind_rel20", "name": "行业内相对动量", "unit": "%", "direction": 0.0,
     "group": "行业", "model_only": True,
     "desc": "个股 mom20 减行业均值，行业内相对强弱（中性化后即行业 α）"},
]
FEATURE_KEYS = [f["key"] for f in FEATURES]
CORE_KEYS = [f["key"] for f in FEATURES if not f.get("model_only")]
IND_KEYS = [f["key"] for f in FEATURES if f.get("model_only")]
DIRECTIONS = {f["key"]: f["direction"] for f in FEATURES}
FEATURE_VERSION = 3      # 因子集版本：模型/训练集元数据里记录，版本不同时明确提示重训
# 退化判据：「区间位置」类因子（布林带 / RSV / K 线实体）的分母是区间宽度，
# 宽度为 0 时该因子无定义。难点在于「0」不是精确的：
#   长期停牌 / 一字板时 20 天价格完全不动，std 理论上为 0，
#   pandas 却会因累加误差算出 1e-7 ~ 5e-7 这种残差，numpy 拿到精确 0.0。
# 于是同一只票在训练集给 0.5、在快照给 NaN —— 两条路径必须用同一个判据。
#
# 判据用「相对 1 元的机器精度尺度」而不是「相对股价」：股价 1.29 元时残差比值
# 达 1.5e-6，而 34 元时只有 1.1e-7，拿价格做分母会让阈值随股价漂移。
# 改用 STD_EPS 直接卡在「std 是否可视为 0」这一层，与价格量级无关：
#   真实可交易的最低 std 也在 1e-4 量级（对应的日振幅已在 0.01% 以下），
#   取 1e-5 能覆盖全部浮点噪声，又不会误伤任何真实数据。
STD_EPS = 1e-5
NEUTRAL_DEFAULT = "industry"
NEUTRAL_MODES = ("industry", "industry+size", "none")
# 标签口径：
#   market  未来 h 日收益 − 当日全样本均值（含行业轮动 β，单位 %）
#   industry 未来 h 日收益 − 当日同行业均值（行业中性标签，单位 %）
#   rank     当日横截面分位映射到 (-0.5, 0.5)，Qlib CSRankNorm 思路：
#            模型只学「谁比谁强」，对异常值不敏感，且不受牛熊整体水位影响
LABEL_MODES = ("market", "industry", "rank")

# 漏斗默认参数：默认只开「数据完整 / 极端风险」，趋势与流动性按需打开
# （实测本地缓存普遍缺成交额与换手率，流动性层对缺失值采取「跳过不淘汰」，绝不因缺数据误杀）
FUNNEL_DEFAULT = {
    "quality": True,        # 必需因子齐全
    "st": True,             # 剔除 ST / *ST / 退市整理
    "extreme": True,        # 剔除涨跌停/极端波动/深跌
    "liquidity": False,     # 换手率下限（有数据才判）
    "trend": False,         # 站上 20 日线
    "max_abs_pct": 9.5,     # 当日涨跌幅绝对值上限（%）
    "max_vol20": 8.0,       # 20 日波动上限（%）
    "min_turnover": 0.2,    # 换手率下限（%）
    "min_ma_dev": 0.0,      # 相对 20 日线的最小偏离（%）
}
REQUIRED_KEYS = ["mom20", "mom60", "vol20", "rsi14", "ma_dev"]
INDUSTRY_CAP_DEFAULT = 2

# 框架模块清单：built=True 的已经在跑，界面上不再标「待建」
MODULES = [
    {"key": "snapshot", "name": "横截面快照", "built": True,
     "desc": "%d 因子全市场快照，只用截止日及之前的 bar，不含未来数据。" % len(FEATURE_KEYS)},
    {"key": "model", "name": "LightGBM 排序", "built": True,
     "desc": "时序切分验证 + IC/RankIC/特征重要度，缺依赖自动降级岭回归。"},
    {"key": "funnel", "name": "漏斗过滤", "built": True,
     "desc": "数据完整性 / 极端风险 / 流动性 / 趋势四层硬筛，逐层公布淘汰数与原因。"},
    {"key": "industry_cap", "name": "行业约束", "built": True,
     "desc": "单行业入选上限，避免同一板块霸榜把行业暴露伪装成 Alpha。"},
    {"key": "industry_neutral", "name": "行业中性化", "built": True,
     "desc": "因子按行业内去均值/标准差后再打分（可选叠加市值中性化），把行业 β 从 Alpha 里剥离。"},
    {"key": "posthoc", "name": "T+N 后验闭环", "built": True,
     "desc": "每期候选落盘，并按 T+5/10/20 回看收益、超额、胜率与跑赢率。"},
    {"key": "llm", "name": "LLM 重排与解读", "built": True,
     "desc": "用户自填 OpenAI 兼容端点，默认关闭，未启用时用本地规则解读兜底。"},
]


def _num(v):
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except Exception:
        return None


def funnel(df: pd.DataFrame, cfg: dict | None = None,
           detail: bool = False) -> tuple:
    """逐层过滤横截面，返回（保留集, 各层统计[, 淘汰归属]）。

    设计原则：只做「硬性、可解释」的剔除，缺数据一律视为「未知 → 放行」，
    并把每层淘汰数写进 stages，界面上可逐层核对，不做黑箱。

    detail=True 时额外返回 {code: 被哪一层淘汰}，供决策档案记录。
    这件事有意义的地方在事后归因：只看「淘汰了多少只」没法回答
    「被极端风险层淘汰的票，后来是不是反而涨得更好」。
    """
    c = {**FUNNEL_DEFAULT, **(cfg or {})}
    stages: list[dict] = []
    killed: dict[str, str] = {}

    def rec(key, name, before, after, note, enabled=True):
        stages.append({"key": key, "name": name, "before": int(before), "after": int(after),
                       "dropped": int(before) - int(after), "note": note,
                       "enabled": bool(enabled)})

    cur = df.copy()
    rec("pool", "样本池", len(cur), len(cur),
        "本次参与打分的股票：本地缓存中因子完整、且落在同一交易日；默认全市场，"
        "若设了「样本上限」则按上限等间隔抽样", True)

    # 1) 数据完整
    before = len(cur)
    if c["quality"]:
        mask = pd.Series(True, index=cur.index)
        for k in REQUIRED_KEYS:
            if k in cur.columns:
                mask &= pd.to_numeric(cur[k], errors="coerce").notna()
        cur = cur[mask]
        note = "必需因子（动量/波动/RSI/均线偏离）齐全"
    else:
        note = "已关闭"
    rec("quality", "数据完整性", before, len(cur), note, c["quality"])

    # 2) ST / 退市整理（按名称，行情源里没有「风险警示」标志位）
    before = len(cur)
    if c.get("st", True) and "name" in cur.columns:
        nm = cur["name"].astype(str)
        cur = cur[~nm.str.contains("ST", case=False, na=False) & ~nm.str.contains("退", na=False)]
        note = "剔除名称含 ST / *ST / 退市整理的标的"
    else:
        note = "已关闭" if not c.get("st", True) else "缺少名称字段，本层跳过"
    rec("st", "ST 与退市", before, len(cur), note, c.get("st", True))

    # 3) 极端风险
    before = len(cur)
    if c["extreme"]:
        pct = pd.to_numeric(cur.get("pct"), errors="coerce")
        vol = pd.to_numeric(cur.get("vol20"), errors="coerce")
        keep = pd.Series(True, index=cur.index)
        if "pct" in cur.columns:
            keep &= pct.isna() | (pct.abs() <= float(c["max_abs_pct"]))
        if "vol20" in cur.columns:
            keep &= vol.isna() | (vol <= float(c["max_vol20"]))
        cur = cur[keep]
        note = "剔除 |当日涨跌| ≥ %.1f%%（涨跌停难成交）或 20 日波动 > %.1f%%" % (
            float(c["max_abs_pct"]), float(c["max_vol20"]))
    else:
        note = "已关闭"
    rec("extreme", "极端风险", before, len(cur), note, c["extreme"])

    # 4) 流动性（缺数据放行）
    before = len(cur)
    if c["liquidity"] and "turnover" in cur.columns:
        to = pd.to_numeric(cur["turnover"], errors="coerce")
        known = to.notna() & (to > 0)
        keep = ~known | (to >= float(c["min_turnover"]))
        cur = cur[keep]
        unknown = int((~known).sum())
        note = "换手率 ≥ %.2f%%；另有 %d 只缺换手率数据，按「未知放行」处理" % (
            float(c["min_turnover"]), unknown)
    else:
        note = "已关闭" if not c["liquidity"] else "缓存里没有换手率字段，本层跳过"
    rec("liquidity", "流动性", before, len(cur), note, c["liquidity"])

    # 5) 趋势
    before = len(cur)
    if c["trend"] and "ma_dev" in cur.columns:
        dev = pd.to_numeric(cur["ma_dev"], errors="coerce")
        cur = cur[dev.isna() | (dev >= float(c["min_ma_dev"]))]
        note = "收盘价相对 20 日线偏离 ≥ %.1f%%（即不追破位下行）" % float(c["min_ma_dev"])
    else:
        note = "已关闭" if not c["trend"] else "缺少均线偏离字段，本层跳过"
    rec("trend", "趋势", before, len(cur), note, c["trend"])

    # 6) 排序取候选池（截断由 select() 做，这里只记录进入排序的数量）
    rec("rank", "进入打分排序", len(cur), len(cur), "按分数降序，随后受 TopN 与行业上限约束", True)
    if not detail or "code" not in df.columns:
        return cur, stages
    # 定位「谁被哪层淘汰」：逐层单独重跑一次（纯布尔运算，5500 行约 5ms）。
    # 这样做而不是在每层里埋点，是为了让「过滤逻辑」只写一遍 ——
    # 两处各写一份 mask 迟早会不一致，而归因结果一旦错位就完全失去意义。
    remain = set(df["code"].astype(str))
    for key, name in (("quality", "数据完整性"), ("st", "ST 与退市"),
                      ("extreme", "极端风险"), ("liquidity", "流动性"), ("trend", "趋势")):
        if not remain:
            break
        c2 = {**FUNNEL_DEFAULT, **(cfg or {})}
        for kk in ("quality", "st", "extreme", "liquidity", "trend"):
            c2[kk] = False
        c2[key] = True
        try:
            keep, _ = funnel(df[df["code"].astype(str).isin(remain)], c2)
            kept = set(keep["code"].astype(str))
        except Exception:
            break
        for code in remain - kept:
            killed.setdefault(code, name)
        remain &= kept
    return cur, stages, killed


def _ind_label(v) -> str:
    """行业名清洗：pandas 的 NaN / None / 字符串 'nan' 统一成空，交给上层显示「未分类」。"""
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("", "nan", "none", "null") else s


def select(scored: pd.DataFrame, topn: int = 10, cap: int | None = None) -> dict:
    """在已打分的横截面上取 TopN，并施加单行业上限。返回入选列表与约束明细。

    额外给出两件与行业相关的东西：
      · ind_rank  该股在本行业内的分数百分位（「在同行业里排第几」）
      · exposure  组合的行业权重 vs 横截面基线、HHI 集中度
    """
    import neutral
    topn = max(1, int(topn))
    df = scored.sort_values("score", ascending=False).reset_index(drop=True)
    cap = int(cap) if cap else 0
    # 行业内百分位：在全部样本上算，而不是只算入选的几只，否则「排第几」没有参照
    try:
        df["ind_rank"] = neutral.intra_rank(df, "score").round(3)
    except Exception:
        df["ind_rank"] = None
    picked, trimmed, by_ind = [], [], {}
    # 先过 _rows()：把 NaN / inf / numpy 标量统一成 JSON 安全的值，
    # 否则 jsonify 会输出非法 JSON 的 NaN，前端直接解析失败（表现为「点了没数据」）
    for r in _rows(df):
        if len(picked) >= topn:
            break
        ind = _ind_label(r.get("industry")) or "未分类"
        # 「未分类」也受上限约束：行业数据缺口不该成为绕开分散度限制的后门
        if cap and by_ind.get(ind, 0) >= cap:
            trimmed.append({"code": str(r.get("code")), "name": r.get("name"),
                            "industry": ind, "score": r.get("score")})
            continue
        by_ind[ind] = by_ind.get(ind, 0) + 1
        item = dict(r)
        item["industry"] = ind
        item["rank"] = len(picked) + 1     # 重排序号：漏斗与行业约束后必须重编，否则显示的是旧排名
        picked.append(item)
    counts = {}
    for x in picked:
        counts[x["industry"]] = counts.get(x["industry"], 0) + 1
    try:
        exp = neutral.exposure(picked, df)
    except Exception as e:
        exp = {"ok": False, "note": "行业暴露计算失败：%s" % str(e)[:60], "rows": []}
    return {
        "picked": picked,
        "cap": cap,
        "capped": bool(cap and trimmed),
        "trimmed": trimmed[:20],
        "trimmed_count": len(trimmed),
        "industry_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "exposure": exp,
        "note": ("单行业上限 %d 只，因超限剔除 %d 只候选" % (cap, len(trimmed))) if (cap and trimmed)
                else ("单行业上限 %d 只，本期未触发" % cap if cap else "未启用行业约束"),
    }


META_KEYS = ("vol20", "vol60", "turnover", "mom5", "mom20", "mom60", "rsi14",
             "vol_ratio", "ma_dev", "macd_hist", "dd20", "close", "score",
             "base_score", "pv_corr", "total_mv", "pe_ttm", "pb")


def _pick_meta(item: dict) -> dict:
    """落盘时一并记下的因子快照与决策信息（供事后归因）。"""
    out = {}
    for k in META_KEYS:
        v = item.get(k)
        if isinstance(v, (int, float)) and v == v:
            out[k] = round(float(v), 4)
    for k in ("funnel_stage", "verdict", "llm_reason", "llm_delta", "llm_score",
              "bear_case", "name", "industry"):
        if item.get(k) not in (None, ""):
            out[k] = item.get(k)
    return out


def record_picks(picked: list[dict], as_of: str, archive: dict | None = None) -> dict:
    """把当期候选落盘，供 T+N 后验闭环回看。

    每次选股＝一个批次（as_of + recorded_at）。同一数据日再选一次（改 TopN 或
    调参数）会新增一个批次，旧批次保留；T+N 后验也按批次聚合，实测口径可对照。

    除了候选本身，还写两样东西：
      · picks_meta  每只票的因子值与决策信息（归因要用，字段会演进，所以存 JSON）
      · pick_archive 整批的完整快照（含被淘汰的候选与理由），半年后复盘才有据可查
    """
    if not picked:
        return {"ok": True, "added": 0, "total": 0}
    import store
    as_of = str(as_of or "").strip()[:10]
    if len(as_of) != 10 or as_of[4] != "-" or as_of[7] != "-":
        return {"ok": False, "error": "选股记录的数据日无效：%s" % (as_of or "—"),
                "added": 0, "total": 0}
    now = _now_iso()
    rows = [{"code": str(x.get("code")).zfill(6), "name": x.get("name", ""),
             "industry": x.get("industry", ""), "rank": x.get("rank", ""),
             "score": x.get("score", ""), "close": x.get("close", ""),
             "recorded_at": now} for x in picked]
    try:
        added, replaced, total = store.picks_replace(as_of, rows)
    except Exception as e:
        return {"ok": False, "error": str(e), "added": 0, "total": 0}
    meta_n, aid = 0, 0
    try:
        meta = [{"code": str(x.get("code")).zfill(6), **_pick_meta(x)} for x in picked]
        meta_n = store.picks_meta_put(as_of, now, meta)
    except Exception as e:
        try:
            store.log("screener", "选股附注写入失败：%s" % e, level="warn", act="picks")
        except Exception:
            pass
    if archive:
        try:
            aid = store.archive_put(as_of, now, dict(archive, recorded_at=now,
                                                     as_of=as_of, count=len(picked)))
        except Exception:
            aid = 0
    return {"ok": True, "added": added, "replaced": replaced, "total": total,
            "recorded_at": now, "meta": meta_n, "archive_id": aid,
            "file": str(store.DB_PATH)}



def _now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_read(p: Path, default=None):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _json_write(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _universe_names() -> dict[str, dict]:
    if not UNIVERSE_PATH.exists():
        return {}
    try:
        with UNIVERSE_PATH.open("r", encoding="utf-8-sig", newline="") as f:
            return {r["code"]: r for r in csv.DictReader(f) if r.get("code")}
    except Exception:
        return {}


def _code_name(code: str, uni: dict[str, dict]) -> str:
    r = uni.get(code) or {}
    if r.get("name"):
        return r["name"]
    try:
        import watchlist
        n = watchlist.stock_name(code)
        if n and n != code:
            return n
    except Exception:
        pass
    return code


def _today_unclosed() -> str:
    """盘中（15:00 前）当日未收盘 K 线必须剔除，否则因子含未完成 bar。"""
    if dt.datetime.now().hour < 15:
        return dt.date.today().isoformat()
    return ""


def load_history(code: str, as_of: str | None = None) -> pd.DataFrame | None:
    """纯本地读取缓存日线；不联网、不触发刷新。"""
    import store
    d = store.bars_frame(code)
    if d is None:
        return None
    d["date"] = d["date"].astype(str).str.slice(0, 10)
    for c in ("open", "high", "low", "close", "volume", "amount", "pct", "turnover"):
        if c not in d.columns:
            d[c] = 0.0
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[(d["close"] > 0) & (d["open"] > 0)].sort_values("date")
    d = d.drop_duplicates("date").reset_index(drop=True)
    drop_day = _today_unclosed()
    if drop_day:
        d = d[d["date"] < drop_day]
    if as_of:
        d = d[d["date"] <= str(as_of)[:10]]
    return d.reset_index(drop=True)


def load_recent(code: str, n: int = 200) -> pd.DataFrame | None:
    """最近 n 根已收盘日线。比 load_history 快 3 倍：只解析文件尾部，不读全表。

    因子最长窗口 61 个交易日，n=200 留了三倍余量；ewm 类指标（rsi/macd）
    靠 200 期衰减到 1e-6 以下，与全量计算的差异在第 4 位小数，不影响排序。
    """
    import histcache
    d = histcache.recent(code, n)
    if d is None:
        return load_history(code)
    return d


def feature_frame(d: pd.DataFrame) -> pd.DataFrame:
    """单只股票全时序特征矩阵。所有计算只用 t 及之前的 bar，无前视。"""
    d = d.reset_index(drop=True).copy()
    c = d["close"].astype(float)
    h = d["high"].astype(float)
    l = d["low"].astype(float)
    v = d["volume"].astype(float).replace(0, np.nan)
    a = d.get("amount", pd.Series(0.0, index=d.index)).astype(float).replace(0, np.nan)
    ret = c.pct_change()
    ma20 = c.rolling(20, min_periods=20).mean()
    ma60 = c.rolling(60, min_periods=60).mean()
    sd20 = c.rolling(20, min_periods=20).std()
    up, dn = ma20 + 2 * sd20, ma20 - 2 * sd20

    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    hist = (macd - macd.ewm(span=9, adjust=False).mean()) / c * 100

    out = pd.DataFrame({"date": d["date"].astype(str).str.slice(0, 10)})
    out["ret1"] = ret * 100
    out["mom5"] = c.pct_change(5) * 100
    out["mom20"] = c.pct_change(20) * 100
    out["mom60"] = c.pct_change(60) * 100
    out["dist_ma60"] = (c / ma60 - 1) * 100
    out["ma_dev"] = (c / ma20 - 1) * 100
    out["macd_hist"] = hist
    out["rsi14"] = rsi
    out["boll_pos"] = (c - dn) / (up - dn).replace(0, np.nan)
    # 价格连续 20 天完全不动（长期停牌、一字板）时 std 理论上为 0，布林带宽度退化，
    # 「在带内的位置」没有定义。pandas 的滚动 std 会给出 1e-7 量级的浮点残差，
    # 于是 (up-dn) 非零、除出 0.5 这种「看着合理其实无意义」的值；
    # numpy 路径拿到的是精确 0.0，返回 NaN。用 STD_EPS 统一判成 NaN。
    out.loc[sd20.fillna(0.0) <= STD_EPS, "boll_pos"] = np.nan
    out["dd20"] = (c / h.rolling(20, min_periods=20).max() - 1) * 100
    out["vol20"] = ret.rolling(20, min_periods=20).std() * 100
    out["vol60"] = ret.rolling(60, min_periods=60).std() * 100
    out["amp20"] = ((h - l) / c.shift(1) * 100).rolling(20, min_periods=20).mean()
    out["vol_ratio"] = v.rolling(5, min_periods=5).mean() / v.rolling(20, min_periods=20).mean().replace(0, np.nan)
    out["amount_ratio"] = a.rolling(5, min_periods=5).mean() / a.rolling(20, min_periods=20).mean().replace(0, np.nan)
    out["turnover"] = d.get("turnover", pd.Series(0.0, index=d.index)).astype(float)
    # ---- K 线形态 / 位置类（Alpha158 组）----
    o = d["open"].astype(float).replace(0, np.nan)
    rng = (h - l).replace(0, np.nan)          # 一字板无实体，位置无定义，留空不编造
    low20 = l.rolling(20, min_periods=20).min()
    high20 = h.rolling(20, min_periods=20).max()
    out["kmid"] = (c / o - 1) * 100
    out["klen"] = (h - l) / o * 100
    out["ksft"] = (c - o) / rng
    out["rsv20"] = (c - low20) / (high20 - low20).replace(0, np.nan)
    # 一字板 / 长期停牌时当日或 20 日区间宽度为 0，「收盘在区间里的位置」无定义。
    # 与 boll_pos 同理：宽度可能只是浮点残差而非精确 0，用 STD_EPS 统一判成 NaN，
    # 保证两条实现路径给出同一个答案。
    out.loc[(h - l).abs() <= STD_EPS, "ksft"] = np.nan
    out.loc[(high20 - low20).abs() <= STD_EPS, "rsv20"] = np.nan
    out["cntp20"] = (ret > 0).astype(float).where(ret.notna()).rolling(20, min_periods=20).mean()
    out["corr20"] = c.rolling(20, min_periods=20).corr(v)
    out["close"] = c
    return out.replace([np.inf, -np.inf], np.nan)


def _ewm(x: np.ndarray, alpha: float) -> np.ndarray:
    """等价于 pandas ewm(alpha=alpha, adjust=False).mean()：首值作种子后递推。"""
    y = np.empty_like(x)
    y[0] = x[0]
    for i in range(1, len(x)):
        y[i] = (1.0 - alpha) * y[i - 1] + alpha * x[i]
    return y


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    if np.isnan(x).any() or np.isnan(y).any():
        return float("nan")
    xm, ym = x - x.mean(), y - y.mean()
    den = math.sqrt(float((xm * xm).sum()) * float((ym * ym).sum()))
    return float((xm * ym).sum() / den) if den else float("nan")


def _strict_mean(x: np.ndarray) -> float:
    """pandas rolling(mean) 遇 NaN 直接给 NaN，不用 nanmean 掩盖 —— 口径要一致。"""
    return float("nan") if np.isnan(x).any() else float(np.mean(x))


def last_features(d: pd.DataFrame) -> dict | None:
    """只算最后一根 bar 的因子（numpy 实现，供批量快照用）。

    为什么另写一份：feature_frame 每只 200 行要 13 ms，几乎全是 pandas 逐列
    构造 Series 的固定开销，跟数据量无关；1500 只就是 20 秒，页面卡在这里。
    这里直接对 numpy 数组算末尾值，快 30 倍以上。

    口径必须与 feature_frame().iloc[-1] 一致 —— 这是硬约束，现已由
    tests_factor_consistency.py（逐只）与 screener._consistency_probe（建快照时
    自动抽样）双重看住，改动任何一个公式都要跑一次。
    """
    if d is None or len(d) < 80:
        return None
    return _lf_arrays(_frame_cols(d))


def last_features_raw(code: str, n: int = 200) -> dict | None:
    """从库尾直接读数组算因子，跳过 DataFrame（省掉 pandas 1.5 ms 固定开销）。"""
    import histcache
    a = histcache.ohlcv(code, n)
    if not a or len(a["c"]) < 80:
        return None
    return _lf_arrays(_ohlcv_cols(a))


def _frame_cols(d: pd.DataFrame) -> dict:
    """DataFrame -> 按列名索引的数组字典（快照路径的取数口）。

    参考 Qlib 的 DataHandlerLP：它的训练与推理数据来自同一个 handler 的
    `_learn` / `_infer`，靠「同一套 processor」保证两边口径一致，而不是靠
    「两份实现各自小心」。这里做不到只留一份实现（DataFrame 版本要给
    build_dataset 逐期算，numpy 版本要给全市场逐只算，性能差 30 倍），
    但可以做到**所有取数都经过按列名的映射**：位置对齐只在一个地方发生，
    而那个地方是可以被测试一次性钉死的。
    """
    def col(name, default=None):
        if name not in d.columns:
            return default
        return d[name].to_numpy("f8")

    return {
        "c": col("close"), "o": col("open"), "h": col("high"), "l": col("low"),
        "v": col("volume"), "a": col("amount"), "t": col("turnover"),
        "date": str(d["date"].iloc[-1])[:10],
    }


def _ohlcv_cols(a: dict) -> dict:
    """histcache.ohlcv 的返回值 -> 同一个按列名字典。

    ohlcv 内部已经把 9 列映射成 o/h/l/c/v/a/t 这些**具名键**，
    这里只做一次转手，不再按下标取数 —— 历史上正是 `arr[:,6]` 这种
    位置索引把 turnover 取成了 pct（turnover 在 arr[:,7]）。
    """
    return {
        "c": a["c"], "o": a["o"], "h": a["h"], "l": a["l"],
        "v": a["v"], "a": a.get("a"), "t": a.get("t"),
        "date": str(a["date"][-1])[:10],
    }


def _lf_arrays(a: dict) -> dict | None:
    """因子计算的唯一实现：输入按列名字典，输出最后一根 bar 的因子字典。

    签名从「7 个位置参数」改成「一个按列名的 dict」，是这次重构的核心：
    位置参数一旦增删一列，调用方不会报错、只会静默错位（历史 bug 的成因）；
    具名键取不到就是 KeyError，当场炸出来。
    """
    c = a["c"]
    o = a["o"]
    h = a["h"]
    l = a["l"]
    v = a["v"]
    amt = a.get("a")
    t = a.get("t")
    date_str = a.get("date", "")
    n = len(c)
    if n < 80:
        return None
    v = np.where(v == 0, np.nan, v)
    # 金额列missing时用全 0 填充：amount_ratio 会得到 NaN（未知），不会被当成有数据
    amt = np.zeros(n) if amt is None else np.where(amt == 0, np.nan, amt)
    t = np.zeros(n) if t is None else t
    out: dict = {"date": str(date_str)[:10], "close": float(c[-1])}
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.diff(c) / c[:-1]                     # 与 pct_change() 同位
        out["ret1"] = (c[-1] / c[-2] - 1) * 100
        out["mom5"] = (c[-1] / c[-6] - 1) * 100
        out["mom20"] = (c[-1] / c[-21] - 1) * 100
        out["mom60"] = (c[-1] / c[-61] - 1) * 100

        ma20 = c[-20:].mean()
        ma60 = c[-60:].mean()
        sd20 = c[-20:].std(ddof=1)
        up, dn = ma20 + 2 * sd20, ma20 - 2 * sd20
        out["dist_ma60"] = (c[-1] / ma60 - 1) * 100
        out["ma_dev"] = (c[-1] / ma20 - 1) * 100
        # 与 feature_frame 用同一个退化判据（见 STD_EPS 注释）
        out["boll_pos"] = (float("nan") if sd20 <= STD_EPS
                           else (c[-1] - dn) / (up - dn))

        alpha = 1.0 / 14.0
        # RSI 的 gain/loss 用「价差」而不是收益率：ewm 是线性加权，
        # 换成收益率后权重与价格量级不再一致，结果会悄悄偏离（实测差 1 个点）。
        delta = np.diff(c)
        gain = _ewm(np.clip(delta, 0, None), alpha)
        loss = _ewm(np.clip(-delta, 0, None), alpha)
        out["rsi14"] = 100 - 100 / (1 + gain[-1] / loss[-1]) if loss[-1] != 0 else float("nan")

        e12 = _ewm(c, 2.0 / 13.0)
        e26 = _ewm(c, 2.0 / 27.0)
        macd = e12 - e26
        dea = _ewm(macd, 2.0 / 10.0)
        out["macd_hist"] = (macd[-1] - dea[-1]) / c[-1] * 100

        out["dd20"] = (c[-1] / h[-20:].max() - 1) * 100
        out["vol20"] = ret[-20:].std(ddof=1) * 100
        out["vol60"] = ret[-60:].std(ddof=1) * 100
        amp = (h[1:] - l[1:]) / c[:-1] * 100
        out["amp20"] = _strict_mean(amp[-20:])
        out["vol_ratio"] = _strict_mean(v[-5:]) / _strict_mean(v[-20:])
        out["amount_ratio"] = _strict_mean(amt[-5:]) / _strict_mean(amt[-20:])
        out["turnover"] = float(t[-1]) if n else float("nan")

        # ---- K 线形态 / 位置类（Alpha158 组）----
        rng = h[-1] - l[-1]
        out["kmid"] = (c[-1] / o[-1] - 1) * 100 if o[-1] else float("nan")
        out["klen"] = rng / o[-1] * 100 if o[-1] else float("nan")
        # 一字板 / 长期停牌：区间宽度退化为 0（可能是浮点残差），位置无定义
        out["ksft"] = (float("nan") if abs(rng) <= STD_EPS
                       else (c[-1] - o[-1]) / rng)
        low20, high20 = l[-20:].min(), h[-20:].max()
        out["rsv20"] = (float("nan") if abs(high20 - low20) <= STD_EPS
                        else (c[-1] - low20) / (high20 - low20))
        r20 = ret[-20:]
        out["cntp20"] = float("nan") if np.isnan(r20).any() else float((r20 > 0).mean())
        out["corr20"] = _corr(c[-20:], v[-20:])
    for k, val in out.items():
        if isinstance(val, float) and not math.isfinite(val):
            out[k] = None
        elif isinstance(val, np.floating):
            fv = float(val)
            out[k] = None if not math.isfinite(fv) else fv
    return out


def _cs_z(s: pd.Series) -> pd.Series:
    """横截面去极值 + 标准化（委托给 neutral.zscore，保持单一实现）。"""
    import neutral
    return neutral.zscore(pd.to_numeric(s, errors="coerce"))


def base_scores(df: pd.DataFrame, mode: str | None = None) -> pd.Series:
    """无模型时的基准分：方向加权横截面 z-score（可解释、可复现）。

    mode=industry 时先做行业内标准化，再按方向加权 —— 这样「动量最强」不再等于
    「押中最热行业」，而是「在同行业里相对最强」。行业类因子（model_only）不参与合成。
    """
    import neutral
    mode = mode or NEUTRAL_DEFAULT
    keys = [k for k in CORE_KEYS if k in df.columns]
    if not keys:
        return pd.Series(0.0, index=df.index)
    z = neutral.neutral_z(df, keys, mode=mode, skip=IND_KEYS)
    s = pd.Series(0.0, index=df.index)
    for k in keys:
        s = s + z[k] * float(DIRECTIONS.get(k) or 0.0)
    return _cs_z(s).fillna(0.0)


def attach_market_cap(df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """给当期横截面补总市值（亿元），供「行业+市值」中性化使用。

    只用于**当前**截面的可选中性化；训练集不这么做：本地只有最近一期的估值快照，
    拿它回填 2022 年的历史样本属于前视，宁可让训练退化为行业中性。
    返回（补好列的 df, 估值数据日）。
    """
    try:
        import datasource
        basic = datasource.load_basic() or {}
    except Exception:
        basic = {}
    if not basic:
        return df, ""
    vals, dates = [], []
    for c in df.get("code", []):
        e = basic.get(str(c).zfill(6)) or {}
        vals.append(e.get("total_mv") if e.get("total_mv") not in ("", None) else None)
        if e.get("trade_date"):
            dates.append(str(e.get("trade_date")))
    df = df.copy()
    df["total_mv"] = pd.to_numeric(pd.Series(vals, index=df.index), errors="coerce")
    return df, (max(dates) if dates else "")


def add_industry_features(df: pd.DataFrame) -> pd.DataFrame:
    """补行业类因子与行业内百分位。行业缺失时全部按中性值（0 / 空）处理，不编造。

    行业归属用的是当前映射回填历史，属于近似（真实历史成分会变），在 meta 里已注明。
    """
    import neutral
    df = df.copy()
    ind = neutral._ind_series(df)
    df["industry"] = ind
    mom = pd.to_numeric(df.get("mom20"), errors="coerce") if "mom20" in df.columns else None
    if mom is None:
        df["ind_mom20"] = 0.0
        df["ind_rel20"] = 0.0
        df["ind_n"] = 0
        df["ind_rank20"] = np.nan
        return df
    grouped = ind.ne("")
    gm = mom.groupby(ind).transform("mean").where(grouped)
    cnt = mom.groupby(ind).transform("count").where(grouped, 0).fillna(0)
    df["ind_mom20"] = gm.fillna(0.0).round(4)
    df["ind_rel20"] = (mom - gm).fillna(0.0).round(4)
    df["ind_n"] = cnt.astype(int)
    df["ind_rank20"] = neutral.intra_rank(df.assign(_m=mom), "_m").round(3)
    return df


def _sample_even(codes: list[str], limit: int) -> list[str]:
    """在代码序列里等间隔取样，而不是直接切前 N 个。

    旧实现 `codes[:limit]` 在 limit=400 时只取到 000001~000981，横截面里全是深市
    主板老股，没有 002/300/600/688 —— 行业分布和因子分布都是偏的，任何 IC、行业
    中性化都会失真。等间隔取样能在不增加计算量的前提下覆盖整个代码空间。
    """
    n = len(codes)
    if limit <= 0 or limit >= n:
        return list(codes)
    step = n / float(limit)
    return [codes[min(n - 1, int(i * step))] for i in range(limit)]


def resolve_scope(scope: str, limit: int = 0) -> list[str]:
    """scope: cached（有本地行情缓存） / pool（股票池） / all（全 A 名单，仍只取有缓存的）

    limit=0 表示不抽样、用全市场；正数才等间隔抽样。默认全市场，避免用户
    以为「样本池」等于全部 A 股，实际却只算了 1500 只。
    """
    uni = _universe_names()
    import store
    cached = store.cached_codes()
    if scope == "pool":
        try:
            import watchlist
            codes = [str(c).zfill(6) for c in watchlist.codes()]
        except Exception:
            codes = []
        codes = [c for c in codes if c in set(cached)]
    elif scope == "all":
        codes = [c for c in (uni.keys() or cached) if c in set(cached)]
    else:
        # 名单按交易所前缀只收个股；缓存里还留有已退市/改码的旧代码（如 000022
        # 已改 001872），选股只取当前名单内的个股。uni 为空时回退全部缓存。
        stocks = set(uni.keys())
        codes = [c for c in cached if (not stocks or c in stocks)]
    return _sample_even(sorted(codes), int(limit) if limit else 0)


def norm_limit(v) -> int:
    """样本上限归一：0 / 空 / 非法值都按「全市场」处理，正数限制在 200~20000。"""
    try:
        n = int(v or 0)
    except Exception:
        return 0
    if n <= 0:
        return 0
    return max(200, min(n, 20000))


# --------------------------------------------------------------------------
# 1) 横截面快照
# --------------------------------------------------------------------------

def cache_updating() -> bool:
    """行情批量更新是否正在进行。更新是逐只覆盖写缓存，期间读到的会是半更新数据。"""
    try:
        import bulkdata
        return bool(bulkdata.status().get("running"))
    except Exception:
        return False


def _snap_item(code: str, last: dict, uni: dict) -> dict | None:
    """把一只股票的因子字典转成快照行；关键因子缺失就跳过（不编造）。"""
    if last.get("mom60") is None or last.get("vol20") is None:
        return None
    item = {"code": code, "name": _code_name(code, uni),
            "market": (uni.get(code) or {}).get("market", ""),
            "industry": "",
            "date": str(last["date"]), "close": round(float(last["close"]), 2),
            "pct": round(float(last["ret1"]), 2) if last.get("ret1") is not None else None}
    for k in CORE_KEYS:
        v = last.get(k)
        item[k] = None if v is None or (isinstance(v, float) and math.isnan(v)) \
            else round(float(v), 4)
    return item


def _consistency_probe(rows: list[dict], limit: int = 40) -> str:
    """抽样比对「快照实际走的路径」与「训练集实际走的路径」算出的因子是否一致。

    返回空字符串表示一致；否则返回一句可直接展示给人看的告警。

    必须比对真正在跑的两条路，不能只比「同一份 DataFrame 的两种算法」：
      · 训练集：store.bars_frame -> feature_frame()        pandas，按列名取数
      · 快照：  histcache.ohlcv  -> _lf_arrays()           numpy，按位置取数

    历史 bug 就出在后者：ohlcv 里写死 arr[:,6] 取 turnover，而 arr[:,6] 实际是 pct
    （turnover 在 arr[:,7]）。所以探测必须从 last_features_raw 出发 —— 只比
    last_features(d) 与 feature_frame(d) 的话，两者共用同一个 DataFrame，
    永远“一致”，探测等于没做。
    """
    import store

    picked, bad = [], []
    step = max(1, len(rows) // max(1, limit))
    for r in rows[::step][:limit]:
        code = str(r.get("code") or "")
        if not code:
            continue
        d = store.bars_frame(code, 200)
        if d is None or len(d) < 80:
            continue
        # 快照路径：与 build_snapshot 里实际调用的一模一样
        fast = last_features_raw(code)
        # 训练路径：与 build_dataset 里实际调用的一模一样
        slow_df = feature_frame(d)
        if fast is None or slow_df is None or not len(slow_df):
            continue
        if str(fast.get("date"))[:10] != str(slow_df["date"].iloc[-1])[:10]:
            bad.append((code, "__date__"))
            continue
        last = slow_df.iloc[-1]
        for k in CORE_KEYS:
            a, b = fast.get(k), last.get(k)
            a_na = a is None or (isinstance(a, float) and math.isnan(a))
            b_na = b is None or (isinstance(b, float) and math.isnan(b))
            if a_na and b_na:
                continue
            if a_na != b_na:
                bad.append((code, k))
                continue
            try:
                fa, fb = float(a), float(b)
            except (TypeError, ValueError):
                continue
            tol = max(1e-6, abs(fb) * 1e-4)
            if abs(fa - fb) > tol:
                bad.append((code, k))
        picked.append(code)

    if not bad:
        return ""
    fields = sorted({k for _c, k in bad})
    sample = bad[0]
    return ("因子口径自查未通过：%d 只样本里有 %d 处不一致（%s）。"
            "例如 %s 的 %s，快照与训练集算出的不是同一个数 —— "
            "模型会在它没训练过的特征分布上打分，请先修口径再选股。"
            % (len(picked), len(bad), "、".join(fields[:4]),
               sample[0], sample[1]))


def build_snapshot(scope: str = "cached", as_of: str | None = None,
                   limit: int = 0) -> dict:
    """limit=0 表示全市场（默认）；正数则等间隔抽样，兼顾速度与代码空间覆盖。"""
    # 与行情更新并发时直接拒绝：否则会把更新前的旧 K 线固化成一张过期快照，
    # 表面上「选股成功」，实际数据日停在几天前。
    if cache_updating():
        st = {}
        try:
            import bulkdata
            st = bulkdata.status() or {}
        except Exception:
            pass
        return {"ok": False, "updating": True,
                "error": "行情正在增量更新（%s/%s），完成后再选股，否则会读到写了一半的行情"
                         % (st.get("done", "?"), st.get("total", "?"))}
    limit = norm_limit(limit)
    codes = resolve_scope(scope, limit)
    if not codes:
        return {"ok": False, "error": "没有可用的本地行情缓存，请先在「股票池 → 全 A 股名单」里预热行情"}
    uni = _universe_names()
    rows = []
    # 分批读取：一次把全市场五千多只的 DataFrame 全展开会吃掉 1 GB 以上内存，
    # 每批 300 只读完即算即弃，峰值只有几十 MB。
    for i in range(0, len(codes), 300):
        batch = codes[i:i + 300]
        if as_of:
            for code in batch:
                d = load_history(code, as_of)
                last = last_features(d) if d is not None else None
                if last:
                    item = _snap_item(code, last, uni)
                    if item:
                        rows.append(item)
        else:
            # 当期快照：完全不构造 DataFrame，直接读 CSV 尾部数组算因子。
            # 1500 只约 2 秒，全市场五千多只约 7 秒，是首屏「点一下要等很久」的主因。
            for code in batch:
                last = last_features_raw(code)
                if last:
                    item = _snap_item(code, last, uni)
                    if item:
                        rows.append(item)
    if not rows:
        return {"ok": False, "error": "有效样本不足：需要至少 80 根已缓存日线，请先预热行情"}
    # 横截面必须落在同一交易日：增量更新失败的个股最后 bar 会落后一两天，
    # 若混进来，等于把不同交易日的涨跌幅放进同一个横截面排序，因子与打分全部失真。
    # 取覆盖样本最多的交易日为基准（并列取较新），落后的直接剔除并如实告知。
    vc = pd.Series([r["date"] for r in rows]).value_counts()
    base = max(vc[vc == vc.max()].index)
    lag_rows = [r for r in rows if r["date"] != base]
    rows = [r for r in rows if r["date"] == base]
    if not rows:
        return {"ok": False, "error": "没有落在同一交易日的有效样本"}
    snap = pd.DataFrame(rows)
    # 口径自查：快照走 last_features_raw（numpy 按位置取数），训练集走 feature_frame
    # （pandas 按列名取数）。两条路径一旦不一致，模型吃到的是它没训练过的分布 ——
    # 这类错误不会抛异常，只会让分数悄悄失真（历史上 turnover 就被取成了 pct）。
    # 建快照是唯一同时具备「全市场数据」和「值得付出这几百毫秒」的时机，
    # 抽样 40 只做一次逐值比对，发现问题写进 warning，界面与日志都看得见。
    warn_consistency = ""
    try:
        warn_consistency = _consistency_probe(rows, limit=40)
    except Exception:
        warn_consistency = ""
    try:
        import industry
        snap = industry.enrich(snap)      # 补「代码 → 行业」，查不到留空（不编造）
    except Exception:
        pass
    snap, mv_day = attach_market_cap(snap)   # 行业+市值中性化需要总市值
    snap = add_industry_features(snap)    # 行业动能 / 行业内相对动量 / 行业内百分位
    snap["base_score"] = base_scores(snap, NEUTRAL_DEFAULT).round(4)
    snap = snap.sort_values("base_score", ascending=False).reset_index(drop=True)
    snap.insert(0, "rank", range(1, len(snap) + 1))
    # 带截止日的属于「历史回看」，只用于本次计算，不能覆盖「最新横截面」快照文件；
    # 否则下次首屏复用快照时又会拿回历史日的数据。
    persist = not as_of
    if persist:
        _atomic_csv(snap, SNAPSHOT_CSV)
    dates = sorted({r["date"] for r in rows})
    lag_days = sorted({r["date"] for r in lag_rows})
    dist = snap["industry"].replace("", "未分类").value_counts().to_dict()
    warns = []
    if lag_rows:
        # 两种方向都要说清楚：落后是没抓到，超前是数据源分批入库（北交所当天就有 bar）
        ahead = [d for d in lag_days if d > base]
        behind = [d for d in lag_days if d < base]
        why = []
        if behind:
            why.append("停在 %s（落后）" % "、".join(behind))
        if ahead:
            why.append("已到 %s（比基准日新，属数据源分批入库）" % "、".join(ahead))
        warns.append("已剔除 %d 只最后交易日与基准日不一致的股票（%s），横截面统一到 %s；"
                     "若落后的一批数量偏多，请到「股票池」页对它们单独补抓行情"
                     % (len(lag_rows), "；".join(why), base))
    if as_of:
        warns.append("当前按历史截止日 %s 计算，不是最新行情；清空「截止日」即可回到最新" % as_of)
    if warn_consistency:
        warns.append(warn_consistency)
    meta = {"ok": True, "generated": _now_iso(), "scope": scope, "as_of": base,
            "as_of_param": as_of or "",
            "limit": limit, "count": len(rows), "date_min": dates[0], "date_max": dates[-1],
            "date_aligned": True,
            "lag_excluded": len(lag_rows),
            "lag_days": lag_days,
            "lag_sample": [r["code"] for r in lag_rows[:6]],
            "features": [f["key"] for f in FEATURES],
            "feature_version": FEATURE_VERSION,
            "neutral": NEUTRAL_DEFAULT,
            "industries": len([k for k in dist if k != "未分类"]),
            "industry_dist": dist,
            "industry_cover": round((len(rows) - dist.get("未分类", 0)) / max(1, len(rows)) * 100, 1),
            "market_cap_cover": (round(float(snap["total_mv"].notna().mean()) * 100, 1)
                                 if "total_mv" in snap.columns else 0.0),
            "market_cap_day": mv_day,
            "is_historical": bool(as_of),
            "persisted": persist,
            "warning": "；".join(warns)}
    if persist:
        _json_write(SNAPSHOT_META, meta)
    return {**meta, "rows": _rows(snap), "file": str(SNAPSHOT_CSV)}


def _rows(df: pd.DataFrame) -> list[dict]:
    out = []
    for r in df.to_dict("records"):
        for k, v in list(r.items()):
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                r[k] = None
            elif isinstance(v, (np.integer,)):
                r[k] = int(v)
            elif isinstance(v, (np.floating,)):
                r[k] = float(v)
        out.append(r)
    return out


_SNAP_CACHE: dict = {"key": None, "df": None, "meta": None}


def load_snapshot() -> tuple[pd.DataFrame | None, dict]:
    """读横截面快照。按文件 mtime+size 缓存，返回副本。

    单股分析、选股、数据中枢都会读它，每次 read_csv 5000 行约 40ms；
    缓存后只剩一次 copy（几毫秒）。返回副本是因为调用方可能往 df 上加列。
    """
    if not SNAPSHOT_CSV.exists():
        return None, _json_read(SNAPSHOT_META, {}) or {}
    try:
        st = SNAPSHOT_CSV.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _SNAP_CACHE.get("key") == key and _SNAP_CACHE.get("df") is not None:
        return _SNAP_CACHE["df"].copy(), dict(_SNAP_CACHE.get("meta") or {})
    try:
        df = pd.read_csv(SNAPSHOT_CSV, dtype={"code": str})
        if "industry" in df.columns:      # 空行业读回来是 NaN，统一成空字符串
            df["industry"] = df["industry"].fillna("").replace({"nan": ""})
        # 行业口径可能升级过（如新浪 49 板块 → 申万一级）：读回旧快照时按当前映射重刷，
        # 否则「行业约束」和候选表会继续显示旧口径，和新做的行业页自相矛盾
        try:
            import industry as _ind
            m = _ind.load_map()
            if m and "code" in df.columns:
                fresh = [m.get(str(c).zfill(6), "") for c in df["code"]]
                old = df["industry"].tolist() if "industry" in df.columns else [""] * len(df)
                df["industry"] = [f or o for f, o in zip(fresh, old)]
        except Exception:
            pass
        meta = _json_read(SNAPSHOT_META, {}) or {}
        if key is not None:
            _SNAP_CACHE.update(key=key, df=df, meta=meta)
            # 缓存里那份永不外借，否则调用方加一列就把缓存污染了
            return df.copy(), dict(meta)
        return df, meta
    except Exception:
        return None, {}


# --------------------------------------------------------------------------
# 2) 训练数据集
# --------------------------------------------------------------------------

def build_dataset(start: str = "20220101", end: str | None = None,
                  horizon: int = 10, step: int = 5,
                  max_symbols: int = 800, scope: str = "cached",
                  neutral_mode: str = "none",
                  label_mode: str = "rank",
                  entry_lag: int = 1) -> dict:
    """多期横截面样本。

    标签两口径（label_mode）：
      market    未来 horizon 日收益 − 当日全样本均值（旧口径，含行业轮动 β）
      industry  未来 horizon 日收益 − 当日**同行业**均值（行业中性标签）
                模型只学「同行业里谁更好」，不会把行业轮动当成 alpha

    特征两口径（neutral_mode）：与推理时保持一致，行业中性化在构造训练集时就要做，
    否则「训练用原始值、推理用中性值」这种口径错配会让模型输出变形。
    """
    import neutral
    start = str(start).replace("-", "")[:8]
    end = str(end or dt.date.today().strftime("%Y%m%d")).replace("-", "")[:8]
    horizon = max(1, min(int(horizon), 60))
    step = max(1, min(int(step), 30))
    neutral_mode = neutral_mode if neutral_mode in NEUTRAL_MODES else NEUTRAL_DEFAULT
    label_mode = label_mode if label_mode in LABEL_MODES else "market"
    neutral_requested = neutral_mode
    neutral_note = ""
    size_cover = None
    codes = resolve_scope(scope, max_symbols)
    if not codes:
        return {"ok": False, "error": "没有可用的本地行情缓存，无法构造训练集"}

    frames: dict[str, pd.DataFrame] = {}
    # 标签口径：entry_lag=1 表示 T 日收盘后打分、T+1 收盘建仓，收益算到 T+1+h，
    # 与 Qlib 的 Ref($close,-2)/Ref($close,-1)-1 同构，不含 T→T+1 隔夜跳空。
    # entry_lag=0 是旧口径（假设 T 日收盘即可成交），仅用于回看「前视能虚增多少」。
    entry_lag = 1 if int(entry_lag) != 0 else 0
    for code in codes:
        d = load_history(code)
        if d is None or len(d) < 100:
            continue
        f = feature_frame(d)
        close = d["close"].astype(float)
        f["y_fwd"] = (close.shift(-(horizon + entry_lag)) /
                      close.shift(-entry_lag) - 1) * 100
        f["_daykey"] = f["date"].str.replace("-", "", regex=False)
        frames[code] = f[(f["_daykey"] >= start) & (f["_daykey"] <= end)]

    if not frames:
        return {"ok": False, "error": "区间内没有足够历史数据，请调早开始日期或先预热行情"}

    all_days = sorted({x for f in frames.values() for x in f["_daykey"]})
    sample_days = set(all_days[::step])
    uni = _universe_names()
    records = []
    for code, f in frames.items():
        sub = f[f["_daykey"].isin(sample_days)]
        if sub.empty:
            continue
        sub["code"] = code
        sub["name"] = _code_name(code, uni)
        sub = sub[["_daykey", "date", "code", "name"] + CORE_KEYS + ["y_fwd"]]
        sub = sub.replace([np.inf, -np.inf], np.nan)
        records.append(sub)

    if not records:
        return {"ok": False, "error": "样本为空：需要更长历史或更小的采样步长"}
    ds = pd.concat(records, ignore_index=True)

    # 行业归属（当前映射回填历史，属近似；分类变动本身是低频事件）
    try:
        import industry
        m = industry.load_map()
    except Exception:
        m = {}
    ds["industry"] = [m.get(str(c).zfill(6), "") for c in ds["code"]]

    # 市值中性化需要「当时」的总市值，不能拿今天的估值回填历史（前视）。
    # 数据源每次拉 daily_basic 都会累积到 data/extra/mv_history.csv；这里按
    # 「同一代码 ≤ 样本日、最多向前 8 个自然日」取最近一次快照。
    if neutral_requested == "industry+size":
        try:
            import datasource
            hist = datasource.load_mv_history()
        except Exception:
            hist = {}
        if hist:
            try:
                hrows = [{"_dt": pd.Timestamp(day), "code": code, "total_mv": mv}
                         for day, mp in hist.items() for code, mv in mp.items()]
                hdf = pd.DataFrame(hrows).sort_values(["_dt", "code"]).reset_index(drop=True)
                ds["_dt"] = pd.to_datetime(ds["date"], errors="coerce")
                ds = pd.merge_asof(ds.sort_values(["_dt", "code"]).reset_index(drop=True),
                                   hdf, on="_dt", by="code", direction="backward",
                                   tolerance=pd.Timedelta(days=8))
                ds = ds.drop(columns=["_dt"])
                size_cover = round(float(pd.to_numeric(ds["total_mv"], errors="coerce")
                                         .notna().mean()) * 100, 1)
            except Exception:
                size_cover = None
                ds.drop(columns=[c for c in ("_dt", "total_mv") if c in ds.columns],
                        inplace=True)
        if size_cover is None or size_cover < 60:
            neutral_mode = "industry"
            neutral_note = ("历史总市值覆盖不足（%s），训练退化为行业中性；"
                            "在「数据中枢 → 数据源」拉取估值会逐日累积，覆盖够了会自动启用市值中性"
                            % ("无历史市值" if size_cover is None else "%.1f%%" % size_cover))
        else:
            neutral_note = "历史总市值覆盖 %.1f%%（按样本日前 8 天内的最近估值匹配），已启用行业 + 市值中性" % size_cover

    # 行业类因子：按「同日 + 同行业」算组均值，保证与推理快照同口径
    ind = neutral._ind_series(ds)
    mom = pd.to_numeric(ds["mom20"], errors="coerce")
    gm = mom.groupby([ds["_daykey"], ind]).transform("mean")
    grouped = ind.ne("")
    gm = gm.where(grouped, np.nan)
    ds["ind_mom20"] = gm.fillna(0.0)
    ds["ind_rel20"] = (mom - gm).fillna(0.0)

    # 特征中性化：逐日逐截面做，口径与推理端完全一致
    if neutral_mode != "none":
        parts = []
        for _day, sub in ds.groupby("_daykey", sort=False):
            z = neutral.neutral_z(sub, CORE_KEYS, mode=neutral_mode, skip=IND_KEYS)
            s2 = sub.copy()
            s2[CORE_KEYS] = z.reindex(columns=CORE_KEYS).to_numpy()
            parts.append(s2)
        ds = pd.concat(parts, ignore_index=True).sort_values(["_daykey", "code"]).reset_index(drop=True)

    # 标签：y_excess 恒为「% 口径的横截面超额」，用于评估与报告；
    # y_target 才是模型真正拟合的目标（rank 模式下是分位，否则等于 y_excess）。
    # 分开存是为了避免「用分位训练、却把分位当收益率汇报」这种口径混淆。
    day_mean = ds.groupby("_daykey")["y_fwd"].transform("mean")
    ind_key = neutral._ind_series(ds).replace("", np.nan)
    ind_mean = ds.groupby([ds["_daykey"], ind_key])["y_fwd"].transform("mean")
    ind_base = ind_mean.where(ind_key.notna(), day_mean)
    if label_mode == "industry":
        ds["y_excess"] = ds["y_fwd"] - ind_base
        base_desc = "当日同行业均值"
    else:
        ds["y_excess"] = ds["y_fwd"] - day_mean
        base_desc = "当期横截面均值"
    ds["y_excess"] = ds["y_excess"].clip(-25, 25)
    ds["y_fwd"] = ds["y_fwd"].clip(-50, 50)
    if label_mode == "rank":
        pct = ds.groupby("_daykey")["y_fwd"].rank(pct=True, method="average")
        ds["y_target"] = pct - 0.5
        label_desc = "未来 %d 日收益的当日横截面分位（−0.5~0.5，CSRankNorm 思路）" % horizon
    else:
        ds["y_target"] = ds["y_excess"]
        label_desc = "未来 %d 个交易日收益 − %s（超额，%%）" % (horizon, base_desc)
    ds[FEATURE_KEYS] = ds[FEATURE_KEYS].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    ds = ds.sort_values(["_daykey", "code"]).reset_index(drop=True)
    _atomic_csv(ds, DATASET_CSV)

    cross = ds.groupby("_daykey").size()
    meta = {"ok": True, "generated": _now_iso(), "start": start, "end": end,
            "horizon": horizon, "step": step, "scope": scope,
            "max_symbols": int(max_symbols or 0),
            "symbols": len(records), "rows": int(len(ds)),
            "cross_sections": int(len(cross)),
            "date_min": str(ds["date"].min()), "date_max": str(ds["date"].max()),
            "avg_width": round(float(cross.mean()), 1),
            "label": label_desc, "label_mode": label_mode,
            "label_entry": ("T+1 收盘建仓（T 日收盘后打分，不含 T→T+1 隔夜跳空）" if entry_lag
                            else "T 日收盘建仓（旧口径，含隔夜跳空，收益偏高估）"),
            "entry_lag": entry_lag,
            "target_col": "y_target",
            "neutral": neutral_mode, "neutral_requested": neutral_requested,
            "neutral_note": neutral_note,
            "size_cover": size_cover,
            "feature_version": FEATURE_VERSION,
            "features": FEATURE_KEYS,
            "industry_cover": round(float(neutral._ind_series(ds).ne("").mean()) * 100, 1),
            "file": str(DATASET_CSV),
            "note": "标签使用未来数据仅用于训练；推理快照不含任何未来信息。行业归属用当前映射回填，属近似。"
                    + (neutral_note + "。" if neutral_note else "")}
    _json_write(DATASET_META, meta)
    return meta


def load_dataset() -> tuple[pd.DataFrame | None, dict]:
    if not DATASET_CSV.exists():
        return None, _json_read(DATASET_META, {}) or {}
    try:
        return pd.read_csv(DATASET_CSV, dtype={"code": str, "_daykey": str,
                                              "date": str}), (_json_read(DATASET_META, {}) or {})
    except Exception:
        return None, {}


def status() -> dict:
    import store
    snap_df, snap_meta = load_snapshot()
    ds_df, ds_meta = load_dataset()
    uni = _universe_names()
    ind = {}
    try:
        import industry
        cached_codes = store.cached_codes()
        ind = industry.status(cached_codes)
    except Exception as e:
        ind = {"ok": False, "error": str(e)[:120]}
    picks_rows = store.picks_count()
    neutral_info = {"default": NEUTRAL_DEFAULT, "modes": list(NEUTRAL_MODES),
                    "describe": {}, "size": {}, "min_group": 5}
    try:
        import neutral as _n
        neutral_info["describe"] = {m: _n.describe(m) for m in NEUTRAL_MODES}
        neutral_info["size"] = _n.size_available()
        neutral_info["min_group"] = _n.MIN_GROUP
    except Exception as e:
        neutral_info["error"] = str(e)[:100]
    return {
        "ok": True,
        "universe_total": len(uni),
        "cached_total": store.cached_count(),
        "feature_count": len(FEATURES),
        "feature_version": FEATURE_VERSION,
        "core_feature_count": len(CORE_KEYS),
        "industry_feature_count": len(IND_KEYS),
        "features": FEATURES,
        "neutral": neutral_info,
        "snapshot": {**snap_meta, "loaded": snap_df is not None,
                     "rows": 0 if snap_df is None else int(len(snap_df))},
        "dataset": {**ds_meta, "loaded": ds_df is not None,
                    "rows": 0 if ds_df is None else int(len(ds_df))},
        "modules": MODULES,
        "industry": ind,
        "funnel": {"defaults": FUNNEL_DEFAULT, "industry_cap": INDUSTRY_CAP_DEFAULT},
        "picks": {"rows": picks_rows, "file": str(store.DB_PATH)},
        "data_dir": str(SCREEN_DIR),
        "note": "三层链路 + 漏斗 / 行业中性化 / 行业约束 / T+N 后验已全部落地；指标仍受本地行情覆盖度限制。",
    }
