# -*- coding: utf-8 -*-
"""
datasource.py — 可选「账号型」数据源（默认关闭，不填 Token 全站照常可用）

参考 GitHub 上主流 A 股项目的通行做法（TradingAgents-CN、aiagents-stock、AI-Trader 等）：
  - 免登录的 akshare / baostock 作为主力（行情、名单、行业）
  - Tushare Pro Token 作为「增强源」，用来补免登录接口拿不到/不稳定的字段：
    每日估值（PE / PE_TTM / PB）、总市值 / 流通市值、换手率、量比

本模块刻意不引入 tushare 包，直接 POST api.tushare.pro，少一个依赖、行为更透明：
  POST {"api_name": "...", "token": "...", "params": {...}, "fields": "..."}

落盘：
  data/sources.json           {"tushare": {"token": "...", "enabled": true}}
  data/extra/daily_basic.csv  代码 / 交易日 / PE / PB / 市值 / 换手 / 量比

对外：
  status()                 配置状态（Token 打码）
  save(token, enabled)     保存配置
  test()                   测试连通性与权限
  fetch_daily_basic(date)  拉某一交易日的全市场估值并落盘
  load_basic()             代码 → 估值字段（供名单 / 行业成分股叠加）
"""
from __future__ import annotations

import csv
import datetime as dt
import functools
import json
import math
import os
import threading
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
EXTRA = DATA / "extra"
SOURCES = DATA / "sources.json"
BASIC_CSV = EXTRA / "daily_basic.csv"
# 历史估值累积：每次拉 daily_basic 都按交易日追加，训练端做市值中性化时用。
# 只存总市值（亿元），单日一行一只；重复拉同一日期会覆盖该日旧值。
MV_HISTORY = EXTRA / "mv_history.csv"
HIST_FIELDS = ["trade_date", "code", "total_mv"]
API = "https://api.tushare.pro"
# 估值接口限频 1 次/小时：失败或「源站没给新数据」之后，这段时间内不再重复请求
RETRY_GAP_MIN = 65
BASIC_FIELDS = ["code", "trade_date", "close", "pe", "pe_ttm", "pb",
                "total_mv", "circ_mv", "turnover_rate", "volume_ratio", "name"]


