#!/usr/bin/env python3
"""冒烟验证：纯 Python 依赖在无图形 Linux 环境下可用。

用法：/path/to/venv/bin/python mobile/smoke_test.py
验证项：
  1) import emtl（本地 wheel 安装）
  2) baostock 登录并拉 sh.600000 最近 10 根日线
  3) Flask 可导入（手机版 WebView 后端）
"""
import sys

print("== smoke test, python:", sys.version.split()[0], "==")

# 1) emtl
try:
    import emtl
    import importlib.metadata as md
    print(f"[OK] import emtl (version {md.version('emtl')})")
    from emtl import client
    print("[OK] emtl.client importable")
except Exception as e:
    print(f"[FAIL] emtl: {type(e).__name__}: {e}")

# 2) baostock
try:
    import baostock as bs
    lg = bs.login()
    print("[..] login:", lg.error_code, lg.error_msg)
    rs = bs.query_history_k_data_plus(
        "sh.600000", "date,code,close,volume",
        start_date="2025-11-01", end_date="2026-11-25",
        frequency="d", adjustflag="3")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    print(f"[{'OK' if rows else 'FAIL'}] baostock daily rows: {len(rows)} (error_code={rs.error_code})")
    for r in rows[-10:]:
        print("   ", r)
    bs.logout()
except Exception as e:
    print(f"[FAIL] baostock: {type(e).__name__}: {e}")

# 3) flask
try:
    import flask
    print(f"[OK] import flask {flask.__version__}")
except Exception as e:
    print(f"[FAIL] flask: {type(e).__name__}: {e}")
