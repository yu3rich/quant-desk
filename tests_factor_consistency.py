# -*- coding: utf-8 -*-
"""
因子口径一致性校验（回归测试）

为什么需要：同一个因子有两条实现路径 ——
  训练集：store.bars_frame -> feature_frame()    （pandas，按列名取数）
  快照：  histcache.ohlcv -> _lf_arrays()         （numpy，按位置取数）
两条路径必须给出同一个数。历史上 histcache.ohlcv 把 turnover 取成了 pct
（arr[:,6] vs arr[:,7]），快照的「换手率」实际是当日涨跌幅，与训练集不一致，
而模型最看重的正是 turnover —— 这类错位不会抛异常，只会悄悄算错。

用法：
  python tests_factor_consistency.py            抽样 60 只，逐因子比对
  python tests_factor_consistency.py --all      全市场（慢，几分钟）
  python tests_factor_consistency.py --n 200    指定抽样数
退出码：0=全部一致，1=存在不一致（可直接接进 CI / 启动自检）
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

# Windows 控制台默认 cp936，直接 print 非 GBK 字符（如 ✓）会 UnicodeEncodeError，
# 让一个「校验通过」以崩溃收场。统一把 stdout 钉成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import screener
import store

# 逐因子容差：两条实现一个用 pandas rolling/ewm、一个用 numpy 递推，
# 浮点次序不同，只允许末位误差。相对/绝对取较大者。
TOL = {"default": (1e-6, 1e-9)}


def _close(a, b, rel=1e-6, abs_=1e-9) -> bool:
    """数值是否一致。空值语义单独处理：

    last_features 用 None 表示「算不出」，feature_frame 用 NaN，两者等价；
    pandas 的 std/mean 遇 NaN 给 NaN、numpy 路径给 None，都属于同一个含义
    「这个因子在当天无定义」，不应算作口径不一致。
    """
    a_na = a is None or (isinstance(a, float) and np.isnan(a))
    b_na = b is None or (isinstance(b, float) and np.isnan(b))
    if a_na or b_na:
        return a_na and b_na
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if np.isnan(fa) and np.isnan(fb):
        return True
    if np.isnan(fa) or np.isnan(fb):
        return False
    return abs(fa - fb) <= max(abs_, rel * max(abs(fa), abs(fb)))


def compare_code(code: str, n: int = 200) -> dict:
    """比对一只股票的两条路径，返回 {ok, diffs:[...], skipped:str}。"""
    d = store.bars_frame(code, n)
    if d is None or len(d) < 80:
        return {"ok": True, "skipped": "数据不足 80 根", "diffs": []}

    fast = screener.last_features(d)          # numpy 路径
    slow_df = screener.feature_frame(d)       # pandas 路径
    if fast is None or slow_df is None or not len(slow_df):
        return {"ok": True, "skipped": "无法计算", "diffs": []}

    # 日期必须一致：两条路径取的是同一根 bar
    d_last = str(slow_df["date"].iloc[-1])[:10]
    if str(d["date"].iloc[-1])[:10] != d_last:
        return {"ok": False, "skipped": "",
                "diffs": [("__date__", str(d["date"].iloc[-1]), d_last)]}

    slow = slow_df.iloc[-1]
    diffs = []
    for k, v in fast.items():
        if k not in slow.index:
            continue
        ref = slow[k]
        if not _close(v, ref):
            diffs.append((k, v, ref))
    return {"ok": not diffs, "skipped": "", "diffs": diffs, "date": d_last}


def compare_raw_path(code: str, n: int = 200) -> dict:
    """额外校验：histcache.ohlcv 的数组与 bars_frame 的列是否对齐。

    这一条专抓「按位置取数」的错位：ohlcv 返回 t，必须等于 bars 的 turnover，
    不能被内部错位成 pct。
    """
    import histcache

    d = store.bars_frame(code, n)
    a = histcache.ohlcv(code, n)
    if d is None or a is None or not len(d) or len(a["c"]) == 0:
        return {"ok": True, "skipped": "数据不足", "diffs": []}
    diffs = []
    pairs = [
        ("close", float(d["close"].iloc[-1]), float(a["c"][-1])),
        ("open", float(d["open"].iloc[-1]), float(a["o"][-1])),
        ("high", float(d["high"].iloc[-1]), float(a["h"][-1])),
        ("low", float(d["low"].iloc[-1]), float(a["l"][-1])),
        ("volume", float(d["volume"].iloc[-1]), float(a["v"][-1])),
        ("amount", float(d["amount"].iloc[-1]), float(a["a"][-1])),
        ("turnover", float(d["turnover"].iloc[-1]), float(a["t"][-1])),
    ]
    for name, ref, got in pairs:
        if not _close(got, ref):
            diffs.append((name, got, ref))
    return {"ok": not diffs, "skipped": "", "diffs": diffs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全市场（慢）")
    ap.add_argument("--n", type=int, default=60, help="抽样只数")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    codes = store.cached_codes()
    if not codes:
        print("[FAIL] 没有本地行情缓存，无法校验")
        return 1
    if not args.all:
        step = max(1, len(codes) // max(1, args.n))
        codes = codes[::step][:args.n]

    bad_feat, bad_raw, skipped = [], [], 0
    for code in codes:
        r = compare_code(code)
        if r.get("skipped"):
            skipped += 1
        elif not r["ok"]:
            bad_feat.append((code, r["diffs"]))
        r2 = compare_raw_path(code)
        if not r2.get("skipped") and not r2["ok"]:
            bad_raw.append((code, r2["diffs"]))

    print("=" * 70)
    print("因子口径一致性校验：%d 只（跳过 %d）" % (len(codes), skipped))
    print("=" * 70)

    def dump(title, bad, what):
        print("")
        if not bad:
            print("[OK] %s：全部一致" % title)
            return True
        print("[FAIL] %s：%d 只不一致" % (title, len(bad)))
        # 按因子聚合，先看「哪个因子最常错」，比逐只打印有用
        agg: dict[str, int] = {}
        for code, diffs in bad:
            for item in diffs:
                agg[item[0]] = agg.get(item[0], 0) + 1
        print("  出错因子统计：")
        for k, c in sorted(agg.items(), key=lambda x: -x[1]):
            print("    %-14s %d 只" % (k, c))
        print("  样例（最多 5 只）：")
        for code, diffs in bad[:5]:
            print("    %s" % code)
            for item in diffs[:4]:
                print("      %-12s 快照侧=%-16s 训练侧=%s"
                      % (item[0], item[1], item[2]))
        return False

    ok1 = dump("因子数值 last_features vs feature_frame", bad_feat, "因子")
    ok2 = dump("数组对齐 ohlcv vs bars_frame", bad_raw, "列")

    print("")
    if ok1 and ok2:
        print("结论：两条路径口径一致 ✓")
        return 0
    print("结论：存在口径不一致，快照特征与训练特征不是同一个东西 —— 必须先修")
    return 1


if __name__ == "__main__":
    sys.exit(main())