def _read() -> dict:
    if not SOURCES.exists():
        return {}
    try:
        return json.loads(SOURCES.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write(cfg: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    # tmp 名带 pid：定时线程与手动请求可能同时写，固定名会互相截断
    tmp = SOURCES.with_name("sources.%d.json.tmp" % os.getpid())
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SOURCES)


def _ts() -> dict:
    return (_read().get("tushare") or {})


def token() -> str:
    t = _ts()
    return str(t.get("token") or "").strip() if t.get("enabled", True) else ""


def configured() -> bool:
    return bool(token())


def save(token_value: str | None = None, enabled: bool | None = None) -> dict:
    cfg = _read()
    t = dict(cfg.get("tushare") or {})
    if token_value is not None:
        tv = str(token_value).strip()
        if tv:
            t["token"] = tv
    if enabled is not None:
        t["enabled"] = bool(enabled)
    t.setdefault("enabled", True)
    cfg["tushare"] = t
    _write(cfg)
    return status()


def clear() -> dict:
    cfg = _read()
    t = dict(cfg.get("tushare") or {})
    t["token"] = ""
    cfg["tushare"] = t
    _write(cfg)
    return status()


def _mask(tok: str) -> str:
    if not tok:
        return ""
    if len(tok) <= 10:
        return tok[:2] + "***"
    return tok[:4] + "*" * max(4, len(tok) - 8) + tok[-4:]


def _append_mv_history(date: str, rows: list[dict]) -> None:
    """把某交易日总市值追加进历史文件；同日重拉时先删该日旧值再写。"""
    date = str(date)[:8]
    keep: list[dict] = []
    if MV_HISTORY.exists():
        try:
            with MV_HISTORY.open("r", encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if str(r.get("trade_date") or "") != date:
                        keep.append(r)
        except Exception:
            keep = []
    cur = []
    for r in rows:
        code = str(r.get("ts_code") or "")[:6]
        try:
            mv = float(r.get("total_mv") or 0)
        except Exception:
            continue
        if len(code) != 6 or not math.isfinite(mv) or mv <= 0:
            continue
        cur.append({"trade_date": date, "code": code,
                    "total_mv": round(mv / 10000, 4)})   # 万元 → 亿元
    EXTRA.mkdir(parents=True, exist_ok=True)
    tmp = MV_HISTORY.with_name("mv_history.%d.csv.tmp" % os.getpid())
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_FIELDS)
        w.writeheader()
        for r in keep:
            if r.get("code") and r.get("trade_date"):
                w.writerow({k: r.get(k, "") for k in HIST_FIELDS})
        w.writerows(cur)
    tmp.replace(MV_HISTORY)


def load_mv_history() -> dict[str, dict[str, float]]:
    """交易日 → {代码: 总市值（亿元）}。训练端按「日期 + 代码」取，绝不跨期回填。"""
    out: dict[str, dict[str, float]] = {}
    if not MV_HISTORY.exists():
        return out
    try:
        with MV_HISTORY.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                day = str(r.get("trade_date") or "")[:8]
                code = str(r.get("code") or "").zfill(6)
                try:
                    mv = float(r.get("total_mv") or 0)
                except Exception:
                    continue
                if len(day) == 8 and len(code) == 6 and math.isfinite(mv) and mv > 0:
                    out.setdefault(day, {})[code] = mv
    except Exception:
        return {}
    return out


def mv_history_status() -> dict:
    days = load_mv_history()
    return {"days": len(days), "rows": sum(len(v) for v in days.values()),
            "from": min(days) if days else "", "to": max(days) if days else ""}


def call(api_name: str, params: dict | None = None, fields: str = "") -> list[dict]:
    """直连 Tushare Pro；失败抛 RuntimeError（带原始 msg，便于定位权限问题）。"""
    tok = token()
    if not tok:
        raise RuntimeError("未配置 Tushare Token")
    body = {"api_name": api_name, "token": tok, "params": params or {}, "fields": fields}
    r = requests.post(API, json=body, timeout=25,
                      headers={"Content-Type": "application/json"})
    r.raise_for_status()
    js = r.json()
    if js.get("code") != 0:
        raise RuntimeError(js.get("msg") or ("Tushare 返回错误码 %s" % js.get("code")))
    data = js.get("data") or {}
    cols = data.get("fields") or []
    items = data.get("items") or []
    return [dict(zip(cols, row)) for row in items]


def latest_trade_date(lookback: int = 15) -> str:
    """最近的交易日（YYYYMMDD）。

    优先读本地交易所日历（零请求）：低积分账号的 trade_cal 限频 1 次/小时，
    把它放在估值链路上会让「拉估值」整段随机失败，而失败是静默的 —— 页面照样
    显示一个有值的旧日期。本地日历不可用时才问 Tushare，并且取 max：
    接口返回顺序不保证升序，早先取 opened[-1] 拿到的是查询窗口首日
    （2026-11-20 那天拿到 11-05），估值就永久停在两周前，谁都不会发现。
    """
    end = dt.date.today()
    try:
        import pipeline
        # 先问「此刻应该已经入库的最新交易日」：17:45 之前今天的日线本来就没有，
        # 估值还更晚（19:30），直接拿今天去请求 daily_basic 只会拿到空数据，
        # 白烧一次 1 次/小时的配额。
        ready = pipeline._val_expected()
        if ready:
            # 日历过期时 _expected_day 会给出一个去年的日期，不能当真；
            # 只有「离今天 10 天以内」才认。
            if (end - dt.datetime.strptime(ready, "%Y-%m-%d").date()).days <= 10:
                return ready.replace("-", "")
        days = pipeline._market_days(end.strftime("%Y%m%d"))
        if days:
            last = days[-1]
            # 本地日历过期（只覆盖到去年）时不能拿它当「最新交易日」，
            # 否则会以为早就拉全了，时效检查跟着一起失真。
            if (end - dt.datetime.strptime(last, "%Y%m%d").date()).days <= 10:
                return last
    except Exception:
        pass
    start = end - dt.timedelta(days=lookback)
    try:
        rows = call("trade_cal", {"exchange": "SSE",
                                  "start_date": start.strftime("%Y%m%d"),
                                  "end_date": end.strftime("%Y%m%d")}, "cal_date,is_open")
    except Exception:
        # 限频 / 断网：退回本地行情数据日。它至少是「确实有 K 线的一天」，
        # 比拿今天去请求一个不存在的交易日强。
        try:
            import store
            return store.data_day().replace("-", "")
        except Exception:
            return ""
    opened = [str(r["cal_date"]) for r in rows if str(r.get("is_open")) == "1"
              and str(r["cal_date"]) <= end.strftime("%Y%m%d")]
    return max(opened) if opened else end.strftime("%Y%m%d")


def _mark_try():
    """记一次「尝试拉取」的时间（不影响 last_date / last_fetch 两个事实字段）。"""
    try:
        cfg = _read()
        t = dict(cfg.get("tushare") or {})
        t["last_try"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cfg["tushare"] = t
        _write(cfg)
    except Exception:
        pass


def _prev_trade_day(day: str) -> str:
    """上一个交易日（YYYYMMDD）：本地日历优先，取不到退回「减一天」。"""
    d = str(day or "").replace("-", "")
    if not d:
        return ""
    try:
        import pipeline
        days = [x for x in pipeline._market_days(d) if x < d]
        if days:
            return days[-1]
    except Exception:
        pass
    try:
        return (dt.datetime.strptime(d, "%Y%m%d").date()
                - dt.timedelta(days=1)).strftime("%Y%m%d")
    except Exception:
        return ""


def test() -> dict:
    if not token():
        return {"ok": False, "error": "未配置 Token：留空也能用全部免登录功能"}
    try:
        rows = call("stock_basic", {"list_status": "L", "limit": 3}, "ts_code,name,industry")
        return {"ok": True, "rows": len(rows), "sample": rows[:3],
                "message": "连接正常，stock_basic 可读（%d 条样例）" % len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:220]}


_FETCH_LOCK = threading.Lock()


def _single_flight(fn):
    """同一时刻只允许一个估值抓取：定时任务与手动点「取估值」会撞在一起。"""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not _FETCH_LOCK.acquire(blocking=False):
            return {"ok": False, "error": "估值抓取已在运行，等它结束再试",
                    "busy": True}
        try:
            return fn(*a, **kw)
        finally:
            _FETCH_LOCK.release()
    return wrapper


@_single_flight
def fetch_daily_basic(trade_date: str | None = None, force: bool = False) -> dict:
    """取某个交易日的全市场估值（多源按顺序降级，编排在 valsrc.py 里）。

    以前这里直接调 Tushare daily_basic：没 Token、被限频、或源站还没更新，就只能继续用旧值。
    现在交给 valsrc 排序挑源——东财 / 腾讯收盘即有数据，Tushare 负责按历史日回补，谁先把
    「应到日」填上就用谁。本函数只负责落盘：统一成 Tushare 的字段与单位（市值万元），
    并把当日总市值继续累积到 mv_history.csv。
    """
    import valsrc
    day = str(trade_date or "").replace("-", "")[:8]
    r = valsrc.fetch(day or None, force=force)
    rows = r.pop("rows", []) or []
    if not r.get("ok"):
        return r
    if not rows:
        # 全部源都不可用但本地已有旧值：如实返回 stale，不要用空数据覆盖现有文件
        return r
    if float(r.get("cover") or 0) < 0.5:
        # 只拿到小半个市场（单只限流、分页中断）：宁可继续用旧文件，
        # 也不能让 900 只的半份数据把 5500 只的全市场估值覆盖掉。
        r["stale"] = True
        r["rows"] = []
        r["message"] = "%s；本次覆盖率仅 %.0f%%，未覆盖现有文件" % (
            r.get("message") or "取到部分数据", float(r.get("cover") or 0) * 100)
        return r
    date = str(r.get("date") or "")
    EXTRA.mkdir(parents=True, exist_ok=True)
    tmp = BASIC_CSV.with_name("daily_basic.%d.csv.tmp" % os.getpid())
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=BASIC_FIELDS)
        w.writeheader()
        for x in rows:
            code = str(x.get("ts_code") or "")[:6]
            if len(code) != 6:
                continue
            rec = {k: x.get(k) for k in BASIC_FIELDS}
            rec["code"] = code
            rec["trade_date"] = x.get("trade_date") or date
            rec["name"] = x.get("name") or ""
            # 兜底：只有 TTM 市盈率的源（腾讯）不补这一下，pe 会整列为空，
            # 下游 PE 因子、单股 PE、同行 PE 分位全部失效而 PB 看起来正常。
            if rec.get("pe") in (None, "") and rec.get("pe_ttm") not in (None, ""):
                rec["pe"] = rec["pe_ttm"]
            w.writerow({k: ("" if rec.get(k) is None else rec[k]) for k in BASIC_FIELDS})
    tmp.replace(BASIC_CSV)
    try:
        _append_mv_history(date, rows)
    except Exception:
        # 历史累积失败不能拖垮当期估值：当期 CSV 已写好，训练端会提示覆盖不足。
        pass
    cfg = _read()
    t = dict(cfg.get("tushare") or {})
    now_s = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t.update({"last_fetch": now_s, "last_date": date, "rows": len(rows), "last_try": now_s,
              "last_source": r.get("source") or ""})
    cfg["tushare"] = t
    _write(cfg)
    return r


