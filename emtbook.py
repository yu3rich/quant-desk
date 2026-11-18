# -*- coding: utf-8 -*-
"""
实盘复盘账本分析

输入只有两样：emtlink 攒下来的日账本（data/ledger/*.json）与选股记录（data/screen/picks.csv）。
不联网、不读行情接口，所有价格都用账本里东财返回的快照价，避免「复盘口径」与「账户口径」打两套架。

产出六块：
    1. overview + 对比   总资产、当日/累计盈亏、仓位，和上一个账本日比
    2. curve             资金曲线、回撤、日盈亏累加
    3. positions         持仓明细 + 行业 + 贡献
    4. attribution       当日盈亏拆解（持仓贡献 / 已清仓 / 费用 / 未解释）
    5. trades            从累积委托里 FIFO 配对，算胜率、盈亏比、持仓天数、换手、费用
    6. diagnose          行为体检：集中度、追高、割肉、频繁交易、空仓缓冲
    7. vs_picks          与选股系统的候选对照

诚实边界：费用是**按费率估算**（东财委托接口不回佣金），已实现盈亏按 FIFO 成交价配对，
未计分红送转、红利税与融资利息；账本里没有的日期不会插值，曲线直接断开。
"""
from __future__ import annotations

import math
from collections import defaultdict

import emtlink

# 默认费率（可在 /api/emt/board 的 fee 参数覆盖）
FEE = {
    "commission": 0.00025,     # 佣金 万 2.5
    "min_commission": 5.0,     # 单笔最低 5 元
    "stamp_sell": 0.0005,      # 印花税（卖出）
    "transfer": 0.00001,       # 过户费（双向）
}


def _fee(side: str, amount: float, rate: dict | None = None) -> float:
    r = rate or FEE
    if amount <= 0:
        return 0.0
    comm = max(amount * r["commission"], r["min_commission"])
    tax = amount * r["stamp_sell"] if side == "卖出" else 0.0
    return round(comm + tax + amount * r["transfer"], 2)


def _r(v, n=2):
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return 0.0
        return round(x, n)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# 曲线
# --------------------------------------------------------------------------

def _curve(days: list[dict], flows: list[dict] | None = None) -> dict:
    """
    资金曲线。两个收益口径并列：

    - `ret`     总资产涨跌。含出入金，转入 10 万会显示暴涨 —— 这是账户视角，不是业绩。
    - `ret_twr` 剔除出入金后的时间加权收益。逐日 `(今日总资产 − 今日净入金) / 昨日总资产 − 1`
                连乘。有流水才能算，没流水时返回 None 而不是假装 0。
    """
    # 每日净入金（只取「入金 / 出金」两类，分红利息属于收益，不算出入金）
    flow_day: dict[str, float] = defaultdict(float)
    for f in (flows or []):
        cls = _flow_class(f.get("kind"), _r(f.get("amount"), 2))
        if cls in ("in", "out"):
            mag = abs(_r(f.get("amount"), 2))
            flow_day[f.get("date") or ""] += mag if cls == "in" else -mag

    pts = []
    for d in sorted(days, key=lambda x: x.get("date") or ""):
        ov = d.get("overview") or {}
        total = _r(ov.get("total"))
        if total <= 0:
            continue
        pts.append({
            "date": d.get("date") or "",
            "total": total,
            "mv": _r(ov.get("mv")),
            "cash": _r(ov.get("cash")),
            "day_pnl": _r(ov.get("day_pnl")),
            "exposure": _r(ov.get("exposure")),
        })
    if not pts:
        return {"points": [], "start": "", "end": "", "base": 0, "last": 0, "ret": 0,
                "ret_twr": None, "net_in": 0.0, "has_flow": bool(flow_day),
                "mdd": 0, "mdd_from": "", "mdd_to": "", "pnl_cum": 0, "days": 0,
                "best": None, "worst": None}

    base, last = pts[0]["total"], pts[-1]["total"]
    peak, mdd, mdd_from, mdd_to = -1e18, 0.0, "", ""
    peak_date = ""
    for p in pts:
        if p["total"] > peak:
            peak, peak_date = p["total"], p["date"]
        dd = (p["total"] - peak) / peak * 100 if peak > 0 else 0.0
        if dd < mdd:
            mdd, mdd_from, mdd_to = dd, peak_date, p["date"]
        p["cum_ret"] = _r((p["total"] - base) / base * 100)
        p["dd"] = _r(dd)

    # 日盈亏累加（不受出入金影响，但依赖东财当日盈亏口径）
    acc = 0.0
    for p in pts:
        acc += p["day_pnl"]
        p["pnl_cum"] = _r(acc)

    # TWR：把每天的净入金扣掉再算收益，最后连乘
    twr, prev_total, prev_seen = 1.0, None, -1
    net_in_total = 0.0
    dates = [p["date"] for p in pts]
    for i, p in enumerate(pts):
        # 本段（上一记录日之后到今日）累计的净入金
        seg = 0.0
        for d, v in flow_day.items():
            if prev_seen < 0:
                ok = d <= p["date"]
            else:
                ok = dates[prev_seen] < d <= p["date"]
            if ok:
                seg += v
        net_in_total += seg if i > 0 else 0.0
        if prev_total and prev_total > 0:
            r = (p["total"] - seg) / prev_total - 1
            # 单日 ±50% 以上多半是漏了一天的流水或数据错，别让它污染连乘
            if -0.5 < r < 0.5:
                twr *= (1 + r)
            p["ret_twr"] = _r((twr - 1) * 100)
            p["net_in_flow"] = _r(seg)
        else:
            p["ret_twr"] = 0.0
            p["net_in_flow"] = 0.0
        prev_total, prev_seen = p["total"], i

    best = max(pts, key=lambda x: x["day_pnl"])
    worst = min(pts, key=lambda x: x["day_pnl"])
    return {"points": pts, "start": pts[0]["date"], "end": pts[-1]["date"],
            "base": _r(base), "last": _r(last),
            "ret": _r((last - base) / base * 100) if base else 0,
            "ret_twr": _r((twr - 1) * 100) if flow_day else None,
            "net_in": _r(net_in_total), "has_flow": bool(flow_day),
            "mdd": _r(mdd), "mdd_from": mdd_from, "mdd_to": mdd_to,
            "pnl_cum": _r(acc), "days": len(pts),
            "best": {"date": best["date"], "pnl": best["day_pnl"]},
            "worst": {"date": worst["date"], "pnl": worst["day_pnl"]}}


