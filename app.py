# -*- coding: utf-8 -*-
"""QuantDesk 本地服务入口：Flask + 静态前端，仅监听 127.0.0.1。"""
from __future__ import annotations

import datetime as dt
import json
import mimetypes
import socket
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path

import pandas as pd
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

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
import listing
import factors
import portfolio
import capital
import stockview

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from paths import BASE, WEB  # 打包后 BASE = exe 目录；WEB 优先 exe 同级，回落包内资源
# 后验重放结果的小缓存：key → (时间戳, 响应体)
_REVIEW_CACHE: dict = {}
# .webmanifest 默认不在 Python 的映射表里，不显式登记会按 text/plain 发出，
# Chrome 会忽略 manifest（表现为「添加到主屏幕」没有图标和独立窗口）
mimetypes.add_type("application/manifest+json", ".webmanifest")

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


@app.errorhandler(Exception)
def _on_unhandled(e):
    """兜底：任何没被接口自己 try 住的异常都进系统日志，并带上堆栈。

    HTTPException（404/405 之类）是正常的路由结果，不记、原样返回。
    """
    if isinstance(e, HTTPException):
        return e
    try:
        store.log("app", "未捕获异常 %s %s" % (request.method, request.path),
                  level="error", act=request.path,
                  detail=traceback.format_exc()[-2000:])
    except Exception:
        pass
    return jsonify({"ok": False, "error": str(e)}), 500


def cfg():
    return quant.load_config()


def bind_host(force_lan: bool = False) -> str:
    """监听地址。默认只监听本机；config.host 或 --lan 可放开到局域网。

    放开后没有登录、没有鉴权，任何能连上这个端口的人都能看账本、改配置，
    只适合在家用网络临时给手机用。
    """
    if force_lan:
        return "0.0.0.0"
    return str(cfg().get("host") or "127.0.0.1")


def lan_urls(port: int) -> list[str]:
    """本机所有可用的局域网地址（供手机访问）。取不到就返回空列表。"""
    ips = set()
    try:
        # 连一个外部地址只为让系统选出默认出口网卡，UDP 不会真的发包
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.3)
            s.connect(("223.5.5.5", 80))
            ips.add(s.getsockname()[0])
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    out = []
    for ip in sorted(ips):
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        out.append("http://%s:%d/" % (ip, port))
    return out


