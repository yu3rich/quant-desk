# -*- coding: utf-8 -*-
"""
QuantDesk · 目标持仓（target position）

为什么要有这一层：
  选股 ≠ 组合。「Top20 名单」回答不了三个执行问题：
    1) 每只买多少钱？（等权？打分加权？）
    2) 单票 / 单行业最多能占多少？（不做约束，权重会飘到冷门票上）
    3) 相对现在的持仓，具体买卖多少股、大概花多少成本？

做法对齐 vnpy portfolio_strategy 的核心语义：**输出目标持仓，而不是订单**。
差额单是「目标 − 当前」的派生结果，因此可以随时重算、可以对同一目标分多次执行。

约束实现：
  · 单票上限   水填充（超限部分按比例回补给未超限的票，迭代到收敛）
  · 单行业上限 先压超限行业，再把腾出的权重回补给未超限行业（同样尊重单票上限）
  · 换手上限   若目标与当前的偏离超过上限，就按比例向当前权重收缩（不清仓式调仓）
  · 最小交易额 差额小于阈值的直接不动，避免为了 1 手付 5 元最低佣金

成本口径与回测一致（quant config）：佣金双边、印花税卖出单边、滑点双边。
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import qdpaths

BASE = Path(__file__).resolve().parent
DATA = qdpaths.data_path()
CFG_PATH = DATA / "portfolio.json"

DEFAULTS = {"mode": "equal", "max_w": 0.12, "ind_cap": 0.30,
            "turnover_cap": 0.60, "min_trade": 2000, "lot": 100,
            "cash_floor": 0.02,
            # 风险预算类参数：risk_fixed 的单笔风险、ATR 倍数；kelly 的折算比例
            "risk_pct": 0.01, "atr_mult": 2.0, "kelly_scale": 0.25,
            "kelly_win": 0, "kelly_pl": 0,
            # Kelly 的胜率/盈亏比取最近多少笔已平仓交易；账本只留最近 60 笔明细
            "kelly_window": 60,
            # Topk-Drop：0 表示不启用；topk=0 时按候选数
            "n_drop": 0, "topk": 0}


MODE_LABEL = {"equal": "等权", "score": "打分加权", "vol_target": "波动率倒数",
              "risk_fixed": "固定风险预算", "rp": "风险平价",
              "hrp": "层次风险平价", "kelly": "分数 Kelly"}


def cfg() -> dict:
    c = dict(DEFAULTS)
    if CFG_PATH.exists():
        try:
            c.update(json.loads(CFG_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    return c


def save(patch: dict) -> dict:
    c = cfg()
    for k, v in (patch or {}).items():
        if k in DEFAULTS:
            c[k] = v
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "config": c}


def cost_model() -> dict:
    """与回测同一口径的成本参数，避免「回测赚钱、实盘亏在费用」这种口径漂移。"""
    try:
        import quant
        c = quant.load_config()
    except Exception:
        c = {}
    return {"commission": float(c.get("commission", 0.00025)),
            "min_commission": float(c.get("min_commission", 5)),
            "stamp_tax": float(c.get("stamp_tax", 0.0005)),
            "slippage": float(c.get("slippage", 0.0005))}


def current_positions() -> tuple[list[dict], dict]:
    """最近一份账本的持仓与账户概览（只读，不登录）。

    盘中要看实时持仓请走实盘页同步；这里用最近一次落盘的快照，
    目的是「算差额单」而不是「看盈亏」，一个交易日内的偏差可以接受。
    """
    try:
        import store
        rows = store.conn().execute(
            "SELECT day,payload FROM ledger_days ORDER BY day DESC LIMIT 5").fetchall()
    except Exception:
        rows = []
    for day, payload in rows:
        try:
            d = json.loads(payload or "{}")
        except Exception:
            continue
        pos = d.get("positions") or []
        if pos:
            ov = d.get("overview") or {}
            return ([{"code": str(p.get("code") or "").zfill(6),
                      "name": p.get("name") or "", "qty": int(p.get("qty") or 0),
                      "avail": int(p.get("avail") or p.get("qty") or 0),
                      "cost": float(p.get("cost") or 0),
                      "price": float(p.get("price") or 0),
                      "mv": float(p.get("mv") or 0)} for p in pos if p.get("code")],
                    {"day": str(day), "total": float(ov.get("total") or 0),
                     "mv": float(ov.get("mv") or 0), "cash": float(ov.get("cash") or 0)})
    return [], {}


def _warn(msg: str) -> None:
    try:
        import store
        store.log("portfolio", msg, level="info", act="target")
    except Exception:
        pass


def _returns_matrix(picks: list[dict], n: int = 250):
    """候选股近 n 日收益矩阵（列=代码）。数据不足的票直接缺席，由调用方回退。"""
    import pandas as pd
    try:
        import store
    except Exception:
        return None
    cols = {}
    for p in picks:
        code = str(p.get("code") or "").zfill(6)
        try:
            df = store.bars_frame(code, n)
        except Exception:
            df = None
        if df is None or len(df) < 80:
            continue
        cols[code] = df.set_index("date")["close"].pct_change()
    if len(cols) < 2:
        return None
    R = pd.DataFrame(cols).dropna()
    return R if len(R) >= 60 else None


def _risk_weights(picks: list[dict], risk: dict) -> list[float]:
    """波动率倒数：总仓位不变，高波动票权重更低。"""
    vals = []
    for p in picks:
        m = risk.get(str(p.get("code") or "").zfill(6)) or {}
        try:
            v = float(m.get("vol20_pct"))
        except (TypeError, ValueError):
            v = 0
        vals.append(1.0 / v if v > 0 else 1.0 / 2.0)   # 缺失时按 2% 波动处理
    s = sum(vals) or 1.0
    return [x / s for x in vals]


def _risk_fixed_weights(picks: list[dict], risk: dict, capital: float,
                        risk_pct: float, atr_mult: float) -> list[float]:
    """固定风险预算：每只票的「权重 × ATR% × 倍数」都等于同一笔预算。

    先由预算反推目标仓位金额，再按总仓位上限等比压缩；
    这样单票风险金额永远不会超过预算（vol_target 只保证权重反比波动）。
    """
    n = len(picks)
    budget = max(0.0, capital * risk_pct)
    amts = []
    for p in picks:
        m = risk.get(str(p.get("code") or "").zfill(6)) or {}
        try:
            a = float(m.get("atr_pct"))
        except (TypeError, ValueError):
            a = 0
        if a <= 0:
            # 缺 ATR 时按 2% 日波动估，不能直接给 0 权重 —— 那等于把它静默踢出组合
            a = 2.0
        amts.append(budget / (a / 100.0 * atr_mult) if budget > 0 else None)
    known = [x for x in amts if x]
    if not known:
        return [1.0 / n] * n
    cap = capital * 0.98
    scale = min(1.0, cap / sum(known)) if sum(known) > 0 else 1.0
    # 不做 1/s 归一化：预算合计小于 98% 资本时，多出来的应该是现金。
    # 以前这里按 1/s 拉满，单票风险会变成预算的 1/s 倍（实测 2 只票时正好 2 倍），
    # 函数自己的「风险金额不超过预算」承诺就不成立了。
    return [(x * scale / capital) if x else 0.0 for x in amts]


def _rp_weights(picks: list[dict]):
    """风险平价：迭代到各票风险贡献相等。数据不足返回 None。"""
    import numpy as np
    R = _returns_matrix(picks)
    if R is None:
        return None
    codes = [str(p.get("code") or "").zfill(6) for p in picks]
    cols = [c for c in codes if c in R.columns]
    if len(cols) < 2:
        return None
    cov = R[cols].cov().values * 252
    n = len(cols)
    w = np.ones(n) / n
    for _ in range(300):
        var = float(w @ cov @ w)
        if var <= 0:
            break
        rc = w * (cov @ w)
        target = var / n
        w = w * (target / np.maximum(rc, 1e-12)) ** 0.5
        w = np.maximum(w, 1e-6)
        w = w / w.sum()
    out = [0.0] * len(codes)
    for i, c in enumerate(cols):
        out[codes.index(c)] = float(w[i])
    s = sum(out) or 1.0
    return [x / s for x in out]


def _hrp_weights(picks: list[dict]):
    """层次风险平价（HRP）：单链接聚类 + 递归二分。数据不足返回 None。"""
    import numpy as np
    from scipy.cluster.hierarchy import leaves_list, linkage
    from scipy.spatial.distance import squareform
    R = _returns_matrix(picks)
    if R is None:
        return None
    codes = [str(p.get("code") or "").zfill(6) for p in picks]
    cols = [c for c in codes if c in R.columns]
    if len(cols) < 3:
        return None
    corr = R[cols].corr().values
    cov = R[cols].cov().values * 252
    dist = np.sqrt(np.clip((1 - corr) / 2.0, 0, 1))
    np.fill_diagonal(dist, 0.0)
    try:
        order = list(leaves_list(linkage(squareform(dist, checks=False), method="single")))
    except Exception:
        return None
    n = len(cols)
    w = np.ones(n)
    clusters = [order]
    while clusters:
        nxt = []
        for idx in clusters:
            if len(idx) <= 1:
                continue
            mid = len(idx) // 2
            a, b = idx[:mid], idx[mid:]

            def _var(ii):
                sub = cov[np.ix_(ii, ii)]
                ivp = 1.0 / np.maximum(np.diag(sub), 1e-12)
                ivp = ivp / ivp.sum()
                return float(ivp @ sub @ ivp)

            va, vb = _var(a), _var(b)
            alpha = 1 - va / (va + vb) if (va + vb) > 0 else 0.5
            w[a] *= alpha
            w[b] *= 1 - alpha
            nxt += [a, b]
        clusters = nxt
    w = w / w.sum()
    out = [0.0] * len(codes)
    for pos, idx in enumerate(order):
        out[codes.index(cols[idx])] = float(w[pos])
    s = sum(out) or 1.0
    return [x / s for x in out]


def kelly_fraction(win_rate, pl_ratio, scale=0.25):
    """f* = (p·b − q)/b，再乘折算比例。

    返回未截断的值：负值表示「按当前胜率/盈亏比没有正优势」，
    由调用方决定是回退还是保持空仓，不能在这里悄悄抹成 0。
    """
    try:
        p = float(win_rate) / 100.0
        b = float(pl_ratio)
    except (TypeError, ValueError):
        return None
    if not (0 < p < 1) or b <= 0:
        return None
    return (p * b - (1 - p)) / b * float(scale or 0.25)


def _kelly_params():
    """胜率/盈亏比来源优先级：配置显式值 > 账本最近 N 笔滚动 > 账本全历史。

    滚动窗口的意义：交易结构会变（换了选股规则、改了持有期），
    用两年前那批交易的胜率给今天的仓位定上限，等于拿旧样本赌新策略。
    """
    c = cfg()
    try:
        wr, pl = float(c.get("kelly_win") or 0), float(c.get("kelly_pl") or 0)
    except (TypeError, ValueError):
        wr = pl = 0
    if wr > 0 and pl > 0:
        return wr, pl, "config"
    try:
        import emtbook
        tr = (emtbook.board(days=180) or {}).get("trades") or {}
        lst = [x for x in (tr.get("closed_list") or []) if x.get("pnl") is not None]
        try:
            n = max(5, int(c.get("kelly_window") or 60))
        except (TypeError, ValueError):
            n = 60
        recent = lst[:n]          # closed_list 已按 sell_date 倒序，取前 N 就是最近 N 笔
        if len(recent) >= 5:
            wins = [x for x in recent if (x.get("pnl") or 0) > 0]
            losses = [x for x in recent if (x.get("pnl") or 0) <= 0]
            if wins and losses:
                avg_w = sum(float(x["pnl"]) for x in wins) / len(wins)
                avg_l = abs(sum(float(x["pnl"]) for x in losses)) / len(losses)
                if avg_l > 0:
                    return (len(wins) / len(recent) * 100, avg_w / avg_l,
                            "emtbook:%d" % len(recent))
        if int(tr.get("closed") or 0) >= 5 and tr.get("win_rate") and tr.get("pl_ratio"):
            return float(tr["win_rate"]), float(tr["pl_ratio"]), "emtbook"
    except Exception:
        pass
    return None, None, ""


def _topk_drop(picks: list[dict], cur_list: list[dict], topk: int, n_drop: int):
    """Topk-Drop：满仓时只换 n_drop 只，没满仓时先把空位补满。

    这里最容易写错的一点：n_drop 是「每日换手上限」，不是「每日买入上限」。
    空仓第一次建仓时如果也只买 n_drop 只，topk=10、n_drop=2 会变成只持 2 只，
    再被单票上限压到 12%，总仓位只剩 24% —— 分散度和仓位都不是用户要的。
    """
    cur_codes = {str(x.get("code") or "").zfill(6) for x in (cur_list or [])
                 if int(x.get("qty") or 0) > 0}
    ranked = sorted(picks, key=lambda p: -(float(p.get("score") or 0)))
    held = [p for p in ranked if str(p.get("code") or "").zfill(6) in cur_codes]
    fresh = [p for p in ranked if str(p.get("code") or "").zfill(6) not in cur_codes]
    if len(held) >= topk:
        # 已满仓：留下 topk-n_drop 只老仓，换成候选里最好的 n_drop 只
        keep = held[:max(0, topk - min(n_drop, len(fresh)))]
        out = keep + fresh[:n_drop]
    else:
        # 未满仓：空位先补满，不占换手额度
        out = held + fresh[:max(0, topk - len(held))]
    return out or ranked[:topk]


def _weights(picks: list[dict], mode: str, risk: dict | None = None,
             capital: float = 0, cfg: dict | None = None) -> list[float]:
    """初始权重。等权最简单；打分加权按分数的横截面 z 值做 softmax，
    避免最高分那只把权重全吃掉（线性归一会让 Top1 拿到 30%+）。"""
    n = len(picks)
    if n == 0:
        return []
    if mode == "vol_target":
        return _risk_weights(picks, risk or {})
    if mode == "risk_fixed":
        c = cfg or {}
        return _risk_fixed_weights(picks, risk or {}, capital,
                                   float(c.get("risk_pct") or 0.01),
                                   float(c.get("atr_mult") or 2))
    if mode == "rp":
        return _rp_weights(picks) or [1.0 / n] * n
    if mode == "hrp":
        return _hrp_weights(picks) or [1.0 / n] * n
    if mode != "score":
        return [1.0 / n] * n
    sc = []
    for p in picks:
        try:
            sc.append(float(p.get("score")))
        except Exception:
            sc.append(0.0)
    if len(set(sc)) <= 1:
        return [1.0 / n] * n
    mu = sum(sc) / n
    sd = (sum((x - mu) ** 2 for x in sc) / max(1, n - 1)) ** 0.5 or 1.0
    z = [(x - mu) / sd for x in sc]
    # 温度 0.6：z=+1 与 z=-1 的权重比约 3.3 倍，不至于变成单票押注
    import math as _m
    ex = [_m.exp(max(-3.0, min(3.0, v * 0.6))) for v in z]
    s = sum(ex) or 1.0
    return [x / s for x in ex]


def _cap_name(w: list[float], max_w: float, iters: int = 60) -> list[float]:
    """单票上限：超限的削到上限，溢出权重按比例回补给未超限的票。"""
    if max_w <= 0 or max_w >= 1:
        return w
    w = list(w)
    for _ in range(iters):
        over = [i for i, x in enumerate(w) if x > max_w + 1e-12]
        if not over:
            break
        excess = sum(w[i] - max_w for i in over)
        for i in over:
            w[i] = max_w
        free = [i for i, x in enumerate(w) if x < max_w - 1e-12]
        base = sum(w[i] for i in free)
        if not free or base <= 0:
            break
        # 均匀回补更稳（按比例回补会让大的更大，容易在少数票上反复触顶）
        for i in free:
            w[i] += excess / len(free)
        if all(x <= max_w + 1e-9 for x in w):
            break
    return w


def _cap_industry(w: list[float], inds: list[str], cap: float,
                  max_w: float, iters: int = 60) -> list[float]:
    """单行业上限：压超限行业，腾出的权重回补给未超限行业（仍受单票上限约束）。"""
    if cap <= 0 or cap >= 1:
        return w
    w = list(w)
    for _ in range(iters):
        tot: dict[str, float] = {}
        for x, ind in zip(w, inds):
            tot[ind] = tot.get(ind, 0.0) + x
        over = {k: v for k, v in tot.items() if v > cap + 1e-9}
        if not over:
            break
        spare = 0.0
        for k, v in over.items():
            scale = cap / v
            for i, ind in enumerate(inds):
                if ind == k:
                    spare += w[i] * (1 - scale)
                    w[i] *= scale
        # 回补：只给还能加仓的票，且不让任何行业再次超限
        cand = [i for i, ind in enumerate(inds)
                if tot.get(ind, 0) < cap - 1e-9 and w[i] < max_w - 1e-9]
        if not cand or spare <= 1e-12:
            break
        # 平均分给可加仓的票，并逐票截断到剩余额度
        for _ in range(20):
            if spare <= 1e-12:
                break
            room = [max(0.0, min(max_w - w[i], cap - tot.get(inds[i], 0))) for i in cand]
            s = sum(room)
            if s <= 1e-12:
                break
            give = min(spare, s)
            for i, r in zip(cand, room):
                add = give * (r / s)
                w[i] += add
                tot[inds[i]] = tot.get(inds[i], 0.0) + add
            spare -= give
    # 同样只往下压、不上抬：10 只票被 5% 上限压到合计 50% 是正常结果，
    # 按 1/s 归一化会把每只抬回 10%，上限当场失效（这个坑我踩过一次）。
    s = sum(w)
    return [x * (1.0 / s) for x in w] if s > 1.0 else w


def _industry_of(picks: list[dict]) -> list[str]:
    out = []
    for p in picks:
        ind = str(p.get("industry") or "").strip()
        out.append(ind or "未分类")
    return out


def target(picks: list[dict], capital: float = 0, current: list[dict] | None = None,
           patch: dict | None = None, price_of: dict | None = None) -> dict:
    """给定候选，算出目标持仓与差额单。

    picks   选股候选（需含 code / industry / score / close_real 或 close）
    capital 组合总资产；0 表示用账本里的总资产
    current 当前持仓；None 表示用最近一份账本
    """
    c = {**cfg(), **(patch or {})}
    picks = [p for p in (picks or []) if str(p.get("code") or "").strip()]
    if not picks:
        return {"ok": False, "error": "没有候选：先在选股工作台跑一次，或调整 TopN"}

    if current is None:
        cur_list, ov = current_positions()
    else:
        cur_list, ov = current, {}
    cur_map = {x["code"]: x for x in cur_list}

    if not capital or capital <= 0:
        capital = float(ov.get("total") or 0) or (
            sum(x.get("mv", 0) for x in cur_list) +
            float(ov.get("cash") or 0))
    if capital <= 0:
        return {"ok": False,
                "error": "不知道组合规模：请填「总资产」，或先在实盘页同步一次账户"}

    mode = str(c.get("mode") or "equal")
    max_w = float(c.get("max_w") or 0.05)
    ind_cap = float(c.get("ind_cap") or 0.30)
    tcap = float(c.get("turnover_cap") or 0.60)
    lot = int(c.get("lot") or 100)
    min_trade = float(c.get("min_trade") or 0)
    cash_floor = float(c.get("cash_floor") or 0)
    n_drop = max(0, int(c.get("n_drop") or 0))
    topk = max(0, int(c.get("topk") or 0)) or len(picks)

    if n_drop > 0:
        picks = _topk_drop(picks, cur_list, topk, n_drop)
    risk = {}
    if mode in ("vol_target", "risk_fixed", "rp", "hrp"):
        try:
            import riskview
            risk = riskview.batch([p["code"] for p in picks])
        except Exception:
            risk = {}
    inds = _industry_of(picks)
    w0 = _weights(picks, mode, risk, capital, c)
    w1 = _cap_name(w0, max_w)
    w2 = _cap_industry(w1, inds, ind_cap, max_w)

    # 换手上限：与当前权重比，超了就向当前收缩（老仓没被选中也不会被一次性清掉）
    cur_w = {}
    for x in cur_list:
        cur_w[x["code"]] = float(x.get("mv") or 0) / capital
    codes = [str(p["code"]) for p in picks]
    tgt = {code: w for code, w in zip(codes, w2)}
    turn_before = None
    if cur_w:
        diff = sum(abs(tgt.get(k, 0.0) - cur_w.get(k, 0.0)) for k in set(tgt) | set(cur_w))
        turn_before = diff / 2.0
        if tcap > 0 and turn_before > tcap:
            alpha = tcap / turn_before
            keys = set(tgt) | set(cur_w)
            tgt = {k: cur_w.get(k, 0.0) + alpha * (tgt.get(k, 0.0) - cur_w.get(k, 0.0))
                   for k in keys}
            # 收缩后可能又破单票上限，再修一次
            order = list(tgt.keys())
            fixed = _cap_name([tgt[k] for k in order], max_w)
            tgt = dict(zip(order, fixed))

    # 归一化只能往下压，绝不上抬。
    # 反例（我第一版就写错了）：10 只票 × 5% 上限 = 50%，按 1/s 归一化会把每只抬到 9.8%，
    # 单票上限当场作废，而且看起来「满仓」其实是偷偷突破了约束。
    # 正确的语义是：上限压住之后剩下的是现金，不是硬要塞进股票里。
    kelly = {}
    if mode == "kelly":
        wr, pl, src = _kelly_params()
        f = kelly_fraction(wr, pl, c.get("kelly_scale"))
        if f is None:
            kelly = {"available": False, "reason": "缺少胜率/盈亏比"}
            _warn("Kelly 缺少胜率/盈亏比，回退等权")
        elif f <= 0:
            kelly = {"available": False, "win_rate": wr, "pl_ratio": pl,
                     "f": round(f, 4), "source": src,
                     "reason": "按当前胜率/盈亏比，Kelly 没有正优势（f*≤0）"}
            _warn("Kelly 判定没有正优势，回退等权")
        else:
            kelly = {"available": True, "win_rate": wr, "pl_ratio": pl,
                     "f": round(min(1.0, f), 4), "source": src}
    invest = max(0.0, 1.0 - cash_floor)
    if kelly.get("available"):
        invest = min(invest, kelly["f"])
    s = sum(tgt.values()) or 1.0
    if s > invest:
        tgt = {k: v * (invest / s) for k, v in tgt.items()}
    if mode == "risk_fixed":
        # 单票上限的溢出回补会把低波动票的权重抬高，从而突破风险预算；
        # 这里按「每只风险金额 ≤ 预算」再压一次，超出的部分留现金，不回补。
        budget = capital * float(c.get("risk_pct") or 0.01)
        mult = float(c.get("atr_mult") or 2)
        for k in list(tgt.keys()):
            a = (risk.get(k) or {}).get("atr_pct")
            if not a or not capital:
                continue
            cap_w = budget / (capital * float(a) / 100.0 * mult)
            tgt[k] = min(tgt[k], cap_w)
    gross_w = sum(tgt.values())

    pmap = {}
    for p in picks:
        code = str(p["code"])
        px = None
        for key in ("close_real", "close", "last_real", "rec_close"):
            try:
                v = float(p.get(key))
                if v > 0:
                    px = v
                    break
            except Exception:
                continue
        if px is None and price_of:
            px = price_of.get(code)
        pmap[code] = {"price": px or 0.0, "name": p.get("name") or code,
                      "industry": str(p.get("industry") or "") or "未分类",
                      "score": p.get("score")}

    # 换手上限收缩会把「没入选的老仓」也并进 tgt，它们不在 picks 里。
    # 不在这里补价格的话，下面会走 pmap.get() or {"price": 0} 分支：
    # px=0 → target_qty=0 → 差额金额 0 → 被最小交易额判成「不动」，
    # 结果权重写着 7% 而目标股数还是原来的满仓股数，整张差额单失效。
    for x in cur_list:
        code = str(x.get("code") or "")
        if not code or code in pmap:
            continue
        px = None
        for key in ("price", "last_real", "close"):
            try:
                v = float(x.get(key))
                if v > 0:
                    px = v
                    break
            except (TypeError, ValueError):
                continue
        if px is None and price_of:
            px = price_of.get(code)
        pmap[code] = {"price": px or 0.0,
                      "name": x.get("name") or code,
                      "industry": str(x.get("industry") or "") or "未分类",
                      "score": None}

    rows, buys, sells, holds = [], [], [], []
    for code, w in sorted(tgt.items(), key=lambda kv: -kv[1]):
        info = pmap.get(code) or {"price": 0.0, "name": code, "industry": "未分类"}
        px = float(info["price"] or 0)
        amt = w * capital
        qty = int(math.floor(amt / px / lot) * lot) if px > 0 else 0
        cur = cur_map.get(code) or {}
        cq = int(cur.get("qty") or 0)
        delta = qty - cq
        # 买入必须是整手：持仓可能是送转后的零股（如 1050 股），
        # 直接买 96950 股会被柜台废单。卖出允许卖零股，不用取整。
        if delta > 0 and lot > 1:
            delta = (delta // lot) * lot
            qty = cq + delta
        row = {"code": code, "name": info["name"], "industry": info["industry"],
               "weight": round(w * 100, 2), "target_qty": qty, "cur_qty": cq,
               "delta": delta, "price": round(px, 2),
               "target_amt": round(qty * px, 0), "score": info.get("score"),
               "action": "持有" if delta == 0 else ("买入" if delta > 0 else "卖出"),
               "note": ""}
        m = risk.get(code) or {}
        a = m.get("atr_pct")
        row["atr_pct"] = a
        # 风险金额 = 权重 × 总资产 × ATR% × 倍数；它是「波动参考」，不是止损指令
        row["risk_amt"] = (round(w * capital * float(a) / 100.0
                                 * float(c.get("atr_mult") or 2), 0) if a else None)
        if delta > 0:
            row["amount"] = round(delta * px, 0)
            if min_trade and row["amount"] < min_trade:
                row["action"] = "不动"
                row["note"] = "差额 %.0f 元 < 最小交易额 %.0f，为省最低佣金跳过" % (
                    row["amount"], min_trade)
                row["delta"] = 0
                row["target_qty"] = cq
                holds.append(row)
            else:
                buys.append(row)
        elif delta < 0:
            row["amount"] = round(abs(delta) * px, 0)
            avail = int(cur.get("avail") or cq or 0)
            if abs(delta) > avail:
                row["note"] = "T+1 限制：可卖 %d 股，缺口 %d 股需次日" % (avail, abs(delta) - avail)
            if min_trade and row["amount"] < min_trade:
                row["action"] = "不动"
                row["note"] = "差额 %.0f 元 < 最小交易额，为省最低佣金跳过" % row["amount"]
                row["delta"] = 0
                row["target_qty"] = cq
                holds.append(row)
            else:
                sells.append(row)
        else:
            # 权重给了钱，但一手都买不起（高价股 + 小资金 + 单票上限三件事撞一起）。
            # 这时写「持有」是在骗人：目标股数就是 0，永远建不了仓。
            if cq == 0 and w > 0 and lot > 0:
                row["action"] = "无法建仓"
                row["note"] = ("目标金额 %.0f 元买不到 1 手（%d 股 × %.2f = %.0f 元）；"
                               "加大总资产、提高单票上限或降低最小手数" % (amt, lot, px, lot * px))
            holds.append(row)
        rows.append(row)

    # 现有持仓里没被选中的：目标权重 0，需要清掉（但受换手上限保护后的权重已包含它们）
    for code, cur in cur_map.items():
        if code in tgt:
            continue
        px = float(cur.get("price") or 0)
        cq = int(cur.get("qty") or 0)
        if cq <= 0:
            continue
        w = tgt.get(code, 0.0)
        qty = int(math.floor(w * capital / px / lot) * lot) if px > 0 else 0
        row = {"code": code, "name": cur.get("name") or code,
               "industry": "（未入选）", "weight": round(w * 100, 2),
               "target_qty": qty, "cur_qty": cq, "delta": qty - cq,
               "price": round(px, 2), "target_amt": round(qty * px, 0),
               "score": None, "action": "卖出" if qty < cq else "持有",
               "amount": round(abs(qty - cq) * px, 0) if qty != cq else 0,
               "note": "本期未入选；受换手上限保护，未一次性清仓" if qty > 0 else ""}
        (sells if qty < cq else holds).append(row)
        rows.append(row)

    # 排序按「要动手的先看」：买入 → 卖出 → 不动 → 持有 → 无法建仓。
    # 单按权重降序会把「权重 4.9% 但一手都买不起」的票顶到最上面，读起来像坏了。
    _ORDER = {"买入": 0, "卖出": 1, "不动": 2, "持有": 3, "无法建仓": 4}
    rows.sort(key=lambda r: (_ORDER.get(r["action"], 5), -abs(r.get("amount") or 0)))

    cm = cost_model()
    gross_buy = sum(x.get("amount", 0) for x in buys)
    gross_sell = sum(x.get("amount", 0) for x in sells)

    def _fee(amt: float, sell: bool) -> float:
        if amt <= 0:
            return 0.0
        comm = max(amt * cm["commission"], cm["min_commission"])
        tax = amt * cm["stamp_tax"] if sell else 0.0
        slip = amt * cm["slippage"]
        return comm + tax + slip

    fee_buy = sum(_fee(x.get("amount", 0), False) for x in buys)
    fee_sell = sum(_fee(x.get("amount", 0), True) for x in sells)
    total_fee = fee_buy + fee_sell
    invested = sum(r["target_amt"] for r in rows)
    cash_left = capital - invested - fee_buy + fee_sell
    turn = (gross_buy + gross_sell) / 2.0 / capital if capital else 0

    ind_w: dict[str, float] = {}
    for r in rows:
        ind_w[r["industry"]] = ind_w.get(r["industry"], 0.0) + r["target_amt"]
    ind_rows = [{"industry": k, "amount": round(v, 0),
                 "weight": round(v / capital * 100, 2) if capital else 0}
                for k, v in sorted(ind_w.items(), key=lambda kv: -kv[1])]

    # 计划存档：之后拿它和实际成交比，才能回答「偏离计划的是哪几笔」。
    # 只存代码和金额，不存完整 rows —— 体检只需要知道计划买卖谁、多大。
    plan = {
        "date": str(ov.get("day") or "")[:10] or dt.date.today().isoformat(),
        "mode": mode, "topk": topk, "n_drop": n_drop,
        "buys": [{"code": x["code"], "name": x.get("name"), "amount": x.get("amount")}
                 for x in buys],
        "sells": [{"code": x["code"], "name": x.get("name"), "amount": x.get("amount")}
                  for x in sells],
        "buy_amount": round(gross_buy, 0), "sell_amount": round(gross_sell, 0),
        "turnover": round(turn * 100, 1), "capital": round(capital, 0),
    }
    try:
        import store
        store.plan_put(plan)
    except Exception:
        pass

    return {
        "ok": True,
        "capital": round(capital, 0),
        "mode": mode, "mode_label": MODE_LABEL.get(mode, mode),
        "max_w": max_w, "ind_cap": ind_cap, "turnover_cap": tcap,
        "n_drop": n_drop, "topk": topk,
        "risk_pct": float(c.get("risk_pct") or 0),
        "atr_mult": float(c.get("atr_mult") or 0),
        "kelly": kelly,
        "max_w_hit": sum(1 for x in rows if abs(x["weight"] / 100 - max_w) < 0.005),
        "ind_cap_hit": sum(1 for x in ind_rows if ind_cap and x["weight"] / 100 > ind_cap + 0.005),
        "rows": rows,
        "buys": buys, "sells": sells, "holds": holds,
        "n_target": len([r for r in rows if r["target_qty"] > 0]),
        "unbuyable": sum(1 for r in rows if r["action"] == "无法建仓"),
        "buy_amount": round(gross_buy, 0), "sell_amount": round(gross_sell, 0),
        "fee": round(total_fee, 0), "fee_bps": round(total_fee / capital * 10000, 1) if capital else 0,
        "fee_buy": round(fee_buy, 0), "fee_sell": round(fee_sell, 0),
        "invested": round(invested, 0), "cash_left": round(cash_left, 0),
        "cash_pct": round(cash_left / capital * 100, 2) if capital else 0,
        "turnover": round(turn * 100, 1),
        "gross_w": round(gross_w * 100, 2),
        "binding_note": ("" if gross_w >= invest * 0.95 else
                         ("单票上限 %.0f%% 把目标仓位压到 %.0f%%（%d 只 × %.0f%%），其余 %.0f%% 是现金；"
                          "想更满仓就把单票上限提到 %.1f%% 左右，或增加候选数"
                          % (max_w * 100, gross_w * 100, len(tgt), max_w * 100,
                             (invest - gross_w) * 100,
                             (invest / len(tgt) * 100) if tgt else 100))),
        "turnover_before_cap": None if turn_before is None else round(turn_before * 100, 1),
        "turnover_trimmed": bool(turn_before is not None and tcap and turn_before > tcap),
        "industries": ind_rows,
        "plan": plan,
        "current": {"day": ov.get("day", ""), "positions": len(cur_list)},
        "cost_model": cm,
        "note": ("目标持仓按「%s + 单票 %.0f%% + 单行业 %.0f%%」生成；差额单＝目标−当前；"
                 "费用按佣金 %.2f%%（最低 %.0f 元）+ 印花税 %.2f%%（卖出）+ 滑点 %.2f%% 估算。"
                 % (MODE_LABEL.get(mode, mode), max_w * 100, ind_cap * 100,
                    cm["commission"] * 100, cm["min_commission"],
                    cm["stamp_tax"] * 100, cm["slippage"] * 100)),
    }