_BASIC_CACHE: dict = {"key": None, "v": None}


def load_basic() -> dict[str, dict]:
    """代码 → 估值字段。total_mv / circ_mv 单位为亿元（Tushare 原始为万元）。

    按文件 mtime+size 缓存：这个函数被单股分析、实盘体检、组合权重反复调用，
    每次解析 5600 行 CSV 约 30ms。写入端用原子替换，mtime 一变缓存自然失效。
    """
    if not BASIC_CSV.exists():
        return {}
    try:
        st = BASIC_CSV.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _BASIC_CACHE.get("key") == key:
        return _BASIC_CACHE["v"]
    out: dict[str, dict] = {}
    try:
        with BASIC_CSV.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                code = str(r.get("code") or "").zfill(6)
                if len(code) != 6:
                    continue

                def _f(k):
                    try:
                        v = float(r.get(k))
                        return round(v, 2) if math.isfinite(v) else ""
                    except Exception:
                        return ""
                mv = _f("total_mv")
                cmv = _f("circ_mv")
                out[code] = {
                    "trade_date": r.get("trade_date") or "",
                    "pe": _f("pe"), "pe_ttm": _f("pe_ttm"), "pb": _f("pb"),
                    "total_mv": round(mv / 10000, 2) if isinstance(mv, float) else "",
                    "circ_mv": round(cmv / 10000, 2) if isinstance(cmv, float) else "",
                    "turnover_rate": _f("turnover_rate"),
                    "volume_ratio": _f("volume_ratio"),
                }
    except Exception:
        return {}
    if key is not None:
        _BASIC_CACHE.update(key=key, v=out)
    return out


