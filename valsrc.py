# -*- coding: utf-8 -*-
"""
valsrc.py — 估值多源编排：按顺序降级，取「日期最新 + 覆盖最全」

为什么单独一层：
  PE / PB / 市值 / 换手 / 量比 以前只有 Tushare daily_basic 一个来路。账号没权限、
  被限频（1 次/小时）或源站还没更新，整条链就只能继续用旧值，页面上还看不出来。
  其实东财、腾讯的行情接口本身就带这些字段，收盘后立刻就有，一个账号都不用。

四类源的能力并不一样，所以「顺序」不能只看谁快：
  * snapshot（东财 / 腾讯）：只能给「当下这个交易日」，但收盘即有、全市场一次齐活
  * date（Tushare）：能按任意历史交易日取，官方口径，但要 19:30 后才更新、1 次/小时
  * fill（baostock）：逐只查历史，很慢，只在主源缺个股/缺字段时补漏

仲裁规则（见 fetch）：
  1. 目标日 T：显式传入，否则「应到日」——有 snapshot 源按 15:05 算，否则按 19:30
  2. 依序尝试，每个源产出「日期 + 行集 + 覆盖率 + 关键字段完整度」
  3. 日期 ≥ T 且覆盖 ≥ 95% 且完整度 ≥ 90% → 直接采纳，停止降级
  4. 都不达标 → 取日期最新、同日期覆盖最高的那个，如实标 stale，不伪装成功
  5. 覆盖率 < 50% 的候选绝不落盘：半份数据不能覆盖掉全市场文件
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import math
import time
from pathlib import Path

import requests

from paths import BASE, DATA
CFG_PATH = DATA / "valsrc.json"

COVER_OK = 0.95      # 覆盖率达到这个比例才算「全市场」
FILL_OK = 0.90       # 关键字段（PE_TTM / PB / 总市值）完整度
MIN_WRITE = 0.50     # 低于这个覆盖率绝不覆盖既有文件
BACKOFF_SEC = 90     # 普通源失败后的退避，避免连点把源打挂
BACKUP_BACKOFF_SEC = 6 * 3600   # 低频备份源（东财）失败后的冷却：本机代理常不通，别每小时都敲
ORDER_VERSION = 2    # 出厂顺序迁到「腾讯主源、东财低频备份」，老配置只迁一次
BS_CAP = 120         # baostock 每次最多补多少只（逐只 1.1 秒，别让流水线卡住）
OPEN_HM = "0915"     # 集合竞价开始；此前东财/腾讯给的还是上一交易日收盘
SNAP_READY_HM = "1505"   # 收盘后 5 分钟，当日估值才算定盘

_HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

# 源清单：role=main 参与「采纳」，role=fill 只补漏
META: dict[str, dict] = {
    "em": {
        "key": "em", "name": "东方财富快照 · 低频备份", "short": "东财(备)",
        "cap": "snapshot", "role": "main", "token": False,
        "speed": "约 1~2 秒", "est": 1.5, "enabled": True,
        "backup": True, "backoff_sec": BACKUP_BACKOFF_SEC,
        "fields": "PE(动/TTM) · PB · 总/流通市值 · 换手 · 量比",
        "note": "push2 分页并发取全市场；本机代理对该站不稳定，已降为备份：失败后冷却 6 小时、不写 warn，腾讯撑住时不必再敲",
    },
    "tx": {
        "key": "tx", "name": "腾讯批量行情", "short": "腾讯",
        "cap": "snapshot", "role": "main", "token": False,
        "speed": "约 4~6 秒", "est": 5.0, "enabled": True,
        "fields": "PE(TTM) · PB · 总/流通市值 · 换手 · 量比",
        "note": "qt.gtimg.cn 每批 60 只并发取，项目里最稳的通道；这轮行情的主力源",
    },
    "ts": {
        "key": "ts", "name": "Tushare Pro", "short": "Tushare",
        "cap": "date", "role": "main", "token": True,
        "speed": "1 次请求", "est": 1.0, "enabled": True,
        "fields": "PE · PE(TTM) · PB · 总/流通市值 · 换手 · 量比",
        "note": "官方口径，也是唯一能按历史交易日整市场回补的源；1 次/小时限频",
    },
    "sina": {
        "key": "sina", "name": "新浪行情兜底", "short": "新浪",
        "cap": "price", "role": "main", "token": False,
        "speed": "约 1~2 秒", "est": 1.5, "enabled": True,
        "fields": "现价 · 开高低 · 成交量额（无 PE/PB/市值）",
        "note": "hq.sinajs.cn 每批 800 只并发，只给行情不给估值；腾讯/东财都挂时作价格兜底，不会用空估值覆盖现有文件",
    },
    "tdx": {
        "key": "tdx", "name": "通达信 TCP", "short": "通达信",
        "cap": "price", "role": "main", "token": False,
        "speed": "约 3~8 秒", "est": 5.0, "enabled": True,
        "fields": "五档买卖 · 现价 · 开高低 · 成交量额（无 PE/PB/市值）",
        "note": "tdxpy 走通达信 7709 TCP 不封 IP；只给行情不给估值，作腾讯/东财之外的独立通道冗余。服务器可在 data/tdx.json 覆盖",
    },
    "bs": {
        "key": "bs", "name": "baostock 补漏", "short": "baostock",
        "cap": "date", "role": "fill", "token": False,
        "speed": "1.1 秒/只", "est": 120.0, "enabled": False,
        "fields": "PE(TTM) · PB(MRQ) · 换手（无市值）",
        "note": "逐只查历史，只用来补主源缺的个股；全市场要 100 分钟，默认关闭",
    },
}
# 腾讯先试（本机最稳），Tushare 按历史日兜底，东财退到 Tushare 之后作低频备份。
# 老配置里的出厂顺序由 load_cfg 一次性迁移；用户自定义顺序不动。
DEFAULT_ORDER = ["tx", "sina", "tdx", "ts", "em", "bs"]
OLD_DEFAULT_ORDERS = [
    ["em", "tx", "ts", "bs"],                 # 最初四源
    ["em", "tx", "sina", "tdx", "ts", "bs"],  # 加了新浪/通达信之后
]


# ---------------------------------------------------------------- 配置读写

def load_cfg() -> dict:
    """顺序 / 开关 / 上次结果。缺字段一律按默认补齐，坏文件不炸整个应用。"""
    cfg = {}
    try:
        if CFG_PATH.exists():
            cfg = json.loads(CFG_PATH.read_text("utf-8")) or {}
    except Exception:
        cfg = {}
    # 出厂顺序升级（只一次）：老机器上是 em 打头，而东财在本机代理下几乎必挂，
    # 每次都先撞它再轮到腾讯，白等还留一屏失败。用户手动改过的顺序不动。
    old = [k for k in (cfg.get("order") or []) if k in META]
    ver = 1
    try:
        ver = int(cfg.get("order_version") or 1)
    except (TypeError, ValueError):
        ver = 1
    migrate = False
    if ver < ORDER_VERSION:
        base = OLD_DEFAULT_ORDERS[-1]
        it = iter(base)
        factory_like = all(any(x == y for y in it) for x in old)
        if (factory_like and "em" in old
                and ("tx" not in old or old.index("em") < old.index("tx"))):
            old = list(DEFAULT_ORDER)
            migrate = True
        ver = ORDER_VERSION
    # 新增源按 DEFAULT_ORDER 的位置插入已有顺序，而不是一律追加到末尾：
    # 否则价格兜底源会排到 fill 源后面，主源全挂时轮不到它。
    # 用户自定义过的相对顺序保持不变，只把没出现过的源按出厂位置补进去。
    order = list(old)
    for k in DEFAULT_ORDER:
        if k in order:
            continue
        pos = next((i for i, x in enumerate(order)
                    if x in DEFAULT_ORDER and DEFAULT_ORDER.index(x) > DEFAULT_ORDER.index(k)),
                   len(order))
        order.insert(pos, k)
    enabled = {k: bool((cfg.get("enabled") or {}).get(k, META[k]["enabled"])) for k in META}
    out = {"order": order, "enabled": enabled, "order_version": ver,
           "last": cfg.get("last") or {}, "fetched": cfg.get("fetched") or {}}
    if migrate:
        save_cfg(out)          # 只写这一次；之后 load_cfg 不再动用户的顺序
    return out


def save_cfg(cfg: dict) -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    # order 已由 load_cfg 按出厂位置补齐过新源；这里保留用户的自定义顺序即可。
    # 早先的「没出现就 append」会把新增的价格兜底源挤到 fill 源后面。
    order = [k for k in (cfg.get("order") or []) if k in META]
    out = {"order": order,
           "enabled": {k: bool(cfg.get("enabled", {}).get(k, META[k]["enabled"])) for k in META},
           "order_version": cfg.get("order_version") or ORDER_VERSION,
           "last": cfg.get("last") or {}, "fetched": cfg.get("fetched") or {}}
    tmp = CFG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(CFG_PATH)
    return out


def save(patch: dict) -> dict:
    """UI 用：只改顺序和开关，别把 last / fetched 冲掉。"""
    cfg = load_cfg()
    if isinstance(patch.get("order"), list):
        cfg["order"] = [str(k) for k in patch["order"]]
    if isinstance(patch.get("enabled"), dict):
        for k, v in patch["enabled"].items():
            if k in META:
                cfg["enabled"][k] = bool(v)
    if isinstance(patch.get("key"), str) and "on" in patch:      # 单个开关
        if patch["key"] in META:
            cfg["enabled"][patch["key"]] = bool(patch["on"])
    return save_cfg(cfg)


def any_enabled() -> bool:
    c = load_cfg()
    return any(c["enabled"].get(k) and META[k]["role"] == "main" for k in META)


def has_snapshot() -> bool:
    c = load_cfg()
    return any(c["enabled"].get(k) and META[k]["cap"] == "snapshot" for k in META)


def name_of(key: str) -> str:
    if key in ("local", "cache", ""):
        return "本地缓存"
    return (META.get(key) or {}).get("name", key)


# ---------------------------------------------------------------- 目标日

def target_date(now: dt.datetime | None = None) -> str:
    """此刻应该已经拿到的估值交易日。

    有快照源时 15:05 就有当日收盘数据（东财/腾讯），没开快照源只能等 Tushare 19:30。
    比行情（17:45）早还是晚，完全取决于开的是哪几路。
    """
    now = now or dt.datetime.now()
    cutoff = "1505" if has_snapshot() else "1930"
    ref = now.strftime("%Y%m%d")
    if now.strftime("%H%M") < cutoff:
        ref = (now - dt.timedelta(days=1)).strftime("%Y%m%d")
    try:
        import pipeline
        days = pipeline._market_days(ref)
        return days[-1] if days else ""
    except Exception:
        return ""


def precheck(now: dt.datetime | None = None) -> dict:
    """此刻手动取估值到底有没有意义 —— 不值就别发起请求。

    为什么需要这个：14:35 点「立即取估值」，三个源必然全失败（东财/腾讯给的是当日
    没收盘的半截、Tushare 要 19:30 且 1 次/小时），然后把三条错误原样拼给用户看。
    用户看到的是一屏红字，实际问题只是「还没到点」。

    这里只做判断、不发请求：
      can=False, kind=intraday  盘中，等 15:05
      can=False, kind=backoff   各源都在退避，等到点
      can=False, kind=done      今天已经取到了
      can=True                  可以取
    """
    now = now or dt.datetime.now()
    cfg = load_cfg()
    T = target_date(now)
    got = cfg.get("fetched") or {}
    prev = got if int(got.get("count") or 0) > 1000 else _local_state()
    out = {"can": True, "kind": "ok", "until": "", "wait_sec": 0, "reason": "",
           "target": T, "last": str(prev.get("date") or ""),
           "count": int(prev.get("count") or 0), "source": str(prev.get("source") or ""),
           "snapshot": has_snapshot(), "intraday": False}
    if not T:
        out.update(can=False, kind="calendar",
                   reason="交易日历不可用，算不出该到哪一天；先更新交易日历")
        return out
    if str(got.get("date") or "") >= T and int(got.get("count") or 0) > 1000:
        out.update(can=False, kind="done",
                   reason="%s 的估值今天已经取到（%d 只），不必重复取"
                          % (got["date"], int(got["count"])))
        return out

    intraday = _in_session(now)
    out["intraday"] = intraday
    live: list[str] = []
    cands: list[tuple[dt.datetime, str, str]] = []      # (可试时刻, 源, 原因)
    for key in cfg["order"]:
        m = META[key]
        if m["role"] != "main" or not cfg["enabled"].get(key, m["enabled"]):
            continue
        nt = _dt_of((cfg["last"].get(key) or {}).get("next_try"))
        if m["cap"] == "snapshot" and intraday:
            # 盘中先记到收盘；若上一次失败已经退避到 15:06 之后，就以退避为准，
            # 否则 15:05:30 会显示「现在可以取」而实际还要等一分钟。
            cut = dt.datetime.combine(now.date(), dt.time(15, 5))
            if nt and nt > cut:
                cut = nt
            cands.append((cut, key, "盘中"))
            continue
        if nt and nt > now:
            cands.append((nt, key, "退避"))
            continue
        live.append(key)

    if live:
        return out                      # 有源现在就能用
    if not cands:
        out.update(can=False, kind="nosource",
                   reason="没有启用的估值主源：至少打开腾讯/Tushare/东财中的一路")
        return out

    at, key, why = min(cands, key=lambda x: x[0])
    out.update(can=False, until=at.strftime("%H:%M"),
               wait_sec=max(0, int((at - now).total_seconds())),
               kind=("intraday" if why == "盘中" else "backoff"))
    if out["kind"] == "intraday":
        out["reason"] = ("现在 %s 盘中，腾讯/东财要等 15:05 收盘后才给当日估值"
                         "（盘中取到的是没收盘的半截，不会写库），Tushare 每日 19:30 才更新。"
                         % now.strftime("%H:%M"))
        hourly = [c for c in cands if c[1] == "ts" and c[2] == "退避"]
        if hourly:
            out["reason"] += "Tushare 本小时配额已用完（%s 恢复）。" % hourly[0][0].strftime("%H:%M")
        out["reason"] += "%s 后系统会自动抓一次，现在不用手动点。" % out["until"]
    else:
        out["reason"] = "各估值源都在退避（%s），最早 %s 后可重试。" % (
            "、".join("%s %s" % (META[c[1]]["short"], c[0].strftime("%H:%M")) for c in cands),
            out["until"])
    if out["last"]:
        out["reason"] += "当前沿用 %s 的 %d 只旧值。" % (out["last"], out["count"])
    return out


def _in_session(now: dt.datetime) -> bool:
    """是否处在「快照源只能拿到当日半截数据」的时段。

    9:15 集合竞价前，东财/腾讯返回的仍是上一交易日收盘，可以直接用；
    15:05 之后当日估值已经定盘。只有中间这 5 小时 50 分钟不能用快照源。
    """
    if not (OPEN_HM <= now.strftime("%H%M") < SNAP_READY_HM):
        return False
    try:
        import pipeline
        return pipeline._is_trading_day(now.strftime("%Y%m%d")) is not False
    except Exception:
        return True                     # 日历不可用时保守当作盘中


def _dt_of(s) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _log(msg: str, level: str = "info", act: str = "", detail: str = "") -> None:
    """估值链路的日志。写失败一律忽略 —— 日志不能影响取数。"""
    try:
        import store
        store.log("valsrc", msg, level=level, act=act, detail=detail)
    except Exception:
        pass


# ---------------------------------------------------------------- 工具

def _f(v) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _row(code: str, **kw) -> dict:
    ex = "SH" if code[0] == "6" else ("BJ" if code[0] in "489" else "SZ")
    # 腾讯只给市盈率(TTM)（f39），没有单独的静态/动态 PE。不兜底的话
    # 主源一刷新，daily_basic.csv 的 pe 整列为空：估值因子、单股 PE、
    # 同行 PE 分位全部失效，而 PB 与市值都正常，很难第一眼发现。
    pe = kw.get("pe")
    pe_ttm = kw.get("pe_ttm")
    if pe in (None, "") and pe_ttm not in (None, ""):
        pe = pe_ttm
    out = {"ts_code": "%s.%s" % (code, ex), "trade_date": kw.get("date") or "",
           "close": kw.get("price"), "pe": pe, "pe_ttm": pe_ttm,
           "pb": kw.get("pb"), "total_mv": kw.get("total_mv"),
           "circ_mv": kw.get("circ_mv"), "turnover_rate": kw.get("turnover"),
           "volume_ratio": kw.get("vr"), "name": kw.get("name") or ""}
    return out


def _denom() -> int:
    """覆盖率分母：本地股票池只数。取不到就按 5000 估，别让覆盖率变成 100%。"""
    try:
        import store
        return max(1000, int(store.cached_count()))
    except Exception:
        return 5000


def _cover(rows: list[dict], denom: int) -> float:
    codes = {str(r.get("ts_code") or "")[:6] for r in rows}
    # 源站通常比本地缓存多几只（退市整理、未入池新股），覆盖率封顶 100%，
    # 否则「106%」这种数字会让人以为多拿了什么。
    return round(min(1.0, len(codes) / float(denom)), 4) if denom else 0.0


def _complete(rows: list[dict]) -> float:
    """关键字段完整度：PB 与总市值都有的比例。

    不把 PE 计入：亏损股的 PE 本来就是空的（东财给负数、Tushare 给 null），
    拿它当「不完整」会让 Tushare 永远只有 69% 而被误判成次品。
    """
    if not rows:
        return 0.0
    ok = sum(1 for r in rows
             if _f(r.get("pb")) is not None and _f(r.get("total_mv")) is not None)
    return round(ok / float(len(rows)), 4)


# ---------------------------------------------------------------- 各源实现

# 东财 push2 全市场列表：m:0 t:6 深主板 / t:80 创业板 / t:81 北交所，m:1 t:2 沪主板 / t:23 科创板
_EM_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_EM_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
_EM_FIELDS = "f2,f8,f9,f10,f12,f14,f20,f21,f23,f115,f124"


def _em_page(pn: int, tries: int = 2):
    # 不要带 ut：社区流传的固定 token「bd1d9ddb…」已失效，带它必连接超时（2026-11-24 实测）。
    # 去掉 ut 后 f124 时间戳 / PE / PB / 总市值等字段仍在，6 并发 12/12 成功。
    # 若以后要加回，先用 valsrc.test("em") 验证，别直接抄 akshare 里的常量（它也是过期的）。
    p = {"pn": pn, "pz": 100, "po": 0, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
         "fs": _EM_FS, "fields": _EM_FIELDS}
    err = None
    for _ in range(tries):
        try:
            r = requests.get(_EM_URL, params=p, headers=_HEAD, timeout=12)
            d = (r.json() or {}).get("data") or {}
            diff = d.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            return diff, int(d.get("total") or 0)
        except Exception as e:
            err = e
            time.sleep(0.3)
    raise err or RuntimeError("东财无响应")


def _src_em(date: str) -> dict:
    t0 = time.time()
    first, total = _em_page(1)
    if not first:
        raise RuntimeError("东财返回空列表（可能被限流）")
    pages = max(1, math.ceil(total / 100))
    raw = list(first)
    if pages > 1:
        with cf.ThreadPoolExecutor(6) as ex:
            for diff, _t in ex.map(_em_page, range(2, pages + 1)):
                raw.extend(diff)
    out, day = [], ""
    for x in raw:
        code = str(x.get("f12") or "")
        if len(code) != 6 or not code.isdigit():
            continue
        ts = _f(x.get("f124"))
        if ts:
            day = max(day, dt.datetime.fromtimestamp(ts).strftime("%Y%m%d"))
        mv, cmv = _f(x.get("f20")), _f(x.get("f21"))
        out.append(_row(code, date=day or date, name=str(x.get("f14") or ""),
                        price=_f(x.get("f2")), pe=_f(x.get("f9")), pe_ttm=_f(x.get("f115")),
                        pb=_f(x.get("f23")),
                        total_mv=(mv / 1e4 if mv else None),      # 元 → 万元
                        circ_mv=(cmv / 1e4 if cmv else None),
                        turnover=_f(x.get("f8")), vr=_f(x.get("f10"))))
    return {"ok": True, "date": day or date, "rows": out,
            "note": "%d 页并发，共 %d 只" % (pages, len(out)),
            "ms": int((time.time() - t0) * 1000)}


_TX_URL = "https://qt.gtimg.cn/q="
_TX_HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def _tx_batch(codes: list[str]) -> list[dict]:
    import spot
    keys = [spot._prefix(c) + c for c in codes]      # _prefix 只给 sh/sz/bj，不拼代码
    r = requests.get(_TX_URL + ",".join(keys), headers=_TX_HEAD, timeout=12)
    r.encoding = "gbk"
    out = []
    for line in r.text.split(";"):
        if "~" not in line:
            continue
        f = line.split("~")
        if len(f) < 50:
            continue
        code = str(f[2] or "")
        if len(code) != 6:
            continue
        day = str(f[30] or "")[:8]
        circ, tot = _f(f[44]), _f(f[45])       # 亿元
        out.append(_row(code, date=day, name=f[1],
                        price=_f(f[3]), pe=None, pe_ttm=_f(f[39]), pb=_f(f[46]),
                        total_mv=(tot * 10000 if tot else None),   # 亿 → 万元
                        circ_mv=(circ * 10000 if circ else None),
                        turnover=_f(f[38]), vr=_f(f[49])))
    return out


def _src_tx(date: str) -> dict:
    t0 = time.time()
    import store
    codes = store.cached_codes()
    if not codes:
        raise RuntimeError("本地还没有股票池，先预热行情")
    chunks = [codes[i:i + 60] for i in range(0, len(codes), 60)]
    rows, day, fails = [], "", 0
    with cf.ThreadPoolExecutor(4) as ex:
        for got in ex.map(_tx_batch, chunks):
            if got:
                rows.extend(got)
                for r in got:
                    if r.get("trade_date"):
                        day = max(day, r["trade_date"])
            else:
                fails += 1
    if not rows:
        raise RuntimeError("腾讯行情返回空（可能被限流）")
    return {"ok": True, "date": day or date, "rows": rows,
            "note": "%d 批%s" % (len(chunks), "，%d 批空" % fails if fails else ""),
            "ms": int((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- 新浪（纯行情兜底）
# 新浪 hq.sinajs.cn：GBK 文本，每只 34 个逗号字段，只有行情没有估值。
# 单批实测 800 只 0.22 秒（腾讯 60 只要 ~0.1 秒但字段全），只用来在腾讯/东财都挂时兜价格。
_SINA_URL = "http://hq.sinajs.cn/list="
_SINA_HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}


def _sina_batch(codes: list[str]) -> list[dict]:
    import spot
    keys = [spot._prefix(c) + c for c in codes]
    r = requests.get(_SINA_URL + ",".join(keys), headers=_SINA_HEAD, timeout=12)
    r.encoding = "gbk"
    out = []
    for line in r.text.split(";"):
        if '="' not in line:
            continue
        key = line.split("=")[0].strip().split("_")[-1]     # sh600519 / sz000001 / bj430047
        f = line.split('="', 1)[1].rstrip('";\n\r').split(",")
        if len(f) < 32:
            continue
        code = key[2:] if key[:2] in ("sh", "sz", "bj") else ""
        if len(code) != 6 or not code.isdigit():
            continue
        # 盘前（9:15~9:25 集合竞价前）与停牌时现价是 0，但昨收是有效的价格参考。
        # 这条通道只做「价格兜底」，退回昨收比整批丢弃更有用；确实停牌的票，
        # 昨收也就是它的最新成交价。
        price = _f(f[3]) or _f(f[2])
        if not price:
            continue
        day = str(f[30] or "").replace("-", "")[:8]
        out.append(_row(code, date=day, name=f[0], price=price))
    return out


def _src_sina(date: str) -> dict:
    t0 = time.time()
    import store
    codes = store.cached_codes()
    if not codes:
        raise RuntimeError("本地还没有股票池，先预热行情")
    chunks = [codes[i:i + 800] for i in range(0, len(codes), 800)]
    rows, day, fails = [], "", 0
    with cf.ThreadPoolExecutor(4) as ex:
        for got in ex.map(_sina_batch, chunks):
            if got:
                rows.extend(got)
                for r in got:
                    if r.get("trade_date"):
                        day = max(day, r["trade_date"])
            else:
                fails += 1
    if not rows:
        raise RuntimeError("新浪行情返回空（可能被限流）")
    return {"ok": True, "date": day or date, "rows": rows,
            "note": "仅价格无估值 · %d 批%s" % (len(chunks), "，%d 批空" % fails if fails else ""),
            "ms": int((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- 通达信 TCP（独立通道冗余）
# 与 HTTP 源完全独立的一条路：通达信私有协议走 7709 TCP，不走系统代理，
# 东财/腾讯被代理挡住时这条还能通。tdxpy 是 pytdx 的维护版（mootdx 依赖）。
# 只给行情不给估值；服务器列表可用 data/tdx.json 覆盖（格式 [[名称,IP,端口], ...]）。
_TDX_DEFAULT = [
    ["深圳双线1", "110.41.147.114", 7709],
    ["上海双线1", "124.70.199.56", 7709],
    ["杭州", "115.238.90.165", 7709],
    ["上海双线2", "180.153.18.170", 7709],
    ["宁波", "218.75.126.9", 7709],
]
_TDX_CFG = DATA / "tdx.json"


def _tdx_servers() -> list[tuple[str, str, int]]:
    try:
        raw = json.loads(_TDX_CFG.read_text("utf-8")) if _TDX_CFG.exists() else None
        if isinstance(raw, list) and raw:
            return [(str(s[0]), str(s[1]), int(s[2])) for s in raw if len(s) >= 3]
    except Exception:
        pass
    return [(str(s[0]), str(s[1]), int(s[2])) for s in _TDX_DEFAULT]


def _tdx_connect() -> tuple:
    """连一台能取到报价的服务器。列表能读但报价全空也视为不可用。"""
    from tdxpy.hq import TdxHq_API
    servers = _tdx_servers()
    errs = []
    for name, ip, port in servers:
        api = TdxHq_API(heartbeat=False)
        try:
            if not api.connect(ip, port, time_out=4):
                errs.append("%s 连不上" % name)
                continue
            probe = api.get_security_quotes([(1, "600519")])
            if not probe:
                errs.append("%s 报价空" % name)
                api.disconnect()
                continue
            return api, name
        except Exception as e:
            errs.append("%s %s" % (name, type(e).__name__))
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError("通达信无可用服务器：" + "；".join(errs[:4]))


def _tdx_market(code: str) -> int:
    # 通达信：1=沪市（6 开头），0=深市/北交所（0/3/4/8 开头）
    return 1 if code[0] == "6" else 0


def _src_tdx(date: str) -> dict:
    t0 = time.time()
    import store
    codes = store.cached_codes()
    if not codes:
        raise RuntimeError("本地还没有股票池，先预热行情")
    # tdxpy 单次报价上限 80 只，按市场分组避免跨市场混传
    groups = {"1": [], "0": []}
    for c in codes:
        groups[str(_tdx_market(c))].append(c)
    batches = []
    for mkt, cs in groups.items():
        batches += [(int(mkt), cs[i:i + 80]) for i in range(0, len(cs), 80)]

    api, srv = _tdx_connect()
    rows, day = [], ""
    try:
        for mkt, cs in batches:
            q = api.get_security_quotes([(mkt, c) for c in cs]) or []
            for r in q:
                code = str(r.get("code") or "")
                price = _f(r.get("price"))
                if len(code) != 6 or not code.isdigit() or not price:
                    continue
                tm = str(r.get("servertime") or "")       # YYYYMMDDHHMMSSM
                d = tm[:8] if len(tm) >= 8 else ""
                rows.append(_row(code, date=d, price=price,
                                 name=str(r.get("name") or "")))
                if d:
                    day = max(day, d)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if not rows:
        raise RuntimeError("通达信报价为空（%s，行情通道可能被限制）" % srv)
    return {"ok": True, "date": day or date, "rows": rows,
            "note": "仅价格无估值 · %s · %d 批" % (srv, len(batches)),
            "ms": int((time.time() - t0) * 1000)}


_TS_FIELDS = ("ts_code,trade_date,close,pe,pe_ttm,pb,total_mv,circ_mv,"
              "turnover_rate,volume_ratio")


def _src_ts(date: str) -> dict:
    t0 = time.time()
    import datasource
    if not datasource.configured():
        raise RuntimeError("未配置 Token")
    rows = datasource.call("daily_basic", {"trade_date": date}, _TS_FIELDS)
    day, note = date, ""
    if not rows:
        prev = datasource._prev_trade_day(date)
        if prev:
            rows = datasource.call("daily_basic", {"trade_date": prev}, _TS_FIELDS)
            if rows:
                day, note = prev, "源站还没有 %s，退回 %s" % (date, prev)
    if not rows:
        raise RuntimeError("%s 及前一交易日均无数据（多为权限/积分延迟）" % date)
    for r in rows:
        r["name"] = r.get("name") or ""
        r["trade_date"] = r.get("trade_date") or day
    return {"ok": True, "date": day, "rows": rows,
            "note": note or "1 次请求", "ms": int((time.time() - t0) * 1000)}


def _src_bs(codes: list[str], date: str) -> dict:
    """按只补 PE/PB/换手（无市值）。baostock 连接非线程安全，串行。"""
    t0 = time.time()
    import baostock as bs
    import contextlib
    import io
    todo = [c for c in codes if c[0] in "036"][:BS_CAP]
    if not todo:
        raise RuntimeError("待补个股为空")
    with contextlib.redirect_stdout(io.StringIO()):
        lg = bs.login()
    if getattr(lg, "error_code", "0") != "0":
        raise RuntimeError("baostock 登录失败：%s" % getattr(lg, "error_msg", ""))
    bsd = "%s-%s-%s" % (date[:4], date[4:6], date[6:8]) if len(date) == 8 else date
    rows = []
    try:
        for c in todo:
            code = ("sh." if c[0] == "6" else "sz.") + c
            rs = bs.query_history_k_data_plus(
                code, "date,code,close,peTTM,pbMRQ,turn",
                start_date=bsd, end_date=bsd, frequency="d", adjustflag="3")
            # 停牌/退市/代码不在 baostock 覆盖范围时，这里会返回 None 而不是空结果集
            if rs is None or getattr(rs, "error_code", "0") != "0":
                continue
            while rs.next():
                v = rs.get_row_data()
                if len(v) < 6:
                    continue
                rows.append(_row(c, date=date, price=_f(v[2]), pe=None, pe_ttm=_f(v[3]),
                                 pb=_f(v[4]), turnover=_f(v[5])))
    finally:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                bs.logout()
        except Exception:
            pass
    if not rows:
        raise RuntimeError("baostock 无该日数据")
    return {"ok": True, "date": date, "rows": rows,
            "note": "逐只补 %d 只" % len(rows), "ms": int((time.time() - t0) * 1000)}


RUNNERS = {"em": _src_em, "tx": _src_tx, "sina": _src_sina, "tdx": _src_tdx, "ts": _src_ts}


# ---------------------------------------------------------------- 编排

def _try(key: str, date: str) -> dict:
    try:
        r = RUNNERS[key](date)
        r.setdefault("ok", False)
        return r
    except Exception as e:
        return {"ok": False, "date": "", "rows": [], "error": str(e)[:200], "ms": 0}


def _merge(primary: list[dict], extra: list[dict]) -> int:
    """同一天的第二份数据：只补空字段，不覆盖已有值。返回补了多少条。"""
    idx = {str(r.get("ts_code") or "")[:6]: r for r in primary}
    filled = 0
    for r in extra:
        code = str(r.get("ts_code") or "")[:6]
        t = idx.get(code)
        if not t:
            continue
        hit = False
        for k in ("pe", "pe_ttm", "pb", "total_mv", "circ_mv", "turnover_rate", "volume_ratio"):
            if t.get(k) in (None, "") and r.get(k) not in (None, ""):
                t[k] = r[k]
                hit = True
        if hit:
            filled += 1
    return filled


def _local_state() -> dict:
    """本地已有的估值（sources.json 记的 last_date/rows），用作「全部源不可用」时的兜底。"""
    try:
        import json as _j
        cfg = _j.loads((DATA / "sources.json").read_text("utf-8")) or {}
        t = cfg.get("tushare") or {}
        return {"date": str(t.get("last_date") or ""), "count": int(t.get("rows") or 0),
                "source": t.get("last_source") or "local"}
    except Exception:
        return {"date": "", "count": 0, "source": "local"}


def _persist_last(attempts: list[dict], now: dt.datetime, fetched: dict | None = None) -> None:
    """把本轮每源的结果写进 valsrc.json：页面要显示「上次取到哪天」，
    失败也要落盘（带退避时刻），否则下次照样一上来就撞墙。"""
    cfg = load_cfg()
    for a in attempts:
        if a.get("no_persist") or (a.get("skipped") and not a.get("error")):
            continue
        err = str(a.get("error") or "")
        note = str(a.get("note") or "")
        if a.get("ok"):
            nxt = ""
        elif note.startswith("盘中数据"):
            nxt = now.strftime("%Y-%m-%d") + " 15:06:00"     # 盘中重试没意义，等收盘
        elif "频率超限" in err or "限频" in err:
            # 不同积分档限频不一样：有的 1 次/分钟，有的 1 次/小时，按提示退避
            wait = 65 if "分钟" in err else 3600
            nxt = (now + dt.timedelta(seconds=wait)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            m = META[a["key"]]
            wait = m.get("backoff_sec", BACKOFF_SEC)  # 低频备份源失败后冷却更久
            nxt = (now + dt.timedelta(seconds=wait)).strftime("%Y-%m-%d %H:%M:%S")
        cfg["last"][a["key"]] = {
            "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": bool(a.get("ok")),
            "date": a.get("date") or "", "count": a.get("count") or 0,
            "ms": a.get("ms") or 0, "cover": a.get("cover") or 0.0,
            "error": (err or note)[:160], "next_try": nxt,
        }
        # 失败留痕：备份源的失败只记 info，别让「腾讯已达标」的好消息旁边
        # 永远挂一条东财 warn——这台机器代理挡着它，不是当天的故障。
        if not a.get("ok"):
            m = META[a["key"]]
            lvl = "info" if (m.get("backup") or note.startswith("盘中数据")) else "warn"
            _log("%s 未取到：%s" % (m["name"], (err or note or "无数据")[:120]),
                 level=lvl, act=a["key"], detail="下次可试 %s" % (nxt or "—"))
    if fetched:
        cfg["fetched"] = fetched
    save_cfg(cfg)


def fetch(date: str | None = None, force: bool = False, only: str | None = None) -> dict:
    """按配置顺序取估值，返回 {ok, date, source, rows, attempts, ...}。

    only=key 时忽略开关与节流，只跑那一个源（UI 的「测试」用，不落盘）。
    """
    cfg = load_cfg()
    now = dt.datetime.now()
    T = str(date or "").replace("-", "") or target_date(now)
    if not T:
        return {"ok": False, "error": "无法确定应到交易日（交易日历不可用）"}

    # 盘中 / 全部退避时直接告诉用户「还没到点」，不要真的去敲三个源：
    # 敲了必然全失败，用户看到的是一屏错误，实际问题只是没到 15:05。
    # 显式传了 date（回补某个历史交易日）或 force（用户勾了强制）时不拦。
    if not force and not only and not date:
        pre = precheck(now)
        if not pre["can"] and pre["kind"] != "done":
            _log("预检拦下取估值：%s" % pre["reason"], level="info", act="precheck",
                 detail="kind=%s target=%s last=%s" % (pre["kind"], pre["target"], pre["last"]))
            return {"ok": True, "blocked": True, "stale": True,
                    "date": pre["last"], "count": pre["count"],
                    "source": pre["source"], "source_name": name_of(pre["source"]),
                    "rows": [], "expected": T, "precheck": pre, "attempts": [],
                    "message": pre["reason"]}
    denom = _denom()

    # 今天这活已经干过就别再敲：源站限频、账号额度都省下来
    got = cfg.get("fetched") or {}
    if not force and not only and str(got.get("date") or "") >= T and int(got.get("count") or 0) > 1000:
        return {"ok": True, "reused": True, "date": got["date"], "count": int(got["count"]),
                "source": got.get("source"), "source_name": name_of(got.get("source")),
                "expected": T, "stale": False, "attempts": [], "rows": [],
                "message": "%s 的全市场估值已在 %s 拉取（%d 只，来源 %s），直接复用"
                           % (got["date"], got.get("at") or "", int(got["count"]),
                              name_of(got.get("source")))}

    snap_day = target_date(now) if has_snapshot() else ""
    attempts: list[dict] = []
    acc = None            # 达标：日期 ≥ T 且覆盖够
    best = None           # 兜底：日期最新、同日期覆盖最高
    price_backup: list[dict] = []   # 价格兜底源（新浪/通达信）的可用情况，只提示不落盘
    last_key = cfg.get("last") or {}

    for key in cfg["order"]:
        m = META[key]
        if only and key != only:
            continue
        if not only and not cfg["enabled"].get(key, m["enabled"]):
            attempts.append({"key": key, "name": m["name"], "ok": False, "skipped": "已关闭",
                             "date": "", "cover": 0.0, "ms": 0})
            continue
        if m["role"] == "fill":
            continue          # 补漏源不参与采纳，等主源定下来再补
        if m["cap"] == "snapshot" and snap_day and T < snap_day:
            attempts.append({"key": key, "name": m["name"], "ok": False,
                             "skipped": "只能取当前交易日", "date": "", "cover": 0.0, "ms": 0})
            continue
        # 失败退避：连点或源站抽风时别反复敲门。盘中失败会退避到 15:06，
        # Tushare 限频会退避 1 小时；force / 单独测试不受此限。
        if not force and not only:
            nt = last_key.get(key) or {}
            if nt.get("ok") is False and nt.get("next_try"):
                try:
                    if dt.datetime.strptime(str(nt["next_try"]), "%Y-%m-%d %H:%M:%S") > now:
                        attempts.append({"key": key, "name": m["name"], "ok": False,
                                         "skipped": "退避至 %s" % str(nt["next_try"])[11:16],
                                         "error": str(nt.get("error") or ""),
                                         "no_persist": True,
                                         "date": "", "cover": 0.0, "ms": 0})
                        continue
                except Exception:
                    pass

        r = _try(key, T)
        cover = _cover(r.get("rows") or [], denom)
        comp = _complete(r.get("rows") or [])
        rec = {"key": key, "name": m["name"], "ok": bool(r.get("ok")),
               "date": r.get("date") or "", "count": len(r.get("rows") or []),
               "cover": cover, "complete": comp, "ms": r.get("ms") or 0,
               "note": r.get("note") or "", "error": r.get("error") or ""}
        # 盘中拿到的「今天」是没收盘的半截数据，绝不能当成收盘估值写进按日文件；
        # 这种情况比昨天的收盘数据更差，所以直接判无效，等 15:05 之后自然就好。
        if r.get("ok") and m["cap"] in ("snapshot", "price") and (r.get("date") or "") > T:
            rec["ok"] = False
            rec["note"] = "盘中数据（%s 未收盘），等 15:05 后再取" % r["date"]
            attempts.append(rec)
            continue
        attempts.append(rec)
        if r.get("ok") and r.get("rows"):
            cand = r
            cand["_cover"], cand["_comp"], cand["_key"] = cover, comp, key
            # price 源（新浪/通达信）只给行情不给估值：记录能取到多少只价格，
            # 但绝不参与估值采纳 —— 否则 comp=0 的 rows 会覆盖掉带 PE/PB 的文件。
            if m["cap"] == "price":
                if r.get("date") == T:
                    price_backup.append({"key": key, "name": m["name"],
                                         "count": len(r["rows"]), "cover": cover})
                continue
            if (r.get("date") or "") >= T and cover >= COVER_OK and comp >= FILL_OK:
                acc = cand
                break
            if best is None or ((r.get("date") or ""), cover) > ((best.get("date") or ""), best["_cover"]):
                best = cand

    if acc is None and best is not None:
        # 关键字段不够全：再试下一个能取同一天的源补空（不覆盖已有值）
        if best["_comp"] < FILL_OK:
            for key in cfg["order"]:
                if key == best.get("_key") or not cfg["enabled"].get(key, META[key]["enabled"]):
                    continue
                m = META[key]
                if m["role"] != "main" or m["cap"] == "snapshot" and T < snap_day:
                    continue
                if any(a["key"] == key for a in attempts):
                    continue
                r = _try(key, T)
                if r.get("ok") and (r.get("date") or "") == best.get("date"):
                    filled = _merge(best["rows"], r.get("rows") or [])
                    attempts.append({"key": key, "name": m["name"], "ok": True,
                                     "date": r.get("date") or "", "count": len(r.get("rows") or []),
                                     "cover": _cover(r.get("rows") or [], denom),
                                     "complete": _complete(r.get("rows") or []),
                                     "ms": r.get("ms") or 0,
                                     "note": "补全 %d 只的空字段" % filled})
                    best["_comp"] = _complete(best["rows"])
                    if best["_comp"] >= FILL_OK:
                        break
        acc = best

    # 主源缺个股时用 baostock 补（默认关闭；只在覆盖率真的不够时触发）
    if acc is not None and cfg["enabled"].get("bs") and acc["_cover"] < COVER_OK:
        try:
            import store
            have = {str(r.get("ts_code") or "")[:6] for r in acc["rows"]}
            miss = [c for c in store.cached_codes() if c not in have and c[0] in "036"]
            if miss:
                r = _src_bs(miss, acc.get("date") or T)
                filled = _merge(acc["rows"], r.get("rows") or [])
                acc["rows"].extend(x for x in (r.get("rows") or [])
                                   if str(x.get("ts_code") or "")[:6] not in have)
                acc["_cover"] = _cover(acc["rows"], denom)
                attempts.append({"key": "bs", "name": META["bs"]["name"], "ok": True,
                                 "date": acc.get("date") or T,
                                 "count": len(r.get("rows") or []),
                                 "cover": acc["_cover"], "complete": _complete(acc["rows"]),
                                 "ms": r.get("ms") or 0, "note": r.get("note") or "",
                                 "error": ""})
        except Exception as e:
            attempts.append({"key": "bs", "name": META["bs"]["name"], "ok": False,
                             "date": "", "cover": 0.0, "complete": 0.0, "ms": 0, "skipped": "",
                             "note": "", "error": str(e)[:120]})

    if acc is None:
        # 全部源都不可用（盘中 + 限频是常见组合）：不清空既有文件，
        # 但也不谎报成功 —— 返回 stale，让上层按「用的是旧值」处理。
        prev = cfg.get("fetched") or _local_state()
        why = "；".join("%s %s" % (a["name"], a.get("error") or a.get("note") or a.get("skipped") or "空")
                       for a in attempts if not a.get("ok"))
        _persist_last(attempts, now)          # 只落一次：多调一次会把每条日志写两遍
        _log("四路都没取到估值（应到 %s）" % T,
             level=("warn" if int(prev.get("count") or 0) > 1000 else "error"),
             act="all_fail", detail=why[:1500])
        pb_note = ("；价格通道仍可用：" + "、".join(
            "%s %d 只" % (p["name"], p["count"]) for p in price_backup)) if price_backup else ""
        if int(prev.get("count") or 0) > 1000:
            return {"ok": True, "stale": True, "date": prev.get("date") or "",
                    "count": int(prev["count"]), "source": prev.get("source") or "cache",
                    "source_name": name_of(prev.get("source")), "rows": [], "cover": None,
                    "complete": None, "expected": T, "attempts": attempts,
                    "price_backup": price_backup,
                    "message": "本次没取到新估值（%s），继续沿用 %s 的 %d 只旧值%s" %
                               (why, prev.get("date") or "—", int(prev["count"]), pb_note)}
        return {"ok": False, "error": "所有启用的估值源都没取到数据：" + why,
                "attempts": attempts, "expected": T}

    day = str(acc.get("date") or T)
    rows = acc["rows"]
    cover = acc["_cover"]
    comp = _complete(rows)
    # stale 不能只看日期：某个源 date==T、cover 96%，但 PB/总市值只给了 60%，
    # 以前会当成正常成功并写 fetched，导致当天不再重试、PE 分位与市值中性化
    # 静默降级一整天。关键字段不够全时也要如实标 stale（并因此不写 fetched）。
    stale = bool(day < T or comp < FILL_OK)
    key = acc.get("_key") or ""
    _log("采纳 %s：%s · %d 只 · 覆盖 %.0f%%%s" % (
        name_of(key), day, len(rows), cover * 100,
        (" · " + (("落后应到日 %s" % T) if day < T
                  else "关键字段仅 %.0f%%" % (comp * 100))) if stale else ""),
        level=("warn" if stale else "info"), act="adopt",
        detail="完整度 %.0f%% · 耗时 %.1fs · 顺序 %s" % (
            comp * 100, (acc.get("ms") or 0) / 1000.0, "→".join(cfg["order"])))

    # 上次结果落盘（数据源页要显示「上次取到哪天 / 为什么没取到」）
    if not only:
        _persist_last(attempts, now,
                      {"date": day, "count": len(rows), "source": key,
                       "at": now.strftime("%Y-%m-%d %H:%M:%S")}
                      if (not stale and cover >= MIN_WRITE) else None)

    msg = "已用%s取到 %s 全市场估值 %d 只（覆盖 %.0f%%%s，%.1f 秒）" % (
        name_of(key), day, len(rows), cover * 100,
        "，关键字段 %.0f%%" % (_complete(rows) * 100) if _complete(rows) < 1 else "",
        (acc.get("ms") or 0) / 1000.0)
    if stale:
        msg = "估值只拿到 %s（应到 %s，落后 %d 个交易日），来源 %s；先按旧值算，别当真" % (
            day, T, _lag(day, T), name_of(key))
    return {"ok": True, "date": day, "source": key, "source_name": name_of(key),
            "rows": rows, "count": len(rows), "cover": cover,
            "complete": _complete(rows), "expected": T, "stale": stale,
            "attempts": attempts, "message": msg}


def _lag(day: str, expected: str) -> int:
    try:
        import pipeline
        return max(0, int(pipeline._trade_lag(day, expected) or 0))
    except Exception:
        try:
            a = dt.datetime.strptime(day, "%Y%m%d")
            b = dt.datetime.strptime(expected, "%Y%m%d")
            return max(0, (b - a).days)
        except Exception:
            return 0


def test(key: str) -> dict:
    """单源探测：不看开关、不落盘，只回答「这个源现在能不能取到、取到哪一天」。"""
    m = META.get(key)
    if not m:
        return {"ok": False, "error": "未知数据源：%s" % key}
    T = target_date() or dt.datetime.now().strftime("%Y%m%d")
    t0 = time.time()
    if m["role"] == "fill":
        try:
            import store
            r = _src_bs(store.cached_codes()[:20], T)
            return {"ok": True, "key": key, "date": r.get("date"), "count": len(r.get("rows") or []),
                    "ms": int((time.time() - t0) * 1000),
                    "message": "%s 可用：%s 补到 %d 只（无市值字段）" % (m["name"], T, len(r.get("rows") or []))}
        except Exception as e:
            return {"ok": False, "key": key, "error": str(e)[:200]}
    r = _try(key, T)
    if not r.get("ok"):
        return {"ok": False, "key": key, "error": r.get("error") or "无数据",
                "ms": int((time.time() - t0) * 1000)}
    rows = r.get("rows") or []
    return {"ok": True, "key": key, "date": r.get("date"), "count": len(rows),
            "cover": _cover(rows, _denom()), "complete": _complete(rows),
            "ms": r.get("ms") or int((time.time() - t0) * 1000),
            "message": "%s 可用：%s 共 %d 只，覆盖 %.0f%%，关键字段 %.0f%%"
                       % (m["name"], r.get("date"), len(rows),
                          _cover(rows, _denom()) * 100, _complete(rows) * 100)}


def status() -> dict:
    cfg = load_cfg()
    denom = _denom()
    srcs = []
    for i, key in enumerate(cfg["order"]):
        m = META[key]
        last = cfg["last"].get(key) or {}
        ok = last.get("ok")
        err = str(last.get("error") or "")
        if not cfg["enabled"].get(key, m["enabled"]):
            state = "关闭"
        elif ok is None:
            state = "未使用"
        elif ok:
            state = "正常"
        elif err.startswith("盘中数据"):
            state = "待收盘"
        elif "频率超限" in err or "限频" in err:
            state = "限频"
        else:
            state = "失败"
        srcs.append({"key": key, "name": m["name"], "short": m["short"], "cap": m["cap"],
                     "role": m["role"], "backup": bool(m.get("backup")),
                     "token": m["token"], "speed": m["speed"],
                     "est": m["est"], "fields": m["fields"], "note": m["note"],
                     "order": i + 1, "enabled": bool(cfg["enabled"].get(key, m["enabled"])),
                     "state": state, "last": last})
    got = cfg.get("fetched") or _local_state()
    T = target_date()
    day = str(got.get("date") or "")
    pre = precheck()
    return {"ok": True, "sources": srcs, "order": cfg["order"],
            "active": bool(got), "last_date": day, "last_count": int(got.get("count") or 0),
            "last_source": got.get("source") or "", "last_at": got.get("at") or "",
            "expected": T, "lag": _lag(day, T) if (day and T) else None,
            "stale": bool(day and T and day < T),
            "precheck": pre, "next_try": pre.get("until") or "",
            "denom": denom, "any_enabled": any_enabled(), "snapshot": has_snapshot()}
