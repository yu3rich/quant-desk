# -*- coding: utf-8 -*-
"""
QuantDesk 全 A 股代码表（universe）

- 数据来自东方财富公开的 clist 列表接口，无需登录：每页固定 100 条，按代码分页
- 名单只含「代码 / 名称 / 板块 / 最新价 / 涨跌幅」，与历史行情缓存是两回事
- 名单按自然日缓存在 data/universe.csv；失败时回退到已有缓存
- 行情预热：串行限速、断点续传、可随时停止，避免一次性拉 5000+ 只触发限流
"""
from __future__ import annotations

import csv
import datetime as dt
import threading
import time
from pathlib import Path

import requests

from paths import BASE, DATA
DATA.mkdir(parents=True, exist_ok=True)
UNIVERSE_PATH = DATA / "universe.csv"

FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
URL_TPL = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=0&np=1"
           "&fltt=2&invt=2&fid=f12&fs=" + FS + "&fields=f12,f14,f2,f3")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://quote.eastmoney.com/"}
FIELDS = ["code", "name", "market", "price", "pct", "updated"]

# 预热进度（进程内；缓存文件本身是持久的，重启后会跳过已完成的股票）
_PREWARM = {"running": False, "done": 0, "total": 0, "ok": 0, "fail": 0,
            "last": "", "stop": False, "message": "未开始"}


def market_of(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith("688") or c.startswith("689"):
        return "科创板"
    if c.startswith("60"):
        return "沪市主板"
    if c.startswith("30"):
        return "创业板"
    if c.startswith("00"):
        return "深市主板"
    if c[0] in "48" or c.startswith("92"):
        return "北交所"
    return "其他"


def _today() -> str:
    return dt.date.today().isoformat()


def _read() -> list[dict]:
    if not UNIVERSE_PATH.exists():
        return []
    try:
        with UNIVERSE_PATH.open("r", encoding="utf-8-sig", newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("code")]
    except Exception:
        return []


def _write(rows: list[dict]):
    with UNIVERSE_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _target_day() -> str:
    """名单应该更新到哪一天：最近一个「该出数据」的交易日（与行情应到日同口径）。

    按自然日判会两头出错：周末/长假名单本来就停在周五，天天提示「非当日」
    等于制造噪音；反过来，行情已经到今天而名单还是三天前的，才是真的漏了。
    """
    try:
        import pipeline
        return pipeline._expected_day() or ""
    except Exception:
        return ""


def meta() -> dict:
    rows = _read()
    import store
    cached = set(store.cached_codes())
    updated = rows[0].get("updated", "-") if rows else "-"
    target = _target_day()
    lag = None
    if target and updated and updated != "-":
        try:
            import pipeline
            lag = pipeline._trade_lag(updated, target)
        except Exception:
            lag = None
    # 日历不可用时退回「当天」判断，宁可多提示一次，也不要假装最新
    fresh = bool(rows) and (lag == 0 if lag is not None else updated == _today())
    return {"ok": True, "total": len(rows), "updated": updated,
            "target": target, "lag": lag, "fresh": fresh,
            "cached": len([r for r in rows if r["code"] in cached]),
            "file": str(UNIVERSE_PATH)}


def _get_json(url: str, tries: int = 3, headers: dict | None = None):
    """本机代理会间歇性断连，逐次退避重试；不用 Session，避免连接复用放大问题。"""
    last = None
    for i in range(tries):
        try:
            return requests.get(url, timeout=15, headers=headers or HEADERS).json()
        except Exception as e:
            last = e
            time.sleep(0.25 * (i + 1))
    raise last


SINA_TPL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData?page={p}&num=100&sort=symbol&asc=1&node=hs_a")
SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}


def _fetch_eastmoney() -> dict[str, dict]:
    out: dict[str, dict] = {}
    total = None
    for pn in range(1, 71):
        try:
            d = (_get_json(URL_TPL.format(pn=pn), tries=2).get("data") or {})
            if total is None:
                total = int(d.get("total") or 0)
            arr = d.get("diff") or []
        except Exception:
            arr = []
        for x in arr:
            c = str(x.get("f12") or "").strip()
            if len(c) == 6 and c.isdigit():
                out[c] = {"code": c,
                          "name": str(x.get("f14") or c).strip().replace(" ", ""),
                          "market": market_of(c),
                          "price": x.get("f2") if isinstance(x.get("f2"), (int, float)) else "",
                          "pct": x.get("f3") if isinstance(x.get("f3"), (int, float)) else "",
                          "updated": _today()}
        if total and len(out) >= total:
            break
        time.sleep(0.1)
    return out