# --------------------------------------------------------------------------
# 持仓 + 归因
# --------------------------------------------------------------------------

def _positions(latest: dict, days: list[dict]) -> dict:
    pos = [dict(p) for p in (latest.get("positions") or [])]
    ov = latest.get("overview") or {}
    total = _r(ov.get("total")) or 1.0
    mv_all = sum(p.get("mv") or 0 for p in pos) or 1.0

    try:
        import industry
        imap = industry.load_map()
    except Exception:
        imap = {}

    groups = defaultdict(lambda: {"mv": 0.0, "pnl": 0.0, "day_pnl": 0.0, "n": 0})
    for p in pos:
        ind = imap.get(p.get("code") or "") or "未分类"
        p["industry"] = ind
        p["mv_share"] = _r((p.get("mv") or 0) / total * 100)
        p["weight_calc"] = _r((p.get("mv") or 0) / mv_all * 100)
        p["day_contrib"] = _r((p.get("day_pnl") or 0) / total * 100, 3)
        g = groups[ind]
        g["mv"] += p.get("mv") or 0
        g["pnl"] += p.get("pnl") or 0
        g["day_pnl"] += p.get("day_pnl") or 0
        g["n"] += 1

    shares = sorted([p["mv_share"] for p in pos], reverse=True)
    hhi = sum((s / 100) ** 2 for s in shares)
    inds = [{"name": k, "mv": _r(v["mv"]), "share": _r(v["mv"] / total * 100),
             "pnl": _r(v["pnl"]), "day_pnl": _r(v["day_pnl"]), "n": v["n"]}
            for k, v in groups.items()]
    inds.sort(key=lambda x: -x["mv"])

    wins = [p for p in pos if (p.get("pnl") or 0) > 0]
    return {
        "items": pos,
        "count": len(pos),
        "mv": _r(sum(p.get("mv") or 0 for p in pos)),
        "pnl": _r(sum(p.get("pnl") or 0 for p in pos)),
        "day_pnl": _r(sum(p.get("day_pnl") or 0 for p in pos)),
        "win_count": len(wins),
        "industry": inds,
        "top1": _r(shares[0]) if shares else 0,
        "top3": _r(sum(shares[:3])),
        "hhi": _r(hhi, 4),
        "eff_n": _r(1 / hhi, 2) if hhi > 0 else 0,
    }


def _attribution(latest: dict, pos: dict, days: list[dict], rate: dict) -> dict:
    ov = latest.get("overview") or {}
    day_pnl = _r(ov.get("day_pnl"))
    items = []
    for p in (pos.get("items") or []):
        items.append({"code": p.get("code"), "name": p.get("name"),
                      "day_pnl": _r(p.get("day_pnl")),
                      "pct": _r((p.get("day_pnl") or 0) / day_pnl * 100) if day_pnl else 0.0,
                      "mv_share": p.get("mv_share")})
    items.sort(key=lambda x: -abs(x["day_pnl"]))

    # 当日成交费用（委托接口不回佣金，按费率估算）
    orders = latest.get("orders") or []
    fee_sum, turnover, traded = 0.0, 0.0, 0
    for o in orders:
        if (o.get("filled") or 0) <= 0:
            continue
        amt = (o.get("amount") or 0) or (o.get("avg_price") or 0) * (o.get("filled") or 0)
        if amt <= 0:
            continue
        traded += 1
        turnover += amt
        fee_sum += _fee(o.get("side") or "买入", amt, rate)

    held_sum = sum(i["day_pnl"] for i in items)
    fee_sum = _r(fee_sum)
    other = _r(day_pnl - held_sum + fee_sum)   # 持仓没覆盖的：当日卖出兑现、已清仓个股
    return {"day_pnl": day_pnl, "items": items, "held": _r(held_sum),
            "fee": fee_sum, "turnover": _r(turnover), "traded": traded,
            "other": other,
            "top_gain": items[0] if items else None,
            "top_loss": items[-1] if items else None}


