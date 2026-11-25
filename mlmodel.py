# -*- coding: utf-8 -*-
"""
QuantDesk 选股框架 · 模型层（Stage 2）

LightGBM 为主：横截面特征 → 未来 N 日超额收益回归，时序切分验证，输出 IC / RankIC / 特征重要度。
无 lightgbm 时自动降级为 NumPy 岭回归（同样保存权重与评估指标），界面明确标出降级状态。
未训练时不假装预测，使用「方向加权横截面 z-score」作为可解释基准分。
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import screener

BASE = Path(__file__).resolve().parent
SCREEN_DIR = BASE / "data" / "screen"
SCREEN_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = SCREEN_DIR / "model.txt"
MODEL_META = SCREEN_DIR / "model.json"

try:
    import lightgbm as lgb
    LGB_OK = True
    LGB_ERR = ""
    LGB_VER = getattr(lgb, "__version__", "unknown")
except Exception as e:  # 缺依赖 / 版本不兼容时仍能跑通框架
    lgb = None
    LGB_OK = False
    LGB_ERR = str(e)
    LGB_VER = ""


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _meta() -> dict:
    if not MODEL_META.exists():
        return {}
    try:
        return json.loads(MODEL_META.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(m: dict):
    MODEL_META.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _corr(a, b) -> float | None:
    x, y = pd.Series(a), pd.Series(b)
    if len(x) < 10 or x.std() < 1e-12 or y.std() < 1e-12:
        return None
    return round(float(x.corr(y)), 4)


def _rank_corr(a, b) -> float | None:
    return _corr(pd.Series(a).rank(), pd.Series(b).rank())


def _matrix(df: pd.DataFrame, keys: list[str] | None = None) -> pd.DataFrame:
    keys = keys or screener.FEATURE_KEYS
    x = df.reindex(columns=keys).apply(pd.to_numeric, errors="coerce")
    return x.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _model_keys(meta: dict) -> list[str]:
    """模型实际使用的因子列。旧模型（v1）没有 features 字段，按 16 个核心因子处理，
    这样升级因子集后老模型仍能推理，不会因为列数不匹配直接报错。"""
    keys = meta.get("features") or screener.CORE_KEYS
    return [k for k in keys if k in screener.FEATURE_KEYS]


def _prep_infer(df: pd.DataFrame, meta: dict) -> pd.DataFrame:
    """推理前的因子处理：口径必须与训练时一致（meta 里记着当时的 neutral 模式）。"""
    keys = _model_keys(meta)
    mode = meta.get("neutral") or "none"
    if mode in ("industry", "industry+size") and "industry" in df.columns:
        try:
            import neutral
            z = neutral.neutral_z(df, [k for k in keys if k in screener.CORE_KEYS],
                                  mode=mode, skip=screener.IND_KEYS)
            out = df[keys].copy() if all(k in df.columns for k in keys) else _matrix(df, keys)
            for k in z.columns:
                if k in out.columns:
                    out[k] = z[k]
            return out.fillna(0.0)
        except Exception:
            pass
    return _matrix(df, keys)


def train(params: dict | None = None, only_keys: list[str] | None = None) -> dict:
    """用 screener.build_dataset() 产出的数据集训练模型。

    only_keys 可选：只训练指定的因子列。用于消融实验（例如对比「旧 18 因子 vs 新增形态因子」），
    正常调用不传，走训练集自己记录的完整因子集。
    """
    p = {"n_estimators": 400, "learning_rate": 0.04, "num_leaves": 31,
         "min_child_samples": 100, "subsample": 0.85, "colsample_bytree": 0.8,
         "lambda_l1": 1.0, "lambda_l2": 5.0}
    p.update({k: v for k, v in (params or {}).items() if k in p})
    ds, dsm = screener.load_dataset()
    if ds is None or len(ds) < 200:
        return {"ok": False, "error": "训练样本不足（需要 ≥200 行），请先在选股工作台构造训练集"}
    ds = ds.sort_values(["_daykey", "code"]).reset_index(drop=True)
    ds["_daykey"] = ds["_daykey"].astype(str)
    days = sorted(ds["_daykey"].unique())
    if len(days) < 8:
        return {"ok": False, "error": "横截面期数太少（需要 ≥8 期），请减小采样步长或拉长区间"}
    # 时序切分 + 隔离带（purge）：训练集末尾的标签窗口会覆盖到验证期，
    # 不隔离就等于拿验证期的未来收益训练。需要隔开 horizon+step 个**交易日**，
    # 而 days 里每个元素是「采样后的截面」（间隔 step 个交易日），所以要除以 step 换算，
    # 否则会按截面数砍，白白丢掉数倍长度的训练样本。
    horizon = int(dsm.get("horizon") or 10)
    step = int(dsm.get("step") or 5)
    gap = max(1, int(math.ceil((horizon + step) / float(step))))
    gap = min(gap, max(1, len(days) // 5))
    cut = max(1, int(len(days) * 0.75))
    tr_end = max(1, cut - gap)
    tr_days, va_days = set(days[:tr_end]), set(days[cut:])
    tr, va = ds[ds["_daykey"].isin(tr_days)], ds[ds["_daykey"].isin(va_days)]
    if tr.empty or va.empty:
        return {"ok": False, "error": "切分后训练集或验证集为空，请拉长训练区间或减小标签周期"}

    # 用训练集自己记录的因子集与中性化口径，保证「训练 → 推理」口径完全一致
    keys = [k for k in (dsm.get("features") or screener.CORE_KEYS) if k in ds.columns]
    if only_keys:
        keys = [k for k in only_keys if k in ds.columns] or keys
    if not keys:
        return {"ok": False, "error": "训练集里没有任何可用因子列，请重新构造训练集"}
    ds_neutral = dsm.get("neutral") or "none"
    # 训练目标优先用 y_target（rank 模式下是横截面分位，−0.5~0.5），
    # 评估一律用 y_excess（% 口径），避免「拿分位当收益率念」的口径混淆。
    # 末尾样本的未来收益还没到期时标签是 NaN：训练/验证都必须剔除，
    # 填 0 等于告诉模型「未来不涨不跌」，会把验证期尾部的 IC 和超额算歪。
    tcol = str(dsm.get("target_col") or "y_target")
    if tcol not in ds.columns:
        tcol = "y_excess"          # 兼容旧版训练集
    ytr_raw = pd.to_numeric(tr[tcol], errors="coerce")
    yva_raw = pd.to_numeric(va[tcol], errors="coerce")
    if not ytr_raw.notna().all():
        tr, ytr_raw = tr.loc[ytr_raw.notna()], ytr_raw[ytr_raw.notna()]
    if not yva_raw.notna().all():
        va, yva_raw = va.loc[yva_raw.notna()], yva_raw[yva_raw.notna()]
    if tr.empty or va.empty:
        return {"ok": False, "error": "剔除未到期标签后训练集或验证集为空，请重建训练集"}
    tr_days, va_days = set(tr["_daykey"]), set(va["_daykey"])
    ytr, yva = ytr_raw.astype(float), yva_raw.astype(float)
    Xtr, Xva = _matrix(tr, keys), _matrix(va, keys)
    lo, hi = Xtr.quantile(0.005), Xtr.quantile(0.995)
    Xtr, Xva = Xtr.clip(lo, hi, axis=1), Xva.clip(lo, hi, axis=1)

    engine, warning, importance = "", "", {}
    if LGB_OK:
        # 用 lightgbm 原生 train / Dataset 接口：不需要 scikit-learn 这个额外重依赖
        dtrain = lgb.Dataset(Xtr, label=ytr.to_numpy(), free_raw_data=False)
        dvalid = lgb.Dataset(Xva, label=yva.to_numpy(), reference=dtrain,
                             free_raw_data=False)
        lgbm_p = {
            "objective": "regression", "metric": "l2", "verbosity": -1,
            "learning_rate": float(p["learning_rate"]), "num_leaves": int(p["num_leaves"]),
            "min_data_in_leaf": int(p["min_child_samples"]),
            "feature_fraction": float(p["colsample_bytree"]),
            "bagging_fraction": float(p["subsample"]), "bagging_freq": 1,
            "lambda_l1": float(p["lambda_l1"]), "lambda_l2": float(p["lambda_l2"]),
            "num_threads": 2, "seed": 42,
        }
        booster = lgb.train(lgbm_p, dtrain, num_boost_round=int(p["n_estimators"]),
                            valid_sets=[dvalid],
                            callbacks=[lgb.early_stopping(40, verbose=False),
                                       lgb.log_evaluation(0)])
        pred = booster.predict(Xva)
        booster.save_model(str(MODEL_PATH))
        imp = booster.feature_importance(importance_type="gain")
        total = float(imp.sum()) or 1.0
        importance = {k: round(float(v) / total, 4)
                      for k, v in sorted(zip(keys, imp), key=lambda kv: -kv[1])}
        engine = "lightgbm"
        best_iter = int(booster.best_iteration or p["n_estimators"])
    else:
        # 纯 NumPy 岭回归降级：标准化 + L2 闭式解，保证框架在无 wheel 的机器也能跑
        mu, sd = Xtr.mean(), Xtr.std(ddof=0).replace(0, 1.0)
        Z = ((Xtr - mu) / sd).to_numpy()
        Z = np.hstack([np.ones((len(Z), 1)), Z])
        lam = 10.0
        w = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ ytr.to_numpy())
        Zv = ((Xva - mu) / sd).to_numpy()
        pred = np.hstack([np.ones((len(Zv), 1)), Zv]) @ w
        importance = {k: round(float(abs(v)) / (float(np.abs(w[1:]).sum()) or 1.0), 4)
                      for k, v in sorted(zip(keys, w[1:]), key=lambda kv: -abs(kv[1]))}
        engine = "ridge-fallback"
        warning = "未检测到可用的 LightGBM（%s），已降级为岭回归；梯度提升的非线性能力暂不可用。" % LGB_ERR[:80]
        best_iter = 0
        _write_meta_ridge(w.tolist(), mu.tolist(), sd.tolist())

    vdf = va[["_daykey", "y_excess", "y_fwd", "industry"]].copy() \
        if "industry" in va.columns else va[["_daykey", "y_excess", "y_fwd"]].copy()
    vdf["pred"] = pred
    vdf["y_excess"] = pd.to_numeric(vdf["y_excess"], errors="coerce")
    vdf = vdf[vdf["y_excess"].notna()].copy()
    if vdf.empty:
        return {"ok": False, "error": "验证集没有可用的未来收益标签，请重建训练集"}
    top = (vdf.groupby("_daykey", group_keys=False)
           .apply(lambda g: g.nlargest(max(1, int(len(g) * 0.2)), "pred"), include_groups=False))
    label_mode = str(dsm.get("label_mode") or "market")
    # 逐期 RankIC 序列：均值只说明「平均有没有用」，标准差/IR 才说明「稳不稳」。
    # 单看 IC 均值容易把「某几个季度运气好」当成能力，这里把稳定性一起报出来。
    daily_ic = []
    for _dk, g in vdf.groupby("_daykey"):
        v = _rank_corr(g["pred"], g["y_excess"])
        # 某期预测值或标签全同（涨跌停一字板等）时相关系数无定义，会返回 nan，
        # 不过滤就会把整条 IC 序列污染成 nan
        if v is not None and np.isfinite(v):
            daily_ic.append(float(v))
    ic_mean = round(float(np.mean(daily_ic)), 4) if daily_ic else None
    ic_std = round(float(np.std(daily_ic, ddof=1)), 4) if len(daily_ic) > 1 else None
    metrics = {
        "ic": _corr(vdf["pred"], vdf["y_excess"]),
        "rank_ic": _rank_corr(vdf["pred"], vdf["y_excess"]),
        # rank 标签下预测值是分位，与 % 收益不同尺度，RMSE 无意义，留空不误报
        "rmse": (None if label_mode == "rank" else
                 round(float(np.sqrt(np.mean((vdf["pred"] - vdf["y_excess"]) ** 2))), 4)),
        "top20_excess": round(float(top["y_excess"].mean()), 4) if len(top) else None,
        "all_excess": round(float(vdf["y_excess"].mean()), 4),
        "valid_sections": len(va_days),
        "ic_periods": len(daily_ic),
        "ic_mean": ic_mean, "ic_std": ic_std,
        "ic_ir": (round(ic_mean / ic_std, 2) if (ic_mean is not None and ic_std) else None),
        "ic_pos_ratio": (round(sum(1 for v in daily_ic if v > 0) / len(daily_ic), 3)
                         if daily_ic else None),
        "ic_last10": [round(v, 3) for v in daily_ic[-10:]],
        # 预测值离散度：模型输出趋于常数就说明信号已经衰减
        "pred_std": round(float(np.std(pred)), 5) if len(pred) else None,
    }
    # 头部超额（head excess）：TopN 相对当期池子均值的超额，逐期平均。
    #
    # 为什么单看 rank_ic 不够：IC 衡量的是「整条排序对不对」，而实盘只买头部。
    # 实测出现过 IC 仍为正（0.10）却 Top10 亏损的情形 —— 全市场都在跌时，
    # 排序能力还在，但「头部那 10 只相对池子的优势」已经消失，钱是亏的。
    # 这个指标才对应「按榜单买入会怎样」，IC 对应「模型还懂不懂排序」，
    # 两者背离时必须看前者。
    try:
        _hx = {}
        for _n in (1, 5, 10, 20):
            _per, _win = [], []
            for _dk, g in vdf.groupby("_daykey"):
                if len(g) < _n * 2:
                    continue
                _top = g.nlargest(_n, "pred")["y_fwd"]
                _ex = float(_top.mean()) - float(g["y_fwd"].mean())
                _per.append(_ex)
                _win.append(1 if _ex > 0 else 0)
            if _per:
                _hx[_n] = {"mean": round(float(np.mean(_per)), 4),
                           "win": round(float(np.mean(_win)), 3),
                           "n": len(_per)}
        if _hx:
            metrics["head_excess"] = _hx
            metrics["head10"] = _hx.get(10, {}).get("mean")
            metrics["head10_win"] = _hx.get(10, {}).get("win")
    except Exception:
        pass
    # 行业超额口径的对照评估：同一批预测，把基准从「全市场均值」换成「同行业均值」，
    # 用来回答「这套打分到底是行业轮动还是个股 α」。不管标签怎么定都算一遍。
    if "industry" in vdf.columns:
        try:
            ik = vdf["industry"].astype(str).str.strip().replace({"nan": "", "None": ""}).replace("", np.nan)
            gm = vdf.groupby([vdf["_daykey"], ik])["y_fwd"].transform("mean")
            day_m = vdf.groupby("_daykey")["y_fwd"].transform("mean")
            vdf["y_ind"] = (vdf["y_fwd"] - gm.where(ik.notna(), day_m)).clip(-25, 25)
            top2 = (vdf.groupby("_daykey", group_keys=False)
                    .apply(lambda g: g.nlargest(max(1, int(len(g) * 0.2)), "pred"), include_groups=False))
            metrics.update({
                "ic_ind": _corr(vdf["pred"], vdf["y_ind"]),
                "rank_ic_ind": _rank_corr(vdf["pred"], vdf["y_ind"]),
                "top20_ind": round(float(top2["y_ind"].mean()), 4) if len(top2) else None,
                "all_ind": round(float(vdf["y_ind"].mean()), 4),
            })
        except Exception:
            pass
    notes = []
    if warning:
        notes.append(warning)
    missing = [k for k in screener.FEATURE_KEYS if k not in keys]
    if missing:
        ind_missing = [k for k in missing if k in screener.IND_KEYS]
        notes.append("本次训练缺少 %d 个因子（%s）%s；重建训练集后重训即可纳入。"
                     % (len(missing), "、".join(missing[:4]),
                        "，其中含行业类因子" if ind_missing else ""))
    if ds_neutral == "none":
        notes.append("训练集按原始因子训练（未做行业中性化）：实测把行业 β 一并剥掉会让 RankIC 下降，"
                     "所以保留行业暴露；要纯个股 α 可在构造训练集时选「行业中性」。")
    # 训练集口径必须取数据集自己记录的版本，不能记成「当前代码版本」：
    # 否则拿旧因子集训练完却标成新版本，freshness 的漂移检查会失灵。
    ds_fv = dsm.get("feature_version", screener.FEATURE_VERSION)
    meta = {"ok": True, "engine": engine, "version": LGB_VER if LGB_OK else "numpy",
            "trained": _now(), "rows_train": int(len(tr)), "rows_valid": int(len(va)),
            "train_days": len(tr_days), "valid_days": len(va_days),
            "split_date": days[cut], "train_end": days[tr_end - 1],
            "valid_start": days[cut], "purge_days": gap,
            "best_iteration": best_iter,
            "params": p, "metrics": metrics, "feature_importance": importance,
            "features": keys, "feature_version": ds_fv,
            "neutral": ds_neutral, "label_mode": dsm.get("label_mode", "market"),
            "dataset": {k: dsm.get(k) for k in
                        ("horizon", "step", "start", "end", "date_max", "rows",
                         "symbols", "neutral", "label_mode", "feature_version",
                         "generated")},
            "dataset_generated": dsm.get("generated", ""),
            "target": dsm.get("label", "未来 N 日超额收益（%）"),
            "warning": " ".join(notes),
            "model_file": str(MODEL_PATH) if LGB_OK else str(MODEL_META)}
    if engine == "ridge-fallback":
        meta.update(_ridge_meta())
    _write_meta(meta)
    return meta


def _write_meta_ridge(w, mu, sd):
    (SCREEN_DIR / "ridge.json").write_text(
        json.dumps({"w": w, "mu": mu, "sd": sd}, ensure_ascii=False), encoding="utf-8")


def _ridge_meta() -> dict:
    return {"ridge_file": str(SCREEN_DIR / "ridge.json")}


def _predict_matrix(x: pd.DataFrame, meta: dict) -> np.ndarray:
    if meta["engine"] == "lightgbm":
        booster = lgb.Booster(model_file=str(MODEL_PATH))
        return booster.predict(x)
    r = json.loads((SCREEN_DIR / "ridge.json").read_text(encoding="utf-8"))
    # 旧岭回归文件按 16 列存 mu/sd，列数跟着模型自身走，不硬编码成当前因子集
    mu = pd.Series(r["mu"]).set_axis(x.columns[:len(r["mu"])])
    sd = pd.Series(r["sd"]).set_axis(x.columns[:len(r["sd"])]).replace(0, 1.0)
    Z = ((x[list(mu.index)] - mu) / sd).to_numpy()
    return np.hstack([np.ones((len(Z), 1)), Z]) @ np.array(r["w"])


def predict_frame(df: pd.DataFrame) -> np.ndarray | None:
    """对任意横截面（含因子列与 industry 列）打分。模型不可用时返回 None，由调用方降级。"""
    meta = _meta()
    if not meta.get("ok"):
        return None
    engine = meta.get("engine")
    ready = ((engine == "lightgbm" and MODEL_PATH.exists()) or
             (engine == "ridge-fallback" and (SCREEN_DIR / "ridge.json").exists()))
    if not ready:
        return None
    return _predict_matrix(_prep_infer(df, meta), meta)


_SCORE_CACHE: dict = {"k": None, "v": None}


def _score_cache_key(smeta: dict, rows: list, neutral: str, use_model: bool):
    """打分缓存的指纹：快照日期 + 行数 + 首末代码 + 口径 + 模型 mtime + 特征版本。"""
    try:
        mtime = int(MODEL_PATH.stat().st_mtime) if MODEL_PATH.exists() else 0
    except Exception:
        mtime = 0
    first = str((rows[0] or {}).get("code") or "") if rows else ""
    last = str((rows[-1] or {}).get("code") or "") if rows else ""
    return (str(smeta.get("as_of") or smeta.get("date_max") or ""),
            len(rows), first, last, str(neutral or "auto"), bool(use_model),
            mtime, screener.FEATURE_VERSION)


def score_snapshot(rows: list[dict] | None = None, use_model: bool = True,
                   snapshot_meta: dict | None = None, neutral: str = "auto") -> dict:
    """给横截面快照打分。返回模型分（预测超额收益 %）或可解释基准分。

    neutral 三态：
      auto        有模型时跟随模型的训练口径，无模型时用行业中性
      industry / industry+size / none  强制指定（仅在基准分路径下完全生效）

    模型路径下如果用户要求的口径与训练口径不一致，会照训练口径打分并给出 warning：
    拿「用原始值训练的模型」去预测「中性化后的因子」等于喂错输入，分数会失真。
    """
    if rows is None:
        snap, smeta = screener.load_snapshot()
        if snap is None:
            return {"ok": False, "error": "还没有横截面快照，请先点「生成快照 / 一键选股」"}
        rows = screener._rows(snap)
    else:
        smeta = {k: v for k, v in (snapshot_meta or {}).items()
                 if k not in ("rows", "file")}
    if not rows:
        return {"ok": False, "error": "快照为空"}
    # 同一份快照 + 同一个模型，打分结果完全一样；单股分析、选股、页面刷新
    # 会反复调用它，全市场 5000 行的中性化 + 推理约 400ms，缓存后只剩取行的开销。
    # 键里带模型 mtime 与特征版本：重训或改因子口径后自动失效。
    ck = _score_cache_key(smeta, rows, neutral, use_model)
    if _SCORE_CACHE.get("k") == ck and _SCORE_CACHE.get("v"):
        return dict(_SCORE_CACHE["v"])
    df = pd.DataFrame(rows)
    # 行业类因子每次按当前行业映射重算一遍：快照是几天前建的、行业映射后来升级过时，
    # 存量的 ind_mom20 / ind_rel20 会和新行业归属不一致，模型就会吃到过期特征
    try:
        df = screener.add_industry_features(df)
    except Exception:
        pass
    want = (neutral or "auto").strip()
    meta = _meta()
    # 旧快照没有 total_mv 列；选「行业+市值」时补一份当期市值，
    # 否则市值中性化会被静默跳过（模型/基准分都以为做了，实际只做了行业）。
    if ((want == "industry+size") or
            (want == "auto" and meta.get("neutral") == "industry+size")):
        if "total_mv" not in df.columns:
            try:
                df, _mv_day = screener.attach_market_cap(df)
            except Exception:
                pass
    # 特征版本必须与快照口径一致：因子定义改过（窗口/ddof/单位）时，
    # 旧模型照样能"跑通"，但吃的是它没训练过的分布，分数静默失真。
    # 这种情况宁可降级基准分并说明，也不要用一个看起来正常的错分数。
    try:
        _fv = meta.get("feature_version")
        version_ok = (_fv is None) or (int(_fv) == int(screener.FEATURE_VERSION))
    except (TypeError, ValueError):
        version_ok = False
    model_ready = bool(use_model and meta.get("ok") and version_ok and (
        (meta.get("engine") == "lightgbm" and MODEL_PATH.exists()) or
        (meta.get("engine") == "ridge-fallback" and (SCREEN_DIR / "ridge.json").exists())))
    extra_warn = ""
    if use_model and meta.get("ok") and not version_ok:
        extra_warn = ("模型的特征版本（%s）与当前因子口径（%s）不一致，已降级为基准分；"
                      "请重建训练集并重训模型。"
                      % (meta.get("feature_version"), screener.FEATURE_VERSION))
    if model_ready:
        try:
            pred = _predict_matrix(_prep_infer(df, meta), meta)
            df["score"] = np.round(pred, 4)
            engine = meta["engine"]
            _h = meta.get("dataset", {}).get("horizon", "N")
            # 标签口径决定分数含义：rank 模型输出的是分位，不能当超额收益 % 念
            if str(meta.get("label_mode") or "market") == "rank":
                score_label = "模型预测未来 %s 日横截面分位（−0.5~0.5，越高越强）" % _h
            else:
                score_label = "模型预测未来 %s 日超额收益（%%）" % _h
            warning = meta.get("warning", "")
            model_meta = meta
            used = meta.get("neutral") or "none"
            if want not in ("auto", "", used):
                extra_warn = ("模型训练口径是「%s」，本次按该口径打分；"
                              "要切换中性化口径请重建训练集并重训模型。" % used)
        except Exception as e:
            return {"ok": False, "error": "模型推理失败：%s" % e}
    else:
        # 「自动」在基准分路径下也跟随已训练模型的口径：这样勾掉「用模型分」时，
        # 变的只是打分器，而不是连口径一起变，两边的候选才可比
        if want in screener.NEUTRAL_MODES:
            used = want
        elif meta.get("ok") and (meta.get("neutral") in screener.NEUTRAL_MODES):
            used = meta["neutral"]
        else:
            used = screener.NEUTRAL_DEFAULT
        df["score"] = screener.base_scores(df, used).round(4)
        engine = "baseline"
        score_label = "方向加权横截面 z-score（%s）" % (
            "行业中性" if used.startswith("industry") else "未中性化")
        warning = "当前是基准分，不是机器学习预测。训练 LightGBM 后可切换为模型分。"
        model_meta = {}

    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)
    if "base_score" in df:
        df["base_score"] = pd.to_numeric(df["base_score"], errors="coerce").fillna(0.0)
    else:
        df["base_score"] = screener.base_scores(df, "industry").round(4)
    if "rank" in df:
        df = df.drop(columns=["rank"])
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    df.insert(0, "rank", range(1, len(df) + 1))
    out = screener._rows(df)
    info = {"mode": used, "requested": want}
    try:
        import neutral as _n
        info["describe"] = _n.describe(used)
        info["size"] = _n.size_available()
    except Exception:
        pass
    if str(used) == "industry+size":
        applied = bool("total_mv" in df.columns and
                       (info.get("size") or {}).get("available"))
        info["size_applied"] = applied
        if not applied:
            extra_warn = ((extra_warn + " ") if extra_warn else "") + \
                "市值数据不可用，市值中性化已跳过（实际只做了行业中性）。"
    # 快照自身的提示（历史截止日、剔除落后样本）必须透出，否则用户看不到数据日被谁改过
    snap_warn = str((snapshot_meta or {}).get("warning") or "").strip()
    warns = [x for x in (snap_warn, warning, extra_warn) if x]
    result = {"ok": True, "engine": engine, "score_label": score_label,
              "warning": " ".join(warns).strip(),
              "neutral": info, "model": model_meta, "snapshot": smeta,
              "rows": out, "count": len(out),
              "as_of": smeta.get("as_of") or smeta.get("date_max", ""),
              "as_of_param": smeta.get("as_of_param", ""),
              "is_historical": bool(smeta.get("as_of_param")),
              "lag_excluded": smeta.get("lag_excluded", 0)}
    # 缓存里存原对象，返回浅拷贝：调用方只读 rows（加字段会污染下一次命中）
    _SCORE_CACHE.update(k=ck, v=result)
    return dict(result)


def status() -> dict:
    m = _meta()
    keys = _model_keys(m) if m else []
    return {"ok": True, "available": LGB_OK, "version": LGB_VER,
            "error": LGB_ERR, "trained": bool(m.get("ok")),
            "engine": m.get("engine", ""), "features": len(screener.FEATURE_KEYS),
            "model_features": keys, "model_neutral": m.get("neutral") or ("none" if m else ""),
            "label_mode": m.get("label_mode", ""),
            "feature_drift": bool(m) and [k for k in screener.FEATURE_KEYS if k not in keys],
            "drift_keys": [k for k in screener.FEATURE_KEYS if k not in keys] if m else [],
            "meta": m,
            "modes": ["lightgbm", "ridge-fallback", "baseline"]}