def _fetch_sina() -> dict[str, dict]:
    """独立第二源：新浪 hs_a，含北交所，按代码升序分页。"""
    out: dict[str, dict] = {}
    for p in range(1, 71):
        try:
            arr = _get_json(SINA_TPL.format(p=p), tries=2, headers=SINA_HEADERS)
        except Exception:
            arr = []
        if not isinstance(arr, list) or not arr:
            break
        for x in arr:
            c = str(x.get("code") or "").strip()
            if len(c) == 6 and c.isdigit():
                out[c] = {"code": c,
                          "name": str(x.get("name") or c).strip().replace(" ", ""),
                          "market": market_of(c),
                          "price": x.get("trade") or "", "pct": x.get("changepercent") or "",
                          "updated": _today()}
        if len(arr) < 100:
            break
        time.sleep(0.1)
    return out


def _fetch_tushare() -> dict[str, str]:
    """Tushare stock_basic：官方上市名单，一次请求，含北交所（baostock 不覆盖）。"""
    import datasource
    if not datasource.configured():
        return {}
    rows = datasource.call("stock_basic", {"list_status": "L"}, "ts_code,name")
    out: dict[str, str] = {}
    for r in rows:
        c = str(r.get("ts_code") or "")[:6]
        if len(c) == 6 and c.isdigit() and market_of(c) != "其他":
            out[c] = str(r.get("name") or c).strip().replace(" ", "")
    return out


def fetch_all(force: bool = False) -> dict:
    """刷新全 A 股名单：Tushare（若配置，官方含北交所）→ baostock 沪深 → 东财/新浪。"""
    if not force and meta()["fresh"]:
        return {**meta(), "source": "本地缓存（当日）", "fetched": 0}
    # 1) 一次请求拿全名单的源优先：Tushare 官方 > baostock（免限流但不含北交所）
    hs, src_name = {}, ""
    try:
        hs = _fetch_tushare()
        if len(hs) > 3000:
            src_name = "Tushare stock_basic（官方名单，含北交所）"
    except Exception:
        hs = {}
    if len(hs) <= 3000:
        try:
            import bulkdata
            hs = bulkdata.list_hs()
            src_name = "baostock（沪深，免限流）+ 旧缓存（北交所）"
        except Exception:
            hs = {}
    if len(hs) > 3000:
        old = {r["code"]: r for r in _read()}
        merged = {}
        for c, name in hs.items():
            p = old.get(c) or {}
            merged[c] = {"code": c, "name": name, "market": market_of(c),
                         "price": p.get("price", ""), "pct": p.get("pct", ""),
                         "updated": _today()}
        # 旧行只保留 baostock 不覆盖的北交所；其余不在 hs 里的（多为上证指数，
        # 如 sh.000300 沪深300、sh.000001 上证指数）一律丢弃，避免指数混进股票名单。
        for c, r in old.items():
            if c not in merged and market_of(c) == "北交所":
                merged[c] = r
        rows = sorted(merged.values(), key=lambda x: x["code"])
        _write(rows)
        return {**meta(), "source": src_name,
                "fetched": len(rows), "hs": len(hs), "bj": len(rows) - len(hs)}

    # 2) 回退：东财 / 新浪
    em = _fetch_eastmoney()
    sn = _fetch_sina() if len(em) < 5000 else {}
    use_sina = len(sn) > len(em)
    best = sn if use_sina else em
    source = ("新浪财经 hs_a（公开接口，免登录）" if use_sina
              else "东方财富 clist（公开接口，免登录）")
    old = _read()
    if len(best) < 1000:
        if old:
            return {**meta(), "source": "拉取不完整，回退本地缓存", "fetched": len(best),
                    "warning": "本次仅取到 %d 只（本机网络代理不稳定），已沿用上次缓存，可稍后重试" % len(best)}
        return {"ok": False, "error": "全 A 股名单拉取失败：行情站点连接不稳定，请稍后点「刷新名单」重试"}
    rows = sorted(best.values(), key=lambda x: x["code"])
    _write(rows)
    return {**meta(), "source": source, "fetched": len(rows),
            "warning": ("两源数量不一致（东财 %d / 新浪 %d），已采用较多者；公开列表口径与交易所可能有少量差异"
                        % (len(em), len(sn)))
            if sn and em and abs(len(em) - len(sn)) > 50 else ""}


