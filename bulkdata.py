# -*- coding: utf-8 -*-
"""
bulkdata.py — 全市场行情批量下载 / 增量更新（数据层地基）

设计（参考 Sequoia-X 等成熟项目的标准姿势）：
  - 沪深：baostock 后复权日线（免费、免注册、稳定、按 IP 每日约 5 万次调用）
  - 北交所：baostock 不覆盖，回退到 quant 的腾讯通道（qfq）
  - 「首次全量、日常增量」：增量只补 last_date+1 之后的交易日，不重拉历史
  - 每个文件独立复权口径：沪深=hfq，北交所=qfq。因子全是比率/收益率，
    跨文件口径差异不影响横截面（同文件内部必须一致，否则拼接处会出现假跳空）
  - 进度 / 停止 / 断点续传：失败不会中断整批，下次自动只补缺失

对外：
  list_hs()                       沪深全名单（baostock）
  start(mode, limit, scope)       启动后台任务（full / incremental）
  stop() / status()               停止 / 进度
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
CONFIG = BASE / "config.json"
UNIVERSE = DATA / "universe.csv"
# 最近一次行情任务用的数据源（降级/限流提示要跨进程重启保留）
SOURCE_JSON = DATA / "quote_source.json"

COLS = ["date", "open", "high", "low", "close", "volume", "amount", "pct", "turnover"]

_STATE = {
    "running": False, "mode": "", "done": 0, "total": 0, "ok": 0, "fail": 0,
    "skip": 0, "rows": 0, "last": "", "last_err": "", "stop": False,
    "message": "空闲", "started_at": 0.0, "elapsed": 0.0, "workers": 1,
    "retrying": False, "last_err_code": "",
    # source: 本次实际用的数据源（baostock / spot）；error: 降级也失败时的致命原因
    # topup: 数据源还没出当天日线时，用快照补的那一根（{day, updated, fail, left}）
    "source": "", "error": "", "topup": {}, "blocked": False,
    # fresh: 本轮报失败、但对账后确认「缓存里已有当日 K 线」的只数（不是本次下载的）
    "fresh": 0,
}

# 并行任务中失败的代码，结束后统一串行重试一次（高并发时 baostock 会互踢会话）
_FAILED: list[str] = []
# 新股/长期停牌导致日线不足 20 根：属于「本来就没有」，不参与重试
_EMPTY: list[str] = []


# --------------------------------------------------------------------------
# 基础：代码归属 / 复权口径 / 读写
# --------------------------------------------------------------------------

def market_kind(code: str) -> str:
    """返回 sh / sz / bj / ''（空=非 A 股或 B 股）。"""
    c = str(code).zfill(6)
    if c.startswith(("900", "200")):          # B 股，排除
        return ""
    if c.startswith(("60", "68")):
        return "sh"
    if c.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz"
    if c.startswith(("43", "83", "87", "92")) or c[0] == "8":
        return "bj"
    return ""


def is_stock_code(code: str, exchange: str | None = None) -> bool:
    """是否 A 股个股。给了 exchange（sh/sz/bj）就按交易所前缀严格判定 ——
    这样才能区分同一 6 位码的指数与股票：sh.000300 是沪深300指数、sz.000001 是平安银行，
    只看代码会把两者混为一谈。"""
    c = str(code).zfill(6)
    ex = str(exchange or "").lower()
    if c.startswith(("900", "200")):          # B 股
        return False
    if ex == "sh":
        return c.startswith(("60", "68"))      # 沪市个股：主板 60x / 科创板 68x
    if ex == "sz":
        return c.startswith(("000", "001", "002", "003", "300", "301"))
    if ex == "bj":
        return c.startswith(("43", "83", "87", "92")) or c[0] == "8"
    return market_kind(c) != ""


def to_bs_code(code: str) -> str | None:
    """baostock 代码（sh.600000 / sz.000001）；北交所返回 None（走腾讯）。"""
    k = market_kind(code)
    c = str(code).zfill(6)
    return f"{k}.{c}" if k in ("sh", "sz") else None


# --------------------------------------------------------------------------
# 「离线兜底」识别
# --------------------------------------------------------------------------
# quant.fetch_history() 在三条取数通道（akshare / 东财直连 / 腾讯）全失败时，
# 会退回本地旧缓存，并返回 source="本地缓存（离线）"。它的返回值和正常成功
# 完全一致，只有这个字符串能区分。
#
# 为什么必须单独判：把旧数据当成功写回 bar_meta，updated 会变成今天、last_date
# 原地不动 —— 覆盖率永远补不齐、待办一直挂着，而运行记录里显示「成功」。
# 「没拿到新数据」不等于「更新成功」，这里如实报失败，让它进重试与快照补数的队列。
OFFLINE_SRC = "本地缓存（离线）"
# 离线兜底记为 fail（会被重试，并进 _FAILED 交给快照补当日 K 线）；
# 不用 "skip"，因为 skip 不重试 —— 而这类票恰恰是最需要重试的。
OFFLINE_ST = "fail"


def is_offline_source(src) -> bool:
    """这条数据是不是「取数全失败、退回本地旧缓存」拿到的。"""
    return OFFLINE_SRC in str(src or "")


def _last_day_of(d) -> str:
    """DataFrame 末根 K 线的日期，用于在报错里说清「停在几号」。"""
    try:
        if d is None or len(d) == 0:
            return "—"
        return str(d["date"].iloc[-1])[:10]
    except Exception:
        return "—"


def _target_day(today: str) -> str:
    """这批增量「应该跟到」的交易日。

    baostock 的日线大约 17:30 才入库，东财/腾讯快照则收盘（15:05）后才有定盘数据；
    所以盘中跑增量时，「今天」并不是这批数据的目标 —— 目标是有数据的那个最近交易日。
    pipeline._expected_day() 已经把「交易所日历 + 源站入库时刻」这套口径算过了，
    这里直接复用，避免两处各写一套「今天算不算新」（这正是 README 里点名的坑）。
    取不到就退回 today，宁可严格一点也不放松。
    """
    try:
        import pipeline
        exp = str(pipeline._expected_day() or "")[:10]
        if exp:
            return min(exp, today)
    except Exception:
        pass
    return today


def _today() -> str:
    return dt.date.today().isoformat()


def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    except Exception:
        return {}


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["date"] = d["date"].astype(str).str.slice(0, 10)
    for c in COLS:
        if c not in d.columns:
            d[c] = 0.0
        if c != "date":
            d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[COLS].dropna(subset=["open", "high", "low", "close"])
    d = d[(d["close"] > 0) & (d["open"] > 0)]
    if d.empty:
        return d
    return d.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _read_meta(code: str) -> dict:
    import store
    return store.meta_of(code)


def _read_csv(code: str) -> pd.DataFrame | None:
    import store
    d = store.bars_frame(code)
    if d is None:
        return None
    d = _norm(d)
    return d if len(d) else None


def _write(code: str, df: pd.DataFrame, source: str, basis: str, start: str | None = None):
    """整只事务覆盖写（SQLite 原子提交，快照/回测不会读到半截数据）。

    start 记录「这份缓存是按哪个起始日建的全量」，下次全量补全就能判断是否可以
    只追加尾部（新股上市晚，首根 K 线不等于 cache_start，只能靠这个字段判断）。
    """
    import store
    store.bars_write(code, df[COLS], source, basis, start)


# --------------------------------------------------------------------------
# baostock 取数
# --------------------------------------------------------------------------

def _last_trade_day(bs, lookback: int = 20) -> str:
    """从今天往回找最近一个有名单的交易日（周末/节假日返回空）。"""
    d = dt.date.today()
    for _ in range(lookback):
        rs = bs.query_all_stock(day=d.isoformat())
        if rs.error_code == "0" and rs.next():
            return d.isoformat()
        d -= dt.timedelta(days=1)
    return dt.date.today().isoformat()


def list_hs() -> dict[str, str]:
    """沪深全 A 名单（只留个股：sh 60/68、sz 000/001/002/003/300/301）。

    baostock 的 query_all_stock 同时返回指数与个股，且同一 6 位码可能既是指数又是股票
    （如 sh.000001 上证指数 / sz.000001 平安银行）。必须用返回的交易所前缀判定，
    只看代码位数会把 sh.000xxx 这类上证指数当成深市个股混进来。
    """
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError("baostock 登录失败：" + lg.error_msg)
    try:
        rs = bs.query_all_stock(day=_last_trade_day(bs))
        out: dict[str, str] = {}
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            full, status, name = row[0], row[1], row[2]
            parts = full.split(".")
            if len(parts) != 2:
                continue
            ex, code = parts[0], parts[1]
            if len(code) == 6 and is_stock_code(code, ex):
                out[code] = (name or code).strip().replace(" ", "")
        return out
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def _bs_history(bs, bs_code: str, start: str, end: str) -> pd.DataFrame:
    rs = bs.query_history_k_data_plus(
        bs_code, "date,open,high,low,close,volume,amount,pctChg,turn",
        start_date=start, end_date=end, frequency="d", adjustflag="1")  # 1=后复权
    if rs.error_code != "0":
        raise RuntimeError(rs.error_msg)
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame(columns=COLS)
    d = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                    "volume", "amount", "pct", "turnover"])
    return _norm(d)


def _process_hs(bs, code: str, mode: str, cache_start: str, end: str):
    """处理一只沪深股票：已有全区间 hfq 缓存则只追加尾部，否则全量下载。

    全量模式也遵守「已覆盖就只补尾部」，这样「全量补全」可以随时中断再点，
    已完成的股票不会再从 2018 年重拉一遍（否则每次重跑都要多花一小时）。
    """
    old = _read_meta(code)
    olddf = _read_csv(code)
    covered = False
    if old.get("basis") == "hfq" and olddf is not None and len(olddf) > 20:
        first = str(olddf["date"].iloc[0])[:10]
        marked = str(old.get("start") or "")[:10]
        # 有 start 标记的按标记判断；老缓存没有标记则看首根 K 线是否贴近起始日
        if marked:
            covered = marked == cache_start
        else:
            try:
                drift = (dt.date.fromisoformat(first) -
                         dt.date.fromisoformat(cache_start)).days
            except Exception:
                drift = 9999
            covered = drift <= 20
    if covered:
        last = str(old.get("last_date") or olddf["date"].iloc[-1])[:10]
        start = (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat()
        if start > end:
            return "skip", 0, ""
        new = _bs_history(bs, to_bs_code(code), start, end)
        if new is None or new.empty:
            return "skip", 0, ""
        merged = _norm(pd.concat([olddf, new], ignore_index=True))
        _write(code, merged, "baostock", "hfq", cache_start)
        return "ok", len(new), ""
    df = _bs_history(bs, to_bs_code(code), cache_start, end)
    if df is None or len(df) <= 20:
        n = 0 if df is None else len(df)
        return "empty", 0, "有效日线仅 %d 行（<20，多为新股或长期停牌，未缓存）" % n
    _write(code, df, "baostock", "hfq", cache_start)
    return "ok", len(df), ""


def _process_bj(code: str):
    """北交所：baostock 无覆盖，走腾讯通道（qfq），全量下载。

    腾讯/东财的日线在盘中会带上「今天」那根还没收盘的 bar。以前只在收盘后跑
    增量，碰不到这事；现在漏跑补跑可能发生在盘中（比如上午打开工作台补昨天的），
    那根半截 bar 会让北交所整批股票的数据日比基准日新一天，横截面里被全部剔除。
    收盘（15:05）之前一律剔除当日 bar，等收盘后再由下一次全量写回完整的一天。
    """
    try:
        import quant
        d, src = quant.fetch_history(code)
        if d is None or len(d) <= 20:
            return "fail", 0, "北交所无数据"
        # fetch_history 在「三条取数通道全失败」时会退回本地旧缓存，但返回值和
        # 正常成功完全一样，只靠 source 区分。若把旧数据当成功写回：
        # bar_meta.updated 变成今天、last_date 原地不动，覆盖率永远补不齐，
        # 界面上还会显示「更新成功」。这种「没拿到新数据」必须如实报失败。
        if is_offline_source(src):
            return OFFLINE_ST, 0, "取数失败，仅回退本地旧缓存（%s）" % _last_day_of(d)
        ok_win, _why = spot_window()
        if not ok_win and "date" in d.columns:
            today = dt.date.today().isoformat()
            d = d[d["date"].astype(str).str.slice(0, 10) < today]
            if d is None or len(d) <= 20:
                return "fail", 0, "盘中数据已剔除，历史日线不足 20 根"
        _write(code, d, src or "腾讯行情", "qfq")
        return "ok", len(d), ""
    except Exception as e:
        return "fail", 0, str(e)[:80]


# --------------------------------------------------------------------------
# 数据源降级：baostock 连不上时，用当日实时快照补最后一根 K 线
# --------------------------------------------------------------------------
# 为什么需要：baostock 的取数端口（10030）偶尔整段拒连（login 直接报「网络接收错误」），
# 这时全市场增量 100% 失败，但当天行情并非取不到 —— 腾讯批量快照 4 秒就能拿到全市场的
# 开/高/低/收/量/额/换手/涨跌幅。用它把当日这一根 K 线按后复权口径合成出来，
# 流水线就不会因为一个数据源抽风而整天空转。
#
# 合成公式利用后复权的关键性质：**过去的价格永不回改，只有新增 bar 要乘当日复权因子**。
#   今日收盘(后复权) = 昨日收盘(后复权) × (1 + 当日涨跌幅)
# 用官方涨跌幅而不是「现价/昨收」，除权日也自动衔接（涨跌幅本身已剔除除权跳空）；
# 其余三价按当日 开/高/低 与收盘的比例缩放，量额换手直接用快照原值（手→股、万元→元）。
#
# 只在「缓存末根正好是上一交易日」时补，缺了中间交易日就跳过 —— 否则会在 CSV 里
# 留下一个空档，而后续 baostock 增量是从 last_date+1 开始的，永远填不回来。

SPOT_BAR_MINUTE = 15 * 60 + 5      # 15:05 之后快照才是完整的日线


def bs_probe(timeout: float = 8.0) -> dict:
    """baostock 探活：登录一次，顺带问一句「今天有没有数据」再登出。

    在拉起 5000 个子进程之前先判断两件事：
      1) 连不连得上（端口 10030 整段拒连时会直接报「网络接收错误」）；
      2) 连上了但今天的数据还没入库（日线约 17:30 才到），此时逐个查 5000 只纯属浪费时间。
    """
    box: dict = {}

    def _go():
        try:
            import baostock as bs
            lg = bs.login()
            good = lg.error_code == "0"
            box["ok"] = good
            box["msg"] = "" if good else (lg.error_msg or "登录失败")
            if not good:
                return
            try:
                today = dt.date.today().strftime("%Y-%m-%d")
                rs = bs.query_history_k_data_plus(
                    "sh.600000", "date,close", start_date=today, end_date=today,
                    frequency="d", adjustflag="1")
                box["has_day"] = bool(rs.error_code == "0" and rs.next())
            except Exception:
                box["has_day"] = False
            try:
                bs.logout()
            except Exception:
                pass
        except Exception as e:                     # noqa: BLE001
            box["ok"], box["msg"] = False, str(e)[:80]

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {"ok": False, "msg": "登录超时 %.0f 秒" % timeout, "has_day": False}
    return {"ok": bool(box.get("ok")), "msg": box.get("msg") or "未知错误",
            "has_day": bool(box.get("has_day"))}


def _cal_recent(n: int = 12) -> list[str]:
    """最近 n 个交易日（沿用 600000 当日历，与 pipeline 的判据保持一致）。"""
    try:
        import histcache
        d = histcache.ohlcv("600000", n)
        if d and d.get("date"):
            return [str(x)[:10] for x in d["date"]]
    except Exception:
        pass
    return []


def _spot_bar(code: str, row: dict, bar_day: str, prev_day: str):
    """把一只股票的当日快照合成为后复权 K 线并追加到缓存尾部。"""
    import histcache
    import store
    if not store.has_code(code):
        return "empty", 0, "无本地缓存，需全量下载"
    meta = _read_meta(code)
    if str(meta.get("basis") or "") != "hfq":
        return "skip", 0, "复权口径不是后复权，等全量刷新"
    last = str(meta.get("last_date") or "")[:10]
    if last == bar_day:
        return "skip", 0, ""
    if last != prev_day:
        return "skip", 0, "缓存末根 %s 不是上一交易日，缺中间数据需全量补齐" % (last or "—")

    tail = histcache.ohlcv(code, 2)
    if not tail or len(tail["c"]) == 0:
        return "skip", 0, "读不到缓存尾部"
    prev_close = float(tail["c"][-1])

    def _f(k):
        try:
            return float(row.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    px, pct = _f("price"), _f("pct")
    o, h, l = _f("open"), _f("high"), _f("low")
    vol, amt, turn = _f("volume"), _f("amount"), _f("turnover")
    if px <= 0 or o <= 0 or h <= 0 or l <= 0 or vol <= 0:
        return "skip", 0, "当日无成交或快照缺价"
    if abs(pct) > 100 or prev_close <= 0:
        return "skip", 0, "涨跌幅异常（%.2f%%）" % pct
    close = prev_close * (1 + pct / 100.0)
    if close <= 0:
        return "skip", 0, "合成收盘价异常"
    k = close / px
    bar = {
        "date": bar_day,
        "open": "%.6f" % (o * k), "high": "%.6f" % (h * k),
        "low": "%.6f" % (l * k), "close": "%.6f" % close,
        "volume": "%.0f" % (vol * 100), "amount": "%.2f" % (amt * 10000),
        "pct": "%.4f" % pct, "turnover": "%.4f" % turn,
    }
    try:
        store.bars_append(code, bar, {**meta, "updated": _today(), "last_date": bar_day,
                                      "basis": "hfq", "source": "腾讯快照补日线",
                                      "rows": int(meta.get("rows") or 0) + 1})
    except Exception as e:                        # noqa: BLE001
        return "fail", 0, "写入失败：" + str(e)[:50]
    return "ok", 1, ""


def spot_window() -> tuple[bool, str]:
    """快照能不能当完整日线用：周末可以（戳停在上一交易日），工作日要等 15:05。"""
    now = dt.datetime.now()
    if now.weekday() >= 5:
        return True, ""
    if now.hour * 60 + now.minute < SPOT_BAR_MINUTE:
        return False, "盘中（%s）不补日线，收盘 15:05 之后再更新" % now.strftime("%H:%M")
    return True, ""


def _codes_missing(codes: list[str], day: str) -> list[str]:
    """挑出缓存还没更新到 day 的代码（一条 SQL 取全部 meta，毫秒级）。"""
    import store
    mm = store.meta_map()
    return [c for c in codes if str((mm.get(c) or {}).get("last_date") or "")[:10] < day]


def _spot_topup(codes: list[str], tally: bool = True, note: str = "") -> dict:
    """降级路径：用当日实时快照给全市场补最后一根 K 线。"""
    import concurrent.futures as cf
    import spot

    okw, why = spot_window()
    if not okw:
        return {"ok": False, "error": why}
    r = spot.refresh(codes, merge=True)
    if not r.get("ok"):
        return {"ok": False, "error": "数据源不可用，实时快照也取不到：" + str(r.get("error"))}
    rows = spot.load()
    stamp = ""
    for x in rows.values():
        t = str(x.get("time") or "")
        if len(t) >= 8 and t[:8] > stamp:
            stamp = t[:8]
    if not stamp:
        return {"ok": False, "error": "实时快照没有时间戳"}
    bar_day = "%s-%s-%s" % (stamp[:4], stamp[4:6], stamp[6:8])
    prev_day = ""
    for d in reversed(_cal_recent(12)):
        if d < bar_day:
            prev_day = d
            break
    if not prev_day:
        return {"ok": False, "error": "没有交易日历，无法判断上一交易日"}

    _STATE["message"] = "%s，正在用当日快照补 K 线（%d 只）…" % (note or "补当日 K 线", len(codes))
    results = []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {}
        for c in codes:
            row = rows.get(c)
            if not row:
                results.append({"code": c, "st": "skip", "n": 0, "err": "快照里没有这只"})
                continue
            # merge=True 会保留未参与本次刷新的旧行；旧行的时间戳不是今天时绝不能
            # 当成今天这根 K 线写入，否则会把昨天的涨跌复制到今天。
            if str(row.get("time") or "")[:8] != stamp:
                results.append({"code": c, "st": "skip", "n": 0, "err": "快照不是当日"})
                continue
            futs[ex.submit(_spot_bar, c, row, bar_day, prev_day)] = c
        for fu in cf.as_completed(futs):
            c = futs[fu]
            try:
                st, n, err = fu.result()
            except Exception as e:                # noqa: BLE001
                st, n, err = "fail", 0, str(e)[:60]
            results.append({"code": c, "st": st, "n": n, "err": err})
            _STATE["elapsed"] = round(time.time() - _STATE["started_at"], 1)
    if tally:
        for item in results:
            _tally(item)
    ok_n = sum(1 for x in results if x["st"] == "ok")
    hole = sum(1 for x in results if x["st"] == "skip" and "缺中间数据" in (x["err"] or ""))
    return {"ok": True, "degraded": True, "bar_day": bar_day, "updated": ok_n,
            "hole": hole, "fail": sum(1 for x in results if x["st"] == "fail"),
            "codes": [x["code"] for x in results if x["st"] == "ok"],
            "source": "spot"}


# --------------------------------------------------------------------------
# 任务调度
# --------------------------------------------------------------------------

def _codes(scope: str) -> list[str]:
    if scope == "pool":
        try:
            import watchlist
            raw = [str(c).zfill(6) for c in watchlist.codes()]
        except Exception:
            raw = []
    else:
        raw = []
        if UNIVERSE.exists():
            import csv
            with UNIVERSE.open("r", encoding="utf-8-sig", newline="") as f:
                raw = [r["code"] for r in csv.DictReader(f) if r.get("code")]
    return [c for c in dict.fromkeys(raw) if market_kind(c)]


def _prep(codes: list[str], mode: str):
    cfg = _cfg()
    raw = str(cfg.get("cache_start", "20180101")).replace("-", "")
    cache_start = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"   # baostock 要求 YYYY-MM-DD
    end = _today()
    _FAILED.clear()
    _EMPTY.clear()
    _STATE.update(running=True, mode=mode, done=0, total=len(codes), ok=0, fail=0,
                  skip=0, rows=0, last="", last_err="", stop=False, workers=1,
                  message="准备中…", started_at=time.time(), elapsed=0.0, retrying=False,
                  source="", error="", topup={}, blocked=False, fresh=0)
    return cache_start, end


def _tally(item: dict, lock: threading.Lock | None = None):
    """汇总一只股票的处理结果（并行时由读取线程调用，需加锁）。"""
    def _do():
        st = item.get("st")
        retry = bool(_STATE.get("retrying"))
        if not retry:
            _STATE["done"] += 1
        code = str(item.get("code") or "")
        if st == "ok":
            if retry:                      # 重试成功：把原来的 fail 改判成 ok
                _STATE["fail"] = max(0, _STATE["fail"] - 1)
                if _STATE.get("last_err_code") == code:   # 这条报错已过期，别再显示
                    _STATE["last_err"], _STATE["last_err_code"] = "", ""
            _STATE["ok"] += 1
            _STATE["rows"] += int(item.get("n") or 0)
        elif st == "skip":
            if not retry:
                _STATE["skip"] += 1
        elif st == "empty":
            # 新股 / 长期停牌：日线本来就不足 20 根，记失败但不重试
            if not retry:
                _STATE["fail"] += 1
                _EMPTY.append(code)
            _STATE["last_err"], _STATE["last_err_code"] = item.get("err") or "", code
        else:
            if not retry:
                _STATE["fail"] += 1
                _FAILED.append(code)
            _STATE["last_err"], _STATE["last_err_code"] = item.get("err") or "", code
        _STATE["last"] = code
        _STATE["elapsed"] = round(time.time() - _STATE["started_at"], 1)
        if not retry:
            _STATE["message"] = "正在下载"
    if lock:
        with lock:
            _do()
    else:
        _do()


def _run_serial(codes: list[str], mode: str, cache_start: str, end: str):
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        _STATE.update(running=False, message="baostock 登录失败：" + lg.error_msg)
        return
    try:
        for code in codes:
            if _STATE["stop"]:
                _STATE["message"] = "已手动停止"
                break
            try:
                if to_bs_code(code) is None:
                    st, n, err = _process_bj(code)
                else:
                    st, n, err = _process_hs(bs, code, mode, cache_start, end)
            except Exception as e:
                st, n, err = "fail", 0, str(e)[:80]
            _tally({"code": code, "st": st, "n": n, "err": err})
            time.sleep(0.02)   # 轻微节流，避免被判定异常流量
    finally:
        try:
            bs.logout()
        except Exception:
            pass
        # 注意：running 只在 _run 的最外层复位。这里置 False 会让前端轮询提前判定
        # 「跑完了」，把后面的补当日 K 线当成没发生，快照就会按旧截面重算。
        if not _STATE["stop"]:
            _STATE["message"] = "完成"


# --------------------------------------------------------------------------
# 并行：baostock 的连接是模块级全局对象，多线程共用会串包，
# 所以用「独立子进程」而非线程池；每个子进程自己 login/query/logout。
# --------------------------------------------------------------------------

def _worker_main(argv: list[str]) -> None:
    """子进程入口：python bulkdata.py --worker <chunk.json> <mode> <start> <end>"""
    chunk, mode, cache_start, end = argv
    codes = json.loads(Path(chunk).read_text(encoding="utf-8"))
    import baostock as bs
    # 6 个进程同时 login 会被服务端互踢（表现为「用户未登录」），错开半秒登录
    try:
        idx = int(Path(chunk).stem.split("_")[-1])
    except Exception:
        idx = 0
    time.sleep(min(3.0, 0.6 * idx))
    lg = bs.login()
    if lg.error_code != "0":
        for c in codes:
            print(json.dumps({"code": c, "st": "fail", "n": 0,
                              "err": "登录失败:" + lg.error_msg}, ensure_ascii=False), flush=True)
        return

    def _one(code):
        if to_bs_code(code) is None:
            return _process_bj(code)
        return _process_hs(bs, code, mode, cache_start, end)

    try:
        for code in codes:
            try:
                st, n, err = _one(code)
            except Exception as e:
                st, n, err = "fail", 0, str(e)[:80]
            # 会话中途被别的进程踢掉时，重登一次再补这一只，别把整块都记成失败
            if st == "fail" and ("未登录" in err or "登录" in err):
                try:
                    bs.logout()
                except Exception:
                    pass
                if bs.login().error_code == "0":
                    try:
                        st, n, err = _one(code)
                    except Exception as e:
                        st, n, err = "fail", 0, str(e)[:80]
            print(json.dumps({"code": code, "st": st, "n": n, "err": err}, ensure_ascii=False), flush=True)
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def _write_chunks(codes: list[str], workers: int) -> tuple[list[Path], Path]:
    tmp = Path(tempfile.mkdtemp(prefix="qd_bulk_"))
    size = max(1, (len(codes) + workers - 1) // workers)
    paths = []
    for i in range(0, len(codes), size):
        p = tmp / f"chunk_{i // size}.json"
        p.write_text(json.dumps(codes[i:i + size]), encoding="utf-8")
        paths.append(p)
    return paths, tmp


def _run_parallel(codes: list[str], mode: str, cache_start: str, end: str, workers: int):
    paths, tmp = _write_chunks(codes, workers)
    _STATE["workers"] = len(paths)
    _STATE["message"] = ("并行重试（%d 进程）" if _STATE.get("retrying")
                         else "并行下载（%d 进程）") % len(paths)
    lock = threading.Lock()
    procs: list[subprocess.Popen] = []
    chunk_of: dict[int, list[str]] = {}
    seen_of: dict[int, set] = {}
    beat_of: dict[int, float] = {}
    for p in paths:
        try:
            chunk = json.loads(p.read_text(encoding="utf-8"))
            pr = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker",
                 str(p), mode, cache_start, end],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="ignore", bufsize=1,
                # 子进程 stdout 是管道，Windows 上 Python 不会自动用 UTF-8，
                # 而是回落到 ANSI 代码页（简体中文机器是 cp936/GBK）。
                # 父进程按 UTF-8 解码时，GBK 汉字里像 C8A1（「取」）这类双字节
                # 恰好也是合法的 UTF-8 序列 —— errors="ignore" 拦不住，
                # 会静默变成「ȡʧ」这种乱码，把真实的报错原因吃掉。
                # 显式把子进程的 stdio 编码钉死成 UTF-8。
                env={**os.environ, "PYTHONIOENCODING": "utf-8",
                     "PYTHONUTF8": "1"})
            procs.append(pr)
            chunk_of[id(pr)] = chunk
            seen_of[id(pr)] = set()
            beat_of[id(pr)] = time.time()
        except Exception as e:
            _STATE["last_err"] = "子进程启动失败：" + str(e)[:60]

    def _read(pr: subprocess.Popen):
        try:
            for line in pr.stdout:            # type: ignore[union-attr]
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                with lock:
                    beat_of[id(pr)] = time.time()
                    seen_of[id(pr)].add(str(item.get("code") or ""))
                _tally(item, lock)
        except Exception:
            pass

    readers = [threading.Thread(target=_read, args=(pr,), daemon=True) for pr in procs]
    for t in readers:
        t.start()

    # 无输出看门狗：baostock 是同步 socket，个别停牌股/限流会让单次查询长时间不回包。
    # 超过 STALL 秒没有任何输出就杀掉该子进程，把它没做完的股票「重新派发」给下一轮并行；
    # 这里绝不能记成失败——一个 chunk 里有几百只，记失败就只能串行重试，反而更慢。
    STALL = 150.0
    pending: list[str] = []
    while any(pr.poll() is None for pr in procs):
        if _STATE["stop"]:
            for pr in procs:
                try:
                    pr.terminate()
                except Exception:
                    pass
            break
        now = time.time()
        for pr in procs:
            if pr.poll() is not None:
                continue
            if now - beat_of.get(id(pr), now) > STALL:
                left = [c for c in chunk_of.get(id(pr), []) if c not in seen_of.get(id(pr), set())]
                try:
                    pr.kill()
                except Exception:
                    pass
                with lock:
                    seen_of[id(pr)].update(left)   # 已交给下一轮，收尾时不再重复登记
                pending.extend(left)
                _STATE["last_err"] = "看门狗终止 1 个子进程（%d 只转入下一轮重试）" % len(left)
        _STATE["elapsed"] = round(time.time() - _STATE["started_at"], 1)
        time.sleep(0.25)
    try:
        for t in readers:
            t.join(timeout=3)
        # 子进程异常退出（崩溃/被杀）时，把没上报的股票同样交给下一轮重试；
        # 用户手动停止时不补记，避免把「已停」显示成一大堆失败
        if not _STATE["stop"]:
            for pr in procs:
                left = [c for c in chunk_of.get(id(pr), []) if c not in seen_of.get(id(pr), set())]
                if left:
                    seen_of[id(pr)].update(left)
                    pending.extend(left)
    finally:
        for pr in procs:
            if pr.poll() is None:
                try:
                    pr.kill()
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)
        if _STATE["stop"]:
            _STATE["message"] = "已手动停止（已完成 %d/%d）" % (_STATE["done"], _STATE["total"])
    return [c for c in dict.fromkeys(pending) if c]


def _retry_failed(codes: list[str], mode: str, cache_start: str, end: str, workers: int = 3):
    """失败项重试：优先再并行一轮（少几个进程），只剩零星几只才串行。

    重试阶段不增加 done，只把 fail 改判成 ok/skip，避免进度条超过 100%。
    """
    _STATE["retrying"] = True
    _STATE["running"] = True
    try:
        left = list(codes)
        if len(left) >= 6 and workers > 1:
            _STATE["message"] = "并行重试 %d 只失败项…" % len(left)
            left = _run_parallel(left, mode, cache_start, end, min(workers, 4))
        if left and not _STATE["stop"]:
            _STATE["message"] = "串行重试 %d 只…" % len(left)
            _run_serial(left, mode, cache_start, end)
    finally:
        _STATE["retrying"] = False


def _run(codes: list[str], mode: str, workers: int = 1):
    """统一入口；workers 会被限制在 1~8，小批量自动走串行（进程启动开销更划算）。

    开跑前先探一次 baostock：连不上就直接走「当日快照补 K 线」的降级路径，
    不必让 5000 个子进程逐个登录失败（那样几十秒全废，还看不清到底为什么失败）。
    """
    cache_start, end = _prep(codes, mode)
    workers = max(1, min(8, int(workers or 1)))
    try:
        probe = bs_probe()
        _STATE["source"] = "baostock"
        # 「黑名单用户」= 今天登录太频繁被限流（300404），此时再重试只会更糟
        _STATE["blocked"] = "黑名单" in str(probe.get("msg") or "")
        if not probe.get("ok"):
            why = "baostock 登录被限流" if _STATE["blocked"] else "数据源连接失败"
            _STATE["message"] = "%s（%s），改用当日快照补日线…" % (why, probe.get("msg"))
            r = _spot_topup(codes, note=why)
            _STATE["source"] = "spot"
            if not r.get("ok"):
                _STATE["error"] = str(r.get("error") or "降级失败")
                _STATE["message"] = "更新失败：" + _STATE["error"]
                return
            _STATE["message"] = "完成（降级：当日快照补日线 %s）" % r.get("bar_day")
            return

        # 连得上但今天的日线还没入库（17:30 前是常态）：缓存已跟到上一交易日时，
        # 与其让 5000 只逐个查到空结果（约 8 分钟），不如直接用快照补这一根。
        today = dt.date.today().isoformat()
        past = [d for d in _cal_recent(4) if d < today]
        prev_day = past[-1] if past else ""
        win_ok, _why = spot_window()
        if (mode == "incremental" and prev_day and win_ok and not probe.get("has_day")
                and not _codes_missing(codes, prev_day)):
            _STATE["source"] = "spot"
            _STATE["message"] = "数据源还没有今天的日线（通常 17:30 入库），改用快照补…"
            r = _spot_topup(codes, note="数据源还没有今天的日线")
            if r.get("ok"):
                _STATE["message"] = "完成（%s 这一根由当日快照补齐，%d 只）" \
                                    % (r.get("bar_day"), r.get("updated") or 0)
            else:
                _STATE["error"] = str(r.get("error") or "降级失败")
                _STATE["message"] = "更新失败：" + _STATE["error"]
            return

        if workers > 1 and len(codes) >= workers * 2:
            pending = _run_parallel(codes, mode, cache_start, end, workers)
            # 卡死/异常的子进程留下的股票，再并行跑最多两轮（并行重试远比串行快）
            rounds = 0
            while pending and not _STATE["stop"] and rounds < 2:
                rounds += 1
                _STATE["running"] = True
                _STATE["message"] = "重试卡住的 %d 只（第 %d 轮）…" % (len(pending), rounds)
                pending = _run_parallel(pending, mode, cache_start, end, min(workers, 4))
            if pending and not _STATE["stop"]:
                lock = threading.Lock()
                for c in pending:
                    _tally({"code": c, "st": "fail", "n": 0,
                            "err": "多次超时未返回，已跳过"}, lock)
            retry = [c for c in dict.fromkeys(_FAILED) if c]
            if "黑名单" in (_STATE["last_err"] or ""):
                _STATE["blocked"] = True
            # 被限流时别再并行重试（多一次登录只是把黑名单坐实），剩下的交给快照补
            if retry and not _STATE["stop"] and not _STATE["blocked"]:
                _retry_failed(retry, mode, cache_start, end, workers)
        else:
            _run_serial(codes, mode, cache_start, end)
        # baostock 的日线约 17:30 才入库：收盘后早点更新时它只能补到昨天。
        # 这时再拿一次实时快照，把「今天这根」补齐，否则按钮点了等于没更新。
        if mode == "incremental" and not _STATE["stop"] and win_ok:
            left = _codes_missing(codes, today)
            if left:
                r = _spot_topup(left, tally=False, note="数据源还没有今天的数据")
                if r.get("ok"):
                    _STATE["topup"] = {"day": r.get("bar_day"), "updated": r.get("updated"),
                                       "fail": r.get("fail"), "left": len(left)}
                    if r.get("updated"):
                        _STATE["source"] = "spot+baostock"
                else:
                    # 行情补到了昨天、今天这根没补上，别把失败藏起来
                    _STATE["topup"] = {"day": "", "updated": 0, "error": r.get("error")}
        # 对账：报过失败的代码，只要缓存里已经跟到了目标日，就不该继续算失败 ——
        # 用户关心的是「数据到底新不新」，而不是某个子进程的登录成功与否。
        #
        # 目标日不能取 today：baostock 大约 17:30 才入库当天日线，盘中（甚至收盘后
        # 没过 17:30）跑增量时每一只都「缺今天」，missing 是全集 → 一只都修不掉，
        # 对账等于空转。要跟的是「应到交易日」，那才是这批数据该到的位置。
        cur_day = _target_day(today)
        if not _STATE["stop"] and _STATE["fail"]:
            miss = set(_codes_missing(codes, cur_day))
            fixed = [c for c in list(_FAILED) if c not in miss]
            for c in fixed:
                _FAILED.remove(c)
            if fixed:
                _STATE["fail"] = max(0, _STATE["fail"] - len(fixed))
                _STATE["fresh"] = int(_STATE.get("fresh") or 0) + len(fixed)
    finally:
        _STATE["running"] = False
        _save_source()
    # 降级路径的完成文案里带着「补了哪天」，别被一句「完成」盖掉
    if not _STATE["stop"] and _STATE.get("source") == "baostock":
        _STATE["message"] = "完成"
    # 各页快捷触发的增量单独落一条统一更新记录；流水线内部调用传 log=False，
    # 由 pipeline 整段落库，避免同一次更新记两遍。
    if _STATE.get("_log", True):
        try:
            import store
            # 「跑完了」不等于「更新成功了」：以前只看 error/stop，935 只失败也记成
            # 绿色成功，于是更新记录说「成功」、待办却还挂着 —— 两边互相打脸。
            # 有失败只数就记失败，并在 error 里写清有多少只没跟上。
            nfail = int(_STATE.get("fail") or 0)
            okf = (not _STATE.get("error") and not _STATE.get("stop")
                   and nfail == 0 and int(_STATE.get("ok") or 0) > 0)
            ms = int((time.time() - float(_STATE.get("started_at") or time.time())) * 1000)
            detail = str(_STATE.get("message") or "")[:200]
            if nfail:
                detail = ("%s · 失败 %d 只 · 成功 %d 只 · 跳过 %d 只"
                          % (detail, nfail, int(_STATE.get("ok") or 0),
                             int(_STATE.get("skip") or 0)))[:200]
            err = str(_STATE.get("error") or "")[:300]
            if not err and nfail:
                err = "行情更新未全部成功：%d 只失败（详见运行日志的失败原因）" % nfail
            store.run_log("quote", okf,
                          steps=[{"name": "增量行情", "ok": okf, "ms": ms, "detail": detail}],
                          trigger="shortcut", error=err, ms=ms)
        except Exception:
            pass


_START_LOCK = threading.Lock()


def start(mode: str = "incremental", limit: int | None = None, scope: str = "all",
          workers: int | None = None, log: bool = True) -> dict:
    # 检查与置位必须在同一把锁里：以前 running 是子线程 _prep() 才置 True，
    # 两个请求在它置位前都能通过检查，于是并发跑两份、子进程数翻倍。
    with _START_LOCK:
        if _STATE["running"]:
            s = status()
            s["ok"] = False
            s["error"] = "已有下载任务正在运行（%d/%d），先停止或等它跑完" % (
                _STATE["done"], _STATE["total"])
            return s
        _STATE.update(running=True, stop=False, done=0, error="", message="准备中…")
    try:
        return _start_locked(mode, limit, scope, workers, log)
    except Exception:
        _STATE.update(running=False, message="")
        raise


def _start_locked(mode: str, limit, scope: str, workers, log: bool) -> dict:
    codes = _codes(scope)
    if limit:
        # 限量时优先处理「未缓存」和「最久未更新」的股票，保证每次限量都能扩大覆盖
        import store
        mm = store.meta_map()

        def _prio(c):
            m = mm.get(c)
            return (0, "") if not m else (1, m.get("last_date", ""))
        codes = sorted(codes, key=_prio)[:int(limit)]
    if not codes:
        _STATE.update(running=False, message="")
        s = status()
        s["ok"] = False
        s["error"] = "没有需要处理的股票（名单为空或已全部缓存）"
        return s
    if workers is None:
        workers = int(_cfg().get("bulk_workers", 6) or 6)
    workers = max(1, min(8, int(workers)))
    _STATE["_log"] = bool(log)
    _STATE["total"] = len(codes)
    try:
        threading.Thread(target=_run, args=(codes, mode, workers), daemon=True).start()
    except Exception:
        _STATE.update(running=False, message="")
        raise
    return {"ok": True, "started": len(codes), "mode": mode, "scope": scope,
            "workers": workers}


def stop() -> dict:
    _STATE["stop"] = True
    return {"ok": True, "message": "已发送停止信号，并行任务会立即终止子进程"}


def source_now() -> dict:
    """最近一次任务用的数据源（baostock / spot）与降级原因，给时效面板显示用。

    进程重启后内存里的状态就没了，所以结果同时落一份 JSON —— 否则重启一次
    「今天这根是快照补的」这个信息就消失，面板会显示成普通的最新。
    """
    d = {"source": _STATE.get("source") or "", "error": _STATE.get("error") or "",
         "blocked": bool(_STATE.get("blocked")),
         "message": _STATE.get("message") or "", "running": bool(_STATE.get("running"))}
    if not d["source"]:
        try:
            saved = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
            d.update({k: saved.get(k) for k in ("source", "blocked", "message", "error")})
        except Exception:
            pass
    return d


def _save_source() -> None:
    try:
        SOURCE_JSON.write_text(json.dumps(
            {"source": _STATE.get("source") or "", "blocked": bool(_STATE.get("blocked")),
             "message": _STATE.get("message") or "", "error": _STATE.get("error") or "",
             "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def status() -> dict:
    import store
    uni_codes: set[str] = set()
    if UNIVERSE.exists():
        try:
            import csv
            with UNIVERSE.open("r", encoding="utf-8-sig", newline="") as f:
                uni_codes = {r["code"] for r in csv.DictReader(f) if r.get("code")}
        except Exception:
            uni_codes = set()
    have = set(store.cached_codes())
    # 缓存里还留着退市/改码的旧代码，覆盖率只按当前名单算，避免出现 100% 以上的怪数
    cached = len(uni_codes & have) if uni_codes else len(have)
    uni = len(uni_codes)
    st = {k: v for k, v in _STATE.items() if k != "stop"}
    # ok 在状态里是「成功只数」，与接口通用的 ok=true 冲突；分开成 ok_count，
    # 否则前端 if (!s.ok) 会永远提前返回，页面刷新后进度和空闲状态都显示不出来。
    ok_n = int(st.pop("ok", 0) or 0)
    done, total = int(st.get("done") or 0), int(st.get("total") or 0)
    elapsed = float(st.get("elapsed") or 0)
    # 只按「真正需要下载的」算速率：跳过已缓存的股票几乎不花时间，
    # 若把它们算进去，ETA 会先显示 3 分钟、随后跳成 1 小时。
    dl = ok_n + int(st.get("fail") or 0)
    rate = round(dl / elapsed, 2) if elapsed > 2 and dl > 0 else 0.0
    # 剩余量按「本次任务的 total」算，不能按全市场 uni-cached：
    # 限量更新（只跑 500 只）时后者会把 ETA 放大十倍以上。
    left = max(0, total - done) if total else max(0, uni - cached)
    # ETA 用「处理进度」速率而不是纯下载速率：跳过已缓存股票也计入 done，
    # 增量更新时绝大多数是秒级跳过，用下载速率会把剩余时间估得离谱地长。
    proc_rate = round(done / elapsed, 2) if elapsed > 2 and done > 0 else 0.0
    eta = int(round(left / proc_rate)) if proc_rate > 0 and left > 0 else 0
    return {"ok": True, **st, "ok_count": ok_n, "rate": rate, "eta": eta,
            "cached_total": cached, "universe_total": uni,
            "cover": round(cached / uni * 100, 2) if uni else 0.0}


if __name__ == "__main__":
    # 并行下载的 worker 入口（由父进程拉起，正常使用不需要手动调用）
    if len(sys.argv) >= 6 and sys.argv[1] == "--worker":
        _worker_main(sys.argv[2:6])
    else:
        print("用法：python bulkdata.py --worker <chunk.json> <mode> <start> <end>")
