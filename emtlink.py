# -*- coding: utf-8 -*-
"""
实盘账户连接层（基于 emtl，**只读**）

emtl 是什么
-----------
`emtl`（East Money Trading Library）是东财证券交易网关 jywg.18.cn 的 Python 封装，
自带验证码识别、RSA 密码加密、会话票据。比 emta 关键的优势是**有历史数据接口**：

    query_asset_and_position()            资金 + 持仓（当前快照）
    query_orders()                        当日委托
    query_trades()                        当日成交
    query_history_trades(size, st, et)    历史成交       ← 能补历史
    query_history_orders(size, st, et)    历史订单       ← 能算撤单率
    query_funds_flow(size, st, et)        资金流水       ← 能算净入金/真实费用/分红

只读边界
--------
只用上面这些查询。本模块**不 import、不定义、不调用** create_order / cancel_order，
`emtl.TradingClient` 的下单方法在这里从未被引用 —— 这是复盘工具，不碰真钱。

数据沉淀（三层）
----------------
1. `ledger_days` 表（SQLite）     每日快照：总资产、持仓成本、当日委托。成本价只有快照能给。
2. `fills` 表（SQLite）           历史成交明细（`query_history_trades` 回流），FIFO 的唯一权威来源。
3. `flows` 表（SQLite）           资金流水（`query_funds_flow` 回流），用于净入金与真实费用。

`data/ledger/` 下的旧 JSON/CSV 只在首次迁移时读取一次，迁移标志落库后不再碰。

三者互不覆盖：快照回答「当时是什么样」，成交回答「做了什么」，流水回答「钱怎么进出」。

字段名
------
东财没有公开这些接口的字段字典，成交/流水是中文缩写（Cjrq / Cjsl / Fse ...）。
所以解析走**候选别名**（`_FILL_KEYS` 等），一次 sync 后把**原始 JSON 样本**留在
`data/ledger/_raw/`，界面上能直接看到「哪些字段认出来了、哪些没有」。
第一次接真实账号时若出现未识别字段，补一个别名即可，不用改结构。

凭据
----
`data/emt.json`，密码用本机派生密钥的 Fernet 加密（换机器解不开，需重填）。
不入日志、不外传，接口只回掩码账号。

频率
----
emtl 0.3.0 本身**没有节流**，`jywg.18.cn` 也没有公开的查询频率上限。
本模块自己兜底：盘中默认每 5 分钟刷新一次，只复用同一登录会话；
连续失败按 15 / 45 / 60 分钟退避，避免「高频查询」或「反复登录」变成账号风险。
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import getpass
import hashlib
import json
import platform
import threading
import uuid
from pathlib import Path

import journal as journal_md

from paths import BASE, DATA
CFG_PATH = DATA / "emt.json"
LEDGER = DATA / "ledger"
RAW_DIR = LEDGER / "_raw"
FILLS_CSV = LEDGER / "fills.csv"
FLOWS_CSV = LEDGER / "flows.csv"
ORDERS_CSV = LEDGER / "orders.csv"

_LOCK = threading.RLock()
_CFG_LOCK = threading.Lock()
_SYNC_LOCK = threading.Lock()          # 同一时间只允许一个快照写入，避免盘中刷新和手动同步打架
_CLI = {"obj": None, "at": 0.0, "user": ""}
# 东财票据有效期按登录时申请，emtl 默认 30 分钟；留 2 分钟余量
_SESSION_KEEP = 28 * 60

DEFAULT = {
    "user": "", "pass_enc": "", "enabled": False,
    "auto_sync": False, "auto_time": "15:35", "last_eod": "", "eod_try": "",
    # 盘中自动刷新：交易时段内每 N 分钟拉一次资金/持仓，不用等收盘
    "auto_refresh": True, "refresh_min": 5,
    "refresh_at": "", "refresh_ok": None, "refresh_msg": "",
    "last_sync": "", "last_ok": None, "last_msg": "",
    # 历史回流：起始日、最近一次拉取时间与结果
    "hist_from": "", "hist_at": "", "hist_msg": "",
}


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet
    seed = "quantdesk::emt::%s::%s::%s" % (
        platform.node(), getpass.getuser(), uuid.getnode())
    key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode("utf-8")).digest())
    return Fernet(key)


def _enc(s: str) -> str:
    if not s:
        return ""
    try:
        return _fernet().encrypt(s.encode("utf-8")).decode("ascii")
    except Exception:
        return ""


def _dec(s: str) -> str:
    if not s:
        return ""
    try:
        return _fernet().decrypt(s.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def load_cfg() -> dict:
    c = dict(DEFAULT)
    if CFG_PATH.exists():
        try:
            c.update(json.loads(CFG_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    return c


def save_cfg(**kw) -> dict:
    with _CFG_LOCK:
        c = load_cfg()
        if "password" in kw:
            pw = str(kw.pop("password") or "")
            if pw:
                c["pass_enc"] = _enc(pw)
        for k, v in kw.items():
            if k in DEFAULT:
                c[k] = v
        # 频率给上下限：1 分钟是“手动刷新级”，再快没有意义，也容易触发网关风控
        try:
            c["refresh_min"] = max(1, min(60, int(c.get("refresh_min") or 5)))
        except Exception:
            c["refresh_min"] = 5
        c["enabled"] = bool(c.get("user") and c.get("pass_enc"))
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
        return c


def _mask(user: str) -> str:
    u = str(user or "")
    if not u:
        return ""
    if len(u) <= 3:
        return u[0] + "*" * (len(u) - 1)
    return u[:2] + "*" * max(1, len(u) - 4) + u[-2:]


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------

class EmtError(RuntimeError):
    pass


_FRIENDLY = [
    ("验证码", "验证码识别失败，重试一次通常能过；连续失败请先手动登录东财网页确认账号状态"),
    ("verify code", "验证码识别失败，重试一次通常能过；连续失败请先手动登录东财网页确认账号状态"),
    ("密码", "账号或密码不正确"),
    ("password", "账号或密码不正确"),
    ("信息有误", "账号或密码不正确"),
    ("锁定", "账号被临时锁定（连续登录失败会锁），等 30 分钟再试"),
    ("Network", "网络不通，连不上 jywg.18.cn（东财证券交易网关）"),
    ("timeout", "连接东财超时，稍后再试"),
    ("validate", "登录页结构变化，取不到校验票据，可能需要升级 emtl"),
    ("authentication required", "登录态失效，重新登录也失败"),
]


def _explain(msg: str) -> str:
    low = str(msg or "").lower()
    for k, v in _FRIENDLY:
        if k.lower() in low:
            return v
    return str(msg or "未知原因")[:160]


def _now() -> float:
    import time
    return time.time()


def _login():
    import emtl

    cfg = load_cfg()
    user = str(cfg.get("user") or "").strip()
    pw = _dec(cfg.get("pass_enc") or "")
    if not user or not pw:
        raise EmtError("还没配置账号：在「实盘复盘 → 账户设置」里填东财证券的资金账号与交易密码")

    try:
        cli = emtl.TradingClient(user, pw)
    except Exception as e:                                   # noqa: BLE001
        raise EmtError(_explain(str(e))) from e
    try:
        token = cli.login(30)
    except Exception as e:                                   # noqa: BLE001
        try:
            cli.close()
        except Exception:
            pass
        raise EmtError(_explain(str(e))) from e
    if not token:
        try:
            cli.close()
        except Exception:
            pass
        raise EmtError("登录被拒绝：账号密码不对，或该账号未开通东财证券交易权限")

    _CLI.update(obj=cli, at=_now(), user=user)
    return cli


def _client(fresh: bool = False):
    import time
    with _LOCK:
        c = _CLI.get("obj")
        if c is not None and not fresh and (time.time() - _CLI["at"]) < _SESSION_KEEP:
            return c
        _drop()
        return _login()


def _drop() -> None:
    c = _CLI.get("obj")
    _CLI.update(obj=None, at=0.0, user="")
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def logout() -> None:
    with _LOCK:
        _drop()


def _q(fn: str, *args, **kw):
    """统一取 Data。票据过期（RuntimeError authentication required）时重登一次。"""
    c = _client()
    try:
        resp = getattr(c, fn)(*args, **kw)
    except Exception as e:                                   # noqa: BLE001
        msg = str(e)
        if "authentication required" not in msg.lower() and "closed" not in msg.lower():
            raise EmtError(_explain(msg)) from e
        c = _client(fresh=True)
        try:
            resp = getattr(c, fn)(*args, **kw)
        except Exception as e2:                              # noqa: BLE001
            raise EmtError(_explain(str(e2))) from e2
    return _payload(resp)


def _payload(resp) -> list[dict]:
    """东财统一响应 {Status, Data:[...]}；Status 非 0 时带上 Message。"""
    if resp is None:
        raise EmtError("接口没有返回内容（可能被网关限流，稍后再试）")
    if not isinstance(resp, dict):
        raise EmtError("接口返回格式异常：%s" % str(resp)[:80])
    status = resp.get("Status")
    if status not in (0, "0", None):
        raise EmtError(_explain(resp.get("Message") or resp.get("message") or "Status=%s" % status))
    data = resp.get("Data")
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    return [x for x in data if isinstance(x, dict)]


# --------------------------------------------------------------------------
# 字段解析：东财用中文缩写，给每个字段一组候选名
# --------------------------------------------------------------------------

def _v(row: dict, *names, default=None):
    for n in names:
        if n in row and row[n] not in (None, "", "-", "--"):
            return row[n]
    return default


def _f(v, digits: int = 2):
    try:
        if v is None or v == "" or str(v).strip() in ("-", "--"):
            return 0.0
        return round(float(str(v).replace(",", "")), digits)
    except Exception:
        return 0.0


def _i(v) -> int:
    try:
        return int(float(str(v).replace(",", "") or 0))
    except Exception:
        return 0


def _s(v) -> str:
    return str(v or "").strip()


def _day(v) -> str:
    """把 20261117 / 2026-11-17 / 2026/11/17 统一成 2026-11-17。"""
    t = str(v or "").strip()
    if not t:
        return ""
    digits = "".join(ch for ch in t if ch.isdigit())
    if len(digits) >= 8:
        return "%s-%s-%s" % (digits[:4], digits[4:6], digits[6:8])
    return ""


def _hhmmss(v) -> str:
    t = "".join(ch for ch in str(v or "") if ch.isdigit())
    if len(t) >= 6:
        return "%s:%s:%s" % (t[:2], t[2:4], t[4:6])
    return ""


_OVERVIEW_KEYS = {
    "Zzc": "total", "Zxsz": "mv", "Kyzj": "avail", "Kqzj": "withdraw",
    "Djzj": "frozen", "Dryk": "day_pnl", "Ljyk": "total_pnl",
    "Zjye": "cash", "RMBZzc": "rmb_total",
}


def _parse_overview(d: dict) -> dict:
    o = {v: _f(d.get(k)) for k, v in _OVERVIEW_KEYS.items()}
    o["currency"] = _s(d.get("Money_type")) or "人民币"
    base = o["total"] or (o["mv"] + o["cash"])
    o["exposure"] = round(o["mv"] / base * 100, 2) if base > 0 else 0.0
    return o


def _parse_pos(p: dict) -> dict:
    return {
        "code": _s(p.get("Zqdm")),
        "name": _s(p.get("Zqmc")),
        "qty": _i(p.get("Zqsl")),
        "avail": _i(p.get("Kysl")),
        "frozen": _i(p.get("Djsl")),
        "cost": _f(p.get("Cbjg"), 4),
        "price": _f(p.get("Zxjg"), 4),
        "mv": _f(p.get("Zxsz")),
        "pnl": _f(p.get("Ljyk")),
        "pnl_pct": round(_f(p.get("Ykbl"), 6) * 100, 2),
        "day_pnl": _f(p.get("Dryk")),
        "day_pct": round(_f(p.get("Drykbl"), 6) * 100, 2),
        "weight": round(_f(p.get("Cwbl"), 6) * 100, 2),
        "market": _s(p.get("Market")),
        "type": _s(p.get("Zqlxmc")),
    }


def _side(v) -> str:
    t = _s(v)
    if t in ("1", "B", "b", "买", "买入", "证券买入", "买入开仓"):
        return "买入"
    if t in ("2", "S", "s", "卖", "卖出", "证券卖出", "卖出平仓"):
        return "卖出"
    if "买" in t:
        return "买入"
    if "卖" in t:
        return "卖出"
    return t or "—"


_ST_FILLED = ("4", "已成", "全部成交", "已成交")
_ST_PART = ("3", "部成", "部分成交")


def _state(s: str) -> str:
    t = _s(s)
    if t in _ST_FILLED or "全部成交" in t:
        return "已成"
    if t in _ST_PART or "部成" in t or "部分成交" in t:
        return "部成"
    if t in ("5",) or "撤" in t:
        return "已撤"
    if t in ("8",) or "废" in t:
        return "废单"
    if "成" in t and "未" not in t:
        return "已成"
    return "未成"


# 成交明细的候选字段。东财历史成交/当日成交用同一套缩写，
# 历史接口可能多一个 Cjrq（成交日期）而当日只有 Cjsj（成交时间）。
_FILL_KEYS = {
    "date": ("Cjrq", "CjRq", "Jyrq", "Wtrq", "date", "Date", "Cjsj2"),
    "time": ("Cjsj", "Wtsj", "Cjsj2", "time"),
    "code": ("Zqdm", "code"),
    "name": ("Zqmc", "name"),
    "side": ("Mmsm", "Mmlb", "Mmlb_ex", "side"),
    "qty": ("Cjsl", "Wtsl", "qty"),
    "price": ("Cjjg", "Wtjg", "price"),
    "amount": ("Cjje", "amount"),
    "commission": ("Yj", "Yongjin", "Commission", "Sxf"),
    "tax": ("Yhs", "Yhsje", "StampTax"),
    "transfer": ("Ghf", "Ghfje", "TransferFee"),
    "fee": ("Sxf", "Fee", "Fy"),
    "id": ("Cjbh", "Wtbh", "id"),
}

_ORDER_KEYS = {
    "date": ("Wtrq", "Cjrq", "date"),
    "time": ("Wtsj", "Cjsj"),
    "code": ("Zqdm", "code"),
    "name": ("Zqmc", "name"),
    "side": ("Mmsm", "Mmlb"),
    "qty": ("Wtsl", "qty"),
    "price": ("Wtjg", "price"),
    "filled": ("Cjsl", "filled"),
    "amount": ("Cjje", "amount"),
    "state": ("Wtzt", "state"),
    "id": ("Wtbh", "Wtph", "id"),
}

_FLOW_KEYS = {
    "date": ("Fsrq", "Czrq", "Frrq", "Jyrq", "date"),
    "kind": ("Zhaiyao", "Zy", "Zhlx", "Fsnr", "Ywlx", "Abstract", "kind"),
    "amount": ("Fse", "Fsje", "Amount", "amount"),
    "balance": ("Ye", "Zjye", "Balance", "balance"),
    "code": ("Zqdm", "code"),
    "name": ("Zqmc", "name"),
}


def _parse_fill(r: dict) -> dict:
    qty = _i(_v(r, *_FILL_KEYS["qty"]))
    price = _f(_v(r, *_FILL_KEYS["price"]), 4)
    amount = _f(_v(r, *_FILL_KEYS["amount"]))
    if amount <= 0 and qty > 0 and price > 0:
        amount = round(qty * price, 2)
    if price <= 0 and qty > 0 and amount > 0:
        price = round(amount / qty, 4)
    fee = _f(_v(r, *_FILL_KEYS["fee"]))
    if fee <= 0:
        fee = round(_f(_v(r, *_FILL_KEYS["commission"]))
                    + _f(_v(r, *_FILL_KEYS["tax"]))
                    + _f(_v(r, *_FILL_KEYS["transfer"])), 2)
    day = _day(_v(r, *_FILL_KEYS["date"]))
    return {
        "date": day,
        "time": _hhmmss(_v(r, *_FILL_KEYS["time"])),
        "code": _s(_v(r, *_FILL_KEYS["code"])),
        "name": _s(_v(r, *_FILL_KEYS["name"])),
        "side": _side(_v(r, *_FILL_KEYS["side"])),
        "qty": qty, "price": price, "amount": amount,
        "fee": fee,
        "id": _s(_v(r, *_FILL_KEYS["id"])),
    }


def _parse_order(r: dict) -> dict:
    qty = _i(_v(r, *_ORDER_KEYS["qty"]))
    fill = _i(_v(r, *_ORDER_KEYS["filled"]))
    amount = _f(_v(r, *_ORDER_KEYS["amount"]))
    price = _f(_v(r, *_ORDER_KEYS["price"]), 3)
    avg = round(amount / fill, 3) if fill > 0 and amount > 0 else price
    day = _day(_v(r, *_ORDER_KEYS["date"]))
    oid = _s(_v(r, *_ORDER_KEYS["id"]))
    return {
        "id": oid or "%s_%s_%s_%s" % (day, _s(_v(r, *_ORDER_KEYS["code"])),
                                      _side(_v(r, *_ORDER_KEYS["side"])), qty),
        "date": day,
        "time": _hhmmss(_v(r, *_ORDER_KEYS["time"])),
        "code": _s(_v(r, *_ORDER_KEYS["code"])),
        "name": _s(_v(r, *_ORDER_KEYS["name"])),
        "side": _side(_v(r, *_ORDER_KEYS["side"])),
        "qty": qty, "price": price, "filled": fill,
        "amount": amount, "avg_price": avg,
        "state": _state(_v(r, *_ORDER_KEYS["state"])),
        "raw_state": _s(_v(r, *_ORDER_KEYS["state"])),
        "fill_ratio": round(fill / qty * 100, 1) if qty else 0.0,
    }


def _parse_flow(r: dict) -> dict:
    return {
        "date": _day(_v(r, *_FLOW_KEYS["date"])),
        "kind": _s(_v(r, *_FLOW_KEYS["kind"]))[:40],
        "amount": _f(_v(r, *_FLOW_KEYS["amount"])),
        "balance": _f(_v(r, *_FLOW_KEYS["balance"])),
        "code": _s(_v(r, *_FLOW_KEYS["code"])),
        "name": _s(_v(r, *_FLOW_KEYS["name"])),
    }


# --------------------------------------------------------------------------
# 原始样本：第一次接真实账号时用来确认字段名
# --------------------------------------------------------------------------

_SAMPLES = {"trades": {}, "flows": {}, "orders": {}}


def _record_sample(tag: str, rows: list[dict]) -> None:
    """把最近一次响应的**原始**条目留一份，界面上能查「哪些字段认出来了」。"""
    if not rows:
        return
    keys = sorted({k for r in rows[:20] for k in r.keys()})
    _SAMPLES[tag] = {"keys": keys, "sample": rows[:2], "count": len(rows),
                     "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / ("%s.json" % tag)).write_text(
            json.dumps(_SAMPLES[tag], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def probe() -> dict:
    """字段探测结果：每个接口的原始字段清单 + 已识别 / 未识别。"""
    out = {}
    known = {"trades": set().union(*[set(v) for v in _FILL_KEYS.values()]),
             "orders": set().union(*[set(v) for v in _ORDER_KEYS.values()]),
             "flows": set().union(*[set(v) for v in _FLOW_KEYS.values()])}
    for tag in ("trades", "orders", "flows"):
        s = _SAMPLES.get(tag) or {}
        if not s and (RAW_DIR / ("%s.json" % tag)).exists():
            try:
                s = json.loads((RAW_DIR / ("%s.json" % tag)).read_text(encoding="utf-8"))
            except Exception:
                s = {}
        if not s:
            continue
        keys = s.get("keys") or []
        out[tag] = {"at": s.get("at"), "count": s.get("count"),
                    "keys": keys,
                    "known": [k for k in keys if k in known[tag]],
                    "unknown": [k for k in keys if k not in known[tag]],
                    "sample": s.get("sample")}
    return out


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------

def raw_snapshot() -> dict:
    """拉一次当前账户状态（不落盘）。"""
    blocks = _q("query_asset_and_position")
    if not blocks:
        raise EmtError("查询返回空数据，可能账户未开通东财证券交易权限，或登录态失效")

    ov, positions = None, []
    for d in blocks:
        cur = _parse_overview(d)
        if cur["total"] or cur["mv"] or cur["cash"]:
            ov = cur
        for p in (d.get("positions") or []):
            if isinstance(p, dict) and _i(p.get("Zqsl")) > 0:
                positions.append(_parse_pos(p))
    if ov is None:
        ov = _parse_overview({})

    try:
        orders = [_parse_order(o) for o in _q("query_orders")]
    except EmtError:
        orders = []
    try:
        raw_trades = _q("query_trades")
        today_fills = [_parse_fill(o) for o in raw_trades]
        _record_sample("trades", raw_trades)
    except EmtError:
        today_fills = []

    positions.sort(key=lambda x: -x["mv"])
    if positions:
        tot = sum(p["mv"] for p in positions) or 1.0
        for p in positions:
            p["mv_pct"] = round(p["mv"] / tot * 100, 2)

    # 当日成交按 (code, side, qty, price) 合并进 orders 形态，供当日复盘用
    fills_as_orders = [{
        "id": "fill|%s|%s|%s|%s" % (f["date"], f["code"], f["side"], f["qty"]),
        "date": f["date"], "time": f["time"], "code": f["code"], "name": f["name"],
        "side": f["side"], "qty": f["qty"], "price": f["price"], "filled": f["qty"],
        "amount": f["amount"], "avg_price": f["price"], "state": "已成",
        "raw_state": "成交", "fill_ratio": 100.0, "fee": f["fee"],
    } for f in today_fills if f["code"] and f["qty"] > 0]

    return {"at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "overview": ov, "positions": positions,
            "orders": _merge_orders(orders, fills_as_orders),
            "fills": today_fills}


def _merge_orders(old: list, new: list) -> list:
    by = {}
    for o in list(old or []) + list(new or []):
        k = o.get("id") or "%s|%s|%s|%s" % (o.get("date"), o.get("code"), o.get("side"), o.get("price"))
        prev = by.get(k)
        # 同一笔委托后抓到的状态更准（未成 → 部成 → 已成），保留成交量大的
        if prev is None or (o.get("filled") or 0) >= (prev.get("filled") or 0):
            by[k] = o
    rows = list(by.values())
    rows.sort(key=lambda x: (x.get("date") or "", x.get("time") or "", x.get("id") or ""))
    return rows


# --------------------------------------------------------------------------
# 账本：按交易日入库（SQLite，旧 JSON/CSV 首次访问自动迁移）
# --------------------------------------------------------------------------

_LEDGER_DB_READY = False


def _ensure_ledger_db():
    """把旧 data/ledger/*.json 与三份 CSV 一次性迁进库；键用本模块的去重函数算。

    迁移完成后旧文件不再读：`migrate_ledger` 里的 flag 判断在文件扫描之后，
    必须在扫描之前先查，否则每次进程首次访问账本都会白扫一遍旧 JSON/CSV。
    """
    global _LEDGER_DB_READY
    if _LEDGER_DB_READY:
        return
    import store
    if store.migration_done("ledger_migrated"):
        _LEDGER_DB_READY = True
        return
    days = []
    if LEDGER.exists():
        for p in sorted(LEDGER.glob("*.json")):
            if len(p.stem) != 10:
                continue
            try:
                rec = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if rec:
                days.append((p.stem, rec))
    kv = {}
    for path, fields, keyf, table in (
            (FILLS_CSV, _FILL_FIELDS, _fill_key, "fills"),
            (ORDERS_CSV, _ORDER_FIELDS, _order_key, "orders"),
            (FLOWS_CSV, _FLOW_FIELDS, _flow_key, "flows")):
        items = []
        if path.exists():
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as f:
                    for r in csv.DictReader(f):
                        r = dict(r)
                        k = keyf(r)
                        if k:
                            items.append((k, str(r.get("date") or ""),
                                          {fl: r.get(fl, "") for fl in fields}))
            except Exception:
                pass
        kv[table] = items
    store.migrate_ledger(days, kv)
    _LEDGER_DB_READY = True


def _read_day(day: str) -> dict:
    _ensure_ledger_db()
    import store
    return store.ledger_get(day)


def ledger_days() -> list[str]:
    _ensure_ledger_db()
    import store
    return store.ledger_days()


def load_all(limit: int = 800) -> list[dict]:
    _ensure_ledger_db()
    import store
    return store.ledger_all(limit)


def sync(note: str = "", wait: bool = True) -> dict:
    """同步一次并落盘。返回 {ok, day, at, overview, positions, orders, msg}

    wait=False 供盘中自动刷新使用：如果手动同步 / 收盘同步正在跑，就跳过这轮，
    而不是排队再写一次重复快照。
    """
    got = _SYNC_LOCK.acquire(timeout=30) if wait else _SYNC_LOCK.acquire(blocking=False)
    if not got:
        return {"ok": False, "busy": True, "error": "已有账户同步在进行，稍后再试"}
    try:
        return _sync_locked(note)
    finally:
        _SYNC_LOCK.release()


def _sync_locked(note: str = "") -> dict:
    at = dt.datetime.now()
    day = at.strftime("%Y-%m-%d")
    try:
        snap = raw_snapshot()
    except EmtError as e:
        _mark(False, str(e))
        return {"ok": False, "error": str(e)}
    except Exception as e:                                   # noqa: BLE001
        msg = _explain(str(e))
        _mark(False, msg)
        return {"ok": False, "error": msg}

    rec = _read_day(day)
    snaps = rec.get("snaps") or []
    snaps.append({"at": snap["at"], "total": snap["overview"]["total"],
                  "mv": snap["overview"]["mv"], "day_pnl": snap["overview"]["day_pnl"]})
    rec.update({
        "date": day,
        "at": snap["at"],
        "overview": snap["overview"],
        "positions": snap["positions"],
        "orders": _merge_orders(rec.get("orders"), snap["orders"]),
        "snaps": snaps[-20:],
    })
    if note:
        rec["note"] = str(note)[:500]
    import store
    store.ledger_put(day, rec)

    # 当日成交顺手并进 fills.csv，历史接口没拉到今天时也不缺
    added = _upsert(FILLS_CSV, _FILL_FIELDS, snap.get("fills") or [],
                    key=lambda r: _fill_key(r))

    _mark(True, "已同步 %s · 总资产 %s · 持仓 %d 只 · 当日成交 %d 笔"
          % (snap["at"], "%.2f" % snap["overview"]["total"],
             len(snap["positions"]), added))
    return {"ok": True, "day": day, "at": snap["at"], "overview": snap["overview"],
            "positions": snap["positions"], "orders": rec["orders"],
            "fills_added": added, "ledger_days": len(ledger_days())}


def _mark(ok: bool, msg: str) -> None:
    with _CFG_LOCK:
        c = load_cfg()
        c["last_sync"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c["last_ok"] = bool(ok)
        c["last_msg"] = str(msg)[:300]
        try:
            CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    # 成功不记：盘中每 5 分钟同步一次，全记会把日志刷满；失败必须留痕，
    # 「今天曲线缺一段」往往就是某次登录失败被静默吞了。
    if not ok:
        try:
            import store
            store.log("emt", "账户同步失败：%s" % str(msg)[:180], level="error",
                      act="sync", detail=str(msg)[:1500])
        except Exception:
            pass


def _mark_refresh(ok: bool, msg: str, at: str = "") -> None:
    """盘中刷新自己的状态，跟收盘日结的 last_* 分开记，互不覆盖。"""
    with _CFG_LOCK:
        c = load_cfg()
        c["refresh_at"] = at or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c["refresh_ok"] = bool(ok)
        c["refresh_msg"] = str(msg)[:240]
        try:
            CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass


# --------------------------------------------------------------------------
# CSV 存取（成交 / 流水 / 委托）
# --------------------------------------------------------------------------

_FILL_FIELDS = ["date", "time", "code", "name", "side", "qty", "price", "amount", "fee", "id"]
_FLOW_FIELDS = ["date", "kind", "amount", "balance", "code", "name"]
_ORDER_FIELDS = ["date", "time", "code", "name", "side", "qty", "price", "filled",
                 "amount", "state", "id"]


def _fill_key(r: dict) -> str:
    """成交去重键。有成交编号就用它；没有就用『日+时+码+向+量+价』。"""
    if r.get("id"):
        return "id:%s" % r["id"]
    return "%s|%s|%s|%s|%s|%s" % (r.get("date"), r.get("time"), r.get("code"),
                                  r.get("side"), round(float(r.get("qty") or 0), 4),
                                  round(float(r.get("price") or 0), 4))


def _order_key(r: dict) -> str:
    if r.get("id"):
        return "id:%s" % r["id"]
    return "%s|%s|%s|%s|%s" % (r.get("date"), r.get("code"), r.get("side"),
                               r.get("qty"), r.get("price"))


def _flow_key(r: dict) -> str:
    return "%s|%s|%s|%s|%s" % (r.get("date"), r.get("kind"), r.get("amount"),
                               r.get("balance"), r.get("code"))


_KV_OF = {str(FILLS_CSV): "fills", str(FLOWS_CSV): "flows", str(ORDERS_CSV): "orders"}


def _read_csv(path: Path, fields: list[str]) -> list[dict]:
    _ensure_ledger_db()
    import store
    return store.kv_rows(_KV_OF[str(path)])


def _upsert(path: Path, fields: list[str], rows: list[dict], key) -> int:
    """按 key 去重合并入库（INSERT OR IGNORE 幂等），返回新增条数。"""
    if not rows:
        return 0
    _ensure_ledger_db()
    import store
    items, seen = [], set()
    for r in rows:
        k = key(r)
        if not k or k in seen:
            continue
        seen.add(k)
        items.append((k, str(r.get("date") or ""), {f: r.get(f, "") for f in fields}))
    return store.kv_upsert(_KV_OF[str(path)], items)


def fills() -> list[dict]:
    out = []
    for r in _read_csv(FILLS_CSV, _FILL_FIELDS):
        try:
            out.append({"date": r.get("date") or "", "time": r.get("time") or "",
                        "code": _s(r.get("code")).zfill(6), "name": r.get("name") or "",
                        "side": _side(r.get("side")), "qty": _f(r.get("qty"), 0),
                        "price": _f(r.get("price"), 4), "amount": _f(r.get("amount")),
                        "fee": _f(r.get("fee")), "id": r.get("id") or ""})
        except Exception:
            continue
    return out


def flows() -> list[dict]:
    out = []
    for r in _read_csv(FLOWS_CSV, _FLOW_FIELDS):
        out.append({"date": r.get("date") or "", "kind": r.get("kind") or "",
                    "amount": _f(r.get("amount")), "balance": _f(r.get("balance")),
                    "code": _s(r.get("code")), "name": r.get("name") or ""})
    return out


def hist_orders() -> list[dict]:
    out = []
    for r in _read_csv(ORDERS_CSV, _ORDER_FIELDS):
        qty = _f(r.get("qty"), 0)
        filled = _f(r.get("filled"), 0)
        amount = _f(r.get("amount"))
        price = _f(r.get("price"), 3)
        out.append({
            "date": r.get("date") or "", "time": r.get("time") or "",
            "code": _s(r.get("code")).zfill(6), "name": r.get("name") or "",
            "side": _side(r.get("side")), "qty": qty, "filled": filled,
            "price": price, "amount": amount,
            "avg_price": round(amount / filled, 3) if filled > 0 and amount > 0 else price,
            "state": r.get("state") or "", "id": r.get("id") or "",
        })
    return out


# --------------------------------------------------------------------------
# 历史回流
# --------------------------------------------------------------------------

def _month_spans(start: str, end: str) -> list[tuple[str, str]]:
    """把 [start, end] 按自然月切片，东财历史查询按区间返回，切片能避免一次被截断。"""
    try:
        a = dt.date.fromisoformat(start)
        b = dt.date.fromisoformat(end)
    except Exception:
        return [(start, end)]
    if a > b:
        a, b = b, a
    out = []
    cur = dt.date(a.year, a.month, 1)
    while cur <= b:
        nxt = dt.date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        s = max(cur, a)
        e = min(nxt - dt.timedelta(days=1), b)
        if s <= e:
            out.append((s.isoformat(), e.isoformat()))
        cur = nxt
    return out


def pull_history(start: str = "", end: str = "", size: int = 500,
                 what: tuple[str, ...] = ("trades", "orders", "flows")) -> dict:
    """
    拉历史成交 / 历史订单 / 资金流水并落 CSV。

    为什么按月切片：单次查询有条数上限，一次要一年的数据必然被截断，
    而截断是**静默**的（返回条数刚好等于上限，看不出少了）。按月切片 + 逐月核对
    条数，比一次拉完更可验证。
    """
    end = end or dt.date.today().isoformat()
    if not start:
        cfg = load_cfg()
        start = cfg.get("hist_from") or (dt.date.today() - dt.timedelta(days=365)).isoformat()
    start = _day(start) or (dt.date.today() - dt.timedelta(days=365)).isoformat()
    size = max(50, min(2000, int(size or 500)))

    spans = _month_spans(start, end)
    res = {"ok": True, "from": start, "to": end, "months": len(spans),
           "trades": 0, "orders": 0, "flows": 0, "truncated": [], "errors": []}

    for s, e in spans:
        for tag in what:
            fn = {"trades": "query_history_trades",
                  "orders": "query_history_orders",
                  "flows": "query_funds_flow"}[tag]
            try:
                rows = _q(fn, size, s, e)
            except EmtError as err:
                res["errors"].append("%s %s~%s：%s" % (tag, s, e, str(err)[:80]))
                continue
            if not rows:
                continue
            if len(rows) >= size:
                res["truncated"].append("%s %s~%s 返回 %d 条（已达单次上限）"
                                        % (tag, s, e, len(rows)))
            if tag == "trades":
                _record_sample("trades", rows)
                parsed = [_parse_fill(r) for r in rows]
                parsed = [p for p in parsed if p["code"] and p["qty"] > 0 and p["price"] > 0]
                res["trades"] += _upsert(FILLS_CSV, _FILL_FIELDS, parsed, key=_fill_key)
            elif tag == "orders":
                _record_sample("orders", rows)
                parsed = [_parse_order(r) for r in rows]
                parsed = [p for p in parsed if p["code"]]
                res["orders"] += _upsert(ORDERS_CSV, _ORDER_FIELDS, parsed, key=_order_key)
            else:
                _record_sample("flows", rows)
                parsed = [_parse_flow(r) for r in rows]
                parsed = [p for p in parsed if p["date"]]
                res["flows"] += _upsert(FLOWS_CSV, _FLOW_FIELDS, parsed, key=_flow_key)

    with _CFG_LOCK:
        cfg = load_cfg()
        cfg["hist_from"] = start
        cfg["hist_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 零新增时保留上一次的文案，否则面板会被「+0 · +0 · +0」覆盖掉有效信息
        if res["trades"] or res["orders"] or res["flows"]:
            cfg["hist_msg"] = "成交 +%d · 委托 +%d · 流水 +%d" % (res["trades"], res["orders"], res["flows"])
        try:
            CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return res


def hist_state() -> dict:
    c = load_cfg()
    fl = fills()
    flows_ = flows()
    days = sorted({f["date"] for f in fl if f["date"]})
    return {
        "from": c.get("hist_from") or "",
        "at": c.get("hist_at") or "",
        "msg": c.get("hist_msg") or "",
        "fills": len(fl),
        "flows": len(flows_),
        "orders": len(hist_orders()),
        "first_day": days[0] if days else "",
        "last_day": days[-1] if days else "",
        "fill_days": len(days),
    }


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------

def _eod_state(c: dict, now: dt.datetime) -> dict:
    """今天该日结、但账本里还没有这一天。

    实盘这条链路最容易「静默过期」：电脑晚上没开，15:35 的自动日结就不发生，
    账本少一天，页面上只是曲线短一截，没有任何提示。而资产快照只能取当下，
    漏掉的日子补不回来 —— 所以提示的落点只能是「现在补上今天」，不是历史缺口。
    只在「已配置 + 交易日 + 过了日结时刻」判断，盘中不喊。
    """
    if not (c.get("user") and c.get("pass_enc")):
        return {}
    day = now.strftime("%Y-%m-%d")
    hm = str(c.get("auto_time") or "15:35").replace(":", "")
    if not (len(hm) == 4 and now.strftime("%H%M") >= hm):
        return {}
    try:
        import pipeline
        if pipeline._is_trading_day(day) is False:
            return {}
    except Exception:
        if now.weekday() >= 5:
            return {}
    try:
        days = ledger_days()
    except Exception:
        return {}
    last = max(days) if days else ""
    if day in days:
        return {"pending": False, "last": last}
    return {"pending": True, "day": day,
            "last": last,
            "note": "今天还没日结同步" + ("（账本最新 %s）" % last if last else "")}


def status() -> dict:
    c = load_cfg()
    import time
    now = dt.datetime.now()
    a = _CLI.get("obj")
    alive = a is not None and (time.time() - _CLI["at"]) < _SESSION_KEEP
    due, why = _refresh_due(c, now)
    return {
        "configured": bool(c.get("user") and c.get("pass_enc")),
        "user": _mask(c.get("user") or ""),
        "enabled": bool(c.get("enabled")),
        "auto_sync": bool(c.get("auto_sync")),
        "auto_time": c.get("auto_time") or "15:35",
        "auto_refresh": bool(c.get("auto_refresh", True)),
        "refresh_min": int(c.get("refresh_min") or 5),
        "refresh_at": c.get("refresh_at") or "",
        "refresh_ok": c.get("refresh_ok"),
        "refresh_msg": c.get("refresh_msg") or "",
        # 状态按「当前时间 + 配置」现算，比上一轮循环留下的 note 更准：
        # 关掉开关立刻显示已关闭，刚刷新完则显示下次大约几分钟后。
        "refresh_state": ("正在刷新…" if _REFRESH.get("running")
                          else (why or "等待刷新")),
        "refresh_due": bool(due),
        "in_session": _in_session(now),
        "logged_in": alive,
        "last_sync": c.get("last_sync") or "",
        "last_ok": c.get("last_ok"),
        "last_msg": c.get("last_msg") or "",
        "ledger_days": len(ledger_days()),
        "eod": _eod_state(c, now),
        "lib": _lib_ver(),
        "history": hist_state(),
    }


def _lib_ver() -> str:
    try:
        from importlib import metadata
        return "emtl %s" % metadata.version("emtl")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 复盘日记（按交易日）
# --------------------------------------------------------------------------

JOURNAL_SRC = ("manual", "ai", "local")


def journal_set(day: str, text: str, src: str = "manual", model: str = "") -> dict:
    """写入当日日记。落盘为 data/journal/YYYY-MM-DD.md（唯一权威），
    账本里若已有这一天再镜像一份，供看板与体检沿用旧字段。"""
    r = journal_md.save(day, text, src=src, model=model)
    if not r.get("ok"):
        return r
    day = r["day"]
    journal_mirror(day)
    return {"ok": True, "day": day, "journal": r.get("text") or "",
            "journal_src": r.get("src") or "manual", "journal_at": r.get("updated_at") or ""}


def journal_mirror(day: str) -> None:
    """把 .md 内容镜像进已有账本日，供看板等旧读取路径使用。

    没有账本快照的日子只留 .md —— 写日记不应该凭空造出一个账本日。
    """
    day = str(day or "")[:10]
    r = journal_md.read(day)
    if not (r.get("ok") and r.get("exists")):
        return
    rec = _read_day(day)
    if not rec:
        return
    import store
    rec["journal"] = str(r.get("text") or "")[:20000]
    rec["journal_at"] = r.get("updated_at") or ""
    rec["journal_src"] = r.get("src") or "manual"
    rec["journal_model"] = r.get("model") or ""
    store.ledger_put(day, rec)


def journal_clear(day: str) -> None:
    """删除日记时同步清掉账本里的镜像字段，避免旧内容被看板或迁移重新带回来。"""
    day = str(day or "")[:10]
    rec = _read_day(day)
    if not rec:
        return
    for k in ("journal", "journal_at", "journal_src", "journal_model"):
        rec.pop(k, None)
    import store
    store.ledger_put(day, rec)


def journal_get(day: str) -> str:
    r = journal_md.read(day)
    if r.get("ok") and r.get("exists"):
        return str(r.get("text") or "")
    # 升级前的旧日记还只存在账本字段里，读时回退，第一次保存后就成了 .md
    return str((_read_day(str(day or "")[:10]) or {}).get("journal") or "")


def day_merge(day: str, fields: dict) -> dict:
    """把若干字段并进某日账本，保留其余内容。用于体检结果等附属数据。"""
    day = str(day or "")[:10]
    if len(day) != 10:
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
    rec = _read_day(day) or {"date": day, "overview": {}, "positions": [], "orders": []}
    rec.update(fields or {})
    import store
    store.ledger_put(day, rec)
    return {"ok": True, "day": day}


def journal_meta(day: str) -> dict:
    """当日日记与 AI 体检的元信息，供 emtai 判断是否需要生成。

    日记以 .md 为准；没有 .md 时才回退到升级前的账本字段。
    """
    day = str(day or "")[:10]
    rec = _read_day(day) or {}
    r = journal_md.read(day)
    if r.get("ok") and r.get("exists"):
        text = str(r.get("text") or "")
        return {"day": day, "journal": text,
                "journal_at": str(r.get("updated_at") or ""),
                "journal_src": str(r.get("src") or ("manual" if text else "")),
                "journal_model": str(r.get("model") or ""),
                "health_ai": rec.get("health_ai") or None}
    text = str(rec.get("journal") or "")
    return {"day": day, "journal": text,
            "journal_at": str(rec.get("journal_at") or ""),
            "journal_src": str(rec.get("journal_src") or ("manual" if text else "")),
            "journal_model": str(rec.get("journal_model") or ""),
            "health_ai": rec.get("health_ai") or None}


# --------------------------------------------------------------------------
# 自动刷新与收盘日结（进程内 daemon）
# --------------------------------------------------------------------------
#
# 两个定时器，职责分开：
#   盘中刷新：交易时段每 N 分钟一次，让「实盘复盘」看到的是当前资金/持仓，不用等收盘。
#   收盘日结：15:35 再同步一次并补当天成交，给账本留下正式的日快照。
#
# 为什么单独一个定时器，不并进 scheduler.py 的数据链：
#   数据链 (18:10) 是「等 baostock 入库」，账户快照 (15:35) 是「等收盘价定盘」，
#   时间点、失败代价、前置条件都不一样。混在一个配置里，改一边必然影响另一边。
#
# 风控取舍：emtl 不内置限流，东财网关也没有公开的频率文档。这里默认 5 分钟、
# 复用同一个登录会话，连续失败按 15/45/60 分钟退避；宁可少几次刷新，也不让
# 「高频查询」或「反复登录」把账号玩坏。错过的成交以后用「拉取历史」补。

_AUTO = {"started": False, "last_try": "", "note": ""}
_REFRESH = {"started": False, "next_try": 0.0, "fails": 0, "note": "", "running": False}


def _in_session(now: dt.datetime | None = None) -> bool:
    """A 股连续竞价时段：09:25~11:30、13:00~15:02。09:25 含集合竞价快照。"""
    now = now or dt.datetime.now()
    if now.isoweekday() > 5:
        return False
    m = now.hour * 60 + now.minute
    return (9 * 60 + 25 <= m <= 11 * 60 + 30) or (13 * 60 <= m <= 15 * 60 + 2)


def _refresh_due(cfg: dict, now: dt.datetime) -> tuple[bool, str]:
    if not cfg.get("auto_refresh", True):
        return False, "盘中自动刷新未开启"
    if not (cfg.get("user") and cfg.get("pass_enc")):
        return False, "未配置账号"
    if now.isoweekday() > 5:
        return False, "周末不刷新"
    if not _in_session(now):
        return False, "非交易时段，暂不刷新"
    import time
    wait = float(_REFRESH.get("next_try") or 0) - time.time()
    if wait > 0:
        return False, "失败退避中，约 %d 分钟后重试" % (int(wait / 60) + 1)
    try:
        mins = max(1, min(60, int(cfg.get("refresh_min") or 5)))
    except Exception:
        mins = 5
    # 自动刷新与手动同步都算“刚取过数”：取两者中较晚的时间，避免手动同步后
    # 紧跟着又自动打一遍网关。
    stamps = []
    for s in (cfg.get("refresh_at"), cfg.get("last_sync")):
        try:
            stamps.append(dt.datetime.strptime(str(s or "")[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    t = max(stamps) if stamps else None
    if t is not None:
        left = mins * 60 - (now - t).total_seconds()
        if left > 0:
            return False, "约 %d 分钟后刷新" % (int(left / 60) + 1)
    return True, ""



def _auto_due(cfg: dict, now: dt.datetime) -> tuple[bool, str]:
    if not cfg.get("auto_sync"):
        return False, "未开启自动同步"
    if not (cfg.get("user") and cfg.get("pass_enc")):
        return False, "未配置账号"
    if now.isoweekday() > 5:
        return False, "周末不取数"
    # 只看星期不够：国庆/春节这类工作日休市日会空跑一次日结，生成一份空复盘
    # 还把 eod_try 标记成当天。交易日历不可用（返回 None）时不拦，宁可多跑一次。
    try:
        import pipeline
        if pipeline._is_trading_day(now.strftime("%Y-%m-%d")) is False:
            return False, "今天不是交易日"
    except Exception:
        pass
    hm = str(cfg.get("auto_time") or "15:35")
    try:
        h, m = [int(x) for x in hm.split(":")[:2]]
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    except Exception:
        return False, "时间格式不对"
    if now < t:
        return False, "未到 %s" % hm
    if (now - t).total_seconds() > 4 * 3600:
        return False, "已过 4 小时补跑窗口"
    # 用 eod_try 而不是 last_sync：盘中刷新会不断更新 last_sync，拿它判断会漏掉
    # 收盘日结；同时 eod_try 在发起前就写入，失败当天也不再重试，避免反复登录锁号。
    if str(cfg.get("eod_try") or "")[:10] == now.strftime("%Y-%m-%d"):
        return False, "今天已日结过（含失败，不重试）"
    return True, ""


def _refresh_loop():
    """交易时段内低频刷新账户快照。失败不立即重试，避免触发登录风控。"""
    import time
    time.sleep(20)                       # 等服务启动完成，也让手动操作先走
    while True:
        try:
            now = dt.datetime.now()
            cfg = load_cfg()
            due, why = _refresh_due(cfg, now)
            if due:
                _REFRESH["running"] = True
                try:
                    r = sync(note="盘中自动刷新", wait=False)
                except Exception as err:                 # noqa: BLE001
                    r = {"ok": False, "error": str(err)[:120]}
                finally:
                    _REFRESH["running"] = False
                if r.get("busy"):
                    _REFRESH["note"] = "有同步任务在进行，本轮跳过"
                elif r.get("ok"):
                    _REFRESH.update(fails=0, next_try=0.0)
                    msg = "盘中已刷新 · %s" % (r.get("at") or "")
                    _REFRESH["note"] = msg
                    _mark_refresh(True, msg, r.get("at") or "")
                else:
                    fails = int(_REFRESH.get("fails") or 0) + 1
                    base = max(5, int(cfg.get("refresh_min") or 5))
                    delay = min(60, base * (3 ** min(fails, 3)))
                    _REFRESH.update(fails=fails, next_try=time.time() + delay * 60)
                    msg = "刷新失败：%s，%d 分钟后再试" % (str(r.get("error") or "未知原因")[:120], delay)
                    _REFRESH["note"] = msg
                    _mark_refresh(False, msg)
            elif why:
                _REFRESH["note"] = why
        except Exception:
            pass
        time.sleep(20)


def _auto_loop():
    import time
    time.sleep(45)                       # 避开服务启动时的请求高峰
    while True:
        try:
            now = dt.datetime.now()
            stamp = now.strftime("%Y-%m-%d %H:%M")
            if _AUTO["last_try"] != stamp:      # 同一分钟只判断一次
                cfg = load_cfg()
                due, why = _auto_due(cfg, now)
                if due:
                    _AUTO.update(last_try=stamp, note="自动同步中…")
                    save_cfg(eod_try=now.strftime("%Y-%m-%d"))   # 发起即记，失败也不当天重试
                    threading.Thread(target=_auto_run, name="emt-auto", daemon=True).start()
                else:
                    _AUTO["note"] = why
        except Exception:
            pass
        time.sleep(60)


def _auto_run():
    # 收盘日结比盘中刷新重要：宁可等手动同步几十秒，也不因为锁被占用漏掉当天快照
    r = sync(note="自动同步")
    if r.get("busy"):
        # 锁被手动同步占着：这不算失败，清掉今天的“已尝试”标记，下一轮再补
        save_cfg(eod_try="")
        _AUTO["note"] = "收盘同步遇到进行中的任务，稍后重试"
        return
    if not r.get("ok"):
        _AUTO["note"] = "自动同步失败：" + str(r.get("error") or "")[:120]
        return
    # 日结标记与盘中刷新分开记；只有这里成功才算「今天已日结」
    save_cfg(last_eod=r.get("day") or dt.date.today().isoformat())
    # 快照成功后顺带补当天成交（历史接口按天查最省事，也不会重复）
    try:
        pull_history(r.get("day") or "", r.get("day") or "", size=200,
                     what=("trades", "orders"))
    except Exception:
        pass
    # 日结完成后按用户开关生成复盘（AI 或本地模板）；失败不影响账本
    try:
        import emtai
        ar = emtai.auto_after_eod(r.get("day") or "")
        _AUTO["note"] = "自动同步成功 · %s · 复盘%s" % (
            r.get("at") or "",
            "已更新" if ar.get("ok") and ar.get("result") else "未生成（按设置跳过）")
    except Exception:
        _AUTO["note"] = "自动同步成功 · %s" % (r.get("at") or "")


def start_auto() -> bool:
    if _AUTO["started"]:
        return False
    _AUTO["started"] = True
    threading.Thread(target=_auto_loop, name="emt-auto-tick", daemon=True).start()
    if not _REFRESH["started"]:
        _REFRESH["started"] = True
        threading.Thread(target=_refresh_loop, name="emt-refresh-tick", daemon=True).start()
    return True


def auto_state() -> dict:
    cfg = load_cfg()
    due, why = _auto_due(cfg, dt.datetime.now())
    return {"auto_sync": bool(cfg.get("auto_sync")), "auto_time": cfg.get("auto_time") or "15:35",
            "auto_due": bool(due), "auto_state": _AUTO["note"] or why}
