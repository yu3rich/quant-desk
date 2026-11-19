# -*- coding: utf-8 -*-
"""QuantDesk 本地服务入口：Flask + 静态前端，仅监听 127.0.0.1。"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from importlib import metadata
from pathlib import Path

import pandas as pd
from flask import Flask, Response, jsonify, request, send_from_directory

import quant
import watchlist
import universe
import screener
import mlmodel
import llm
import industry
import datasource
import review
import bulkdata
import spot
import chartdata
import pipeline
import scheduler
import emtlink
import emtbook
import emtai
import store
import journal

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
# 后验重放结果的小缓存：key → (时间戳, 响应体)
_REVIEW_CACHE: dict = {}
app = Flask(__name__, static_folder=None)

# NaN / Infinity 不是合法 JSON：Python 的 json 会原样输出，浏览器 JSON.parse 会直接失败，
# 表现为「接口看着 200，前端整页空白」。这里统一在出口处清洗，避免任何模块漏掉。
import math as _math


def _json_safe(o):
    if isinstance(o, float):
        return None if (_math.isnan(o) or _math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


_orig_dumps = app.json.dumps


def _safe_dumps(obj, **kw):
    return _orig_dumps(_json_safe(obj), **kw)


app.json.dumps = _safe_dumps

# 接口返回的是 JSON，候选榜一次就有上千行、行业成分股几百行，压缩比通常 5~8 倍。
# 本地回环虽然快，但浏览器要解析的东西少了，首屏渲染的前后感知差别很明显。
import gzip as _gzip


@app.after_request
def _gzip_json(resp):
    try:
        if resp.status_code != 200 or resp.direct_passthrough:
            return resp
        if resp.mimetype != "application/json":
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or ""):
            return resp
        raw = resp.get_data()
        if len(raw) < 1024:
            return resp
        packed = _gzip.compress(raw, 6)
        if len(packed) >= len(raw) * 0.92:
            return resp
        resp.set_data(packed)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(packed))
        resp.headers["Vary"] = "Accept-Encoding"
    except Exception:
        pass
    return resp


def cfg():
    return quant.load_config()


def _latest_bar_day() -> str:
    """行情缓存里的最新交易日（取几只不会长期停牌的大票里最新的那个）。

    只读文件尾部，0.1 毫秒级；不要用「读一只票的全表」那种做法，没必要。
    """
    import histcache
    best = ""
    for c in ("600000", "000001", "600519", "000002"):
        b = histcache.last_bar(c)
        if b and b["date"] > best:
            best = b["date"]
    return best[:10]


def ok(**kw):
    return jsonify({"ok": True, **kw})


def fail(e):
    return jsonify({"ok": False, "error": str(e)})


def _shortcut(kind: str, name: str, fn, data_day: str = ""):
    """各页快捷单步操作：执行并把结果落进 store.runs（数据中枢统一更新记录）。

    流水线整段更新由 pipeline.run 自己落库；这里只记各页按钮触发的单步，
    trigger=shortcut 与流水线/定时任务区分开。异步任务（增量行情/行业）在各自
    模块的完成点落库，不走这个同步包装。
    """
    import store
    t0 = time.time()
    try:
        r = fn()
        d = r if isinstance(r, dict) else {}
        okf = bool(d.get("ok", True))
        ms = int((time.time() - t0) * 1000)
        detail = str(d.get("error") or d.get("message") or "")[:200]
        store.run_log(kind, okf, steps=[{"name": name, "ok": okf, "ms": ms, "detail": detail}],
                      trigger="shortcut", data_day=data_day,
                      error=str(d.get("error") or "")[:300], ms=ms)
        return r
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        store.run_log(kind, False, steps=[{"name": name, "ok": False, "ms": ms,
                                           "detail": str(e)[:200]}],
                      trigger="shortcut", error=str(e)[:300], ms=ms)
        raise


@app.route("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.route("/static/<path:fn>")
def static_files(fn):
    return send_from_directory(WEB, fn)


@app.route("/api/status")
def status():
    """首屏只消费少量字段；不再逐个读取缓存 meta、也不回传整份股票池。"""
    try:
        c = cfg()
        try:
            ak_ver = metadata.version("akshare")
        except Exception:
            ak_ver = "未安装"
        import store
        cache_count = store.cached_count()
        return ok(python=sys.version.split()[0], akshare=ak_ver,
                  port=c.get("port", 8765), cache_count=cache_count,
                  akshare_broken=quant._AKSHARE_BROKEN, config=c)
    except Exception as e:
        return fail(e)


@app.route("/api/backtest", methods=["POST"])
def backtest():
    try:
        b = request.get_json(force=True) or {}
        code = str(b.get("code", "601318")).zfill(6)
        start = str(b.get("start", "20220101"))
        end = str(b.get("end") or dt.date.today().strftime("%Y%m%d"))
        c = dict(cfg())
        for k in ("capital", "commission", "min_commission", "stamp_tax",
                  "transfer_fee", "slippage", "limit_pct"):
            if b.get(k) is not None:
                c[k] = b[k]
        d, source = quant.get_quotes(code, start, end,
                                     refresh=bool(b.get("refresh")), cfg=c)
        r = quant.run_backtest(d, strategy=str(b.get("strategy", "ma_cross")),
                               params=b.get("params") or {}, cfg=c,
                               capital=b.get("capital"))
        r.update(code=code, name=quant.stock_name(code, cfg()),
                 source=source, start=start, end=end,
                 date_from=str(d["date"].iloc[0]), date_to=str(d["date"].iloc[-1]),
                 bars=int(len(d)))
        return jsonify(r)
    except Exception as e:
        return fail(e)


@app.route("/api/scan", methods=["POST"])
def scan():
    try:
        b = request.get_json(force=True) or {}
        code = str(b.get("code", "601318")).zfill(6)
        c = dict(cfg())
        for k in ("commission", "min_commission", "stamp_tax",
                  "transfer_fee", "slippage", "limit_pct", "capital"):
            if b.get(k) is not None:
                c[k] = b[k]
        d, source = quant.get_quotes(code, str(b.get("start", "20220101")),
                                     str(b.get("end") or dt.date.today().strftime("%Y%m%d")),
                                     cfg=c)
        r = quant.scan_params(d, b.get("grid") or {}, cfg=c)
        r.update(code=code, name=quant.stock_name(code, cfg()), source=source)
        return jsonify(r)
    except Exception as e:
        return fail(e)


# --------------------------------------------------------------------------
# 股票池：完全由用户管理，独立于 config.json
# --------------------------------------------------------------------------

@app.route("/api/watchlist", methods=["GET", "POST"])
def watchlist_api():
    try:
        if request.method == "GET":
            return jsonify(watchlist.list_view())
        b = request.get_json(force=True) or {}
        action = str(b.get("action") or "")
        allowed = ("add", "remove", "rename", "default", "move", "clear",
                   "replace", "remove_source")
        if action not in allowed:
            return fail("未知操作：" + action)
        kw = {k: b[k] for k in ("code", "name", "dir", "items", "source") if k in b}
        return jsonify(watchlist.mutate(action, **kw))
    except Exception as e:
        return fail(e)


@app.route("/api/watchlist/export")
def watchlist_export():
    try:
        fmt = request.args.get("format", "txt")
        body = watchlist.export_text(fmt)
        mime = "application/json; charset=utf-8" if fmt == "json" else "text/plain; charset=utf-8"
        return Response("\ufeff" + body if fmt == "txt" else body, mimetype=mime)
    except Exception as e:
        return fail(e)


@app.route("/api/symbol")
def symbol_lookup():
    try:
        return jsonify(watchlist.lookup(request.args.get("q", "")))
    except Exception as e:
        return fail(e)


@app.route("/api/indices")
def index_quotes():
    """行情走势顶部的五个主要指数：上证 / 深证 / 创业板 / 北证 / 科创。"""
    try:
        return jsonify(chartdata.indices())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160]})


@app.route("/api/chart")
def chart_data():
    """行情看图：period = intraday / five / day / week / month（分时、五日、日、周、月）。

    与回测用的后复权缓存分开：这里统一走腾讯前复权，最新价与实时行情一致。
    """
    try:
        code = request.args.get("code", "")
        period = request.args.get("period", "day")
        if not code:
            return jsonify({"ok": False, "error": "缺少股票代码"})
        return jsonify(chartdata.get(code, period))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160]})


# --------------------------------------------------------------------------
# 东方财富账户：仅官方 API Key 授权，只读同步自选股
# --------------------------------------------------------------------------

@app.route("/api/account", methods=["GET", "POST"])
def account_api():
    try:
        if request.method == "POST":
            b = request.get_json(force=True) or {}
            if b.get("action") == "clear":
                r = watchlist.set_apikey("")
            elif b.get("apikey") is not None:
                r = watchlist.set_apikey(b["apikey"])
            else:
                return fail("缺少 apikey")
            return jsonify({**r, **watchlist.account_status()})
        return jsonify(watchlist.account_status())
    except Exception as e:
        return fail(e)


@app.route("/api/account/sync", methods=["POST"])
def account_sync():
    try:
        b = request.get_json(force=True) or {}
        return jsonify(watchlist.sync_account(b.get("mode") or "merge"))
    except Exception as e:
        return fail(e)


# --------------------------------------------------------------------------
# 全 A 股名单 / 行情预热 / 实盘成交导入
# --------------------------------------------------------------------------

@app.route("/api/universe")
def universe_api():
    try:
        return jsonify(universe.query(
            q=request.args.get("q", ""),
            market=request.args.get("market", "all"),
            cached_only=request.args.get("cached", "0") in ("1", "true", "True"),
            limit=max(1, min(int(request.args.get("limit", 100)), 500)),
            offset=max(0, int(request.args.get("offset", 0)))))
    except Exception as e:
        return fail(e)


@app.route("/api/universe/refresh", methods=["POST"])
def universe_refresh():
    try:
        return jsonify(_shortcut("universe", "全 A 名单",
                                 lambda: universe.fetch_all(force=True)))
    except Exception as e:
        return fail(e)


@app.route("/api/universe/apply", methods=["POST"])
def universe_apply():
    try:
        b = request.get_json(force=True) or {}
        return jsonify(universe.apply_to_watchlist(
            mode=b.get("mode") or "merge", q=b.get("q") or "",
            market=b.get("market") or "all",
            cached_only=bool(b.get("cached_only")),
            cap=int(b.get("cap") or 6000)))
    except Exception as e:
        return fail(e)


@app.route("/api/universe/prewarm", methods=["GET", "POST"])
def universe_prewarm():
    try:
        if request.method == "POST":
            b = request.get_json(force=True) or {}
            if b.get("action") == "stop":
                return jsonify(universe.prewarm_stop())
            return jsonify(universe.prewarm_start(
                limit=int(b.get("limit") or 200),
                only_in_pool=bool(b.get("only_in_pool"))))
        return jsonify(universe.prewarm_status())
    except Exception as e:
        return fail(e)


# --------------------------------------------------------------------------
# 全市场行情批量下载 / 增量更新（baostock 后复权，北交所走腾讯兜底）
# --------------------------------------------------------------------------

@app.route("/api/bulk", methods=["GET", "POST"])
def bulk_api():
    try:
        if request.method == "GET":
            return jsonify(bulkdata.status())
        b = request.get_json(silent=True) or {}
        if b.get("action") == "stop":
            return jsonify(bulkdata.stop())
        return jsonify(bulkdata.start(
            mode=str(b.get("mode") or "incremental"),
            limit=int(b["limit"]) if b.get("limit") else None,
            scope=str(b.get("scope") or "all"),
            workers=int(b["workers"]) if b.get("workers") else None))
    except Exception as e:
        return fail(e)


@app.route("/api/spot", methods=["GET", "POST"])
def spot_api():
    """全市场实时快照：GET 查状态，POST action=refresh 触发刷新（腾讯批量）。"""
    try:
        if request.method == "GET":
            return jsonify(spot.status())
        b = request.get_json(silent=True) or {}
        if b.get("action") == "refresh":
            return jsonify(_shortcut("spot", "实时快照",
                                     lambda: spot.refresh(codes=b.get("codes") or None)))
        return fail("未知操作：" + str(b.get("action")))
    except Exception as e:
        return fail(e)


# --------------------------------------------------------------------------
# 实盘复盘（emtl 只读接入 + 本地账本）
# --------------------------------------------------------------------------

@app.route("/api/emt/status")
def emt_status():
    try:
        return jsonify(emtlink.status())
    except Exception as e:
        return fail(e)


@app.route("/api/emt/config", methods=["POST"])
def emt_config():
    """保存东财账号。密码用本机派生密钥加密后落盘，接口只返回掩码。"""
    try:
        b = request.get_json(force=True) or {}
        kw = {}
        if "user" in b:
            kw["user"] = str(b.get("user") or "").strip()
        if "password" in b:
            kw["password"] = str(b.get("password") or "")
        if b.get("clear_password"):
            kw["password"] = ""
            kw["clear_password"] = True
        for k in ("enabled", "auto_sync", "auto_refresh"):
            if k in b:
                kw[k] = bool(b.get(k))
        if "auto_time" in b:
            kw["auto_time"] = str(b.get("auto_time") or "15:35")
        if "refresh_min" in b:
            try:
                kw["refresh_min"] = max(1, min(60, int(b.get("refresh_min") or 5)))
            except Exception:
                kw["refresh_min"] = 5
        # 只有改凭据才丢会话；切自动刷新开关不该逼着下一次重新登录
        if any(k in b for k in ("user", "password", "clear_password")):
            emtlink.logout()
        emtlink.save_cfg(**kw)
        return jsonify(dict(emtlink.status(), ok=True))
    except Exception as e:
        return fail(e)


@app.route("/api/emt/test", methods=["POST"])
def emt_test():
    """试连一次：只验证能不能登录 + 拿到资金，不改账本。"""
    try:
        snap = emtlink.raw_snapshot()
        st = emtlink.status()
        return ok(overview=snap["overview"], positions=len(snap["positions"]),
                  orders=len(snap["orders"]), at=snap["at"], user=st["user"])
    except Exception as e:
        return fail(e)


@app.route("/api/emt/sync", methods=["POST"])
def emt_sync():
    try:
        b = request.get_json(silent=True) or {}
        return jsonify(_shortcut("ledger", "实盘同步",
                                 lambda: emtlink.sync(note=str(b.get("note") or ""))))
    except Exception as e:
        return fail(e)


@app.route("/api/emt/logout", methods=["POST"])
def emt_logout():
    try:
        emtlink.logout()
        return jsonify(dict(emtlink.status(), ok=True))
    except Exception as e:
        return fail(e)


@app.route("/api/emt/board")
def emt_board():
    try:
        days = int(request.args.get("days") or 180)
        b = emtbook.board(days=days)
        b["ai"] = emtai.state(b)          # 只读状态：不触发模型、不写盘
        return jsonify(b)
    except Exception as e:
        return fail(e)


@app.route("/api/emt/journal", methods=["GET", "POST"])
def emt_journal():
    try:
        if request.method == "GET":
            return ok(day=request.args.get("day") or "",
                      journal=emtlink.journal_get(request.args.get("day") or ""))
        b = request.get_json(force=True) or {}
        return jsonify(emtlink.journal_set(b.get("day") or "", b.get("text") or "",
                                           src="manual"))
    except Exception as e:
        return fail(e)


@app.route("/api/journal", methods=["GET", "POST"])
def journal_book():
    """复盘日记本：一天一篇 Markdown。

    GET  —— 列出 data/journal/*.md（日期倒序，带来源/字数/预览）
    POST —— 保存某天（day + text），同一天再次保存原地覆盖
    """
    try:
        if request.method == "GET":
            journal.seed_from_ledger()   # 旧账本日记首次访问时迁成 .md
            try:
                limit = int(request.args.get("limit") or 500)
            except Exception:
                limit = 500
            return jsonify(journal.list_entries(limit=max(1, min(limit, 2000))))
        b = request.get_json(force=True) or {}
        r = journal.save(b.get("day") or "", b.get("text") or "",
                         src=str(b.get("src") or "manual"))
        if r.get("ok"):
            emtlink.journal_mirror(r.get("day") or "")   # 账本日存在时同步旧字段
        return jsonify(r)
    except Exception as e:
        return fail(e)


@app.route("/api/journal/<day>", methods=["GET", "DELETE"])
def journal_entry(day):
    """单日日记：GET 读取正文，DELETE 删除该文件。"""
    try:
        if request.method == "DELETE":
            r = journal.delete(day)
            if r.get("ok"):
                emtlink.journal_clear(day)
            return jsonify(r)
        return jsonify(journal.read(day))
    except Exception as e:
        return fail(e)


@app.route("/api/emt/ai/config", methods=["GET", "POST"])
def emt_ai_config():
    """复盘 AI 开关：日记与体检各自独立选择 local / ai。"""
    try:
        if request.method == "GET":
            return jsonify(emtai.status())
        return jsonify(emtai.configure(request.get_json(force=True) or {}))
    except Exception as e:
        return fail(e)


@app.route("/api/emt/ai/generate", methods=["POST"])
def emt_ai_generate():
    """生成当日日记或行为体检。force=true 才会覆盖手写日记。"""
    try:
        b = request.get_json(silent=True) or {}
        return jsonify(emtai.generate(kind=str(b.get("kind") or ""),
                                      day=str(b.get("day") or ""),
                                      force=bool(b.get("force"))))
    except Exception as e:
        return fail(e)


@app.route("/api/emt/history", methods=["GET", "POST"])
def emt_history():
    """
    GET  —— 历史数据现状（已拉多少条、覆盖到哪天）
    POST —— 拉历史成交 / 历史订单 / 资金流水，按月切片写入 CSV

    这是 emtl 相对 emta 的关键能力：不用手工导入交割单，也能拿到一年前的成交。
    """
    try:
        if request.method == "GET":
            return jsonify(dict(emtlink.hist_state(), ok=True))
        b = request.get_json(silent=True) or {}
        start = str(b.get("start") or "")
        end = str(b.get("end") or "")
        size = int(b.get("size") or 500)
        what = b.get("what") or ["trades", "orders", "flows"]
        what = tuple(w for w in what if w in ("trades", "orders", "flows")) or ("trades",)
        r = emtlink.pull_history(start, end, size, what)
        return jsonify(dict(r, state=emtlink.hist_state()))
    except Exception as e:
        return fail(e)


@app.route("/api/emt/probe")
def emt_probe():
    """字段探测：成交/委托/流水各自的原始字段，标出已识别与未识别。"""
    try:
        return jsonify(dict(emtlink.probe(), ok=True))
    except Exception as e:
        return fail(e)


# 最近一次选股结果（仅进程内内存，用于 LLM 重排默认候选，不落盘）
LAST_RUN: dict = {"rows": [], "as_of": "", "horizon": None, "engine": "", "at": "", "count": 0}


@app.route("/api/screen/status")
def screen_status():
    """框架总状态：数据层 / 模型层 / LLM 层 + 模块清单与行业覆盖率。"""
    try:
        return jsonify({"ok": True, "data": screener.status(),
                        "model": mlmodel.status(), "llm": llm.status()})
    except Exception as e:
        return fail(e)


@app.route("/api/screen/freshness")
def screen_freshness():
    """流水线时效：行情 / 快照 / 训练集 / 模型各落后几个交易日，以及建议动作。"""
    try:
        return jsonify(pipeline.freshness())
    except Exception as e:
        return fail(e)


@app.route("/api/screen/pipeline", methods=["POST"])
def screen_pipeline():
    """一键更新流水线。level=auto（按需）/ quick（只重算快照）/ full（全套）。
    quote=true 时先做增量行情；全程后台跑，前端轮询 freshness 看进度。"""
    try:
        b = request.get_json(silent=True) or {}
        level = str(b.get("level") or "auto")
        if level not in ("auto", "quick", "full"):
            level = "auto"
        only = b.get("only")
        if isinstance(only, list):
            only = [str(x) for x in only if str(x) in ("quote", "snapshot", "dataset", "model")]
        else:
            only = None
        workers = int(b["workers"]) if b.get("workers") else None
        return jsonify(pipeline.run_async(level, bool(b.get("quote")),
                                          trigger=str(b.get("trigger") or "manual"),
                                          only=only, workers=workers))
    except Exception as e:
        return fail(e)


@app.route("/api/screen/scheduler", methods=["GET", "POST"])
def screen_scheduler():
    """内置定时任务：GET 看状态与下次计划；POST 改开关/时刻/星期/是否先更新行情。

    定时器活在服务进程里（scheduler.py），不是系统计划任务 —— 服务不在时不该跑。
    """
    try:
        if request.method == "GET":
            return jsonify(scheduler.status())
        b = request.get_json(silent=True) or {}
        patch = {}
        for k in ("enabled", "time", "weekdays", "quote", "level", "window_min"):
            if k in b:
                patch[k] = b[k]
        if patch:
            scheduler.save(patch)
        return jsonify(scheduler.status())
    except Exception as e:
        return fail(e)


@app.route("/api/screen/industry", methods=["GET", "POST"])
def screen_industry():
    """行业分类：GET 查状态；POST 触发同步（申万一级为主 + 证监会补全，约 40~90 秒，后台跑）。"""
    try:
        if request.method == "GET":
            return jsonify(industry.status())
        b = request.get_json(silent=True) or {}
        if b.get("wait"):
            return jsonify(industry.sync(force=bool(b.get("force", True))))
        return jsonify(industry.sync_async(force=bool(b.get("force", True))))
    except Exception as e:
        return fail(e)


@app.route("/api/industry/list")
def industry_list():
    """行业列表：成分数、有行情数、20 日等权涨幅、5 日平均换手（按强度降序）。"""
    try:
        return jsonify({**industry.summary(), "meta": industry.status()})
    except Exception as e:
        return fail(e)


@app.route("/api/industry/members")
def industry_members():
    """某行业的成分股（现价/涨跌/PE/PB/市值/是否已缓存）。"""
    try:
        name = str(request.args.get("name") or "").strip()
        if not name:
            return fail("缺少行业名称")
        limit = int(request.args.get("limit", 400))
        return jsonify(industry.members(name, limit=limit))
    except Exception as e:
        return fail(e)


@app.route("/api/sources", methods=["GET", "POST"])
def sources_api():
    """可选账号数据源（Tushare Pro）：GET 状态，POST 保存 / 清除 Token。"""
    try:
        if request.method == "GET":
            return jsonify(datasource.status())
        b = request.get_json(silent=True) or {}
        if b.get("action") == "clear":
            return jsonify(datasource.clear())
        return jsonify(datasource.save(b.get("token"), b.get("enabled")))
    except Exception as e:
        return fail(e)


@app.route("/api/sources/test", methods=["POST"])
def sources_test():
    try:
        return jsonify(datasource.test())
    except Exception as e:
        return fail(e)


@app.route("/api/sources/fetch", methods=["POST"])
def sources_fetch():
    """拉取某个交易日的全市场估值（PE/PB/市值/换手/量比）。"""
    try:
        b = request.get_json(silent=True) or {}
        return jsonify(_shortcut("valuation", "全市场估值",
                                 lambda: datasource.fetch_daily_basic(
                                     b.get("trade_date"), force=bool(b.get("force")))))
    except Exception as e:
        return fail(e)


# --------------------------------------------------------------------------
# 数据中枢：统一配置 / 统一更新 / 更新记录（各页快捷仍保留，记录汇入此处）
# --------------------------------------------------------------------------

@app.route("/api/data/hub")
def data_hub():
    """一页聚合：数据链时效 + 定时任务 + 各数据源状态 + 并发参数 + 最近更新记录。"""
    try:
        c = cfg()
        return ok(
            freshness=pipeline.freshness(),
            scheduler=scheduler.status(),
            sources=datasource.status(),
            emt=emtlink.status(),
            llm=llm.status(),
            bulk_workers=int(c.get("bulk_workers", 6) or 6),
            runs=pipeline.history(40))
    except Exception as e:
        return fail(e)


@app.route("/api/data/run", methods=["POST"])
def data_run():
    """中枢统一更新：level=auto/quick/full，或 only 显式选段；后台跑，轮询 freshness。"""
    try:
        b = request.get_json(silent=True) or {}
        level = str(b.get("level") or "auto")
        if level not in ("auto", "quick", "full"):
            level = "auto"
        only = b.get("only")
        if isinstance(only, list):
            only = [str(x) for x in only if str(x) in ("quote", "snapshot", "dataset", "model")]
            only = only or None
        else:
            only = None
        workers = int(b["workers"]) if b.get("workers") else None
        return jsonify(pipeline.run_async(level, bool(b.get("quote")),
                                          trigger="manual", only=only, workers=workers))
    except Exception as e:
        return fail(e)


@app.route("/api/data/runs")
def data_runs():
    """统一更新记录（流水线 + 各页快捷 + 定时任务），最新在前。"""
    try:
        limit = max(1, min(int(request.args.get("limit", 60)), 200))
        return ok(runs=pipeline.history(limit))
    except Exception as e:
        return fail(e)


@app.route("/api/data/config", methods=["POST"])
def data_config():
    """保存中枢里的全局参数（目前仅行情并发数 bulk_workers，1~8）。"""
    try:
        b = request.get_json(silent=True) or {}
        patch = {}
        if "bulk_workers" in b:
            patch["bulk_workers"] = max(1, min(8, int(b.get("bulk_workers") or 6)))
        if patch:
            quant.save_config(patch)
        return ok(bulk_workers=int(cfg().get("bulk_workers", 6) or 6))
    except Exception as e:
        return fail(e)


@app.route("/api/screen/review")
def screen_review():
    """T+N 后验：历史重放（当前打分逻辑）+ 真实候选的到期回看。"""
    try:
        days = int(request.args.get("days", 60))
        step = int(request.args.get("step", 5))
        topn = int(request.args.get("topn", 10))
        # 历史重放要给 200 只股票各算一遍二十年因子，冷跑 6 秒。
        # 它只依赖「数据日 + 参数 + 模型」，这几个量都没变就直接给上次的结果。
        snap = screener._json_read(screener.SNAPSHOT_META, {}) or {}
        key = ("review", days, step, topn, _latest_bar_day(),
               str(snap.get("generated") or ""), str(snap.get("as_of") or ""))
        hit = _REVIEW_CACHE.get(key)
        if hit and time.time() - hit[0] < 900:
            return jsonify(hit[1])
        sc = review.scorecard(days=days, step=step, topn=topn)
        tr = review.track_record()
        if not sc.get("ok"):
            return jsonify({"ok": False, "error": sc.get("error"),
                            "track": tr})
        out = {**sc, "track": tr}
        if len(_REVIEW_CACHE) > 8:
            _REVIEW_CACHE.clear()
        _REVIEW_CACHE[key] = (time.time(), out)
        return jsonify(out)
    except Exception as e:
        return fail(e)



@app.route("/api/screen/picks", methods=["GET", "POST"])
def screen_picks():
    """选股记录：每次选股一个批次，含记录价、最新价与 T+N 后验；POST 删除整批。"""
    try:
        if request.method == "POST":
            b = request.get_json(force=True) or {}
            if b.get("action") != "delete":
                return fail("未知操作：" + str(b.get("action")))
            as_of = str(b.get("as_of") or "")[:10]
            rec = str(b.get("recorded_at") or "")
            if not as_of or not rec:
                return fail("缺少批次标识（as_of / recorded_at）")
            import store
            n = store.picks_delete(as_of, rec)
            return ok(deleted=n)
        return jsonify(review.picks_log(limit=int(request.args.get("limit", 40))))
    except Exception as e:
        return fail(e)


@app.route("/api/screen/snapshot", methods=["POST"])
def screen_snapshot():
    """生成横截面因子快照（只读本地缓存，不联网）。"""
    try:
        b = request.get_json(silent=True) or {}
        return jsonify(screener.build_snapshot(
            scope=str(b.get("scope") or "cached"),
            as_of=(b.get("as_of") or None),
            limit=screener.norm_limit(b.get("limit"))))
    except Exception as e:
        return fail(e)


@app.route("/api/screen/dataset", methods=["POST"])
def screen_dataset():
    """构造多期横截面训练样本（标签使用未来收益，仅供模型训练）。"""
    try:
        b = request.get_json(silent=True) or {}
        return jsonify(screener.build_dataset(
            start=str(b.get("start") or "20220101"),
            end=b.get("end") or dt.date.today().strftime("%Y%m%d"),
            horizon=int(b.get("horizon") or 10), step=int(b.get("step") or 5),
            max_symbols=int(b.get("max_symbols") or 300),
            scope=str(b.get("scope") or "cached"),
            neutral_mode=str(b.get("neutral") or "none"),
            label_mode=str(b.get("label_mode") or "rank")))
    except Exception as e:
        return fail(e)


@app.route("/api/screen/train", methods=["POST"])
def screen_train():
    """训练 LightGBM（缺失时自动降级岭回归），时序切分验证。"""
    try:
        b = request.get_json(silent=True) or {}
        return jsonify(mlmodel.train(b.get("params") or {}))
    except Exception as e:
        return fail(e)


@app.route("/api/screen/run", methods=["POST"])
def screen_run():
    """一键选股：横截面快照 → 模型/基准打分 → 漏斗过滤 → 行业约束 → TopN 候选。"""
    try:
        b = request.get_json(silent=True) or {}
        topn = max(1, min(int(b.get("topn") or 10), 100))
        use_model = b.get("use_model", True) in (True, "1", "true", "True")
        cap = b.get("industry_cap")
        cap = screener.INDUSTRY_CAP_DEFAULT if cap is None else max(0, min(int(cap), 20))
        funnel_cfg = b.get("funnel") if isinstance(b.get("funnel"), dict) else None
        want_neutral = str(b.get("neutral") or "auto")
        scope = str(b.get("scope") or "cached")
        limit = screener.norm_limit(b.get("limit"))
        want_as_of = str(b.get("as_of") or "")[:10]

        # 「优先复用」语义：有快照直接复用，没有才现建，避免首屏因缺缓存而空白报错
        snap_df, snap_meta = screener.load_snapshot() if b.get("reuse_snapshot") else (None, {})
        # 磁盘快照若是历史截止日的产物，不能当「最新横截面」复用，必须现建；
        # 反过来，用户指定了历史截止日时也不能复用「最新」快照——否则截止日被静默忽略，
        # 界面显示的是最新截面，和历史回看的结果对不上。
        if snap_df is not None and (str(snap_meta.get("as_of_param") or "") or want_as_of):
            snap_df, snap_meta = None, {}
        # 快照过期（数据日落后于行情缓存）也不能复用，否则界面显示「已是最新」却拿着旧截面打分。
        # 行情缓存只增不改，重算一次全市场快照要 7 秒左右，这个校验只要 0.1 毫秒，没有理由省。
        reused = False
        if snap_df is not None:
            latest = _latest_bar_day()
            # 数据日之外，范围与样本上限也得一致：否则改了「全市场 / 上限」还复用
            # 旧截面，界面上的样本数会停在上一组参数，看着像参数没生效。
            same_cfg = (str(snap_meta.get("scope") or "cached") == scope
                        and screener.norm_limit(snap_meta.get("limit")) == limit)
            if (latest and str(snap_meta.get("as_of") or "")[:10] != latest) or not same_cfg:
                snap_df, snap_meta = None, {}
            else:
                reused = True
        built = None
        if snap_df is not None:
            rows = screener._rows(snap_df)
        else:
            built = screener.build_snapshot(
                scope=scope,
                as_of=b.get("as_of") or None,
                limit=limit)
            if not built.get("ok"):
                return jsonify(built)
            rows, snap_meta = built["rows"], built
        r = mlmodel.score_snapshot(rows, use_model=use_model, snapshot_meta=snap_meta,
                                   neutral=want_neutral)
        if not r.get("ok"):
            return jsonify(r)
        if built:
            r["snapshot_built"] = True
        r["snapshot_reused"] = bool(reused)

        scored = pd.DataFrame(r.pop("rows", []))
        if scored.empty:
            return jsonify({"ok": False, "error": "打分结果为空"})
        # 1) 漏斗：逐层剔除硬性不合格样本（缺数据放行，不做黑箱）
        kept, stages = screener.funnel(scored, funnel_cfg)
        if kept.empty:
            return jsonify({"ok": False, "error": "漏斗过滤后没有剩余样本，请放宽「极端风险 / 趋势」条件",
                            "funnel": {"stages": stages}})
        # 2) 行业约束 + TopN
        sel = screener.select(kept, topn=topn, cap=cap)
        # 3) 落盘候选，交给 T+N 后验。首屏自动出数会带 record=False：
        #    那种跑法只是把界面填满，不是用户的一次选股，写进记录会污染历史。
        # 历史截止日只用于回看，不能覆盖那一天的实盘候选记录：
# 同 as_of 的记录是整期替换的，一次历史实验就会把真实留档冲掉。
        latest_day = _latest_bar_day()
        if b.get("record", True) and want_as_of and latest_day and want_as_of != latest_day:
            rec = {"ok": True, "skipped": True,
                   "note": "历史截止日回看，不计入选股记录"}
        elif b.get("record", True):
            rec = screener.record_picks(sel["picked"], r.get("as_of", ""))
        else:
            rec = {"ok": True, "skipped": True, "note": "首屏自动出数，不计入选股记录"}
        r["funnel"] = {"stages": stages, "dropped": len(scored) - len(kept),
                       "kept": len(kept)}
        r["industry"] = {"cap": sel["cap"], "capped": sel["capped"],
                         "trimmed": sel["trimmed"], "trimmed_count": sel["trimmed_count"],
                         "counts": sel["industry_counts"], "note": sel["note"],
                         "exposure": sel.get("exposure") or {}}
        r["record"] = rec
        # 最近一次真正落盘的选股时间：前端据此判断「今天是否已经选过」，
        # 避免只按数据日（天然滞后）判断而误亮「重新选股」的彩虹提醒。
        try:
            r["last_pick_at"] = str(store.picks_version()[1] or "")
        except Exception:
            r["last_pick_at"] = ""
        r["topn"] = topn
        r["count"] = len(kept)
        r["candidates"] = sel["picked"]
        # 候选表的 close 是后复权价（因子口径），直接给人看会差出复权因子
        # （罗曼股份 hfq 97.87 vs 真实 52）。用腾讯昨收/现价把展示价换算回真实价，
        # 涨跌幅与收益口径不受影响（本就同口径相除）。
        try:
            import histcache
            latest_day = _latest_bar_day()
            qmap = spot.prev_closes([c["code"] for c in r["candidates"]])
            for c in r["candidates"]:
                q = qmap.get(c["code"])
                hfq_last, last_date = c.get("close"), str(c.get("date") or "")[:10]
                if latest_day and last_date != latest_day:
                    lb = histcache.last_bar(c["code"])
                    if lb:
                        hfq_last, last_date = lb["close"], lb["date"]
                c["close_real"] = spot.real_price(c.get("close"), hfq_last, last_date, q)
        except Exception:
            pass
        LAST_RUN.update(rows=r["candidates"], as_of=r.get("as_of", ""),
                        horizon=(r.get("model") or {}).get("dataset", {}).get("horizon"),
                        engine=r.get("engine", ""), at=dt.datetime.now().isoformat(timespec="seconds"),
                        count=len(scored))
        return jsonify(r)
    except Exception as e:
        return fail(e)


@app.route("/api/screen/llm", methods=["POST"])
def screen_llm():
    """LLM 配置 / 连通性测试 / 候选重排。未启用时返回本地规则解读。"""
    try:
        b = request.get_json(silent=True) or {}
        action = str(b.get("action") or "status")
        if action == "configure":
            return jsonify(llm.configure(b))
        if action == "test":
            return jsonify(llm.test(b))
        if action == "rerank":
            rows = b.get("candidates") or LAST_RUN.get("rows") or []
            as_of = b.get("as_of") or LAST_RUN.get("as_of", "")
            r = llm.rerank(rows, as_of=as_of,
                           horizon=b.get("horizon") or LAST_RUN.get("horizon"),
                           force=bool(b.get("force")))
            try:
                llm.save_last(r, as_of)
            except Exception:
                pass
            return jsonify(r)
        if action == "last":
            return jsonify(llm.load_last())
        if action == "status":
            return jsonify(llm.status())
        return fail("未知操作：" + action)
    except Exception as e:
        return fail(e)


@app.route("/api/cache", methods=["POST"])
def cache():
    """只保留前端实际使用的清缓存操作；缓存明细由 /api/cache 之外的状态接口不再重复传送。"""
    try:
        b = request.get_json(force=True) or {}
        if b.get("action") != "clear":
            return fail("未知操作：" + str(b.get("action")))
        import store
        n = store.clear_bars()
        return ok(cleared=n)
    except Exception as e:
        return fail(e)


if __name__ == "__main__":
    c = cfg()
    port = int(c.get("port", 8765))
    print("=" * 56)
    print("  QuantDesk - Local A-Share Quant Workbench")
    print("  Open in browser: http://127.0.0.1:%d/" % port)
    print("  Press Ctrl+C or close this window to stop.")
    print("=" * 56)
    scheduler.start()          # 收盘后自动更新数据链（配置见「选股设置 → 数据准备」）
    emtlink.start_auto()       # 盘中低频刷新 + 收盘日结实盘账户（配置见「实盘复盘 → 账户设置」）
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
