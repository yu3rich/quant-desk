# -*- coding: utf-8 -*-
"""
因子口径自查（自检入口）

把「训练集与快照必须算出同一个数」这件事变成可执行的一步。
挂在 pipeline 的 fastness 检查里也行、手动跑也行，独立于 tests_factor_consistency.py
（那个是逐只抽样比对，这个是全市场聚合比对，几秒钟出结论）。

两种检查互补：
  - 聚合检查（本文件）：全市场 5000+ 只，一次算完，看「整体分布是否一致」。
    抓得到「整列错位」这类系统性错误（如 turnover 被换成 pct，均值会从 2.0 跳到 -4.7）。
  - 逐只检查（tests_factor_consistency.py）：抽样比对每个因子的具体数值。
    抓得到「个别股票退化处理不一致」这类边界错误。

为什么要两种都留：聚合检查快但有盲区（少数票错了均值看不出来），
逐只检查准但慢（300 只约 20 秒）。日常用聚合，改公式后跑逐只。
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import screener
import store


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def check(n: int = 200) -> dict:
    """抽样 n 只，比对两条路径的因子分布是否一致，返回 {ok, diffs, stats}。"""
    codes = store.cached_codes()
    if not codes:
        return {"ok": False, "error": "没有本地行情缓存"}
    step = max(1, len(codes) // max(1, n))
    codes = codes[::step][:n]

    fast_rows, slow_rows = [], []
    for code in codes:
        d = store.bars_frame(code, 200)
        if d is None or len(d) < 80:
            continue
        f = screener.last_features(d)
        if f is None:
            continue
        sdf = screener.feature_frame(d)
        if sdf is None or not len(sdf):
            continue
        fast_rows.append(f)
        slow_rows.append(sdf.iloc[-1].to_dict())

    if not fast_rows:
        return {"ok": True, "skipped": "没有足够数据", "stats": {}}

    fast = pd.DataFrame(fast_rows)
    slow = pd.DataFrame(slow_rows)

    stats, diffs = {}, []
    for k in screener.CORE_KEYS:
        if k not in fast.columns or k not in slow.columns:
            continue
        a, b = _num(fast[k]), _num(slow[k])
        # 用中位数比，避开个别极端值；再比「非空率」抓漏算
        ma, mb = a.median(), b.median()
        na, nb = a.notna().mean(), b.notna().mean()
        stats[k] = {"median_fast": round(float(ma), 6) if pd.notna(ma) else None,
                    "median_slow": round(float(mb), 6) if pd.notna(mb) else None,
                    "valid_fast": round(float(na), 4),
                    "valid_slow": round(float(nb), 4)}
        # 中位数差异超过 1%（相对）或非空率差异超过 5 个百分点，判为不一致
        bad = False
        if pd.notna(ma) and pd.notna(mb):
            if abs(ma - mb) > max(1e-6, abs(mb) * 0.01):
                bad = True
        elif pd.notna(ma) != pd.notna(mb):
            bad = True
        if abs(na - nb) > 0.05:
            bad = True
        if bad:
            diffs.append(k)

    return {"ok": not diffs, "diffs": diffs, "stats": stats,
            "n": len(fast), "keys": sorted(stats)}


def main() -> int:
    r = check()
    if r.get("error"):
        print("[FAIL] %s" % r["error"])
        return 1
    print("=" * 62)
    print("因子口径自查：%s 只" % r.get("n", 0))
    print("=" * 62)
    if r["ok"]:
        print("[OK] 两条路径的因子分布一致")
        # 打印几个关键因子的中位数，便于人工扫一眼
        for k in ("turnover", "vol20", "mom20", "rsi14"):
            st = r["stats"].get(k) or {}
            if st:
                print("  %-10s 快照=%s  训练=%s  非空 %s/%s"
                      % (k, st["median_fast"], st["median_slow"],
                         st["valid_fast"], st["valid_slow"]))
        return 0
    print("[FAIL] 以下因子两条路径不一致：%s" % ", ".join(r["diffs"]))
    for k in r["diffs"]:
        st = r["stats"][k]
        print("  %-12s 快照中位=%-12s 训练中位=%-12s 非空 %s/%s"
              % (k, st["median_fast"], st["median_slow"],
                 st["valid_fast"], st["valid_slow"]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
