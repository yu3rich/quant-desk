#!/usr/bin/env python3
"""把 quant-desk 根目录的手机版白名单模块同步进 Android 工程。

用法（在仓库根目录执行）：
    python3 mobile/sync_core.py            # 同步
    python3 mobile/sync_core.py --check    # 只校验一致性，不写文件（CI/预提交用）

白名单见 mobile/MODULES.md。同步是单向覆盖：仓库根目录为唯一真源，
不要直接改 mobile/app/src/main/python/ 下的文件。

- 引擎模块 + config.json  ->  app/src/main/python/
- web/ 静态前端           ->  app/src/main/assets/web/
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = Path(__file__).resolve().parent / "app"

# ---- 白名单：手机版允许打包的引擎模块（与 mobile/MODULES.md 保持一致） ----
PY_MODULES = [
    "quant.py", "store.py", "chartdata.py", "industry.py", "watchlist.py",
    "screener.py", "spot.py", "universe.py", "histcache.py",
    "emtlink.py", "emtbook.py", "journal.py", "llm.py", "factors.py",
    "portfolio.py", "neutral.py", "review.py",
    "app.py",                # Flask 服务入口
    "config.json",           # 默认配置（Chaquopy 会把它打进 python/ 源码目录）
]
WEB_DIR = "web"


def sync_one(src: Path, dst: Path) -> str:
    if not src.exists():
        return f"MISS-SRC  {src.name}（仓库根目录不存在，跳过）"
    if dst.exists() and filecmp.cmp(src, dst, shallow=False):
        return f"same      {src.relative_to(REPO)}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return f"SYNC      {src.relative_to(REPO)} -> {dst.relative_to(REPO)}"


def sync_tree(src: Path, dst: Path) -> list[str]:
    lines = []
    if not src.exists():
        return [f"MISS-SRC  {src}（不存在，跳过）"]
    # 目标端多余文件先清掉，保证目标端是源端真子集
    if dst.exists():
        for p in sorted(dst.rglob("*")):
            rel = p.relative_to(dst)
            if not (src / rel).exists():
                if p.is_file():
                    p.unlink()
                    lines.append(f"DEL       {p.relative_to(REPO)}（源端已删除）")
                elif p.is_dir():
                    shutil.rmtree(p)
    for p in sorted(src.rglob("*")):
        if p.is_dir():
            continue
        dst_file = dst / p.relative_to(src)
        lines.append(sync_one(p, dst_file))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验不写入")
    args = ap.parse_args()

    py_dst = APP / "src/main/python"
    web_dst = APP / "src/main/assets/web"
    changed = 0
    lines = []
    for name in PY_MODULES:
        line = sync_one(REPO / name, py_dst / name)
        if line.startswith(("SYNC", "MISS-SRC")):
            changed += 1
        lines.append(line)
    for line in sync_tree(REPO / WEB_DIR, web_dst):
        if line.startswith(("SYNC", "DEL", "MISS-SRC")):
            changed += 1
        lines.append(line)

    print("\n".join(lines))
    if args.check:
        n_bad = sum(1 for l in lines if l.startswith(("SYNC", "DEL", "MISS-SRC")))
        print(f"\ncheck: {n_bad} 个文件不一致/缺失")
        return 1 if n_bad else 0
    print(f"\n同步完成，{changed} 个文件更新。白名单说明见 mobile/MODULES.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
