# -*- coding: utf-8 -*-
"""
QuantDesk · 行业分类（申万一级为主 + 证监会行业补全）

为什么要重做：
  - 旧版只用了新浪 49 个板块，覆盖 ~52%，且混入「次新股 / 其它行业」这类非行业标签，
    粒度粗、口径也不是研究界通用的分类，行业约束会失真。
  - 主流做法（TradingAgents-CN、aiagents-stock 等项目）是「申万一级做研究口径，
    证监会 / 交易所行业做全覆盖兜底」。

当前分级策略（优先级从高到低）：
  1. 手工维护  data/industry.csv 中 source=manual 的行，永远优先，不会被自动覆盖
  2. 申万一级   akshare  sw_index_first_info + index_component_sw（31 个行业，研究通用口径）
  3. 证监会行业 baostock query_stock_industry（84 个大类，覆盖沪深全市场，含申万未纳入的新股）
  4. 新浪行业   旧数据源，仅在前两者都没覆盖时兜底

落盘字段：code / name / industry / level / csrc / source / updated
  industry  主口径（行业约束用这一列）
  csrc      证监会行业原文（如「J66货币金融服务」），保留双口径便于交叉验证

对外：
  load_map()            代码 → 主口径行业
  enrich(df)            给横截面表补 industry 列
  sync(force)           同步（自动先申万、再证监会、必要时新浪）
  sync_async(force)     后台同步，界面轮询 status()
  status(codes=None)    覆盖率 / 来源 / 更新时间 / 同步进度
  summary()             行业列表 + 成分数 + 有行情数 + 20 日强度 + 换手
  members(name)         某行业的成分股（现价 / 涨跌 / 估值 / 是否已缓存）
  strength()            按行情缓存算各行业 20 日等权涨幅、5 日平均换手
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import threading
import time
from pathlib import Path

import pandas as pd

from paths import BASE  # 打包后 = exe 目录；开发时 = 项目根
DATA = BASE / "data"
UNIVERSE = DATA / "universe.csv"
INDUSTRY_CSV = DATA / "industry.csv"
META_JSON = DATA / "industry.json"
STRENGTH_JSON = DATA / "industry_strength.json"
FIELDS = ["code", "name", "industry", "level", "csrc", "source", "updated"]
STALE_DAYS = 30

_SYNC = {"running": False, "message": "空闲", "started": 0.0, "result": None,
         "progress": "", "done_at": ""}
_STRENGTH = {"at": 0.0, "data": [], "day": ""}
_LOCK = threading.Lock()


def _today() -> str:
    return dt.date.today().isoformat()


def _read_meta() -> dict:
    if not META_JSON.exists():
        return {}
    try:
        return json.loads(META_JSON.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write_meta(m: dict):
    META_JSON.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


_ROWS_CACHE: dict = {"key": None, "v": None}


def rows() -> list[dict]:
    """行业归属行。按文件 mtime+size 缓存：单股分位、行业暴露、体检都要用，
    每次重读 5000 行 CSV 约 30ms。写入走 tmp+replace，mtime 一变缓存自动失效。"""
    if not INDUSTRY_CSV.exists():
        return []
    try:
        st = INDUSTRY_CSV.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _ROWS_CACHE.get("key") == key:
        return _ROWS_CACHE["v"]
    try:
        with INDUSTRY_CSV.open("r", encoding="utf-8-sig", newline="") as f:
            out = [r for r in csv.DictReader(f) if r.get("code")]
    except Exception:
        return []
    if key is not None:
        _ROWS_CACHE.update(key=key, v=out)
    return out


def load_map() -> dict[str, str]:
    out = {}
    for r in rows():
        code = str(r.get("code") or "").zfill(6)
        ind = (r.get("industry") or "").strip()
        if code and ind:
            out[code] = ind
    return out


def _write_rows(items: dict[str, dict]):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = INDUSTRY_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for code in sorted(items):
            w.writerow({k: items[code].get(k, "") for k in FIELDS})
    tmp.replace(INDUSTRY_CSV)


def enrich(df, code_col: str = "code", industry_col: str = "industry"):
    """补行业列；查不到的留空字符串，不编造。"""
    m = load_map()
    df[industry_col] = [m.get(str(c).zfill(6), "") for c in df[code_col]]
    return df


def _math_ok(x) -> bool:
    return isinstance(x, float) and math.isfinite(x)


def _digits(s) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit()).zfill(6)[:6]


def _int(v) -> int:
    """「5,123」「5123.0」这类值安全转 int；解析失败按 0，不让一行脏数据毁掉整次同步。"""
    try:
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return 0


# --------------------------------------------------------------------------
# 三个数据源
# --------------------------------------------------------------------------

def _fetch_sw() -> tuple[dict[str, str], list[str], dict[str, dict]]:
    """申万一级：31 个行业，逐个取成分股。返回 代码→行业、失败行业、行业元数据。"""
    import akshare as ak
    info = ak.sw_index_first_info()
    out: dict[str, str] = {}
    failed: list[str] = []
    meta: dict[str, dict] = {}
    for _, r in info.iterrows():
        code = str(r.get("行业代码") or "").strip()
        name = str(r.get("行业名称") or "").strip()
        if not code or not name:
            continue
        meta[name] = {
            "pe": r.get("TTM(滚动)市盈率"), "pb": r.get("市净率"),
            "dividend": r.get("静态股息率"), "expect": _int(r.get("成份个数")),
        }
        try:
            d = ak.index_component_sw(symbol=code.split(".")[0])
        except Exception:
            failed.append(name)
            continue
        if d is None or d.empty:
            failed.append(name)
            continue
        col = "证券代码" if "证券代码" in d.columns else ("股票代码" if "股票代码" in d.columns else None)
        if not col:
            failed.append(name)
            continue
        for c in d[col].astype(str):
            c = _digits(c)
            if len(c) == 6:
                out.setdefault(c, name)
    return out, failed, meta


def _fetch_csrc() -> tuple[dict[str, tuple[str, str]], str]:
    """证监会行业（baostock）：一次请求拿全沪深，84 个大类。返回 代码→(清洗名, 原文)。"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError("baostock 登录失败：" + lg.error_msg)
    try:
        rs = bs.query_stock_industry()
        out: dict[str, tuple[str, str]] = {}
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            full = row[1] if len(row) > 1 else ""
            raw = (row[3] if len(row) > 3 else "").strip()
            code = _digits(str(full).split(".")[-1])
            if len(code) != 6 or not raw:
                continue
            # 「J66货币金融服务」→「货币金融服务」；保留原文用于 csrc 列
            clean = raw
            i = 0
            while i < len(clean) and (clean[i].isascii() and (clean[i].isalpha() or clean[i].isdigit())):
                i += 1
            if i:
                clean = clean[i:]
            out[code] = (clean.strip() or raw, raw)
        return out, "证监会行业分类"
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def _fetch_sina() -> tuple[dict[str, str], list[str]]:
    """新浪行业：49 个板块（旧口径，仅兜底）。"""
    import akshare as ak
    spot = ak.stock_sector_spot(indicator="新浪行业")
    boards = []
    for _, r in spot.iterrows():
        label = str(r.get("label") or "").strip()
        name = str(r.get("板块") or "").strip()
        if label and name:
            boards.append((label, name))
    out: dict[str, str] = {}
    failed = []
    for label, name in boards:
        try:
            d = ak.stock_sector_detail(sector=label)
            if d is None or d.empty or "code" not in d.columns:
                failed.append(name)
                continue
            for c in d["code"].astype(str):
                c = _digits(c)
                if len(c) == 6:
                    out.setdefault(c, name)
        except Exception:
            failed.append(name)
    return out, failed