def _latest_bar_day() -> str:
    """行情缓存的数据日：多数股票都到齐的那一天（1 毫秒级）。

    以前只看 4 只大盘股的末根 K 线，它们一旦被快照/单只补抓抢先写进新的一天，
    就会误判「截面已是最新」，于是拿旧截面打分、界面还显示最新。
    """
    try:
        import store
        d = store.data_day()
        if d:
            return d
    except Exception:
        pass
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
    """接口级错误：回给前端一句话，同时留一条带路径的日志。

    「刚才点了一下就报错」这类问题，如果没有路径和原始异常，事后根本没法定位。
    """
    try:
        store.log("api", "%s %s：%s" % (request.method, request.path, str(e)[:160]),
                  level="warn", act=request.path,
                  detail="%s\n%s" % (str(e), traceback.format_exc()[-1200:]))
    except Exception:
        pass
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
        # 「没到点，压根没发请求」和「跑了但失败」不是一回事：记录里要能分开看，
        # 否则更新记录全是成功，反而看不出估值其实一直没动。
        skip = bool(d.get("blocked"))
        if skip:
            detail = ("跳过：" + detail)[:200]
        store.run_log(kind, okf, steps=[{"name": name, "ok": okf, "ms": ms,
                                         "detail": detail, "skip": skip}],
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
    """首页：把静态资源的版本号替换成本次启动时的指纹。

    为什么必须自动：以前 index.html 里写死 `?v=20261121f`，改完 CSS/JS 忘了改这个
    字符串，浏览器就一直用旧缓存 —— 表现是「改了半天没变化」，其实是本地缓存没失效。
    现在版本号由文件 mtime 算出来，改完刷新就生效，不需要手动维护。
    """
    try:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        stamp = _asset_stamp()
        # 同时替换 CSS / JS / 图标三类资源的 ?v=
        import re as _re
        html = _re.sub(r'(\?v=)[0-9A-Za-z._-]+', r"\g<1>" + stamp, html)
        resp = Response(html, mimetype="text/html")
        # 首页本身不缓存，否则里面的新版本号也读不到
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp
    except Exception:
        return send_from_directory(WEB, "index.html")


def _asset_stamp() -> str:
    """静态资源的版本指纹：取 style.css / app.js / index.html 的最新 mtime。

    只精确到秒不够（同一秒内改两次会撞车），所以带上文件大小一起哈希。
    """
    try:
        import hashlib
        h = hashlib.md5()
        for name in ("style.css", "app.js", "index.html", "icon.svg"):
            p = WEB / name
            if p.exists():
                st = p.stat()
                h.update(("%s:%d:%d;" % (name, int(st.st_mtime), st.st_size)).encode())
        return h.hexdigest()[:10]
    except Exception:
        return "0"


@app.after_request
def _no_cache_static(resp):
    """静态资源只走协商缓存，不设 max-age。

    版本号本来由 index() 按文件指纹自动替换（URL 变了自然重新拉取），
    但本地工具只有一个用户：多一次 304 校验几乎没有代价，却能在指纹机制
    没生效时（例如直接用 file:// 打开、或反向代理改写了 URL）避免拿着旧文件
    排查半天「为什么改了没反应」。
    """
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


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
        sname = quant.stock_name(code, cfg())
        r = quant.run_backtest(d, strategy=str(b.get("strategy", "ma_cross")),
                               params=b.get("params") or {}, cfg=c,
                               capital=b.get("capital"),
                               code=code, name=sname)
        r.update(code=code, name=sname,
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
                   "replace", "remove_source", "import")
        if action not in allowed:
            return fail("未知操作：" + action)
        kw = {k: b[k] for k in ("code", "name", "dir", "items", "source",
                                "text", "mode") if k in b}
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


@app.route("/api/stock/detail")
def stock_detail():
    """单股画像：行情 / 估值 / 模型分与排名 / 24 因子有利分位。"""
    try:
        return jsonify(stockview.detail(request.args.get("code") or "",
                                        force=request.args.get("force") == "1"))
    except Exception as e:
        return fail(e)


@app.route("/api/stock/analyze", methods=["POST"])
def stock_analyze():
    """生成一份单股分析并存档；LLM 失败会回退本地模板，mode 如实标注。"""
    try:
        b = request.get_json(force=True) or {}
        return jsonify(stockview.analyze(b.get("code") or "", force=bool(b.get("force"))))
    except Exception as e:
        return fail(e)


@app.route("/api/stock/reports")
def stock_reports():
    try:
        return jsonify(stockview.history(request.args.get("code") or "",
                                        limit=int(request.args.get("limit") or 60)))
    except Exception as e:
        return fail(e)


@app.route("/api/stock/reports/clear", methods=["POST"])
def stock_reports_clear():
    try:
        b = request.get_json(force=True) or {}
        return jsonify(stockview.clear(b.get("code") or ""))
    except Exception as e:
        return fail(e)


@app.route("/api/stock/reports/<int:rid>", methods=["GET", "DELETE"])
def stock_report_one(rid):
    try:
        if request.method == "DELETE":
            return jsonify(stockview.delete(rid))
        return jsonify(stockview.get(rid))
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
    """全 A 行情预热（后台限量补缓存）。

    目前前端没有入口：预热能力与「股票池 → 刷新行情」的 bulkdata 重叠，
    保留接口是为了脚本化调用（curl / 定时任务）。若要接回页面，
    在数据中枢「数据链」的名单段加一个按钮即可。
    """
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


@app.route("/api/journal/normalize", methods=["POST"])
def journal_normalize():
    """旧稿补格式：裸节标题加 `##`、`【小节】` 转标题。带 day 只处理单篇。"""
    try:
        b = request.get_json(force=True) or {}
        day = str(b.get("day") or "").strip()
        r = journal.normalize(day) if day else journal.normalize_all()
        if r.get("ok") and day and r.get("changed"):
            emtlink.journal_mirror(day)
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
        kind = str(b.get("kind") or "")

        def _gen():
            r = emtai.generate(kind=kind, day=str(b.get("day") or ""),
                               force=bool(b.get("force")))
            # mode=ai 才是真调了模型；local/fallback 已在 emtai 里单独记 warn
            if r.get("ok") and r.get("mode") == "ai":
                r["message"] = "AI 生成完成（%s，%s 字）" % (
                    "日记" if kind == "journal" else "体检",
                    len(str(r.get("text") or "")))
            return r
        return jsonify(_shortcut("ai", "AI 生成", _gen))
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

        def _pull():
            r = emtlink.pull_history(start, end, size, what)
            # pull_history 自己返回 ok/total/ms；补一句人能读的进记录
            if r.get("ok"):
                r.setdefault("message", "历史回流 %s ~ %s · %s 共 %s 条" % (
                    start or "最早", end or "至今", "/".join(what),
                    r.get("total") or r.get("added") or 0))
            return r
        return jsonify(dict(_shortcut("emt_history", "实盘历史回流", _pull),
                            state=emtlink.hist_state()))
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
    """数据源：GET 现状；POST 存 Tushare Token / 清除 / 改估值源顺序与开关。"""
    try:
        if request.method == "GET":
            return jsonify(datasource.status())
        b = request.get_json(silent=True) or {}
        act = str(b.get("action") or "")
        if act == "clear":
            return jsonify(datasource.clear())
        if act == "valsrc":
            import valsrc
            valsrc.save(b)
            return ok(valuation=valsrc.status(), sources=datasource.status())
        return jsonify(datasource.save(b.get("token"), b.get("enabled")))
    except Exception as e:
        return fail(e)


@app.route("/api/sources/test", methods=["POST"])
def sources_test():
    """单源探测：带 source=em/tx/ts/bs 就测估值源的某一路，否则测 Tushare 账号。"""
    try:
        b = request.get_json(silent=True) or {}
        key = str(b.get("source") or "")
        if key:
            import valsrc
            return jsonify(valsrc.test(key))
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


@app.route("/api/tasks")
def tasks_status():
    """全局任务状态：顶栏那盏指示灯读这一份。

    为什么要聚合在一处：行情增量、流水线、上市名单预热、退市股补数据
    各自都有 running 标志，分散在 4 个模块里。界面上要是每个地方画一个进度条，
    用户得满屏找「现在到底在跑什么」。这里统一成一组 {name, done, total, pct}，
    顶栏只显示一条总进度，悬停看明细。

    返回：
      running  是否有任何任务在跑
      tasks    正在跑的任务列表（含进度与文案）
      overall  所有任务的总进度（已完成的加权平均，0~1）
      recent   最近结束的任务（给悬停面板补一句「刚完成什么」）
    """
    try:
        tasks = []

        # 1) 行情增量（bulkdata）：最慢的一段，进度最值得看
        try:
            import bulkdata
            st = bulkdata.status() or {}
            if st.get("running"):
                d, t = int(st.get("done") or 0), int(st.get("total") or 0)
                tasks.append({
                    "key": "quote", "name": "行情增量",
                    "done": d, "total": t,
                    "pct": (d / t) if t else None,
                    "detail": ("%d / %d 只 · 成功 %s · 跳过 %s · 失败 %s"
                               % (d, t, st.get("ok") or 0, st.get("skip") or 0, st.get("fail") or 0)),
                })
        except Exception:
            pass

        # 2) 流水线（pipeline）：按「第几段 / 共几段」估进度
        try:
            import pipeline
            run = pipeline.RUN or {}
            if run.get("running"):
                stp = run.get("step") or {}
                idx = int(stp.get("index") or 0)
                tot = int(stp.get("total") or 0)
                pct = stp.get("pct")
                if pct is None and tot:
                    pct = max(0.0, min(1.0, (idx - 1) / float(tot)))
                tasks.append({
                    "key": "pipeline", "name": stp.get("name") or "数据链更新",
                    "done": max(0, idx - 1), "total": tot,
                    "pct": (float(pct) if pct is not None else None),
                    "detail": (("第 %d / %d 步" % (idx, tot)) if tot else "准备中") +
                              ((" · " + str(stp.get("detail"))) if stp.get("detail") else ""),
                })
        except Exception:
            pass

        # 3) 上市名单同步 / 退市股补行情 / 全 A 行情预热：都是长任务
        try:
            import listing
            st = listing.prewarm_status() or {}
            if st.get("running"):
                d, t = int(st.get("done") or 0), int(st.get("total") or 0)
                tasks.append({
                    "key": "listing_prewarm", "name": "退市股补行情",
                    "done": d, "total": t, "pct": (d / t) if t else None,
                    "detail": str(st.get("message") or "")[:80],
                })
        except Exception:
            pass
        try:
            import listing
            sy = getattr(listing, "_SYNC", {}) or {}
            if sy.get("running"):
                tasks.append({
                    "key": "listing_sync", "name": "上市名单同步",
                    "done": 0, "total": 0, "pct": None,
                    "detail": str(sy.get("message") or "正在同步")[:80],
                })
        except Exception:
            pass
        try:
            import universe
            st = universe.prewarm_status() or {}
            if st.get("running"):
                d, t = int(st.get("done") or 0), int(st.get("total") or 0)
                tasks.append({
                    "key": "universe_prewarm", "name": "全 A 行情预热",
                    "done": d, "total": t, "pct": (d / t) if t else None,
                    "detail": str(st.get("message") or "")[:80],
                })
        except Exception:
            pass

        # 总进度：对各任务的 pct 取平均（没给 pct 的按「刚开始」算 0）
        if tasks:
            vals = [float(t["pct"]) if t.get("pct") is not None else 0.0 for t in tasks]
            overall = sum(vals) / len(vals)
        else:
            overall = None

        recent = []
        try:
            import store
            recent = [r for r in (store.runs_list(6) or []) if r.get("ok") is not None][:3]
        except Exception:
            pass

        return ok(running=bool(tasks), tasks=tasks,
                  overall=(round(overall, 4) if overall is not None else None),
                  recent=recent)
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
    """统一更新记录（流水线 + 各页快捷 + 定时任务），最新在前。

    支持 kind/trigger/ok/q 筛选与 offset 翻页：记录不清空，翻历史靠分页。
    """
    try:
        a = request.args
        limit = max(1, min(int(a.get("limit", 60)), 300))
        offset = max(0, int(a.get("offset", 0) or 0))
        return jsonify(store.runs_page(
            limit=limit, offset=offset,
            kind=str(a.get("kind") or ""), trigger=str(a.get("trigger") or ""),
            ok=str(a.get("ok") or ""), q=str(a.get("q") or "").strip()))
    except Exception as e:
        return fail(e)


@app.route("/api/logs", methods=["GET", "POST"])
def logs_api():
    """系统运行日志：GET 列表/统计/导出，POST 清理。

    和「更新记录」的区别：那边记「做了什么、成没成」，这里记「为什么」——
    退避到几点、降级到哪个源、异常栈、AI 回退原因。
    """
    try:
        if request.method == "POST":
            b = request.get_json(silent=True) or {}
            if b.get("action") == "prune":
                n = store.log_prune()
                store.log("app", "手动清理日志 %d 条" % n, level="info", act="prune")
                return ok(removed=n, facets=store.log_facets())
            return fail("未知操作")
        a = request.args
        if a.get("export"):
            txt = store.log_text(
                limit=max(1, min(int(a.get("limit", 2000)), 20000)),
                level=str(a.get("level") or ""), src=str(a.get("src") or ""),
                q=str(a.get("q") or "").strip())
            name = "quantdesk-log-%s.txt" % dt.datetime.now().strftime("%Y%m%d-%H%M")
            return Response(txt, mimetype="text/plain; charset=utf-8",
                            headers={"Content-Disposition": "attachment; filename=%s" % name})
        d = store.logs_list(
            limit=max(1, min(int(a.get("limit", 200)), 1000)),
            offset=max(0, int(a.get("offset", 0) or 0)),
            level=str(a.get("level") or ""), src=str(a.get("src") or ""),
            q=str(a.get("q") or "").strip())
        d["facets"] = store.log_facets()
        d["keep"] = {"days": store.LOG_KEEP_DAYS, "rows": store.LOG_KEEP_ROWS}
        return jsonify(d)
    except Exception as e:
        return fail(e)


@app.route("/api/logs/ui", methods=["POST"])
def logs_ui():
    """右上角通知回流：把「用户当时看到了什么」也存进运行日志。

    前端在 toast() 里攒 1.5 秒合成一批发过来，不是每条一个请求。
    同样的提示 20 秒内重复弹（轮询失败最常见）前端已合并，这里只做长度与
    级别的兜底校验，写多少条如实回多少。
    """
    try:
        b = request.get_json(silent=True) or {}
        items = b.get("items")
        if not isinstance(items, list):
            return jsonify({"ok": False, "error": "items 必须是数组"})
        n = store.log_many(items, src="ui")
        return ok(written=n)
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
        topn = int(request.args.get("topn", 20))
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


@app.route("/api/screen/resume")
def screen_resume():
    """首屏恢复：直接返回上一次真正落盘的那批候选，不做任何重算。

    为什么要有这个接口：首屏原本调 /api/screen/run 自动出数，虽然 record=False
    不写记录，但数据日推进后第一次打开仍会重建全市场快照（约 6 秒），
    用户误以为「一打开就重新选股」。落盘的 picks + picks_meta 已经保存了
    候选与因子，恢复它们只要几十毫秒 —— 首屏应该先看到上次的结果，
    由彩虹提醒告诉用户「该重新选了」，而不是悄悄重算。

    返回结构与 /api/screen/run 对齐（candidates / as_of / funnel / industry 等），
    前端 renderScrRows / renderFunnel 可以直接用，无需改渲染层。
    """
    try:
        import store
        import histcache

        # 最近一次真正落盘的批次：按 recorded_at 倒序取第一批
        rows = store.conn().execute(
            "SELECT as_of, recorded_at, code, name, industry, rank, score, close "
            "FROM picks ORDER BY recorded_at DESC, as_of DESC LIMIT 200").fetchall()
        if not rows:
            return jsonify({"ok": True, "empty": True, "candidates": [],
                            "as_of": "", "last_pick_at": "",
                            "note": "还没有落盘的选股记录"})

        as_of = str(rows[0][0])[:10]
        rec_at = str(rows[0][1])
        # meta 里存了因子（mom20/vol20/rsi14/turnover/score/base_score 等）
        mm = store.picks_meta_map(as_of, rec_at)
        cands = []
        for r in rows:
            if str(r[0])[:10] != as_of or str(r[1]) != rec_at:
                continue
            code = str(r[2]).zfill(6)
            m = mm.get((as_of, rec_at, code)) or {}
            item = {
                "code": code, "name": r[3] or "", "industry": r[4] or "",
                "rank": r[5], "score": r[6], "close": r[7],
                "funnel_stage": "保留",
            }
            # 因子值直接铺进候选行，展开详情时才用得到
            for k in screener.META_KEYS:
                if k in m and m[k] is not None:
                    item[k] = m[k]
            # pct 由缓存算：记录日收盘 → 最新收盘（与 picks_log 同口径）
            try:
                bar = store.conn().execute(
                    "SELECT close FROM bars WHERE code=? AND date>=? ORDER BY date DESC LIMIT 1",
                    (code, as_of)).fetchone()
                if bar and bar[0] and item.get("close"):
                    item["pct"] = (float(bar[0]) / float(item["close"]) - 1) * 100
            except Exception:
                pass
            cands.append(item)

        cands.sort(key=lambda x: (x.get("rank") if isinstance(x.get("rank"), int) else 999))
        for i, c in enumerate(cands, 1):
            c["rank"] = i

        # 真实价换算（与 screen_run 末尾一致）
        try:
            qmap = spot.prev_closes([c["code"] for c in cands])
            for c in cands:
                hfq_last = c.get("close")
                lb = histcache.last_bar(c["code"])
                if lb and lb.get("close"):
                    hfq_last = lb["close"]
                c["close_real"] = spot.real_price(c.get("close"), hfq_last,
                                                  str(lb.get("date") or "")[:10] if lb else "",
                                                  qmap.get(c["code"]))
        except Exception:
            pass

        # 恢复同批次的漏斗与辩护：记录页回看不该烧 token，直接取存档。
        # 决策档案（pick_archive）按 (as_of, recorded_at) 存了当时的漏斗链；
        # 辩护（pick_bear）存了裁决全文。首屏恢复拿回来，才能让「候选榜 +
        # 行业分布 + 辩护」成对出现，而不是刷新一次就剩一排光杆候选。
        funnel_stages, dropped_n, sample_n, neutral_mode = [], 0, 0, ""
        try:
            # 先只查摘要定位 id，再读那一份 payload：存档每份可能几 MB，
            # 一次拉 40 份完整 JSON 会让首屏恢复慢一个数量级。
            hit_id = next((a["id"] for a in store.archive_meta_list(limit=40, as_of=as_of)
                           if str(a.get("recorded_at") or "") == rec_at), 0)
            if hit_id:
                a = store.archive_get(hit_id)
                if a:
                    f = a["payload"].get("funnel") or []
                    funnel_stages = [s for s in f if isinstance(s, dict)]
                    # 打分口径（中性化模式）随批次存档；老存档没有这个字段，
                    # 前端会显示「—」，不影响候选本身。
                    neutral_mode = str(a["payload"].get("neutral") or "")
                    # 漏斗淘汰数 = 样本池 before − 最后一层 after（与 screen_run 的
                    # len(scored)-len(kept) 同义，只是从存档反推）
                    if funnel_stages:
                        before = int(funnel_stages[0].get("before") or 0)
                        after = int(funnel_stages[-1].get("after") or 0)
                        dropped_n = max(0, before - after)
                        # 右上角的「只样本」= 进入打分排序的只数（screen_run 的
                        # len(kept)），不是候选只数。从存档的 rank 层反推，
                        # 老存档没有 rank 层时退到最后一层，二者都是 kept。
                        rank = next((s for s in funnel_stages
                                     if s.get("key") == "rank"), funnel_stages[-1])
                        sample_n = int(rank.get("after") or 0)
        except Exception:
            pass
        # 行业暴露：从恢复出来的候选现场重算（等权行业权重 + 全市场基线）。
        # 不落盘也拿得到 —— 基线用行业映射的全体成分数，与 screen_run 的
        # neutral.exposure(picked, base) 保持同口径。
        expo = {}
        try:
            import neutral
            base = pd.DataFrame({"industry": list(industry.load_map().values())})
            picked_df = pd.DataFrame([
                {"code": c["code"], "industry": c.get("industry") or "未分类",
                 "name": c.get("name") or ""}
                for c in cands if c.get("industry")])
            if len(picked_df):
                expo = neutral.exposure(picked_df.to_dict("records"), base)
        except Exception:
            expo = {}

        return jsonify({
            "ok": True, "empty": False, "candidates": cands,
            "as_of": as_of, "count": sample_n, "topn": len(cands),
            "last_pick_at": str(store.picks_version()[1] or ""),
            "restored": True,
            "engine": "restored",
            "funnel": {"stages": funnel_stages, "dropped": dropped_n},
            "industry": {"cap": 0, "capped": False, "trimmed": [], "counts": {},
                         "note": "恢复上次落盘候选", "exposure": expo},
            "neutral": {"mode": neutral_mode} if neutral_mode else {},
            "model": {}, "snapshot_reused": True,
            "record": {"ok": True, "skipped": True, "note": "恢复展示，不写记录",
                       "recorded_at": rec_at},
        })
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
            max_symbols=int(b.get("max_symbols") or 800),
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
        topn = max(1, min(int(b.get("topn") or 20), 100))
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
        #    detail=True 额外拿回「谁被哪层淘汰」，写进决策档案供事后归因。
        kept, stages, killed = screener.funnel(scored, funnel_cfg, detail=True)
        if kept.empty:
            return jsonify({"ok": False, "error": "漏斗过滤后没有剩余样本，请放宽「极端风险 / 趋势」条件",
                            "funnel": {"stages": stages}})
        # 2) 行业约束 + TopN
        sel = screener.select(kept, topn=topn, cap=cap)
        for c in sel["picked"]:
            c["funnel_stage"] = "保留"

        # 3) 空头辩护（可选）：只对已入选的 TopN 做，成本可控。
        #    被否决的票从最终名单剔除，但完整写进决策档案 —— 否则事后无法检验
        #    「否决到底拦得对不对」，那就等于给自己加了一个不可回测的过滤器。
        bear = None
        all_cand = sel["picked"]
        final_cand = all_cand
        if b.get("bear") and all_cand:
            try:
                import llm as _llm
                bear = _llm.bear_case(all_cand, as_of=str(r.get("as_of") or ""))
                if bear.get("mode") == "llm" and bear.get("rows"):
                    vmap = {x["code"]: x for x in bear["rows"]}
                    for c in all_cand:
                        m = vmap.get(str(c.get("code")))
                        if m:
                            c["verdict"] = m["verdict"]
                            c["bear_case"] = m.get("bears") or []
                            c["llm_reason"] = m.get("judge_reason") or ""
                            c["bear_enough"] = bool(m.get("enough"))
                    drop = set(bear.get("drop_codes") or [])
                    final_cand = [c for c in all_cand if str(c.get("code")) not in drop]
                    for i, c in enumerate(final_cand, 1):
                        c["rank"] = i
            except Exception as e:                     # noqa: BLE001
                bear = {"ok": False, "error": str(e)[:160]}

        # 4) 落盘候选，交给 T+N 后验。首屏自动出数会带 record=False：
        #    那种跑法只是把界面填满，不是用户的一次选股，写进记录会污染历史。
        # 历史截止日只用于回看，不能覆盖那一天的实盘候选记录：
# 同 as_of 的记录是整期替换的，一次历史实验就会把真实留档冲掉。
        latest_day = _latest_bar_day()
        archive = {"engine": r.get("engine"), "topn": topn,
                   "as_of_param": want_as_of,
                   # 中性化口径一起存档：resume 时行业分布卡要显示「当时用的什么口径」
                   "neutral": want_neutral,
                   "funnel": stages,
                   "picks": [{k: c.get(k) for k in
                              ("code", "name", "industry", "score", "close",
                               "close_real", "verdict", "llm_reason", "funnel_stage")}
                             | {"factors": {k: c.get(k) for k in screener.META_KEYS},
                                "bears": c.get("bear_case") or []}
                             for c in all_cand],
                   "dropped": [{"code": k, "stage": v} for k, v in list(killed.items())[:3000]],
                   "llm": ({"mode": bear.get("mode"), "summary": bear.get("summary"),
                            "model": bear.get("model"), "judge_model": bear.get("judge_model"),
                            "dropped": bear.get("dropped")} if bear else None)}
        if b.get("record", True) and want_as_of and latest_day and want_as_of != latest_day:
            rec = {"ok": True, "skipped": True,
                   "note": "历史截止日回看，不计入选股记录"}
        elif b.get("record", True):
            rec = screener.record_picks(final_cand, r.get("as_of", ""), archive=archive)
        else:
            rec = {"ok": True, "skipped": True, "note": "首屏自动出数，不计入选股记录"}
        # 空头辩护随批次一起落盘：只在真正留档的批次上存（首屏静默出数不存），
        # 这样「这一批为什么只留这几只」和候选榜永远在同一份记录里。
        if bear and bear.get("mode") == "llm" and rec.get("recorded_at"):
            try:
                store.bear_put(str(r.get("as_of") or "")[:10], str(rec["recorded_at"]),
                               {**bear, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                               mode="llm")
                bear["saved"] = True
            except Exception:
                bear["saved"] = False
        r["funnel"] = {"stages": stages, "dropped": len(scored) - len(kept),
                       "kept": len(kept),
                       "killed": len(killed)}
        r["bear"] = bear
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
        r["candidates"] = final_cand
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
            # 同时按批次入库：llm_last.json 只能记住「最近一次」，换一批就覆盖，
            # 而候选榜要能按「哪一批」取回当时的解读（不同 TopN 各存各的）。
            try:
                import store
                batch = b.get("batch") if isinstance(b.get("batch"), dict) else {}
                rec = str(batch.get("recorded_at") or "")
                if rec and r.get("ok") and r.get("rows"):
                    store.llm_put(str(batch.get("as_of") or as_of), rec, r,
                                  mode=r.get("mode", ""), model=r.get("model", ""))
                    r["saved"] = True
            except Exception as e:                     # noqa: BLE001
                r["saved"] = False
                r["save_error"] = str(e)[:160]
            return jsonify(r)
        if action == "last":
            # 优先按批次取（前端给了 recorded_at），否则取最近一份
            try:
                import store
                as_of = str(b.get("as_of") or "")[:10]
                rec = str(b.get("recorded_at") or "")
                d = store.llm_get(as_of, rec) if (as_of or rec) else None
                if d:
                    return jsonify({"ok": True, "saved": d, "from": "db"})
            except Exception:
                pass
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


# ==========================================================================
# P0-1 上市名单（point-in-time）/ P0-3 因子体检 / P1-4 目标持仓
# P1-5 空头辩护 A/B / P2-6 资金流
# ==========================================================================

@app.route("/api/listing", methods=["GET"])
def listing_api():
    """上市名单状态 + 退市股行情覆盖（回放能不能真的纳入退市股，看这里）。"""
    try:
        d = {**listing.status(), "coverage": listing.coverage()}
        # fresh 是按自然日算的（sync 的当日缓存依赖它），但周末会显示「待同步」，
        # 而名单其实已经跟上了最近一个交易日。给页面一个按交易日算的 lag，
        # 跟侧栏待办同一口径。
        d["lag"] = pipeline._lag(
            pipeline._market_days(pipeline._expected_day()), d.get("updated") or "")
        return jsonify(d)
    except Exception as e:
        return fail(e)


@app.route("/api/listing/sync", methods=["POST"])
def listing_sync():
    try:
        b = request.get_json(silent=True) or {}
        return jsonify(listing.sync_async(force=bool(b.get("force", True))))
    except Exception as e:
        return fail(e)


@app.route("/api/listing/prewarm", methods=["GET", "POST"])
def listing_prewarm():
    """补退市股历史行情（串行，可停）。没有它们，as-of 过滤只是一句口号。"""
    try:
        if request.method == "GET":
            return jsonify(listing.prewarm_status())
        b = request.get_json(silent=True) or {}
        if str(b.get("action") or "start") == "stop":
            return jsonify(listing.prewarm_stop())
        return jsonify(listing.prewarm_delisted(limit=int(b.get("limit") or 0)))
    except Exception as e:
        return fail(e)


@app.route("/api/factors", methods=["GET", "POST"])
def factors_api():
    """单因子体检。首次计算约 15~45 秒（含资金流因子），之后走缓存秒开。"""
    try:
        if request.method == "POST":
            b = request.get_json(silent=True) or {}
        else:
            b = request.args
        days = int(b.get("days") or 120)
        step = int(b.get("step") or 1)
        horizon = int(b.get("horizon") or 5)
        limit = int(b.get("limit") or 300)
        force = str(b.get("force") or "") in ("1", "true", "True")
        with_cap = str(b.get("capital") or "1") in ("1", "true", "True", "")
        # 资金流因子交给 checkup 在缓存未命中时才算，别在这里预计算
        return jsonify(factors.checkup(days=days, step=step, horizon=horizon,
                                       limit=limit, force=force, with_cap=with_cap))
    except Exception as e:
        return fail(e)


@app.route("/api/portfolio", methods=["GET", "POST"])
def portfolio_api():
    """目标持仓：等权/打分加权 + 单票/行业上限 + 差额头寸 + 成本估算。"""
    try:
        if request.method == "GET":
            cur_list, ov = portfolio.current_positions()
            # 前端要显示「账本持仓 N 只」，以前只返回了 day/total/mv/cash，
            # 于是那行永远是「0 只」。
            cur = dict(ov or {})
            cur["positions"] = len(cur_list or [])
            return jsonify({"ok": True, "config": portfolio.cfg(), "current": cur})
        b = request.get_json(silent=True) or {}
        if str(b.get("action") or "") == "config":
            return jsonify(portfolio.save(b.get("config") or {}))
        picks = b.get("picks") or LAST_RUN.get("rows") or []
        if not picks:
            # 没有本次运行结果时，退回最近一个落盘批次（重启后也能算）
            try:
                last = review.picks_log(limit=1)
                batches = last.get("batches") or []
                if batches:
                    picks = batches[0].get("picks") or []
                    picks = [{"code": x["code"], "name": x["name"],
                              "industry": x["industry"], "score": x.get("score"),
                              "close_real": x.get("rec_real") or x.get("rec_close")}
                             for x in picks]
            except Exception:
                picks = []
        return jsonify(portfolio.target(picks, capital=float(b.get("capital") or 0),
                                        patch=b.get("config") or None))
    except Exception as e:
        return fail(e)


@app.route("/api/screen/bear", methods=["GET", "POST"])
def screen_bear():
    """空头辩护：对上一轮候选生成否决理由并裁决。

    GET  ?as_of=&recorded_at=  读回「那一批」存过的辩护（不给 recorded_at 时取该数据日最近一份）
    POST {candidates, as_of, batch:{as_of,recorded_at}} 跑一次，若指定批次则随批次落盘。
    空头辩护和候选榜是一体的：只存候选不存理由，事后就没法检验「否决到底拦得对不对」。
    """
    try:
        if request.method == "GET":
            import store
            a = request.args
            as_of = str(a.get("as_of") or "")[:10]
            rec = str(a.get("recorded_at") or "")
            p = store.bear_get(as_of, rec)
            if not p:
                return jsonify({"ok": True, "saved": False,
                                "note": "这一批还没有存过空头辩护"})
            return jsonify({**p, "saved": True,
                            "batch": {"as_of": as_of, "recorded_at": rec}})
        b = request.get_json(silent=True) or {}
        rows = b.get("candidates") or LAST_RUN.get("rows") or []
        as_of = str(b.get("as_of") or LAST_RUN.get("as_of", ""))[:10]
        r = llm.bear_case(rows, as_of=as_of, force=bool(b.get("force")),
                          judge=bool(b.get("judge", True)))
        # 只有真跑出裁决才存：local 降级/未启用时存下来，下次界面上会出现一份
        # 「看起来做过辩护」的空结论，比不存更误导。
        batch = b.get("batch") if isinstance(b.get("batch"), dict) else {}
        rec = str(batch.get("recorded_at") or "")
        if rec and r.get("ok") and r.get("mode") == "llm":
            try:
                import store
                store.bear_put(as_of or str(batch.get("as_of") or ""), rec,
                               {**r, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                               mode=r.get("mode"))
                r["saved"] = True
                r["batch"] = {"as_of": as_of, "recorded_at": rec}
            except Exception as e:                     # noqa: BLE001
                r["saved"] = False
                r["save_error"] = str(e)[:160]
        return jsonify(r)
    except Exception as e:
        return fail(e)


@app.route("/api/review/llm-ab", methods=["GET"])
def review_llm_ab():
    """离线 A/B：被 LLM 否决的票 vs 留下的票，谁的 T+N 更好。"""
    try:
        return jsonify(review.llm_ab(limit=int(request.args.get("limit", 30))))
    except Exception as e:
        return fail(e)


@app.route("/api/review/funnel", methods=["GET"])
def review_funnel():
    """漏斗归因：被各层淘汰的票，事后是不是反而涨得好。"""
    try:
        a = request.args
        return jsonify(review.funnel_check(days=int(a.get("days", 60)),
                                           step=int(a.get("step", 5)),
                                           limit=int(a.get("limit", 300))))
    except Exception as e:
        return fail(e)


@app.route("/api/picks/archive", methods=["GET"])
def picks_archive():
    """决策档案：某批次选了谁、为什么、被否决的是谁、被哪层漏斗淘汰。"""
    try:
        a = request.args
        if a.get("id"):
            return jsonify({"ok": True, "archive": store.archive_get(int(a["id"]))})
        return jsonify({"ok": True,
                        "items": store.archive_list(limit=int(a.get("limit", 20)),
                                                    as_of=str(a.get("as_of") or ""))})
    except Exception as e:
        return fail(e)


@app.route("/api/capital", methods=["GET", "POST"])
def capital_api():
    """资金流：两融 / 龙虎榜 / 大宗（北向个股已停止披露）。"""
    try:
        if request.method == "GET":
            a = request.args
            if a.get("codes"):
                codes = [c for c in str(a["codes"]).split(",") if c.strip()]
                return jsonify(capital.preview(codes, as_of=str(a.get("as_of") or "")))
            return jsonify({**capital.status(), "config": capital.cfg(),
                            "factors": capital.factor_meta()})
        b = request.get_json(silent=True) or {}
        if str(b.get("action") or "sync") == "config":
            return jsonify(capital.save(b.get("config") or {}))
        return jsonify(capital.sync_async(days=int(b.get("days") or 25),
                                          force=bool(b.get("force"))))
    except Exception as e:
        return fail(e)



if __name__ == "__main__":
    c = cfg()
    port = int(c.get("port", 8765))
    host = bind_host("--lan" in sys.argv)
    print("=" * 56)
    print("  QuantDesk - Local A-Share Quant Workbench")
    print("  Open in browser: http://127.0.0.1:%d/" % port)
    if host == "0.0.0.0":
        for u in lan_urls(port):
            print("  LAN / phone  : %s" % u)
        print("  [注意] 已放开到局域网：没有登录，同网段的人都能访问")
    print("  Press Ctrl+C or close this window to stop.")
    print("=" * 56)
    scheduler.start()          # 收盘后自动更新数据链（配置见「选股设置 → 数据准备」）
    emtlink.start_auto()       # 盘中低频刷新 + 收盘日结实盘账户（配置见「实盘复盘 → 账户设置」）
    app.run(host=host, port=port, debug=False, threaded=True)
