# -*- mode: python ; coding: utf-8 -*-
"""
QuantDesk 打包配置（PyInstaller one-dir）。

构建：
    ..\\quant-desk\\.venv\\Scripts\\python -m PyInstaller --noconfirm --clean QuantDesk.spec

产物：
    dist/QuantDesk/QuantDesk.exe   +   dist/QuantDesk/_internal/

关键点（踩过就会报难懂的错）：
- emtl 的验证码引擎用 importlib.resources 读 models/tiny_captcha_model.b64，
  必须 collect_data_files("emtl")，否则实盘登录必失败；
- lightgbm 带 lib_lightgbm.dll，要 collect_dynamic_libs；
- cv2 / ddddocr / onnxruntime 共约 250MB：emtl 默认 lightweight 引擎用不到，
  且 ddddocr 只支持 Python 3.11，本机 3.13 根本启用不了，直接排除。
"""

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules, copy_metadata)

APP_NAME = "QuantDesk"

# ---- 数据文件：示例配置（首次运行自动补成正式配置） ----
datas = [
    ("config.example.json", "."),
    ("data/emt.example.json", "data"),
    ("data/llm.example.json", "data"),
    ("data/sources.example.json", "data"),
]

# 前端打进包内作为兜底；exe 同级若存在 web/ 则优先用同级的（方便改了刷新即生效）
datas += [("web", "web")]

# ---- 第三方包内资源 ----
datas += collect_data_files("emtl")       # 验证码模板 tiny_captcha_model.b64
datas += collect_data_files("akshare")    # 部分接口依赖包内 json 映射表
datas += collect_data_files("baostock")
# akshare 的版本号在「数据中枢」里展示：不带上 dist-info，importlib.metadata 会读成「未安装」
try:
    datas += copy_metadata("akshare")
except Exception:
    pass

# ---- 动态库 ----
binaries = []
for pkg in ("lightgbm", "numpy", "scipy", "pandas"):
    binaries += collect_dynamic_libs(pkg)

# ---- 隐式导入 ----
hiddenimports = [
    "lightgbm",
    "tdxpy",
    "tdxpy.hq",
    "emtl",
    "emtl._ocr_lightweight",
    "baostock",
    "akshare",
]
hiddenimports += collect_submodules("akshare")   # akshare 大量子模块按需导入
hiddenimports += collect_submodules("emtl")

# ---- 明确排除 ----
excludes = [
    # emtl[ddddocr] 的附加依赖，默认引擎用不到，共约 250MB
    "cv2",
    "ddddocr",
    "onnxruntime",
    # GUI / 科学计算 / 开发工具链，本项目一概不用
    "tkinter",
    "matplotlib",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "pip",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台：能看到地址与报错，关窗口即退出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