# --------------------------------------------------------------------------
# 同步
# --------------------------------------------------------------------------

def _universe_count() -> int:
    if not UNIVERSE.exists():
        return 0
    try:
        with UNIVERSE.open("r", encoding="utf-8-sig", newline="") as f:
            return sum(1 for r in csv.DictReader(f) if r.get("code"))
    except Exception:
        return 0


def _do_sync(force: bool) -> dict:
    old = {str(r["code"]).zfill(6): dict(r) for r in rows()}
    meta = _read_meta()
    fresh = bool(meta.get("updated")) and meta.get("updated", "")[:10] >= (
        dt.date.today() - dt.timedelta(days=STALE_DAYS)).isoformat()
    if not force and fresh and old:
        return {"ok": True, "cached": True, "count": len(old),
                "updated": meta.get("updated", ""), "source": meta.get("source", ""),
                "message": "行业映射仍在有效期内（%d 天），未重复拉取" % STALE_DAYS}

    uni_total = _universe_count()
    report: dict[str, str] = {}

    _SYNC["progress"] = "申万一级：拉取 31 个行业成分股…"
    try:
        sw, sw_failed, sw_meta = _fetch_sw()
        report["申万一级"] = "%d 只" % len(sw)
    except Exception as e:
        sw, sw_failed, sw_meta = {}, [], {}
        report["申万一级"] = "失败：" + str(e)[:60]

    need_csrc = (not uni_total) or len(sw) < uni_total * 0.9
    csrc: dict[str, tuple[str, str]] = {}
    if need_csrc or force:
        _SYNC["progress"] = "证监会行业：baostock 全市场一次拉取（约 30 秒）…"
        try:
            csrc, _src = _fetch_csrc()
            report["证监会"] = "%d 只" % len(csrc)
        except Exception as e:
            report["证监会"] = "失败：" + str(e)[:60]

    covered = set(sw) | set(csrc) | {c for c, r in old.items() if (r.get("source") or "") == "manual"}
    sina: dict[str, str] = {}
    if uni_total and len(covered) < uni_total * 0.85:
        _SYNC["progress"] = "覆盖不足，补充新浪行业兜底…"
        try:
            sina, _sf = _fetch_sina()
            report["新浪行业"] = "%d 只" % len(sina)
        except Exception as e:
            report["新浪行业"] = "失败：" + str(e)[:60]

    if not sw and not csrc and not sina:
        return {"ok": False, "error": "三个行业数据源都没取到数据，可稍后再试或手工维护 data/industry.csv"}

    merged: dict[str, dict] = {}
    all_codes = set(old) | set(sw) | set(csrc) | set(sina)
    counts = {"manual": 0, "申万一级": 0, "证监会": 0, "新浪行业": 0}
    for code in all_codes:
        prev = old.get(code) or {}
        if (prev.get("source") or "") == "manual":
            merged[code] = prev
            counts["manual"] += 1
            continue
        csrc_pair = csrc.get(code)
        csrc_raw = csrc_pair[1] if csrc_pair else (prev.get("csrc") or "")
        if code in sw:
            ind, level, src = sw[code], "申万一级", "申万"
        elif csrc_pair:
            ind, level, src = csrc_pair[0], "证监会", "证监会(baostock)"
        elif code in sina:
            ind, level, src = sina[code], "新浪行业", "新浪行业"
        else:
            continue
        merged[code] = {"code": code, "name": prev.get("name") or "",
                        "industry": ind, "level": level, "csrc": csrc_raw,
                        "source": src, "updated": _today()}
        counts[level] = counts.get(level, 0) + 1

    # 顺手补名称（便于行业成分股直接显示；名称仍以 universe.csv 为准）
    try:
        if UNIVERSE.exists():
            with UNIVERSE.open("r", encoding="utf-8-sig", newline="") as f:
                names = {str(r["code"]).zfill(6): (r.get("name") or "") for r in csv.DictReader(f) if r.get("code")}
            for code, r in merged.items():
                if not r.get("name") and code in names:
                    r["name"] = names[code]
    except Exception:
        pass

    _write_rows(merged)
    levels = {r["level"] for r in merged.values()}
    inds = {r["industry"] for r in merged.values() if r.get("industry")}
    _write_meta({
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": " + ".join([k for k in ("申万一级", "证监会") if k in levels] or ["新浪行业"]),
        "count": len(merged), "levels": sorted(levels), "industries": len(inds),
        "by_level": counts, "report": report, "sw_failed": sw_failed[:12],
        "sw_industry_meta": sw_meta,
    })
    return {"ok": True, "cached": False, "count": len(merged), "industries": len(inds),
            "by_level": counts, "report": report, "sw_failed": sw_failed[:12],
            "message": "已更新 %d 只 · %d 个行业（申万一级 %d / 证监会 %d / 手工 %d）" % (
                len(merged), len(inds), counts.get("申万一级", 0),
                counts.get("证监会", 0), counts.get("manual", 0))}