# --------------------------------------------------------------------------
# 交易复盘：FIFO 配对
# --------------------------------------------------------------------------

def _all_fills(days: list[dict]) -> list[dict]:
    """
    成交明细来源优先级：

    1. `data/ledger/fills.csv` —— `query_history_trades` 回流的真实成交，有成交编号、有真实费用；
    2. 账本里每日委托（`query_orders` / `query_trades`）—— 只覆盖「同步过的那几天」，
       作为历史接口还没拉到时的兜底，不覆盖已存在的同一天成交。

    两者按「成交编号 / 日+时+码+向+量+价」去重，所以重复同步不会让笔数翻倍。
    """
    out: list[dict] = []
    seen: set[str] = set()

    for f in emtlink.fills():
        k = ("id:" + f["id"]) if f.get("id") else "k:%s|%s|%s|%s|%s|%s" % (
            f["date"], f.get("time", ""), f["code"], f["side"],
            round(f["qty"], 4), round(f["price"], 4))
        if k in seen:
            continue
        seen.add(k)
        out.append({"date": f["date"], "id": f.get("id") or "",
                    "code": f["code"], "name": f.get("name") or "",
                    "side": f["side"], "qty": float(f["qty"]),
                    "price": float(f["price"]), "fee": float(f.get("fee") or 0)})

    for d in days:
        for o in (d.get("orders") or []):
            fill = o.get("filled") or 0
            if fill <= 0:
                continue
            if o.get("state") not in ("已成", "部成"):
                continue
            price = o.get("avg_price") or o.get("price") or 0
            if price <= 0:
                amt = o.get("amount") or 0
                price = amt / fill if fill else 0
            if price <= 0:
                continue
            day = o.get("date") or d.get("date") or ""
            k = "k:%s|%s|%s|%s|%s|%s" % (day, o.get("time") or "", o.get("code") or "",
                                         o.get("side") or "", round(float(fill), 4),
                                         round(float(price), 4))
            if k in seen:
                continue
            seen.add(k)
            out.append({"date": day, "id": o.get("id") or "",
                        "code": o.get("code") or "", "name": o.get("name") or "",
                        "side": o.get("side") or "", "qty": float(fill),
                        "price": float(price), "fee": float(o.get("fee") or 0)})

    out.sort(key=lambda x: (x["date"], x["id"], x["code"]))
    return out


def _fifo(fills: list[dict], rate: dict) -> tuple[list[dict], dict]:
    """
    FIFO 配对。费用优先用**成交回报里的真实值**（`fills.csv` 的 fee 列，
    来自 query_history_trades 的佣金/印花税/过户费），没有才按费率估算。
    这一点直接影响「费用吃掉多少毛利」，估算值通常比真实值低。
    """
    books: dict[str, list] = defaultdict(list)
    closed: list[dict] = []
    fee_total = 0.0
    closed_fee = 0.0
    turnover = 0.0
    real_fee_n = 0

    for f in fills:
        amt = f["qty"] * f["price"]
        real_fee = float(f.get("fee") or 0)
        fee = real_fee if real_fee > 0 else _fee(f["side"], amt, rate)
        if real_fee > 0:
            real_fee_n += 1
        turnover += amt
        fee_total += fee
        if f["side"] == "买入":
            books[f["code"]].append({"date": f["date"], "qty": f["qty"], "qty0": f["qty"],
                                     "price": f["price"], "fee": fee})
            continue
        if f["side"] != "卖出":
            continue
        need = f["qty"]
        while need > 1e-9 and books[f["code"]]:
            lot = books[f["code"]][0]
            take = min(need, lot["qty"])
            gross = take * (f["price"] - lot["price"])
            buy_fee = lot["fee"] * (take / lot["qty0"]) if lot["qty0"] else 0.0
            sell_fee = fee * (take / f["qty"]) if f["qty"] else 0.0
            closed_fee += buy_fee + sell_fee
            pnl = gross - buy_fee - sell_fee
            base = take * lot["price"]
            closed.append({
                "code": f["code"], "name": f["name"] or f["code"],
                "buy_date": lot["date"], "sell_date": f["date"],
                "qty": _r(take, 0), "buy_price": _r(lot["price"], 3),
                "sell_price": _r(f["price"], 3),
                "gross": _r(gross), "pnl": _r(pnl), "fee": _r(buy_fee + sell_fee),
                "pnl_pct": _r(pnl / base * 100) if base > 0 else 0.0,
                "days": _day_gap(lot["date"], f["date"]),
            })
            lot["qty"] -= take
            need -= take
            if lot["qty"] <= 1e-9:
                books[f["code"]].pop(0)

    opens = {c: _r(sum(l["qty"] for l in lots), 0) for c, lots in books.items() if lots}
    return closed, {"fee": _r(fee_total), "closed_fee": _r(closed_fee),
                    "turnover": _r(turnover), "opens": opens,
                    "real_fee_n": real_fee_n}


