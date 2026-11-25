# -*- coding: utf-8 -*-
"""
数据目录统一收敛点（桌面打包用）

规则：
- 开发环境（源码运行）：DATA = 仓库根 data/，与历史行为完全一致
- PyInstaller 冻结环境（frozen）：DATA = %LOCALAPPDATA%/QuantDesk/data
  （Program Files 无写权限；行情库数百 MB 也不能放 exe 旁边）
- 可用环境变量 QUANTDESK_DATA 显式覆盖（测试 / 便携模式用）

只读资源（web/、config.json、data/*.example.json）不经过这里：
它们跟随模块所在目录（冻结后是 _internal/），BASE 的语义保持不变。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def data_root() -> Path:
    env = os.environ.get("QUANTDESK_DATA")
    if env:
        p = Path(env)
    elif getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        p = Path(local) / "QuantDesk" / "data"
    else:
        p = BASE / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_path(*parts: str) -> Path:
    p = data_root()
    for part in parts:
        p = p / part
    return p