def _filter(q: str, market: str, cached_only: bool) -> list[dict]:
    rows = _read()
    import store
    cached = set(store.cached_codes())
    q = (q or "").strip().lower()
    if q:
        rows = [r for r in rows if q in r["code"] or q in r["name"].lower()]
    if market and market != "all":
        rows = [r for r in rows if r["market"] == market]
    if cached_only:
        rows = [r for r in rows if r["code"] in cached]
    return rows


def query(q: str = "", market: str = "all", cached_only: bool = False,
          limit: int = 100, offset: int = 0) -> dict:
    rows = _filter(q, market, cached_only)
    import store
    cached = set(store.cached_codes())
    page = rows[int(offset):int(offset) + int(limit)]
    # 叠加实时快照（新鲜时覆盖名单里的旧价，避免显示「隔日价」）
    sd, sp = {}, {}
    try:
        import spot
        sp = spot.status()
        if sp.get("fresh"):
            sd = spot.load()
    except Exception:
        pass
    for r in page:
        r["cached"] = r["code"] in cached
        s = sd.get(r["code"])
        if s:
            r["price"] = s.get("price") or r.get("price")
            r["pct"] = s.get("pct") or r.get("pct")
    return {**meta(), "rows": page, "filtered": len(rows),
            "offset": int(offset), "limit": int(limit),
            "spot": sp,
            "markets": ["all", "沪市主板", "深市主板", "创业板", "科创板", "北交所"]}


def apply_to_watchlist(mode: str = "merge", q: str = "", market: str = "all",
                       cached_only: bool = False, cap: int = 6000) -> dict:
    """把筛选后的全 A 股名单加入 / 替换股票池（名单自带名称，无需再查接口）。"""
    import watchlist
    rows = _filter(q, market, cached_only)[:int(cap)]
    if not rows:
        return {"ok": False, "error": "筛选结果为空，请先调整搜索条件或刷新名单"}
    items = [{"code": r["code"], "name": r["name"], "source": "全A股名单"} for r in rows]
    if mode == "replace":
        return {**watchlist.mutate("replace", items=items), "applied": len(items)}
    st = watchlist._state()
    old = {i["code"]: i for i in st["items"]}
    added = 0
    for x in items:
        if x["code"] not in old:
            old[x["code"]] = {**x, "added": _today()}
            added += 1
    st["items"] = list(old.values())
    if not any(i["code"] == st.get("default") for i in st["items"]):
        st["default"] = st["items"][0]["code"] if st["items"] else ""
    watchlist._save(st)
    return {**watchlist.list_view(), "ok": True, "applied": len(items), "added": added}


# --------------------------------------------------------------------------
# 行情缓存预热（串行限速、断点续传、可停止）
# --------------------------------------------------------------------------

def prewarm_status() -> dict:
    m = meta()
    return {"ok": True, **{k: v for k, v in _PREWARM.items() if k != "stop"},
            "universe_total": m["total"], "cached_total": m["cached"]}


def _worker(codes: list[str]):
    import quant
    cfg = quant.load_config()
    _PREWARM.update(running=True, done=0, total=len(codes), ok=0, fail=0,
                    last="", stop=False, message="正在预热行情缓存")
    for c in codes:
        if _PREWARM["stop"]:
            _PREWARM["message"] = "已手动停止"
            break
        try:
            d, src = quant.fetch_history(c, cfg)
            if len(d) > 20:
                _PREWARM["ok"] += 1
            else:
                _PREWARM["fail"] += 1
        except Exception:
            _PREWARM["fail"] += 1
        _PREWARM["done"] += 1
        _PREWARM["last"] = c
        time.sleep(0.45)
    else:
        _PREWARM["message"] = "本批完成"
    _PREWARM["running"] = False


def prewarm_start(limit: int = 200, only_in_pool: bool = False) -> dict:
    if _PREWARM["running"]:
        return {**prewarm_status(), "warning": "预热正在进行中"}
    import watchlist
    pool = set(watchlist.codes()) if only_in_pool else None
    rows = _read()
    import store
    have = set(store.cached_codes())
    todo = [r["code"] for r in rows
            if r["code"] not in have
            and (pool is None or r["code"] in pool)]
    todo = todo[:int(limit)]
    if not todo:
        return {**prewarm_status(), "message": "没有需要预热的股票（已全部缓存）", "started": 0}
    t = threading.Thread(target=_worker, args=(todo,), daemon=True)
    t.start()
    return {"ok": True, "started": len(todo), "message": "后台预热已启动"}


def prewarm_stop() -> dict:
    _PREWARM["stop"] = True
    return {"ok": True, "message": "已发送停止信号，当前请求结束后暂停"}