def _day_gap(a: str, b: str) -> int:
    try:
        import datetime as dt
        da = dt.date.fromisoformat(str(a)[:10])
        db = dt.date.fromisoformat(str(b)[:10])
        return (db - da).days
    except Exception:
        return 0


def _trades(days: list[dict], rate: dict, avg_total: float) -> dict:
    fills = _all_fills(days)
    closed, meta = _fifo(fills, rate)
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] <= 0]
    win_sum = sum(c["pnl"] for c in wins)
    loss_sum = abs(sum(c["pnl"] for c in losses))
    avg_win = win_sum / len(wins) if wins else 0.0
    avg_loss = loss_sum / len(losses) if losses else 0.0
    days_n = max(1, len({f["date"] for f in fills}))

    buy_amt = sum(f["qty"] * f["price"] for f in fills if f["side"] == "买入")
    top_win = max(closed, key=lambda c: c["pnl"]) if closed else None
    top_loss = min(closed, key=lambda c: c["pnl"]) if closed else None
    # 年化换手只在样本够时才外推：3 个交易日的成交额乘 244/3 会得出几万百分比，
    # 那不是「换手高」，那是样本太短。
    annual = _r(meta["turnover"] / avg_total / days_n * 244 * 100, 1) \
        if avg_total > 0 and days_n >= 5 else None

    return {
        "fills": len(fills),
        "closed": len(closed),
        "open_lots": len([1 for v in meta["opens"].values() if v > 0]),
        "win_rate": _r(len(wins) / len(closed) * 100) if closed else 0.0,
        "realized": _r(sum(c["pnl"] for c in closed)),
        "gross": _r(sum(c["gross"] for c in closed)),
        "fee": meta["fee"],
        "closed_fee": meta["closed_fee"],
        "turnover": meta["turnover"],
        "avg_hold_days": _r(sum(c["days"] for c in closed) / len(closed), 1) if closed else 0.0,
        "avg_win": _r(avg_win), "avg_loss": _r(avg_loss),
        "pl_ratio": _r(avg_win / avg_loss, 2) if avg_loss > 0 else None,
        "expectancy": _r((len(wins) / len(closed) * avg_win - len(losses) / len(closed) * avg_loss)) if closed else 0.0,
        "top_win": top_win, "top_loss": top_loss,
        "closed_list": sorted(closed, key=lambda c: c["sell_date"], reverse=True)[:60],
        "turnover_rate": annual,
        "sample_days": days_n,
        "daily_amount": _r(meta["turnover"] / days_n) if days_n else 0.0,
        "buy_amount": _r(buy_amt),
        # 多少笔成交带了真实费用（其余为费率估算，会在界面上标出来）
        "real_fee_n": meta.get("real_fee_n") or 0,
        "fee_est_n": len(fills) - (meta.get("real_fee_n") or 0),
        "first_day": min((f["date"] for f in fills if f["date"]), default=""),
        "last_day": max((f["date"] for f in fills if f["date"]), default=""),
    }


# --------------------------------------------------------------------------
# 资金流水与委托质量（emtl 的历史接口带来的两块新信息）
# --------------------------------------------------------------------------

_IN_WORDS = ("转证券", "转入", "入金", "存", "银证转", "bank2sec", "银行转入")
_OUT_WORDS = ("转银行", "转出", "出金", "取", "银证转出", "sec2bank")


def _flow_class(kind: str, amount: float) -> str:
    """把流水摘要归到：入金 / 出金 / 分红 / 利息 / 税 / 费 / 其他。"""
    k = str(kind or "")
    low = k.lower()
    if any(w in k or w in low for w in _OUT_WORDS):
        return "out"
    if any(w in k or w in low for w in _IN_WORDS):
        return "in"
    # 税要在「红利」之前判断：「股息红利税」里同时含税和红利，
    # 先判红利会把它算成分红收入，把税负藏起来。
    if "税" in k:
        return "tax"
    if "红利" in k or "股息" in k or "分红" in k:
        return "dividend"
    if "利息" in k or "结息" in k:
        return "interest"
    if "费" in k or "佣金" in k:
        return "fee"
    return "other"


