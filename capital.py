# -*- coding: utf-8 -*-
"""
QuantDesk · 资金流数据（P2-6）

思路：TradingAgents 用 StockTwits / Reddit 做情绪，这在 A 股不可行（源不稳定、
合规风险、短周期噪音大）。A 股更有信息量、且免费可得的是「真金白银」的仓位证据：

  两融      融资余额与融资净买入 —— 杠杆资金的真实方向
  龙虎榜    营业部/机构席位的净买额 —— 异动当日的资金归因
  大宗交易  折价率与成交额 —— 大额减持 / 接盘的痕迹
  北向持股  陆股通持股数量变动 —— 外资连续加减仓

四路都来自东方财富公开的 datacenter 接口（免登录、无配额），按日过滤 + 分页拉取。
落库到 store.capital（code, date, 8 个字段），一律按 as-of 读取：
某因子在 T 日只用 date <= T 的记录算，不做任何「用今天的数据回填昨天」。

重要纪律：**默认不进模型**。
  文档里的判断是「先用因子体检证明现有 24 个因子没被榨干，再加新维度」。
  所以这里只提供数据与因子计算，体检页里作为独立一组展示 IC，
  想让它进模型必须显式打开 capital.json 的 `in_model` 并重训（FEATURE_VERSION 会变）。
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import threading
import time
from pathlib import Path

import qdpaths

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
DATA = qdpaths.data_path()
CFG_PATH = DATA / "capital.json"

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://data.eastmoney.com/"}
PAGE = 500

DEFAULT = {"in_model": False, "window": 20, "keep_days": 400}

_STATE = {"running": False, "done": 0, "total": 0, "rows": 0, "message": "",
          "last": "", "error": ""}
_LOCK = threading.Lock()

# 因子定义（体检页展示用；direction 只作解释，不进基准分）
CAP_FACTORS = [
    {"key": "cap_margin_chg", "name": "融资余额变化", "unit": "%",
     "desc": "近 %d 日融资余额变化率，杠杆资金加仓为正", "dir": 1},
    {"key": "cap_margin_net", "name": "融资净买占比", "unit": "%",
     "desc": "近 %d 日融资净买入 / 期间成交额，衡量主动加杠杆强度", "dir": 1},
    {"key": "cap_lhb_net", "name": "龙虎榜净买占比", "unit": "%",
     "desc": "近 %d 日龙虎榜净买额 / 成交额，机构与游资的净方向", "dir": 1},
    {"key": "cap_blk_amt", "name": "大宗成交占比", "unit": "%",
     "desc": "近 %d 日大宗交易额 / 成交额，大额转让活跃度", "dir": 0},
    {"key": "cap_blk_prem", "name": "大宗折溢价", "unit": "%",
     "desc": "近 %d 日大宗加权折溢价率，折价为负（减持压力）", "dir": 1},
    {"key": "cap_north_chg", "name": "北向持股变化", "unit": "%",
     "desc": "近 %d 日陆股通持股数量变化率，外资连续加仓为正", "dir": 1},
]


def cfg() -> dict:
    c = dict(DEFAULT)
    if CFG_PATH.exists():
        try:
            c.update(json.loads(CFG_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    return c


def save(patch: dict) -> dict:
    c = cfg()
    for k, v in (patch or {}).items():
        if k in DEFAULT:
            c[k] = v
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "config": c}


# --------------------------------------------------------------------------
# 拉取
# --------------------------------------------------------------------------

def _get(report: str, day: str, date_field: str, tries: int = 3) -> list[dict]:
    out, page = [], 1
    while page <= 12:
        p = {"reportName": report, "columns": "ALL", "pageNumber": page,
             "pageSize": PAGE, "source": "WEB", "client": "WEB",
             "filter": "(%s='%s')" % (date_field, day)}
        last = None
        for i in range(tries):
            try:
                r = requests.get(URL, params=p, headers=HEADERS, timeout=25)
                r.raise_for_status()
                obj = r.json()
                if obj.get("code") != 0:
                    last = RuntimeError(obj.get("message") or "接口返回异常")
                    # 「报表配置不存在」这类错误重试也没用，直接放弃
                    if "不存在" in str(last):
                        return out
                    time.sleep(0.4 * (i + 1))
                    continue
                data = (obj.get("result") or {}).get("data") or []
                out.extend(data)
                if len(data) < PAGE:
                    return out
                break
            except Exception as e:                     # noqa: BLE001
                last = e
                time.sleep(0.4 * (i + 1))
        else:
            if last:
                raise last
        page += 1
    return out


def _code_of(v) -> str:
    c = str(v or "").split(".")[0].strip()
    return c.zfill(6) if len(c) == 6 and c.isdigit() else ""


def _f(v):
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except Exception:
        return None


def fetch_day(day: str) -> list[dict]:
    """拉一天的四路数据，按代码合并成一行。"""
    day = str(day)[:10]
    acc: dict[str, dict] = {}

    def row(code: str) -> dict:
        return acc.setdefault(code, {"code": code, "date": day})

    # 1) 龙虎榜：同一天一只票可能有多条（不同上榜原因），净买额相加
    try:
        for x in _get("RPT_DAILYBILLBOARD_DETAILSNEW", day, "TRADE_DATE"):
            code = _code_of(x.get("SECURITY_CODE") or x.get("SECUCODE"))
            if not code:
                continue
            r = row(code)
            r["lhb_net"] = (r.get("lhb_net") or 0.0) + (_f(x.get("BILLBOARD_NET_AMT")) or 0.0)
            r["lhb_amt"] = (r.get("lhb_amt") or 0.0) + (_f(x.get("BILLBOARD_DEAL_AMT")) or 0.0)
    except Exception:
        pass

    # 2) 大宗交易：金额相加，折价率按金额加权
    try:
        num, den = {}, {}
        for x in _get("RPT_DATA_BLOCKTRADE", day, "TRADE_DATE"):
            code = _code_of(x.get("SECURITY_CODE") or x.get("SECUCODE"))
            if not code:
                continue
            amt = _f(x.get("DEAL_AMT")) or 0.0
            prem = _f(x.get("PREMIUM_RATIO"))
            r = row(code)
            r["blk_amt"] = (r.get("blk_amt") or 0.0) + amt
            if prem is not None and amt > 0:
                num[code] = num.get(code, 0.0) + prem * amt
                den[code] = den.get(code, 0.0) + amt
        for code, v in num.items():
            if den.get(code):
                row(code)["blk_prem"] = v / den[code]
    except Exception:
        pass

    # 3) 两融：融资余额 + 融资净买入
    try:
        for x in _get("RPTA_WEB_RZRQ_GGMX", day, "DATE"):
            code = _code_of(x.get("SCODE") or x.get("SECUCODE"))
            if not code:
                continue
            r = row(code)
            r["margin_bal"] = _f(x.get("RZYE"))
            r["margin_net"] = _f(x.get("RZJME"))
    except Exception:
        pass

    # 4) 北向持股：只收 A 股（6 位数字代码）。
    #    实测 2024-08 之后交易所停止披露个股每日持股，接口只剩港股通标的（002/004），
    #    A 股拿不到数据。默认不再请求（省一次无用调用），字段保留以便将来恢复；
    #    真要试可以设 capital.json 的 north=true。
    if cfg().get("north"):
        try:
            for x in _get("RPT_MUTUAL_STOCK_HOLDRANKS", day, "HOLD_DATE"):
                code = _code_of(x.get("SECURITY_CODE") or x.get("SECUCODE"))
                if not code:
                    continue
                ratio = _f(x.get("HOLD_SHARES_RATIO"))
                chg = _f(x.get("ADD_SHARES_AMP"))
                if ratio is None and chg is None:
                    continue
                r = row(code)
                r["north_ratio"] = ratio
                r["north_chg"] = chg
        except Exception:
            pass

    return [v for v in acc.values()
            if any(v.get(k) is not None for k in
                   ("lhb_net", "blk_amt", "margin_bal", "north_ratio"))]


def _recent_days(n: int) -> list[str]:
    """近 n 个交易日（用本地交易日历，避免把周末当交易日空跑）。

    注意 _trade_days() 返回的是 **set** 且是 YYYYMMDD 格式：
    直接切片取到的是任意三个日期（我第一版就踩了这个坑，白白拉到 1994 年的日子），
    必须 sorted 之后再取尾部，并统一转成 YYYY-MM-DD。
    """
    today = dt.date.today().isoformat()
    try:
        import pipeline
        days = sorted(d for d in (pipeline._trade_days() or set())
                      if str(d)[:8] <= today.replace("-", ""))
        if days:
            return [pipeline.dash(d) for d in days[-int(n):]]
    except Exception:
        pass
    out, d = [], dt.date.today()
    while len(out) < int(n):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return list(reversed(out))


_SYNC_LOCK = threading.Lock()


def _single_flight(fn):
    """同一时刻只允许一个资金流同步。

    pipeline 是直接调 sync() 的，不走 sync_async 的 running 检查；
    用户同时点「同步资金流」时两边会一起打东财、互相覆盖 _STATE 进度。
    """
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not _SYNC_LOCK.acquire(blocking=False):
            return {"ok": True, "started": False, "skipped": True,
                    "message": "资金流同步已在运行，本次跳过", "state": dict(_STATE)}
        try:
            return fn(*a, **kw)
        finally:
            _SYNC_LOCK.release()
    return wrapper


@_single_flight
def sync(days: int = 25, force: bool = False) -> dict:
    """增量拉取最近 days 个交易日。已存在的日期默认跳过（force 覆盖）。"""
    import store
    days = max(1, min(int(days), 400))
    todo = _recent_days(days)
    if not todo:
        return {"ok": False, "error": "取不到交易日列表"}
    if not force:
        have = set()
        try:
            df = store.capital_rows(start=todo[0], end=todo[-1])
            # 只有「四路都试过」的日期才算有；这里用行数粗略判断：
            # 单日行数 > 500 说明主要数据已入库（两融本身就有 4000+ 行）
            cnt = df.groupby("date").size().to_dict() if len(df) else {}
            have = {d for d, n in cnt.items() if n > 500}
        except Exception:
            have = set()
        todo = [d for d in todo if d not in have]
    _STATE.update(running=True, done=0, total=len(todo), rows=0,
                  message="准备中…", last="", error="")
    total_rows = 0
    for day in todo:
        _STATE["last"] = day
        try:
            rows = fetch_day(day)
            n = store.capital_put(rows)
            total_rows += n
            _STATE["rows"] = total_rows
        except Exception as e:                         # noqa: BLE001
            _STATE["error"] = str(e)[:160]
        _STATE["done"] += 1
        _STATE["message"] = "已处理 %d/%d 天" % (_STATE["done"], _STATE["total"])
        time.sleep(0.15)
    _STATE.update(running=False, message="完成：%d 天，%d 行" % (len(todo), total_rows))
    try:
        store.log("capital", "资金流同步完成：%d 天 / %d 行" % (len(todo), total_rows),
                  level="info", act="sync", detail=_STATE.get("error") or "")
    except Exception:
        pass
    return {"ok": True, "days": len(todo), "rows": total_rows, "state": dict(_STATE),
            "status": status()}


def sync_async(days: int = 25, force: bool = False) -> dict:
    if _STATE.get("running"):
        return {"ok": True, "started": False, "message": "正在同步中"}
    threading.Thread(target=sync, kwargs={"days": days, "force": force},
                     daemon=True, name="capital-sync").start()
    return {"ok": True, "started": True, "message": "后台同步已启动（%d 天）" % days}


def fresh() -> dict:
    """轻量时效：只做一次聚合，给侧栏待办用。

    别拿 status() 去问待办：它对每个字段各扫一遍表（那是为了判断「哪一路停了」），
    十几万行时 100ms 起步，而侧栏每次切页都会问一次。
    """
    import store
    try:
        r = store.conn().execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM capital").fetchone()
    except Exception:
        return {"ok": False}
    return {"ok": True, "total": int(r[0] or 0),
            "start": (r[1] or ""), "end": (r[2] or ""),
            "running": bool(_STATE.get("running")),
            "in_model": bool(cfg().get("in_model"))}


def _trade_lag(day: str, ref: str) -> int:
    """day 落后 ref 多少个交易日；拿不到交易日历时退化为自然日差。

    只用来判断「这一路数据是不是停了」，不需要绝对精确，但必须按交易日算 ——
    用自然日算的话，周一永远比周五差 3 天，两融会天天被标成「落后」。
    """
    if not day or not ref:
        return 0
    try:
        import pipeline
        return int(pipeline._lag(pipeline._market_days(ref), day) or 0)
    except Exception:
        try:
            return (dt.date.fromisoformat(ref[:10]) - dt.date.fromisoformat(day[:10])).days
        except Exception:
            return 0


def status() -> dict:
    import store
    meta = store.capital_meta()
    try:
        data_day = store.data_day()
    except Exception:
        data_day = ""
    last = meta.get("_all", {}).get("end", "")
    names = {"lhb_net": "龙虎榜", "blk_amt": "大宗交易", "margin_bal": "融资融券"}
    # 日历只读一次：下面每一路都要算 lag，逐个读 json 文件没必要
    try:
        import pipeline
        cal = pipeline._market_days(data_day) if data_day else None
    except Exception:
        pipeline, cal = None, None
    rows = []
    for k, label in names.items():
        m = meta.get(k) or {}
        day = m.get("last") or ""
        lag = (int(pipeline._lag(cal, day) or 0) if (pipeline and cal)
               else _trade_lag(day, data_day))
        # 两融是交易所 T+1 才发布的：行情到了今天，两融必然只到昨天。
        # 不给这个宽限，那一行会永远挂一个红色「落后」标签，用户看着像坏了。
        allow = 1 if k == "margin_bal" else 0
        rows.append({"key": k, "name": label, "last": m.get("last", ""),
                     "rows": m.get("rows", 0), "lag": lag,
                     "stale": bool(lag > allow),
                     "allow": allow})
    rows.append({"key": "north_ratio", "name": "北向持股", "last": "", "rows": 0,
                 "stale": False, "off": True,
                 "note": "交易所自 2024-08 起停止披露个股每日持股，当前不可用（字段保留）"})
    return {"ok": True, "last": last, "total": meta.get("_all", {}).get("rows", 0),
            "start": meta.get("_all", {}).get("start", ""),
            "sources": rows, "in_model": bool(cfg().get("in_model")),
            "window": int(cfg().get("window") or 20),
            "state": {k: v for k, v in _STATE.items()},
            "note": "四路都是东方财富公开接口，按日过滤拉取；缺数据写 NULL 而不是 0。"}


# --------------------------------------------------------------------------
# 因子（严格 as-of：只用 date <= as_of 的记录）
# --------------------------------------------------------------------------

def factor_frame(codes: list[str], as_of: str, window: int | None = None) -> pd.DataFrame:
    """给定股票与截止日，算近 window 个交易日的资金流因子。

    分母用同一窗口的成交额（来自 store.bars），所以结果是「占成交额的比例」，
    不同市值之间可比，不受绝对金额规模影响。
    """
    import store
    w = int(window or cfg().get("window") or 20)
    as_of = str(as_of)[:10]
    codes = [str(c).zfill(6) for c in codes]
    if not codes or not as_of:
        return pd.DataFrame()
    start = (dt.date.fromisoformat(as_of) - dt.timedelta(days=int(w * 2.2) + 10)).isoformat()
    cap = store.capital_rows(codes, start, as_of)
    if cap.empty:
        return pd.DataFrame()
    cap_dates = sorted(cap["date"].unique())
    tail = cap_dates[-w:] if len(cap_dates) > w else cap_dates
    # 分母用同一窗口的成交额：单条 SQL 批量取，比逐只读整段 K 线快两个数量级
    amt = store.amount_sum(codes, tail[0], tail[-1]) if tail else {}

    out = []
    for code, sub in cap[cap["date"].isin(tail)].groupby("code"):
        sub = sub.sort_values("date")
        a = float(amt.get(code) or 0.0)
        mb = sub["margin_bal"].dropna()
        margin_chg = None
        if len(mb) >= 2 and float(mb.iloc[0]) > 0:
            margin_chg = (float(mb.iloc[-1]) / float(mb.iloc[0]) - 1) * 100
        margin_net = sub["margin_net"].sum(min_count=1)
        lhb_net = sub["lhb_net"].sum(min_count=1)
        blk_amt = sub["blk_amt"].sum(min_count=1)
        blk_prem = sub["blk_prem"].mean()
        north_chg = sub["north_chg"].sum(min_count=1)
        out.append({
            "code": code,
            "cap_margin_chg": None if margin_chg is None else round(float(margin_chg), 3),
            "cap_margin_net": (round(float(margin_net) / a * 100, 4)
                               if (margin_net is not None and a > 0) else None),
            "cap_lhb_net": (round(float(lhb_net) / a * 100, 4)
                            if (lhb_net is not None and a > 0) else None),
            "cap_blk_amt": (round(float(blk_amt) / a * 100, 4)
                            if (blk_amt is not None and a > 0) else None),
            "cap_blk_prem": None if blk_prem is None or blk_prem != blk_prem else round(float(blk_prem), 3),
            "cap_north_chg": None if north_chg is None else round(float(north_chg), 3),
        })
    return pd.DataFrame(out)


def factor_meta() -> list[dict]:
    """给体检页用的因子定义（带窗口天数）。"""
    w = int(cfg().get("window") or 20)
    return [{"key": x["key"], "name": x["name"], "group": "资金流",
             "unit": x["unit"], "desc": x["desc"] % w if "%d" in x["desc"] else x["desc"],
             "model_only": True, "direction": 0}
            for x in CAP_FACTORS]


def preview(codes: list[str], as_of: str = "") -> dict:
    """前端表格：某批股票的原始资金流数值（不做因子换算，看原始量级用）。"""
    import store
    as_of = as_of or store.data_day()
    df = store.capital_rows([str(c).zfill(6) for c in codes], "", as_of)
    if df.empty:
        return {"ok": True, "rows": [], "as_of": as_of}
    last = df[df["date"] == df["date"].max()].copy()
    rows = []
    for _, r in last.iterrows():
        rows.append({"code": r["code"], "date": r["date"],
                     "lhb_net": None if pd.isna(r["lhb_net"]) else round(float(r["lhb_net"]) / 1e4, 1),
                     "blk_amt": None if pd.isna(r["blk_amt"]) else round(float(r["blk_amt"]) / 1e4, 1),
                     "blk_prem": None if pd.isna(r["blk_prem"]) else round(float(r["blk_prem"]), 2),
                     "margin_bal": None if pd.isna(r["margin_bal"]) else round(float(r["margin_bal"]) / 1e8, 2),
                     "north_ratio": None if pd.isna(r["north_ratio"]) else round(float(r["north_ratio"]), 3)})
    return {"ok": True, "as_of": str(df["date"].max()), "rows": rows,
            "unit": "龙虎榜/大宗=万元，融资余额=亿元"}
