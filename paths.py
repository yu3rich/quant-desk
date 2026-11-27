# -*- coding: utf-8 -*-
"""paths.py — 全站统一路径解析。

所有模块只从这里取路径，不要各自写 `Path(__file__).parent`。

  BASE = 项目根，DATA = 项目根/data，WEB = 项目根/web
  RES  = 资源根（兼容冻结运行环境，开发态等于项目根）
  ensure_dirs() 建好 data/ 下的子目录，避免 sqlite / 日志 / 快照写失败。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

# 资源根：冻结环境指向 _internal，开发态是项目根
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

if FROZEN:
    # LOCALAPPDATA 在极少数精简系统上可能缺失，退回用户主目录
    _local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    BASE = Path(_local) / "QuantDesk"
else:
    BASE = Path(__file__).resolve().parent

DATA = BASE / "data"
WEB = RES / "web"

# 打包后第一次启动要把这些目录建出来，否则 sqlite / 日志 / 快照全部写失败
_SUBDIRS = ("data", "data/ledger", "data/ledger/_raw", "data/screen", "data/journal",
            "data/extra", "data/packs", "data/backup", "logs")


def ensure_dirs() -> None:
    for p in _SUBDIRS:
        try:
            (BASE / p).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def is_frozen() -> bool:
    return FROZEN