def _cashflow(flows: list[dict]) -> dict:
    """
    资金流水 -> 净入金 / 分红 / 利息 / 税费。

    为什么这块重要：没有它，账户总资产的变化里混着「转了多少钱进来」，
    收益率会被出入金污染（今天转 10 万进来，曲线会显示暴涨 30%）。
    有了流水才能算「剔除出入金后的真实收益」，也才能把分红、红利税、利息单列。
    """
    if not flows:
        return {"ok": True, "empty": True, "net_in": 0.0, "in_sum": 0.0, "out_sum": 0.0,
                "dividend": 0.0, "interest": 0.0, "tax": 0.0, "fee": 0.0, "other": 0.0,
                "rows": [], "n": 0,
                "note": "没有资金流水。点「拉取历史」把 query_funds_flow 的数据补进来，"
                        "才能把出入金从收益里剔除。"}

    agg = defaultdict(float)
    rows = []
    for f in flows:
        amt = _r(f.get("amount"), 2)
        cls = _flow_class(f.get("kind"), amt)
        mag = abs(amt)
        if cls in ("in", "out") and amt:
            # 有明确方向词时优先信摘要，金额符号可能是「流出为负」也可能全正
            direction = 1 if cls == "in" else -1
        else:
            direction = 1 if amt >= 0 else -1
        agg[cls] += mag if cls in ("dividend", "interest", "tax", "fee") else direction * mag
        if cls in ("in", "out"):
            agg[cls + "_sum"] += mag
        rows.append({"date": f.get("date") or "", "kind": f.get("kind") or "",
                     "class": cls, "amount": round(direction * mag, 2),
                     "balance": _r(f.get("balance"), 2),
                     "code": f.get("code") or "", "name": f.get("name") or ""})

    rows.sort(key=lambda x: x["date"], reverse=True)
    net_in = _r(agg["in"] + agg["out"])
    bal = [r["balance"] for r in rows if r["balance"]]
    return {
        "ok": True, "empty": False, "n": len(rows),
        "net_in": net_in,
        "in_sum": _r(agg["in_sum"]), "out_sum": _r(agg["out_sum"]),
        "dividend": _r(agg["dividend"]), "interest": _r(agg["interest"]),
        "tax": _r(agg["tax"]), "fee": _r(agg["fee"]), "other": _r(agg["other"]),
        "first_day": rows[-1]["date"] if rows else "",
        "last_day": rows[0]["date"] if rows else "",
        "balance_last": _r(bal[0]) if bal else 0.0,
        "rows": rows[:50],
        "note": "净入金为正表示往账户里净转入；收益率是否已剔除，看资金曲线上的「剔除出入金」标注。",
    }


def _order_quality(orders: list[dict]) -> dict:
    """
    委托质量：撤单率、成交转化、委托价与成交价的偏差（滑点）。

    这是「成交明细」看不到的一面 —— 挂出去又撤掉的那些单子，
    反映了犹豫、追价、以及「想买但没买到」的机会成本。
    """
    if not orders:
        return {"ok": True, "empty": True, "n": 0,
                "note": "没有历史委托。点「拉取历史」补 query_history_orders 才能看撤单率与滑点。"}

    n = len(orders)
    cnt = defaultdict(int)
    qty_all, qty_filled = 0.0, 0.0
    slip, slip_n = 0.0, 0
    for o in orders:
        cnt[o.get("state") or "未成"] += 1
        q = float(o.get("qty") or 0)
        fl = float(o.get("filled") or 0)
        qty_all += q
        qty_filled += fl
        price = float(o.get("price") or 0)
        avg = float(o.get("avg_price") or 0)
        if fl > 0 and price > 0 and avg > 0:
            # 买：成交价高于委托价是不利；卖：反之。统一取「不利方向」为正
            d = (avg - price) if o.get("side") == "买入" else (price - avg)
            slip += d / price * 100
            slip_n += 1

    done = cnt.get("已成", 0) + cnt.get("部成", 0)
    recent = []
    for o in sorted(orders, key=lambda x: ((x.get("date") or ""), (x.get("time") or "")),
                    reverse=True)[:14]:
        price = float(o.get("price") or 0)
        avg = float(o.get("avg_price") or 0)
        fl = float(o.get("filled") or 0)
        one_slip = None
        if fl > 0 and price > 0 and avg > 0:
            d = (avg - price) if o.get("side") == "买入" else (price - avg)
            one_slip = _r(d / price * 100, 3)
        recent.append({"date": o.get("date") or "", "code": o.get("code") or "",
                       "name": o.get("name") or "", "side": o.get("side") or "",
                       "qty": o.get("qty") or 0, "filled": fl,
                       "price": _r(price, 3), "avg": _r(avg, 3) if fl else 0.0,
                       "state": o.get("state") or "", "slip": one_slip})
    return {
        "ok": True, "empty": False, "n": n,
        "filled": cnt.get("已成", 0), "part": cnt.get("部成", 0),
        "canceled": cnt.get("已撤", 0), "invalid": cnt.get("废单", 0),
        "unfilled": cnt.get("未成", 0),
        "cancel_rate": _r(cnt.get("已撤", 0) / n * 100) if n else 0.0,
        "fill_rate": _r(done / n * 100) if n else 0.0,
        "qty_fill_rate": _r(qty_filled / qty_all * 100) if qty_all else 0.0,
        "slippage": _r(slip / slip_n, 3) if slip_n else None,
        "slippage_n": slip_n,
        "recent": recent,
        "first_day": min((o.get("date") for o in orders if o.get("date")), default=""),
        "last_day": max((o.get("date") for o in orders if o.get("date")), default=""),
        "note": "滑点为正表示成交价比委托价更不利（买贵了 / 卖便宜了），只统计有成交的委托。",
    }


# --------------------------------------------------------------------------
# 行为体检
# --------------------------------------------------------------------------

