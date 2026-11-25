# -*- coding: utf-8 -*-
"""
QuantDesk 股票池模块（与 config.json 解耦，完全由用户自定义）

设计要点：
  - 股票池独立存于 data/watchlist.json，config.json 的 stocks 只在首次启动时作为种子
  - 名称不依赖配置文件：通过公开行情接口自动解析，结果缓存在 data/names.json
  - 导入兼容东财 / 同花顺 / 通达信导出的 TXT、CSV，以及带 sh/sz 前缀的代码清单
  - 账户同步走东方财富「妙想 Skills」官方 API Key 授权，只读取自选股；
    不保存账号密码、不模拟网页登录、不调用任何写接口
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

import qdpaths

import requests

import store

BASE = Path(__file__).resolve().parent
DATA = qdpaths.data_path()
DATA.mkdir(parents=True, exist_ok=True)
WATCH_PATH = DATA / "watchlist.json"
NAMES_PATH = DATA / "names.json"
ACCOUNT_PATH = DATA / "account.json"
SYNC_RAW_PATH = DATA / "account_last_response.json"
CONFIG_PATH = BASE / "config.json"

SELF_SELECT_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/self-select/get"
NAME_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://gu.qq.com/"}

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)", re.I)
_SUFFIX_RE = re.compile(r"\.(SH|SZ|BJ)$", re.I)
_NAME_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9*]{1,11}")
_HEADER_WORDS = ("代码", "名称", "证券", "股票", "CODE", "NAME", "SECURITY", "最新价", "涨跌幅")


def _clean_name(s) -> str:
    """行情源常见「五 粮 液」「万 科 A」这类对齐空格，统一去掉。"""
    return re.sub(r"\s+", "", str(s or "").strip())


# --------------------------------------------------------------------------
# 基础读写
# --------------------------------------------------------------------------

def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_items() -> list[dict]:
    c = _read_json(CONFIG_PATH, {}) or {}
    out = []
    for s in c.get("stocks", []):
        code = norm_code(s.get("code"))
        if code:
            out.append({"code": code, "name": str(s.get("name") or code),
                        "source": "初始示例", "added": dt.date.today().isoformat()})
    return out


def _state() -> dict:
    st = _read_json(WATCH_PATH, None)
    if not isinstance(st, dict) or not isinstance(st.get("items"), list):
        items = _seed_items()
        st = {"version": 1, "items": items,
              "default": items[0]["code"] if items else "",
              "updated": dt.date.today().isoformat()}
        _write_json(WATCH_PATH, st)
    return st


def _save(st: dict):
    st["updated"] = dt.date.today().isoformat()
    _write_json(WATCH_PATH, st)
    return st


_A_PREFIXES = ("600", "601", "603", "605", "688", "689",
               "000", "001", "002", "003", "300", "301",
               "43", "83", "87", "92")


def is_a_share(code) -> bool:
    """严格的 A 股代码段校验，避免把接口里的元数据字段（如 010000 CIRCULATION）当成股票。"""
    c = str(code or "").zfill(6)
    return len(c) == 6 and c.isdigit() and c.startswith(_A_PREFIXES)


def norm_code(x) -> str | None:
    """把 'sh600519' / '600519.SH' / '600519 贵州茅台' 统一成 6 位 A 股代码。"""
    if x is None:
        return None
    s = _PREFIX_RE.sub("", str(x).strip())
    s = _SUFFIX_RE.sub("", s).strip()
    m = _CODE_RE.search(s)
    if not m:
        return None
    c = m.group(1)
    return c if is_a_share(c) else None


# --------------------------------------------------------------------------
# 名称解析（公开行情接口，无需登录）
# --------------------------------------------------------------------------

def _names() -> dict:
    return _read_json(NAMES_PATH, {}) or {}


def stock_name(code: str) -> str:
    """供 quant.stock_name 调用：股票池 → 本地名称缓存 → 代码本身。"""
    code = str(code).zfill(6)
    st = _state()
    for it in st["items"]:
        if it.get("code") == code and it.get("name"):
            return _clean_name(it["name"])
    return (_clean_name(_names().get(code)) or code)


def _tencent_key(code: str) -> str:
    if code.startswith(("43", "83", "87", "92")):
        return "bj" + code
    return ("sh" if code[0] in "56" else "sz") + code


def resolve_names(codes: list[str], force: bool = False) -> dict:
    """批量查名称：腾讯行情为主，东财 push2 兜底；结果落盘缓存。"""
    cache = _names()
    todo = [c for c in codes if force or not cache.get(c)]
    if not todo:
        return {c: (_clean_name(cache.get(c)) or c) for c in codes}

    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        keys = [_tencent_key(c) for c in batch]
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(keys),
                             timeout=12, headers=NAME_UA)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                m = re.search(r'v_(\w+)="([^"]*)"', line)
                if not m:
                    continue
                parts = m.group(2).split("~")
                if len(parts) > 3 and parts[2].isdigit() and len(parts[2]) == 6:
                    cache[parts[2]] = _clean_name(parts[1])
        except Exception:
            pass

    # 腾讯没解析出来的（含北交所）走东财单点，限速避免触发限流
    for c in todo:
        if cache.get(c):
            continue
        try:
            u = ("https://push2.eastmoney.com/api/qt/stock/get?secid="
                 + ("1." if c[0] in "569" else "0.") + c + "&fields=f57,f58")
            j = requests.get(u, timeout=8, headers={"User-Agent": "Mozilla/5.0"}).json()
            n = _clean_name((j.get("data") or {}).get("f58"))
            if n:
                cache[c] = n
        except Exception:
            pass

    _write_json(NAMES_PATH, cache)
    return {c: (_clean_name(cache.get(c)) or c) for c in codes}


def _bar_meta() -> dict:
    """行情缓存以 SQLite 的 bar_meta 为准。

    旧 data/cache/*.csv 只是首次迁移来源，迁移完成后已清理；
    再按文件判断会把库里有完整日线的股票全显示成「未缓存」。
    """
    try:
        return store.meta_map()
    except Exception:
        return {}


def _has_bars(code: str) -> bool:
    try:
        return store.has_code(code)
    except Exception:
        return False


def lookup(code: str) -> dict:
    """单个代码校验 + 查名称，供前端添加时即时回显。"""
    c = norm_code(code)
    if not c:
        return {"ok": False, "error": "无法识别股票代码，请输入 6 位 A 股代码"}
    n = resolve_names([c]).get(c, c)
    return {"ok": True, "code": c, "name": n,
            "valid": bool(n and n != c), "cached": _has_bars(c)}


# --------------------------------------------------------------------------
# 导入解析
# --------------------------------------------------------------------------

def _extract_name(line: str) -> str:
    """从一行文本里取股票简称：剥离市场前后缀、代码、数值列，中文优先。"""
    s = re.sub(r"\.(SH|SZ|BJ)\b", " ", line, flags=re.I)
    s = _CODE_RE.sub(" ", s)
    s = re.sub(r"\b(SH|SZ|BJ)\b", " ", s, flags=re.I)
    s = re.sub(r"[-+]?\d+(\.\d+)?%?", " ", s)
    m = re.search(r"[\u4e00-\u9fa5][\u4e00-\u9fa5A-Za-z0-9*]{1,11}", s)
    if not m:
        m = _NAME_RE.search(s)
    nm = m.group(0).strip() if m else ""
    if len(nm) < 2 or any(w in nm.upper() for w in _HEADER_WORDS):
        return ""
    return _clean_name(nm)


def parse_import(text: str) -> list[dict]:
    """
    兼容：一行一个代码、逗号/分号/空格/Tab 分隔、CSV 带表头、代码+名称、
    sh/sz/bj 前缀、.SH/.SZ 后缀、东财/同花顺/通达信导出的表格文本。
    纯代码清单取全部；带名称/数值列的表格行只取第一个代码，避免把价格误当代码。
    """
    if not text or not text.strip():
        return []
    out, seen = [], set()

    def push(c, nm):
        if c and c not in seen:
            seen.add(c)
            out.append({"code": c, "name": nm or ""})

    for raw in str(text).replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if not _CODE_RE.search(line):
            continue  # 表头、说明行、空行
        tokens = [t for t in re.split(r"[,;\s|\t]+", line) if t]
        normed = [norm_code(t) for t in tokens]
        if normed and all(n is not None for n in normed):
            # 纯代码清单：一行可能包含多只
            for c in normed:
                push(c, "")
        else:
            # 表格行 / "代码 名称"：只认第一个 6 位代码，其余列交给名称提取
            push(norm_code(line), _extract_name(line))
    return out


def import_items(text: str, mode: str = "merge") -> dict:
    """mode: merge 合并（保留已有）/ replace 替换整个股票池。"""
    parsed = parse_import(text)
    if not parsed:
        return {"ok": False, "error": "没有解析出有效的 6 位 A 股代码，请检查粘贴内容"}
    names = resolve_names([x["code"] for x in parsed])
    st = _state()
    old = {} if mode == "replace" else {i["code"]: i for i in st["items"]}
    added = 0
    for x in parsed:
        c = x["code"]
        if c in old:
            if x["name"] and not old[c].get("name"):
                old[c]["name"] = x["name"]
            continue
        old[c] = {"code": c, "name": x["name"] or names.get(c, c),
                  "source": "手动导入", "added": dt.date.today().isoformat()}
        added += 1
    st["items"] = list(old.values())
    if not any(i["code"] == st.get("default") for i in st["items"]):
        st["default"] = st["items"][0]["code"] if st["items"] else ""
    _save(st)
    return {"ok": True, "parsed": len(parsed), "added": added,
            "total": len(st["items"]), "mode": mode}


# --------------------------------------------------------------------------
# 股票池管理与视图
# --------------------------------------------------------------------------

def list_view() -> dict:
    st = _state()
    meta = _bar_meta()  # 一次取全表，22 只和 5000 只都是一条 SQL
    items = []
    for i in st["items"]:
        c = i.get("code", "")
        m = meta.get(c) or {}
        try:
            rows = int(m.get("rows") or 0)
        except (TypeError, ValueError):
            rows = 0
        items.append({"code": c, "name": _clean_name(i.get("name")) or c,
                      "source": i.get("source", "-"), "added": i.get("added", "-"),
                      "cached": bool(c and rows > 0),
                      "bar_date": str(m.get("last_date") or ""), "bars": rows})
    return {"ok": True, "items": items, "count": len(items),
            "default": st.get("default", ""), "updated": st.get("updated", "-")}


def codes() -> list[str]:
    return [i["code"] for i in _state()["items"]]


def mutate(action: str, **kw) -> dict:
    st = _state()
    items = st["items"]
    extra: dict = {}
    raw_code = str(kw.get("code") or "").zfill(6)
    c = norm_code(raw_code)
    # 删除时放宽：允许清掉历史误入的非 A 股代码（严格校验是后加的，不能挡住清理）
    if action == "remove" and c is None and len(raw_code) == 6 and raw_code.isdigit():
        c = raw_code
    if action == "add":
        if not c:
            return {"ok": False, "error": "无效代码"}
        if any(i["code"] == c for i in items):
            return {"ok": False, "error": "该股票已在池中"}
        nm = (kw.get("name") or "").strip() or resolve_names([c]).get(c, c)
        items.append({"code": c, "name": nm, "source": "手动添加",
                      "added": dt.date.today().isoformat()})
        if not items[:-1]:
            st["default"] = c
    elif action == "remove":
        if not c:
            return {"ok": False, "error": "无效代码"}
        items = [i for i in items if i["code"] != c]
        if st.get("default") == c:
            st["default"] = items[0]["code"] if items else ""
    elif action == "rename":
        if not c:
            return {"ok": False, "error": "无效代码"}
        for i in items:
            if i["code"] == c:
                i["name"] = str(kw.get("name") or c).strip() or c
    elif action == "default":
        if not c or not any(i["code"] == c for i in items):
            return {"ok": False, "error": "该代码不在股票池中"}
        st["default"] = c
    elif action == "move":
        if not c:
            return {"ok": False, "error": "无效代码"}
        idx = next((k for k, i in enumerate(items) if i["code"] == c), -1)
        j = idx + (1 if kw.get("dir") == "down" else -1)
        if idx < 0 or j < 0 or j >= len(items):
            return {"ok": False, "error": "已到边界"}
        items[idx], items[j] = items[j], items[idx]
    elif action == "remove_source":
        # 按来源批量清除（默认清掉部署时预置的「初始示例」），不动行情缓存
        src = str(kw.get("source") or "初始示例")
        kept = [i for i in items if i.get("source") != src]
        extra["removed"] = len(items) - len(kept)
        items = kept
        if not any(i["code"] == st.get("default") for i in items):
            st["default"] = items[0]["code"] if items else ""
    elif action == "clear":
        items = []
        st["default"] = ""
    elif action == "replace":
        raw = kw.get("items") or []
        seen, new = set(), []
        for x in raw:
            cc = norm_code(x.get("code"))
            if cc and cc not in seen:
                seen.add(cc)
                new.append({"code": cc, "name": str(x.get("name") or cc),
                            "source": str(x.get("source") or "批量导入"),
                            "added": dt.date.today().isoformat()})
        items = new
        st["default"] = new[0]["code"] if new else ""
    elif action == "import":
        # 「只加勾选 / 加入全部」走这里：text 每行「代码 名称」，mode=merge 追加去重、
        # mode=replace 先清空。以前后端白名单没有 import，这两个按钮必然报「未知操作」。
        mode = str(kw.get("mode") or "merge").lower()
        text = str(kw.get("text") or "")
        if mode == "replace":
            items = []
        seen = {i["code"] for i in items}
        parsed = []
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split()
            cc = norm_code(parts[0])
            if not cc or cc in seen:
                continue
            seen.add(cc)
            parsed.append((cc, " ".join(parts[1:]).strip()))
        names = resolve_names([c for c, _ in parsed]) if parsed else {}
        for cc, nm in parsed:
            items.append({"code": cc, "name": nm or names.get(cc, cc),
                          "source": "批量导入",
                          "added": dt.date.today().isoformat()})
        extra["added"] = len(parsed)
        if not any(i["code"] == st.get("default") for i in items):
            st["default"] = items[0]["code"] if items else ""
    else:
        return {"ok": False, "error": "未知操作 " + str(action)}
    st["items"] = items
    _save(st)
    return {**list_view(), "ok": True, "total": len(items), **extra}


def export_text(fmt: str = "txt") -> str:
    st = _state()
    if fmt == "json":
        return json.dumps(st, ensure_ascii=False, indent=2)
    return "\n".join(i["code"] + " " + _clean_name(i.get("name")) for i in st["items"]) + "\n"


# --------------------------------------------------------------------------
# 东方财富官方账户同步（只读，API Key 授权）
# --------------------------------------------------------------------------

def account_status() -> dict:
    key = get_apikey()
    return {"ok": True, "configured": bool(key),
            "masked": (key[:8] + "…" + key[-4:]) if key else "",
            "from_env": bool(os.environ.get("MX_APIKEY")),
            "endpoint": SELF_SELECT_URL}


def get_apikey() -> str:
    f = _read_json(ACCOUNT_PATH, {}) or {}
    return str(f.get("apikey") or os.environ.get("MX_APIKEY") or "").strip()


def set_apikey(key: str) -> dict:
    key = str(key or "").strip()
    if not key:
        if ACCOUNT_PATH.exists():
            ACCOUNT_PATH.unlink()
        return {"ok": True, "configured": False, "message": "已清除本地 API Key"}
    _write_json(ACCOUNT_PATH, {"apikey": key, "saved": dt.date.today().isoformat()})
    return {"ok": True, "configured": True, "message": "API Key 已保存到本机 data/account.json"}


def _walk_codes(node, found: dict):
    """容错递归：官方返回的嵌套结构可能随版本变化，提取任意层级里的股票代码与名称。"""
    if isinstance(node, dict):
        raw = (node.get("SECURITY_CODE") or node.get("securityCode")
               or node.get("code") or node.get("股票代码") or node.get("证券代码"))
        c = norm_code(raw)
        if c:
            nm = (node.get("SECURITY_SHORT_NAME") or node.get("securityShortName")
                  or node.get("name") or node.get("股票名称") or node.get("证券简称") or "")
            found[c] = str(nm).strip() or found.get(c, "")
        for v in node.values():
            _walk_codes(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_codes(v, found)
    elif isinstance(node, str):
        # 兼容纯文本行 "600519 贵州茅台"
        m = _CODE_RE.search(node)
        if m and is_a_share(m.group(1)):
            c = m.group(1)
            found[c] = _extract_name(node) or found.get(c, "")


def sync_account(mode: str = "merge") -> dict:
    key = get_apikey()
    if not key:
        return {"ok": False, "error": "尚未配置 API Key。请在东方财富 APP 首页搜索「Skill」领取，或设置环境变量 MX_APIKEY。"}
    try:
        r = requests.post(SELF_SELECT_URL, json={}, timeout=25,
                          headers={"Content-Type": "application/json", "apikey": key,
                                   "User-Agent": "QuantDesk-Local/1.0"})
    except Exception as e:
        return {"ok": False, "error": "连接东方财富接口失败：" + str(e)[:120]}
    if r.status_code != 200:
        return {"ok": False, "error": "接口返回 HTTP %s（可能是 API Key 无效或已过期）" % r.status_code}
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "error": "接口返回内容不是 JSON：" + r.text[:120]}
    try:
        _write_json(SYNC_RAW_PATH, j)
    except Exception:
        pass
    if isinstance(j, dict) and j.get("status") not in (None, 0, "0", 200):
        return {"ok": False, "error": "接口提示：" + str(j.get("message") or j.get("msg") or j.get("status"))[:120]}

    found: dict[str, str] = {}
    _walk_codes(j, found)
    if not found:
        return {"ok": False, "error": "接口调用成功，但未解析到自选股代码。可能是账户自选股为空，或返回结构已变化；原始响应已存到 data/account_last_response.json 供排查。"}
    if mode == "replace":
        res = mutate("replace", items=[{"code": c, "name": n, "source": "账户同步"}
                                       for c, n in found.items()])
    else:
        # 复用导入逻辑，保留用户手工添加的股票
        text = "\n".join(c + " " + n for c, n in found.items())
        res = import_items(text, "merge")
        if res.get("ok"):
            st = _state()
            for i in st["items"]:
                if i["code"] in found:
                    i["source"] = "账户同步"
            _save(st)
            res = {**list_view(), "ok": True, "synced": len(found), "added": res.get("added", 0)}
            return res
        return res
    return {**res, "synced": len(found), "added": len(found)}
