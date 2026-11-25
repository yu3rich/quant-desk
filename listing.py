# -*- coding: utf-8 -*-
"""
QuantDesk · point-in-time 上市名单（as-of universe）

为什么需要它：
  旧口径的股票池是「今天还在交易的全 A」，把历史上退市 / 被借壳的票整体排除在外。
  用它回放历史（review.scorecard）等于只挑活到今天的样本，回放超额会系统性偏乐观
  —— 这就是幸存者偏差。vnpy 用「指数成分历史 + 成分起止区间」解决同一件事，
  这里用交易所级别的上市/退市日期做到同一目的，且不依赖指数成分（A 股指数成分
  历史不容易免费拿全）。

数据来源：baostock `query_stock_basic`（一次请求返回全量 9000+ 行，无需登录额度）
  字段：code / code_name / ipoDate / outDate / type / status
  type='1' 为股票，status='1' 在市、'0' 退市；outDate 为空表示尚未退市。

对外：
  sync(force)            刷新名单（当日缓存，force 强制）
  info(code)             单只的 ipo/out/status
  alive(code, date)      某只票在 date 当天是否在市（ipo <= date < out）
  as_of(date)            某一天在市的全集
  delisted()             已退市清单
  coverage()             退市股里有多少只本地有行情（决定回放能否真的纳入）
  prewarm_delisted()     后台补退市股行情
  window(codes)          批量给出 (ipo, out)，供回放做向量化过滤

纪律：这里的日期一律是交易所口径的自然日；比较时用 ISO 字符串，避免时区问题。
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import threading
import time
from pathlib import Path

from paths import BASE  # 打包后 = exe 目录；开发时 = 项目根
DATA = BASE / "data"
DATA.mkdir(parents=True, exist_ok=True)
LISTING_PATH = DATA / "listing.csv"
FIELDS = ["code", "name", "ipo_date", "out_date", "status", "type", "market", "updated"]

_LOCK = threading.Lock()
_CACHE: dict = {"key": None, "rows": {}}          # 文件 mtime → 解析结果
_SYNC = {"running": False, "message": "", "at": ""}
_PREWARM = {"running": False, "done": 0, "total": 0, "ok": 0, "fail": 0,
            "last": "", "message": "未开始", "stop": False}
_PRE_LOCK = threading.Lock()


def _today() -> str:
    return dt.date.today().isoformat()


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------

def _read_raw() -> list[dict]:
    if not LISTING_PATH.exists():
        return []
    try:
        with LISTING_PATH.open("r", encoding="utf-8-sig", newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("code")]
    except Exception:
        return []


def _write_raw(rows: list[dict]):
    tmp = LISTING_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    tmp.replace(LISTING_PATH)


def _rows() -> dict[str, dict]:
    """code → 行；按文件 mtime 缓存，回放时要按日期查上万次，不能每次都读盘。"""
    try:
        mt = LISTING_PATH.stat().st_mtime if LISTING_PATH.exists() else 0
    except Exception:
        mt = 0
    if _CACHE["key"] == mt and _CACHE["rows"]:
        return _CACHE["rows"]
    out = {}
    for r in _read_raw():
        c = str(r.get("code") or "").zfill(6)
        out[c] = r
    _CACHE.update(key=mt, rows=out)
    return out


def info(code: str) -> dict:
    return _rows().get(str(code).zfill(6)) or {}


def _canon_date(v) -> str:
    s = str(v or "").strip()[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    if len(s) == 8 and s.isdigit():                       # 20240131
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    return ""


# --------------------------------------------------------------------------
# 同步
# --------------------------------------------------------------------------

def status() -> dict:
    rows = _rows()
    listed = sum(1 for r in rows.values() if str(r.get("status")) == "1")
    out = sorted((r for r in rows.values() if str(r.get("status")) != "1"),
                 key=lambda r: str(r.get("out_date") or ""))
    upd = ""
    try:
        upd = str(next(iter(rows.values())).get("updated") or "") if rows else ""
    except Exception:
        pass
    return {"ok": True, "total": len(rows), "listed": listed,
            "delisted": len(out), "updated": upd,
            "fresh": bool(upd == _today()),
            "file": str(LISTING_PATH),
            "sync": {k: v for k, v in _SYNC.items()},
            "prewarm": {k: v for k, v in _PREWARM.items() if k != "stop"}}


def _fetch_baostock() -> list[dict]:
    """一次请求拿全量名单。baostock 的 query_stock_basic 不传 code 时返回所有证券。"""
    import universe
    import baostock as bs
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError("baostock 登录失败：%s" % lg.error_msg)
    try:
        rs = bs.query_stock_basic()
        if rs.error_code != "0":
            raise RuntimeError("query_stock_basic 失败：%s" % rs.error_msg)
        got = []
        while rs.next():
            r = rs.get_row_data()
            if len(r) < 6:
                continue
            full, name, ipo, out, typ, st = r[0], r[1], r[2], r[3], r[4], r[5]
            if str(typ) != "1":                    # 只要股票：剔除指数/ETF/可转债
                continue
            code = str(full).split(".")[-1]
            if len(code) != 6 or not code.isdigit():
                continue
            mk = universe.market_of(code)
            if mk == "其他":                        # B 股等不在研究范围内
                continue
            got.append({"code": code, "name": str(name or code).strip().replace(" ", ""),
                        "ipo_date": _canon_date(ipo), "out_date": _canon_date(out),
                        "status": "1" if str(st) == "1" else "0",
                        "type": str(typ), "market": mk, "updated": _today()})
        return got
    finally:
        with contextlib.redirect_stdout(buf):
            bs.logout()


def sync(force: bool = False) -> dict:
    """刷新上市名单。当日已同步且非 force 时直接返回缓存。"""
    st = status()
    if not force and st["fresh"] and st["total"] > 3000:
        return {**st, "skipped": True, "message": "今日已同步"}
    with _LOCK:
        _SYNC.update(running=True, message="正在拉取上市名单…")
        try:
            rows = _fetch_baostock()
            if len(rows) < 3000:
                _SYNC.update(running=False, message="名单不完整（%d 行），保留旧文件" % len(rows))
                return {**status(), "ok": False,
                        "error": "名单不完整（仅 %d 行），已保留旧文件" % len(rows)}
            # 手工修正优先：本地已有的 out_date 若与 baostock 不同，以已有非空值为准
            # （baostock 偶有延迟，退市当天仍返回 status=1），避免「退市了还当在市」。
            old = _rows()
            for r in rows:
                o = old.get(r["code"]) or {}
                if not r["out_date"] and o.get("out_date"):
                    r["out_date"] = o["out_date"]
                    r["status"] = "0"
            rows.sort(key=lambda x: x["code"])
            _write_raw(rows)
            _CACHE.update(key=None, rows={})
            n_out = sum(1 for r in rows if r["status"] != "1")
            _SYNC.update(running=False, at=time.strftime("%Y-%m-%d %H:%M:%S"),
                         message="完成：在市 %d 只，退市 %d 只" % (len(rows) - n_out, n_out))
            return {**status(), "ok": True,
                    "message": "已同步 %d 只（退市 %d 只）" % (len(rows), n_out)}
        except Exception as e:                     # noqa: BLE001
            _SYNC.update(running=False, message="失败：%s" % str(e)[:160])
            return {**status(), "ok": False, "error": str(e)[:200]}


def sync_async(force: bool = True) -> dict:
    if _SYNC.get("running"):
        return {"ok": True, "started": False, "message": "同步正在进行中"}
    threading.Thread(target=sync, kwargs={"force": force}, daemon=True,
                     name="listing-sync").start()
    return {"ok": True, "started": True, "message": "后台同步已启动"}


# --------------------------------------------------------------------------
# as-of 查询
# --------------------------------------------------------------------------

def alive(code: str, date: str) -> bool:
    """某只票在 date 当天是否在市。名单里没有的代码一律放行（未知不误杀）。"""
    r = info(code)
    if not r:
        return True
    d = str(date)[:10]
    ipo = str(r.get("ipo_date") or "")[:10]
    out = str(r.get("out_date") or "")[:10]
    if ipo and d < ipo:
        return False
    if out and d >= out:
        return False
    return True


def as_of(date: str) -> set[str]:
    d = str(date)[:10]
    return {c for c in _rows() if alive(c, d)}


def window(code: str) -> tuple[str, str]:
    """(上市日, 退市日)；未退市的 out 为空串。"""
    r = info(code)
    return str(r.get("ipo_date") or "")[:10], str(r.get("out_date") or "")[:10]


def windows(codes: list[str]) -> dict[str, tuple[str, str]]:
    return {c: window(c) for c in codes}


def delisted() -> list[dict]:
    rows = [r for r in _rows().values() if str(r.get("status")) != "1"]
    rows.sort(key=lambda r: str(r.get("out_date") or ""), reverse=True)
    return rows


def coverage() -> dict:
    """退市股里有多少只本地有行情 —— 回放能不能真的把它们算进来，取决于这个数。"""
    import store
    cached = set(store.cached_codes())
    out = delisted()
    have = [r for r in out if r["code"] in cached]
    miss = [r for r in out if r["code"] not in cached]
    recent = [r for r in out
              if str(r.get("out_date") or "") >=
              (dt.date.today() - dt.timedelta(days=730)).isoformat()]
    recent_have = [r for r in recent if r["code"] in cached]
    return {"ok": True, "total": len(out), "cached": len(have),
            "missing": len(miss), "cover": round(len(have) / len(out), 4) if out else None,
            "recent_2y": len(recent), "recent_cached": len(recent_have),
            "recent_missing": len(recent) - len(recent_have),
            "missing_sample": [{"code": r["code"], "name": r.get("name"),
                                "out_date": r.get("out_date")} for r in miss[:30]]}


# --------------------------------------------------------------------------
# 退市股行情补全（串行、可停）
# --------------------------------------------------------------------------

def prewarm_status() -> dict:
    return {"ok": True, **{k: v for k, v in _PREWARM.items() if k != "stop"},
            "coverage": coverage()}


def _prewarm_worker(codes: list[str]):
    import bulkdata
    import contextlib as _ctx
    import io as _io
    import store
    cfg = bulkdata._cfg()
    raw = str(cfg.get("cache_start", "20180101")).replace("-", "")
    cache_start = "%s-%s-%s" % (raw[:4], raw[4:6], raw[6:8])
    end = _today()
    _PREWARM.update(running=True, done=0, total=len(codes), ok=0, fail=0,
                    last="", stop=False, message="正在补退市股行情")
    buf = _io.StringIO()
    bs = None
    try:
        import baostock
        with _ctx.redirect_stdout(buf):
            lg = baostock.login()
        if lg.error_code != "0":
            _PREWARM.update(message="baostock 登录失败：%s" % lg.error_msg)
            return
        bs = baostock
        for c in codes:
            if _PREWARM["stop"]:
                _PREWARM.update(message="已手动停止")
                break
            try:
                res, n, err = bulkdata._process_hs(bs, c, "full", cache_start, end)
                if res == "ok":
                    _PREWARM["ok"] += 1
                else:
                    _PREWARM["fail"] += 1
            except Exception:
                _PREWARM["fail"] += 1
            _PREWARM["done"] += 1
            _PREWARM["last"] = c
            time.sleep(0.12)
        else:
            _PREWARM.update(message="本批完成")
    finally:
        if bs is not None:
            with _ctx.redirect_stdout(buf):
                bs.logout()
        _PREWARM["running"] = False
        try:
            store.log("listing", "退市股行情补全结束：成功 %d / 失败 %d / 共 %d" %
                      (_PREWARM["ok"], _PREWARM["fail"], _PREWARM["total"]),
                      level="info", act="prewarm")
        except Exception:
            pass


def prewarm_delisted(limit: int = 0) -> dict:
    """给缺行情的退市股补历史 K 线。没有它们，as-of 过滤只是一句口号。"""
    if _PREWARM["running"]:
        return {**prewarm_status(), "message": "补全正在进行中"}
    import store
    cached = set(store.cached_codes())
    todo = [r["code"] for r in delisted() if r["code"] not in cached]
    # 优先补最近两年退市的：老票（2009 年退市）对当前回放窗口没有意义
    cut = (dt.date.today() - dt.timedelta(days=730)).isoformat()
    recent = [c for c in todo if str(info(c).get("out_date") or "") >= cut]
    old = [c for c in todo if c not in set(recent)]
    todo = recent + old
    if limit:
        todo = todo[:int(limit)]
    if not todo:
        return {**prewarm_status(), "started": 0,
                "message": "退市股行情已全部补齐" if not delisted() else "没有待补的退市股"}
    if store.cached_count() and _PREWARM.get("total") is None:
        pass
    threading.Thread(target=_prewarm_worker, args=(todo,), daemon=True,
                     name="listing-prewarm").start()
    return {"ok": True, "started": len(todo),
            "message": "后台补全已启动：%d 只（先补近两年退市的）" % len(todo)}


def prewarm_stop() -> dict:
    _PREWARM["stop"] = True
    return {"ok": True, "message": "已发送停止信号，当前这只跑完就停"}