def _diagnose(latest: dict, pos: dict, trades: dict, curve: dict, has_equity: bool = True,
              cash: dict | None = None, oq: dict | None = None) -> list[dict]:
    out: list[dict] = []
    ov = latest.get("overview") or {}
    items = pos.get("items") or []
    total = _r(ov.get("total")) or 1.0
    avail = _r(ov.get("avail"))

    if not has_equity:
        # 只导入了交割单、没拉过资金快照：持仓未知，仓位类规则一条都不成立
        out.append({"level": "warn", "title": "没有资金快照，仓位类体检跳过",
                    "detail": "账本里只有成交流水（导入的），没有账户总资产与持仓。"
                              "点「同步账户」拉一次快照，集中度、单票上限、现金缓冲才会开始判断。"
                              "下面的胜率与盈亏比不受影响 —— 它们只依赖成交。"})
    elif not items:
        out.append({"level": "warn", "title": "当前空仓",
                    "detail": "账本里最后一个交易日没有持仓。空仓不用体检，但记得确认是主动清仓还是同步失败。"})
    else:
        # 单票超限
        over = [p for p in items if p.get("mv_share", 0) > 25]
        if over:
            p = over[0]
            out.append({"level": "risk" if p["mv_share"] > 40 else "warn",
                        "title": "单票超限：%s 占总资产 %.1f%%" % (p["name"], p["mv_share"]),
                        "detail": "超过 25%% 的单票占比会把组合波动绑在一只股票上；超过 40%% 基本等于押注。"
                                  "共 %d 只超过 25%%。" % len(over)})
        # 集中度
        if pos["top3"] > 60:
            out.append({"level": "warn", "title": "前三大合计 %.1f%%" % pos["top3"],
                        "detail": "有效分散度只有 %.1f 只（1/HHI）。行业或风格一旦反转，组合几乎没有缓冲。"
                                  % pos["eff_n"]})
        # 持仓过少
        if pos["count"] < 3:
            out.append({"level": "warn", "title": "只持有 %d 只" % pos["count"],
                        "detail": "个股风险远大于系统风险，单只暴雷无法用其他持仓对冲。"})
        # 现金缓冲
        cash_pct = avail / total * 100 if total else 0
        if cash_pct < 2 and pos["count"] > 0:
            out.append({"level": "warn", "title": "可用资金仅 %.1f%%" % cash_pct,
                        "detail": "满仓状态，遇到机会或补仓需求时没有弹药；同时也说明仓位管理没有留余地。"})
        elif cash_pct > 60:
            out.append({"level": "ok", "title": "可用资金 %.1f%%，仓位偏轻" % cash_pct,
                        "detail": "空仓等机会也是一种仓位，但要能说清「在等什么信号」。"})
        # 亏损集中
        bad = [p for p in items if (p.get("pnl") or 0) < 0]
        if bad:
            worst = min(bad, key=lambda p: p["pnl"])
            loss_all = abs(sum(p["pnl"] for p in bad)) or 1
            share = abs(worst["pnl"]) / loss_all * 100
            if share > 50 and worst["pnl"] < -total * 0.02:
                out.append({"level": "warn",
                            "title": "亏损集中在 %s（占总亏损 %.0f%%）" % (worst["name"], share),
                            "detail": "单只亏损 %.2f 元、%.1f%%。集中亏损往往来自「不肯认错」，"
                                      "而不是看错了方向。" % (worst["pnl"], worst["pnl_pct"])})

    # 追高：买入价 vs 当日区间
    chase = [c for c in (trades.get("closed_list") or []) if (c.get("pnl") or 0) < 0 and c.get("days", 0) <= 3]
    if len(chase) >= 2:
        out.append({"level": "warn", "title": "有 %d 笔 3 日内即亏损平仓" % len(chase),
                    "detail": "短持有+亏损通常意味着买入点在当日偏高位，或买入理由不成立后没有及时处理。"})

    # 盈亏比与胜率
    if trades.get("closed", 0) >= 5:
        wr, pl = trades["win_rate"], trades["pl_ratio"]
        if pl is not None and wr < 45 and pl < 1.2:
            out.append({"level": "risk", "title": "胜率 %.0f%%、盈亏比 %.2f" %
                        (wr, pl),
                        "detail": "两头都不占优：既没有高胜率，也没有让盈利奔跑。这类结构长期必然被手续费磨掉。"})
        elif pl is not None and pl >= 2 and wr < 40:
            out.append({"level": "ok", "title": "低胜率高盈亏比（%.0f%% / %.2f）" % (wr, pl),
                        "detail": "只要少数大盈利能覆盖多数小亏损，这个结构是健康的；关键是别提前砍掉盈利单。"})
        elif wr >= 55:
            out.append({"level": "ok", "title": "胜率 %.0f%%，盈亏比 %s" %
                        (wr, ("%.2f" % pl) if pl else "—"),
                        "detail": "胜率不错；注意盈亏比过低时会被单笔大亏吃掉多笔小盈利。"})

    # 换手与费用（年化换手样本不足 5 日时不做外推，只在日成交额上体现）
    if trades.get("turnover_rate") and trades["turnover_rate"] > 800:
        out.append({"level": "warn", "title": "年化换手约 %.0f%%" % trades["turnover_rate"],
                    "detail": "按当前节奏一年要买卖这么多轮，估算费用 %.2f 元。换手本身不产生收益，"
                              "只放大摩擦。" % trades["fee"]})
    if trades.get("closed_fee", 0) > 0 and trades.get("gross"):
        ratio = abs(trades["closed_fee"] / trades["gross"] * 100) if trades["gross"] else 0
        if ratio > 30:
            out.append({"level": "warn", "title": "费用占毛收益 %.0f%%" % ratio,
                        "detail": "已平仓的交易里，佣金+印花税+过户费吃掉了 %.0f%% 的毛利。"
                                  "降低交易频率通常比提高选股准确率更快见效。" % ratio})

    # ---- 以下是 emtl 历史接口带来的两条：委托质量、出入金口径 ----
    if oq and not oq.get("empty") and (oq.get("n") or 0) >= 20:
        cr = oq.get("cancel_rate") or 0
        if cr > 30:
            out.append({"level": "warn", "title": "历史委托撤单率 %.0f%%" % cr,
                        "detail": "共 %d 笔委托，其中 %d 笔已撤。撤单率高通常说明下单前没想好价位，"
                                  "或者习惯性挂低价等成交却在盘中改主意 —— 两者都会让成交价变差。"
                                  % (oq["n"], oq.get("canceled", 0))})
        sl = oq.get("slippage")
        if sl is not None and abs(sl) > 0.3:
            out.append({"level": "warn" if abs(sl) > 0.8 else "ok",
                        "title": "平均滑点 %+.2f%%" % sl,
                        "detail": "有成交的 %d 笔委托里，成交价平均比委托价%s 0.3%% 以上。"
                                  "滑点持续为正（买贵/卖便宜）说明你在追价成交，"
                                  "挂单等价的纪律性比选股更值得先改。"
                                  % (oq.get("slippage_n") or 0, "不利" if sl > 0 else "有利")})

    c = cash or {}
    if not c.get("empty") and curve.get("has_flow"):
        ret, twr = curve.get("ret"), curve.get("ret_twr")
        if twr is not None and ret is not None and abs(ret - twr) > 3 and abs(c.get("net_in") or 0) > 1000:
            out.append({"level": "ok", "title": "总资产 %+.1f%%，剔除出入金后 %+.1f%%" % (ret, twr),
                        "detail": "区间净%s %.2f 元。总资产涨跌里混着转账，"
                                  "判断自己做得怎么样请看剔除出入金后的那个数。"
                                  % ("转入" if (c.get("net_in") or 0) > 0 else "转出",
                                     abs(c.get("net_in") or 0))})
        if (c.get("tax") or 0) > 0:
            out.append({"level": "ok", "title": "红利税 %.2f 元" % c["tax"],
                        "detail": "有分红就说明持仓确实拿到了现金回报；红利税按持股期限差别征收，"
                                  "长期持有比短线波段在这块更划算。"})

    if not out:
        out.append({"level": "ok", "title": "没有触发体检规则",
                    "detail": "集中度、现金缓冲、盈亏比、换手都在常规区间；继续按纪律执行即可。"})
    order = {"risk": 0, "warn": 1, "ok": 2}
    out.sort(key=lambda x: order.get(x["level"], 3))
    return out


