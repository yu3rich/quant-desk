# -*- coding: utf-8 -*-
"""股票池「行情」列回归：必须读 SQLite bar_meta，不能回头找已删除的 CSV 目录。"""
import json
import tempfile
from pathlib import Path

import store
import watchlist

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def test_real_db():
    print("\n[真实库] 股票池行情状态")
    view = watchlist.list_view()
    meta = store.meta_map()
    check(bool(view["items"]), "股票池非空")
    for x in view["items"]:
        m = meta.get(x["code"]) or {}
        expected = bool(m and int(m.get("rows") or 0) > 0)
        check(x["cached"] == expected,
              "%s cached=%s 与 bar_meta 一致" % (x["code"], x["cached"]))
        if expected:
            check(x["bar_date"] == str(m.get("last_date") or "")
                  and x["bars"] == int(m["rows"]),
                  "%s 带出数据日 %s / %s 根" % (x["code"], x["bar_date"], x["bars"]))
    # 清理后的环境里 CSV 目录已经不存在，这正是最容易漏掉的场景
    if not (watchlist.DATA / "cache").exists():
        check(any(x["cached"] for x in view["items"]),
              "旧 CSV 目录不存在时，库中有的票仍显示已缓存")


def test_source_is_sqlite():
    print("\n[隔离场景] 判定来源是 store 而非文件")
    orig_watch, orig_meta, orig_has, orig_names = (
        watchlist.WATCH_PATH, store.meta_map, store.has_code, watchlist.resolve_names)
    try:
        with tempfile.TemporaryDirectory() as td:
            watchlist.WATCH_PATH = Path(td) / "watchlist.json"
            watchlist.WATCH_PATH.write_text(json.dumps({
                "version": 1, "default": "600519",
                "items": [{"code": "600519", "name": "甲"},
                          {"code": "000001", "name": "乙"}],
            }), "utf-8")
            fake = {"600519": {"code": "600519", "last_date": "2026-11-24", "rows": 100}}
            store.meta_map = lambda: fake
            store.has_code = lambda c: c in fake
            watchlist.resolve_names = lambda codes, force=False: {c: c for c in codes}

            a, b = watchlist.list_view()["items"]
            check(a["cached"] and a["bars"] == 100 and a["bar_date"] == "2026-11-24",
                  "有 bar_meta 的股票显示已缓存并带出数据日")
            check(not b["cached"], "无 bar_meta 的股票显示未缓存")
            check(watchlist.lookup("600519")["cached"]
                  and not watchlist.lookup("000001")["cached"],
                  "lookup 的 cached 同样来自 SQLite")
    finally:
        watchlist.WATCH_PATH, store.meta_map = orig_watch, orig_meta
        store.has_code, watchlist.resolve_names = orig_has, orig_names


if __name__ == "__main__":
    test_real_db()
    test_source_is_sqlite()
    print("\n%s" % ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    raise SystemExit(1 if FAILED else 0)
