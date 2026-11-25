# -*- mode: python ; coding: utf-8 -*-
# QuantDesk Windows 打包配置（PyInstaller onedir，双入口）
# 构建：packaging/build.bat（需在 Windows + Python 3.13 环境执行）

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent   # 仓库根（spec 在 packaging/ 下）

a = Analysis(
    [str(ROOT / "packaging" / "entry_desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "web"), "web"),                      # 前端单页应用
        (str(ROOT / "config.json"), "."),                # 默认配置（只读基线）
        (str(ROOT / "data" / "emt.example.json"), "data"),
        (str(ROOT / "data" / "llm.example.json"), "data"),
        (str(ROOT / "data" / "sources.example.json"), "data"),
    ],
    hiddenimports=[
        "lightgbm",          # 自带 DLL，需显式声明才能随包收集
        "emtl",
        "baostock",
        "akshare",
        "tdxpy",
        "ta",
        "webview",
        "qdpaths",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "setuptools"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="QuantDesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # 桌面程序，不弹黑窗
    icon=str(ROOT / "web" / "icon.svg") if False else None,  # PyInstaller 只收 .ico；有 ico 后填路径
)

worker = EXE(
    pyz, a.scripts,
    exclude_binaries=True,
    name="qdworker",
    debug=False,
    strip=False,
    upx=False,
    console=True,                # worker 需要管道 stdout，保留 console
)

coll = COLLECT(
    exe, worker, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False,
    name="QuantDesk",
)