# --------------------------------------------------------------------------
# 与选股系统对照
# --------------------------------------------------------------------------

def _vs_picks(pos: dict, batches: int = 9) -> dict:
    try:
        import pandas as pd
        from pathlib import Path
        p = Path(__file__).resolve().parent / "data" / "screen" / "picks.csv"
        if not p.exists():
            return {"ok": True, "empty": True, "note": "还没有选股记录"}
        df = pd.read_csv(p, dtype={"code": str, "as_of": str})
        if df.empty:
            return {"ok": True, "empty": True, "note": "选股记录为空"}
        df["code"] = df["code"].astype(str).str.zfill(6)
        keys = (df[["as_of", "recorded_at"]].drop_duplicates()
                .sort_values(["as_of", "recorded_at"], ascending=False).head(batches))
        sub = df.merge(keys, on=["as_of", "recorded_at"], how="inner")
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}

    held = {p["code"]: p for p in (pos.get("items") or [])}
    latest_batch = sub[sub["as_of"] == sub["as_of"].max()] if len(sub) else sub
    top = latest_batch.sort_values("rank").head(20) if "rank" in latest_batch.columns else latest_batch.head(20)

    hit, miss = [], []
    for _, r in top.iterrows():
        c = r["code"]
        item = {"code": c, "name": r.get("name") or c, "rank": int(r["rank"]) if pd.notna(r.get("rank")) else None,
                "as_of": r["as_of"], "industry": r.get("industry") or ""}
        if c in held:
            item["hold_pnl_pct"] = held[c].get("pnl_pct")
            hit.append(item)
        else:
            miss.append(item)

    # 候选自记录日到最新收盘的涨幅。
    # 口径必须一致：picks.csv 里的 close 是**后复权**收盘（建快照时写进去的），
    # 所以这里也用 histcache 的后复权序列，绝不能拿它跟账户持仓的**真实成交价**比 ——
    # 那样会算出「宁德时代 465 → 180，跌 61%」这种纯属除权造成的假跌幅。
    perf, avg = [], None
    try:
        import histcache
        codes = sorted(set(sub["code"].tolist()))
        bars = histcache.bulk_ohlcv(codes, 90)
        for _, r in sub.iterrows():
            b = bars.get(r["code"])
            if not b or not b.get("date"):
                continue
            m = {d: c for d, c in zip(b["date"], b["c"]) if c and c > 0}
            as_of = str(r["as_of"])
            base = m.get(as_of)
            if base is None:                       # 记录日停牌：取之前最近一个交易日
                prev = [d for d in m if d <= as_of]
                if not prev:
                    continue
                base = m[max(prev)]
            last_day = max(m)
            cur = m[last_day]
            if base <= 0:
                continue
            perf.append({"code": r["code"], "name": r.get("name") or "",
                         "as_of": as_of, "last": last_day,
                         "from": _r(base, 2), "to": _r(cur, 2),
                         "ret": _r((cur - base) / base * 100),
                         "held": r["code"] in held})
        if perf:
            avg = _r(sum(x["ret"] for x in perf) / len(perf))
    except Exception:                                        # noqa: BLE001
        perf = []

    perf.sort(key=lambda x: -x["ret"])
    held_perf = [x for x in perf if x["held"]]
    return {"ok": True, "empty": False,
            "batches": int(len(keys)),
            "top_n": int(len(top)),
            "hit": hit, "miss": miss,
            "hit_rate": _r(len(hit) / len(top) * 100) if len(top) else 0.0,
            "pick_perf": perf[:40],
            "held_perf": held_perf,
            "held_perf_avg": _r(sum(x["ret"] for x in held_perf) / len(held_perf)) if held_perf else None,
            "pick_ret_avg": avg,
            "perf_n": len(perf),
            "note": "候选涨幅用后复权收盘（与选股记录同口径），不是账户真实成交价；"
                    "你实际赚多少看左边的持仓盈亏。"}


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------