def sync(force: bool = False) -> dict:
    """同步行业映射（同步调用，约 40~90 秒）。界面请用 sync_async。"""
    if _SYNC["running"]:
        return {"ok": True, "running": True, "message": "正在同步中…"}
    _SYNC.update(running=True, message="同步中", progress="准备…",
                 started=time.time(), result=None)
    try:
        r = _do_sync(force)
        _SYNC["result"] = r
        return r
    except Exception as e:
        r = {"ok": False, "error": "行业分类同步失败：%s" % str(e)[:160]}
        _SYNC["result"] = r
        return r
    finally:
        _STRENGTH.update(at=0.0, data=[], day="")   # 行业归属变了，强度缓存作废
        _SYNC.update(running=False, message="完成", progress="",
                     done_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        try:
            import store
            res = _SYNC.get("result") or {}
            okf = bool(res.get("ok", True))
            ms = int((time.time() - float(_SYNC.get("started") or time.time())) * 1000)
            store.run_log("industry", okf,
                          steps=[{"name": "行业分类", "ok": okf, "ms": ms,
                                  "detail": str(res.get("error") or res.get("message") or "")[:200]}],
                          trigger="shortcut", error=str(res.get("error") or "")[:300], ms=ms)
        except Exception:
            pass


def sync_async(force: bool = True) -> dict:
    if _SYNC["running"]:
        return {"ok": True, "running": True, "message": "已有同步任务在进行"}
    threading.Thread(target=sync, args=(force,), daemon=True).start()
    return {"ok": True, "running": True, "message": "已开始同步（申万一级 → 证监会补全）"}


# --------------------------------------------------------------------------
# 行业视图：列表 / 成分股 / 强度
# --------------------------------------------------------------------------

def _cached_codes() -> set[str]:
    try:
        import store
        return set(store.cached_codes())
    except Exception:
        return set()


def strength(ttl: int = 300) -> dict[str, dict]:
    """用本地行情缓存算行业强度：20 日等权涨幅、5 日平均换手。

    只读每只 CSV 的尾部 30 行（因子用不到更早的），5200 只约 1 秒；
    结果按「最新交易日」落盘缓存，进程重启后直接读，不用再扫一遍全市场。
    旧版是 pd.read_csv 全表 × 5200 只，冷启动要 13 秒，首屏打开行业页就卡在那里。
    """
    import histcache
    day = _latest_day()
    now = time.time()
    if (_STRENGTH["data"] and _STRENGTH.get("day") == day
            and now - _STRENGTH["at"] < ttl):
        return {x["industry"]: x for x in _STRENGTH["data"]}
    if not day:
        return {}
    disk = _read_strength()
    if disk and disk.get("day") == day and disk.get("data"):
        _STRENGTH.update(at=now, data=disk["data"], day=day)
        return {x["industry"]: x for x in disk["data"]}

    m = load_map()
    have = _cached_codes()
    codes = [c for c in m if c in have]
    # 只要两个收盘价和 5 个换手率，用 numpy 数组就够，别为这点数字建 DataFrame
    got = histcache.bulk_ohlcv(codes, 30, workers=6)
    agg: dict[str, list] = {}
    for code, a in got.items():
        ind = m.get(code)
        if not ind or a is None or len(a["c"]) < 21:
            continue
        c0, c1 = float(a["c"][-21]), float(a["c"][-1])
        if not (_math_ok(c0) and _math_ok(c1)) or c0 <= 0:
            continue
        ret20 = c1 / c0 - 1.0
        tv = a["t"][-5:]
        tv = tv[~pd.isna(tv)]
        turn5 = float(tv.mean()) if len(tv) else 0.0
        agg.setdefault(ind, []).append((ret20, turn5))
    out = []
    for ind, vals in agg.items():
        n = len(vals)
        out.append({"industry": ind, "n": n,
                    "ret20": round(sum(v[0] for v in vals) / n * 100, 2),
                    "turn5": round(sum(v[1] for v in vals) / n, 3)})
    out.sort(key=lambda x: -x["ret20"])
    _STRENGTH.update(at=now, data=out, day=day)
    _write_strength(day, out)
    return {x["industry"]: x for x in out}


def _latest_day() -> str:
    """行情缓存的数据日（多数股票到齐的那天）；取不到才退回读几只大票的尾部。"""
    try:
        import store
        d = store.data_day()
        if d:
            return d
    except Exception:
        pass
    try:
        import histcache
        best = ""
        for c in ("600000", "000001", "600519", "000002"):
            b = histcache.last_bar(c)
            if b and b["date"] > best:
                best = b["date"]
        return best[:10]
    except Exception:
        return ""


def _read_strength() -> dict:
    try:
        return json.loads(STRENGTH_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_strength(day: str, data: list) -> None:
    try:
        STRENGTH_JSON.write_text(
            json.dumps({"day": day, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "data": data}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def summary() -> dict:
    """行业列表：成分数、有行情数、20 日强度、5 日换手，按强度降序。"""
    m = load_map()
    cached = _cached_codes()
    st = strength()
    bucket: dict[str, dict] = {}
    for code, ind in m.items():
        b = bucket.setdefault(ind, {"industry": ind, "count": 0, "cached": 0,
                                    "ret20": None, "turn5": None, "level": ""})
        b["count"] += 1
        if code in cached:
            b["cached"] += 1
    for ind, b in bucket.items():
        s = st.get(ind)
        if s:
            b["ret20"], b["turn5"] = s["ret20"], s["turn5"]
    for r in rows():
        ind = (r.get("industry") or "").strip()
        if ind in bucket and not bucket[ind]["level"]:
            bucket[ind]["level"] = r.get("level") or r.get("source") or ""
    out = list(bucket.values())
    out.sort(key=lambda x: (x["ret20"] is None, -(x["ret20"] or 0), -x["count"]))
    return {"ok": True, "industries": out, "count": len(m), "cached": len(cached),
            "strength_sample": sum(1 for x in out if x["ret20"] is not None)}


def members(name: str, limit: int = 400) -> dict:
    """某行业成分股：代码 / 名称 / 现价 / 涨跌 / 估值 / 是否已缓存。"""
    m = load_map()
    codes = [c for c, i in m.items() if i == name]
    if not codes:
        return {"ok": False, "error": "未找到行业：" + str(name), "rows": []}
    uni: dict[str, dict] = {}
    if UNIVERSE.exists():
        try:
            with UNIVERSE.open("r", encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("code"):
                        uni[str(r["code"]).zfill(6)] = r
        except Exception:
            pass
    try:
        import spot
        sp = spot.load()
    except Exception:
        sp = {}
    extra = {}
    try:
        import datasource
        extra = datasource.load_basic() or {}
    except Exception:
        extra = {}
    cached = _cached_codes()
    out = []
    for c in codes:
        u = uni.get(c) or {}
        s = sp.get(c) or {}
        e = extra.get(c) or {}
        out.append({
            "code": c, "name": u.get("name") or s.get("name") or "",
            "market": u.get("market") or "",
            "price": s.get("price") or u.get("price") or "",
            "pct": s.get("pct") if s.get("pct") not in (None, "") else u.get("pct", ""),
            "pe": e.get("pe_ttm") or e.get("pe") or "",
            "pb": e.get("pb") or "", "mv": e.get("total_mv") or "",
            "turnover": s.get("turnover") if s.get("turnover") not in (None, "") else "",
            "cached": c in cached,
        })

    def _mv(x):
        try:
            return -float(x["mv"] or 0)
        except Exception:
            return 0
    out.sort(key=_mv)
    return {"ok": True, "industry": name, "count": len(out),
            "cached": sum(1 for x in out if x["cached"]), "rows": out[:int(limit)]}


def status(codes: list[str] | None = None) -> dict:
    m = load_map()
    meta = _read_meta()
    levels = {}
    for r in rows():
        lv = r.get("level") or r.get("source") or "未标注"
        levels[lv] = levels.get(lv, 0) + 1
    out = {"ok": True, "count": len(m), "industries": len(set(m.values())),
           "updated": meta.get("updated", ""), "source": meta.get("source", ""),
           "levels": levels, "by_level": meta.get("by_level", {}),
           "report": meta.get("report", {}),
           "file": str(INDUSTRY_CSV),
           "sync": {"running": _SYNC["running"], "message": _SYNC["message"],
                    "progress": _SYNC["progress"], "done_at": _SYNC["done_at"],
                    "result": _SYNC.get("result")},
           "stale": (meta.get("updated", "")[:10] or "1999-01-01") < (
               dt.date.today() - dt.timedelta(days=STALE_DAYS)).isoformat()}
    if codes:
        hit = sum(1 for c in codes if str(c).zfill(6) in m)
        out.update({"sample": len(codes), "sample_hit": hit,
                    "sample_cover": round(hit / max(1, len(codes)) * 100, 1)})
    return out
