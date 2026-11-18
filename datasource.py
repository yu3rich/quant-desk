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
import json
import math
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
EXTRA = DATA / "extra"
SOURCES = DATA / "sources.json"
BASIC_CSV = EXTRA / "daily_basic.csv"
API = "https://api.tushare.pro"
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
    tmp = SOURCES.with_suffix(".json.tmp")
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
    """最近的交易日（YYYYMMDD）；用 trade_cal 判断，避免周末/节假日拉到空数据。"""
    end = dt.date.today()
    start = end - dt.timedelta(days=lookback)
    rows = call("trade_cal", {"exchange": "SSE",
                              "start_date": start.strftime("%Y%m%d"),
                              "end_date": end.strftime("%Y%m%d")}, "cal_date,is_open")
    opened = [r["cal_date"] for r in rows if str(r.get("is_open")) == "1"
              and str(r["cal_date"]) <= end.strftime("%Y%m%d")]
    return opened[-1] if opened else end.strftime("%Y%m%d")


def test() -> dict:
    if not token():
        return {"ok": False, "error": "未配置 Token：留空也能用全部免登录功能"}
    try:
        rows = call("stock_basic", {"list_status": "L", "limit": 3}, "ts_code,name,industry")
        return {"ok": True, "rows": len(rows), "sample": rows[:3],
                "message": "连接正常，stock_basic 可读（%d 条样例）" % len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:220]}


def fetch_daily_basic(trade_date: str | None = None) -> dict:
    """拉某个交易日的全市场估值。daily_basic 按 trade_date 一次返回全市场，1 次请求即可。"""
    if not token():
        return {"ok": False, "error": "未配置 Token；估值数据需要 Tushare 权限"}
    try:
        date = str(trade_date or "").replace("-", "") or latest_trade_date()
        rows = call("daily_basic", {"trade_date": date},
                    "ts_code,trade_date,close,pe,pe_ttm,pb,total_mv,circ_mv,"
                    "turnover_rate,volume_ratio")
        if not rows:
            return {"ok": False, "error": "%s 无估值数据（可能非交易日，或账号无 daily_basic 权限）" % date}
        EXTRA.mkdir(parents=True, exist_ok=True)
        tmp = BASIC_CSV.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=BASIC_FIELDS)
            w.writeheader()
            for r in rows:
                code = str(r.get("ts_code") or "")[:6]
                if len(code) != 6:
                    continue
                w.writerow({
                    "code": code, "trade_date": r.get("trade_date") or date,
                    "close": r.get("close") or "", "pe": r.get("pe") or "",
                    "pe_ttm": r.get("pe_ttm") or "", "pb": r.get("pb") or "",
                    "total_mv": r.get("total_mv") or "", "circ_mv": r.get("circ_mv") or "",
                    "turnover_rate": r.get("turnover_rate") or "",
                    "volume_ratio": r.get("volume_ratio") or "", "name": "",
                })
        tmp.replace(BASIC_CSV)
        cfg = _read()
        t = dict(cfg.get("tushare") or {})
        t.update({"last_fetch": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "last_date": date, "rows": len(rows)})
        cfg["tushare"] = t
        _write(cfg)
        return {"ok": True, "trade_date": date, "count": len(rows),
                "message": "已拉取 %s 全市场估值 %d 只（PE/PB/市值/换手/量比）" % (date, len(rows))}
    except Exception as e:
        return {"ok": False, "error": str(e)[:220]}


def load_basic() -> dict[str, dict]:
    """代码 → 估值字段。total_mv / circ_mv 单位为亿元（Tushare 原始为万元）。"""
    if not BASIC_CSV.exists():
        return {}
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
    return out


def status() -> dict:
    t = _ts()
    rows = 0
    last_date = t.get("last_date") or ""
    if BASIC_CSV.exists():
        try:
            with BASIC_CSV.open("r", encoding="utf-8-sig", newline="") as f:
                for i, r in enumerate(csv.DictReader(f)):
                    rows = i + 1
                    last_date = last_date or (r.get("trade_date") or "")
        except Exception:
            pass
    return {"ok": True, "configured": bool(t.get("token")),
            "enabled": bool(t.get("enabled", True)),
            "token_masked": _mask(str(t.get("token") or "")),
            "last_fetch": t.get("last_fetch", ""), "last_date": last_date,
            "rows": rows, "file": str(BASIC_CSV),
            "available": ["daily_basic（PE/PB/市值/换手/量比）"],
            "note": "Token 仅存本机 data/sources.json；权限取决于你的 Tushare 积分"}
