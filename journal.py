# -*- coding: utf-8 -*-
"""复盘日记：一天一个 Markdown 文件（data/journal/YYYY-MM-DD.md）。

为什么用 .md 而不是塞进数据库：
- 纯文本，脱离本工具也能直接读、能进版本库、能随手复制备份；
- 文件原型清晰地表达「按天一篇」，不会因为某天没交易就把内容混在一起；
- front matter 只放日期、来源、时间这类元信息，正文保持用户原样，不夹杂 JSON。

格式示例：
    ---
    day: 2026-11-19
    src: manual
    created_at: 2026-11-19 13:09
    updated_at: 2026-11-19 13:52
    model:
    ---

    今天做对了什么……
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIR = BASE / "data" / "journal"

SRC = ("manual", "ai", "local")
SRC_LABEL = {"manual": "手写", "ai": "AI 稿", "local": "本地模板"}
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
META_KEYS = ("day", "src", "created_at", "updated_at", "model")
MAX_CHARS = 50000


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _day(day) -> str:
    return str(day or "").strip()[:10]


def _valid_day(day: str) -> bool:
    if not DAY_RE.match(day or ""):
        return False
    try:
        dt.datetime.strptime(day, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def path_for(day: str) -> Path:
    return DIR / (str(day) + ".md")


def _parse(raw: str) -> tuple[dict, str]:
    """解析 front matter。没有 front matter 的旧文件按手写稿处理。"""
    meta, body = {}, raw
    if raw.startswith("---"):
        m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", raw, re.S)
        if m:
            for line in m.group(1).splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip()
                if k in META_KEYS:
                    meta[k] = v.strip()
            body = raw[m.end():]
    return meta, body.strip("\n")


def _dump(meta: dict, body: str) -> str:
    lines = ["---"]
    for k in META_KEYS:
        v = str(meta.get(k) or "")
        lines.append(("%s: %s" % (k, v)).rstrip())
    lines += ["---", ""]
    return "\n".join(lines) + body.strip("\n") + "\n"


def _preview(body: str, n: int = 76) -> str:
    for line in str(body or "").splitlines():
        s = line.strip().lstrip("#>*- ").strip()
        if s:
            return s[:n] + ("…" if len(s) > n else "")
    return ""


def _entry(day: str, meta: dict, body: str, exists: bool = True) -> dict:
    src = meta.get("src") or ("manual" if body else "")
    return {
        "ok": True,
        "day": day,
        "exists": bool(exists),
        "text": body,
        "src": src if src in SRC else "manual",
        "src_label": SRC_LABEL.get(src, "手写"),
        "created_at": meta.get("created_at") or "",
        "updated_at": meta.get("updated_at") or meta.get("created_at") or "",
        "model": meta.get("model") or "",
        "chars": len(body or ""),
        "preview": _preview(body),
    }


def read(day) -> dict:
    day = _day(day)
    if not _valid_day(day):
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD", "day": day}
    p = path_for(day)
    if not p.exists():
        return {"ok": True, "day": day, "exists": False, "text": "", "src": "",
                "src_label": "", "created_at": "", "updated_at": "", "model": "",
                "chars": 0, "preview": ""}
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "day": day}
    meta, body = _parse(raw)
    if not meta.get("updated_at"):
        try:
            meta["updated_at"] = dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return _entry(day, meta, body, True)


def save(day, text, src: str = "manual", model: str = "", at: str = "") -> dict:
    """写入某天日记。内容为空时拒绝，避免列表里出现空条目（删除请用 delete）。"""
    day = _day(day)
    if not _valid_day(day):
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
    body = str(text or "").replace("\r\n", "\n").strip("\n")
    if not body.strip():
        return {"ok": False, "error": "内容为空，未保存"}
    body = body[:MAX_CHARS]
    src = src if src in SRC else "manual"
    p = path_for(day)
    old = {}
    if p.exists():
        try:
            old, _ = _parse(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            old = {}
    now = str(at or "").strip() or _now()
    meta = {
        "day": day,
        "src": src,
        "created_at": old.get("created_at") or now,
        "updated_at": now,
        "model": str(model or "")[:80],
    }
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(_dump(meta, body), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception as e:
        return {"ok": False, "error": "写入失败：" + str(e)[:160]}
    return _entry(day, meta, body, True)


def delete(day) -> dict:
    day = _day(day)
    if not _valid_day(day):
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
    p = path_for(day)
    if not p.exists():
        return {"ok": True, "day": day, "deleted": False}
    try:
        p.unlink()
    except Exception as e:
        return {"ok": False, "error": "删除失败：" + str(e)[:160]}
    return {"ok": True, "day": day, "deleted": True}


def list_entries(limit: int = 500) -> dict:
    """按日期倒序列出全部日记。只读 front matter 和预览，不改文件。"""
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        files = [f for f in DIR.glob("*.md") if _valid_day(f.stem)]
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "entries": [], "count": 0}
    files.sort(key=lambda f: f.stem, reverse=True)
    out = []
    for f in files[:max(1, int(limit))]:
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        meta, body = _parse(raw)
        if not meta.get("updated_at"):
            try:
                meta["updated_at"] = dt.datetime.fromtimestamp(
                    f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
        e = _entry(f.stem, meta, body, True)
        out.append({
            "day": e["day"], "src": e["src"], "src_label": e["src_label"],
            "created_at": e["created_at"], "updated_at": e["updated_at"],
            "chars": e["chars"], "preview": e["preview"], "model": e["model"],
        })
    return {"ok": True, "dir": str(DIR), "entries": out, "count": len(out)}


def seed_from_ledger() -> int:
    """把旧账本里的 journal 字段迁成 .md（已有文件不覆盖），返回新增篇数。

    只在列表接口第一次被访问时跑一次，并用 .legacy_seeded 记标记：
    否则用户在新页面删掉一篇旧日记后，下次刷新又会被账本字段“复活”。
    账本本身是 SQLite，扫几千天也只是毫秒级。
    """
    mark = DIR / ".legacy_seeded"
    if mark.exists():
        return 0
    try:
        import store
        recs = store.ledger_all(limit=5000)
    except Exception:
        return 0
    added = 0
    for rec in recs:
        day = _day(rec.get("date"))
        text = str(rec.get("journal") or "")
        if not _valid_day(day) or not text.strip():
            continue
        if path_for(day).exists():
            continue
        r = save(day, text, src=rec.get("journal_src") or "manual",
                 model=rec.get("journal_model") or "", at=rec.get("journal_at") or "")
        if r.get("ok"):
            added += 1
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        mark.write_text(_now(), encoding="utf-8")
    except Exception:
        pass
    return added