def status() -> dict:
    """账号与估值来源现状：Tushare 凭据 + 多源编排状态，合成一份给数据中枢用。"""
    import valsrc
    t = _ts()
    rows = 0
    last_date = str(t.get("last_date") or "")
    if BASIC_CSV.exists():
        try:
            with BASIC_CSV.open("r", encoding="utf-8-sig", newline="") as f:
                for i, r in enumerate(csv.DictReader(f)):
                    rows = i + 1
                    last_date = last_date or (r.get("trade_date") or "")
        except Exception:
            pass
    v = valsrc.status()
    if v.get("last_date"):
        last_date = v["last_date"]
    expected = v.get("expected") or ""
    try:
        import pipeline
        lag = pipeline._trade_lag(last_date, expected) if expected else None
    except Exception:
        lag = v.get("lag")
    on = [x["name"] for x in v.get("sources") or [] if x["enabled"] and x["role"] == "main"]
    return {"ok": True, "configured": bool(t.get("token")),
            "enabled": bool(t.get("enabled", True)),
            "token_masked": _mask(str(t.get("token") or "")),
            "last_fetch": t.get("last_fetch", ""), "last_date": last_date,
            "last_source": str(t.get("last_source") or ""),
            "expected": expected, "lag": lag, "stale": bool((lag or 0) > 0),
            "rows": rows, "file": str(BASIC_CSV),
            "mv_history": mv_history_status(),
            "valuation": v,
            "available": on,
            "note": "估值按「东财 → 腾讯 → Tushare」顺序降级，可在上面逐个开关；"
                    "Token 仅存本机 data/sources.json"}