def board(days: int = 180, rate: dict | None = None) -> dict:
    rate = dict(FEE, **(rate or {}))
    led = emtlink.load_all(limit=int(days))
    fl = emtlink.flows()
    ords = emtlink.hist_orders()
    st = emtlink.status()
    hstate = emtlink.hist_state()

    if not led:
        return {"ok": True, "empty": True, "status": st, "history": hstate,
                "note": "还没有账本。先点「同步账户」拉一次持仓快照；"
                        "若只想看成交与胜率，直接「拉取历史」也能出结果（没有资金曲线）。",
                "curve": _curve([], fl), "positions": {"items": [], "count": 0, "industry": []},
                "attribution": {"items": [], "day_pnl": 0}, "trades": {}, "diagnose": [],
                "cashflow": _cashflow(fl), "order_quality": _order_quality(ords),
                "vs_picks": {"ok": True, "empty": True, "note": "等待账本"},
                "journal": "", "days": 0}

    latest = led[0]
    ov = latest.get("overview") or {}
    prev = led[1] if len(led) > 1 else None
    pov = (prev or {}).get("overview") or {}
    delta = {}
    for k, label in (("total", "总资产"), ("mv", "持仓市值"), ("cash", "资金余额")):
        cur_v, pre_v = _r(ov.get(k)), _r(pov.get(k))
        delta[k] = {"now": cur_v, "prev": pre_v,
                    "chg": _r(cur_v - pre_v) if prev else None,
                    "pct": _r((cur_v - pre_v) / pre_v * 100) if prev and pre_v else None}

    pos = _positions(latest, led)
    curve = _curve(led, fl)
    has_equity = bool(ov.get("total"))
    avg_total = (sum(p["total"] for p in curve["points"]) / len(curve["points"])) if curve["points"] else 0
    trades = _trades(led, rate, avg_total)
    cash = _cashflow(fl)
    oq = _order_quality(ords)
    attr = _attribution(latest, pos, led, rate)
    diag = _diagnose(latest, pos, trades, curve, has_equity, cash, oq)

    return {
        "ok": True, "empty": False,
        "status": st,
        "history": hstate,
        "date": latest.get("date") or "",
        "at": latest.get("at") or "",
        "overview": ov,
        "has_equity": has_equity,
        "has_snapshot": bool(latest.get("positions")),
        "prev_date": (prev or {}).get("date") or "",
        "delta": delta,
        "snaps_today": len(latest.get("snaps") or []),
        "curve": curve,
        "positions": pos,
        "attribution": attr,
        "trades": trades,
        "cashflow": cash,
        "order_quality": oq,
        "diagnose": diag,
        "vs_picks": _vs_picks(pos),
        "journal": latest.get("journal") or "",
        "fee_rate": rate,
        "ledger_days": emtlink.ledger_days()[:120],
        "days": len(led),
    }
