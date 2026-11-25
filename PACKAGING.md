# QuantDesk 打包为 EXE 方案

> 目标：在 Windows 上产出一个双击即用、不依赖本机 Python 环境的 QuantDesk。
> 结论先行：**推荐 PyInstaller one-dir + 前端/数据外置**。体积约 350MB（未压缩），启动 1~2 秒，UI 改动无需重新打包。

---

## 一、为什么不能直接 `pyinstaller app.py`

打包前必须先解决三个硬约束，否则打出来必然运行不正常。

### 1. 18 个模块各自硬编码了代码目录

```
app.py bulkdata.py datasource.py emtai.py emtlink.py industry.py journal.py
llm.py mlmodel.py pipeline.py quant.py review.py scheduler.py screener.py
spot.py store.py universe.py watchlist.py
```

每个文件都有：

```python
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
```

冻结成 exe 后 `__file__` 指向 `sys._MEIPASS`（临时解压目录，**只读且每次启动可能变化**）。
后果：数据库、账本、模型、日志全写进临时目录 → 重启即丢，且 `data/quant.db` 925MB 会被反复解压。

### 2. 只读资源和可写数据没有区分

| 类别 | 内容 | 打包后应该在哪 |
|---|---|---|
| 只读资源 | `web/` 前端、`*.example.json`、`web/icon.svg` | exe 内部（`_MEIPASS`）或 exe 同级 |
| 可写数据 | `data/`（quant.db、ledger、screen 模型、journal）、`config.json` | **exe 同级目录**（外置，可备份可迁移） |

### 3. `emtl` 带包内资源文件

`emtl/_ocr_lightweight.py` 用 `importlib.resources` 读取：

```
emtl/models/tiny_captcha_model.b64
```

不显式收集 → 实盘登录时 OCR 找不到模板，验证码必失败，且报错信息很隐晦。

---

## 二、方案对比

| 方案 | 体积 | 启动 | 改动量 | 结论 |
|---|---|---|---|---|
| **A. PyInstaller one-dir**（推荐） | ~350MB（含依赖目录） | 1~2s | 中（路径改造 + spec） | ✅ 首选，排错容易，前端可外置热改 |
| B. PyInstaller one-file | ~150MB 单文件 | 4~8s（每次解压） | 小 | 交付好看但慢、易被杀软误报，不推荐 |
| C. Nuitka 编译 | ~250MB | <1s | 大（编译 20~40 分钟，pandas/akshare 易踩坑） | 备选，追求启动速度时再考虑 |
| D. 免打包绿色版（Python embeddable + 启动器） | ~450MB | 1~2s | 小 | 备选，依赖升级最方便，但目录里能看到一堆 py 文件 |

**推荐 A**：因为你的迭代重心是前端和策略，one-dir 允许 `web/` 留在外面直接改，改完刷新浏览器即生效，不必每次重新打包。

---

## 三、方案 A 实施步骤

### 步骤 1：新增 `paths.py`，统一路径解析（核心改动）

```python
"""打包/开发两套环境下的路径解析。所有模块只从这里取路径。"""
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)          # PyInstaller 运行时为 True

# exe 所在目录（打包后）/ 项目根目录（开发时）—— 用户可写区域
APP_HOME = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent

# 只读资源根（打包后指向 _MEIPASS 临时目录）
RES = Path(getattr(sys, "_MEIPASS", APP_HOME))

BASE = APP_HOME
DATA = APP_HOME / "data"

def _first(*cands):
    for c in cands:
        if c.exists():
            return c
    return cands[0]

# web 优先取 exe 同级（便于热改），不存在则用包内资源
WEB = _first(APP_HOME / "web", RES / "web")

def ensure_dirs():
    for p in ("data", "data/ledger", "data/screen", "data/journal",
              "data/extra", "data/cache", "logs"):
        (APP_HOME / p).mkdir(parents=True, exist_ok=True)
```

然后 18 个模块把

```python
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
```

统一替换为

```python
from paths import BASE, DATA
```

> 语义变化：`BASE` 从「代码所在目录」变为「exe 所在目录」，这正是打包需要的效果。
> 开发时两者相同，行为不变。

### 步骤 2：新增 `main.py` 作为唯一入口

```python
"""QuantDesk 启动器：建目录 → 补默认配置 → 起服务 → 开浏览器。"""
import socket, sys, threading, time, webbrowser
import paths

def port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0

def main():
    paths.ensure_dirs()
    # 首次运行：补默认配置与示例
    cfg = paths.APP_HOME / "config.json"
    if not cfg.exists():
        ex = paths.APP_HOME / "config.example.json"
        if ex.exists():
            cfg.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")

    import app as server          # 延迟导入，确保 paths 已就绪
    port = int(server.cfg().get("port", 8765))
    if not port_free(port):
        print(f"端口 {port} 已被占用，可能已有一个 QuantDesk 在运行：")
        print(f"  http://127.0.0.1:{port}/")
        input("按回车退出…")
        return 1

    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    server.scheduler.start()
    server.emtlink.start_auto()
    server.app.run(host="127.0.0.1", port=port, debug=False, threaded=True)

if __name__ == "__main__":
    sys.exit(main())
```

原 `app.py` 里的 `if __name__ == "__main__"` 段可保留（开发时仍可 `python app.py`），exe 走 `main.py`。

### 步骤 3：写 `QuantDesk.spec`

```python
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

hiddenimports = [
    "lightgbm", "sklearn.utils._typedefs",       # lightgbm 内部可选依赖
    "akshare", "baostock",
    "emtl", "emtl._ocr_lightweight",
    "engineio.async_drivers.threading",
]

datas = [
    ("config.example.json", "."),
    ("data/emt.example.json", "data"),
    ("data/llm.example.json", "data"),
    ("data/sources.example.json", "data"),
]
datas += collect_data_files("emtl")              # 关键：OCR 模板 tiny_captcha_model.b64
datas += collect_data_files("akshare")           # akshare 自带 json 映射表

binaries = []
binaries += collect_dynamic_libs("lightgbm")     # lib_lightgbm.dll
binaries += collect_dynamic_libs("numpy")
binaries += collect_dynamic_libs("scipy")
binaries += collect_dynamic_libs("pandas")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=[
        # 250MB 纯负担：emtl 默认走 lightweight OCR，ddddocr 在 3.13 上根本装不了
        "cv2", "ddddocr", "onnxruntime",
        "matplotlib", "tkinter", "IPython", "pytest",
        "PyQt5", "PySide6", "notebook", "jupyter",
        "pip", "setuptools", "wheel",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="QuantDesk", console=True, icon="web/icon.ico")
coll = COLLECT(exe, a.binaries, a.datas,
               strip=False, upx=False, name="QuantDesk")
```

`console=True` 是有意的：出问题时能看到 traceback，用户关窗口即退出。稳定后可改 `False` + 托盘图标，但需要引入 `pystray` 并处理「怎么退出」，收益不大。

### 步骤 4：准备图标

现有 `web/icon.svg` 需要转成 `.ico`（含 16/32/48/256 四档）。用 Pillow 一条命令即可：

```python
from PIL import Image
Image.open("web/icon.png").save("web/icon.ico", sizes=[(16,16),(32,32),(48,48),(256,256)])
```

### 步骤 5：构建

```bat
.venv\Scripts\pip install pyinstaller==6.11.*
.venv\Scripts\pyinstaller --noconfirm --clean QuantDesk.spec
```

> **必须在 Windows 上构建**，PyInstaller 不支持交叉编译。Python 3.13 需要 PyInstaller ≥ 6.10。

### 步骤 6：组装交付目录

```
QuantDesk\
├─ QuantDesk.exe          ← 代码 + 依赖（构建产出）
├─ _internal\             ← 运行时 DLL / 库（构建产出，勿动）
├─ web\                   ← 前端，可从源码目录复制；改完刷新即生效
├─ config.json            ← 费率/端口等，可改
├─ data\                  ← 数据（空目录也能跑，首次会自动建）
│   ├─ quant.db           ← 可选：带上就是开箱即用（925MB）
│   ├─ ledger\ screen\ journal\
│   └─ emt.json llm.json sources.json   ← 账号与密钥，只在自己机器间搬
└─ logs\
```

**换机使用**：整个目录复制过去即可，`data/` 一起带上就是全量版，不用重跑 66 分钟全市场缓存。这与之前给的 `tar.gz` 全量包是同一份数据，区别只是 Python 环境变成了 exe。

---

## 四、体积预期与削减

| 组成 | 大小 |
|---|---|
| scipy + scipy.libs | 135MB |
| pandas | 75MB |
| numpy + numpy.libs | 55MB |
| Pillow | 16MB |
| cryptography | 11MB |
| akshare + lxml | 19MB |
| lightgbm + DLL | 5MB |
| Python 运行时与其余 | ~40MB |
| **合计（未压缩）** | **约 350MB** |

已排除的 250MB：`cv2` 113MB、`ddddocr` 85MB、`onnxruntime` 54MB。
它们来自 `emtl[ddddocr]` 附加依赖，而 `emtl` 默认 `EMTL_OCR_ENGINE=lightweight`，走自带的模板匹配；且 `ddddocr` 官方只支持 Python 3.11，本机 3.13 上根本无法启用。**排除后必须实测一次实盘登录**，验证码能过才算安全。

还可选排除 `py_mini_racer`（39MB），但 akshare 部分接口依赖它做 JS 解密，删掉可能让个别数据源失效，建议保留。

压缩成 zip 交付约 110~140MB。

---

## 五、风险与对策

| 风险 | 说明 | 对策 |
|---|---|---|
| 杀软误报 | PyInstaller 产物常被 Defender/360 判为可疑 | 用 one-dir 而非 one-file；必要时加信任目录；重度误报则改 Nuitka |
| 数据写进临时目录 | 最容易犯的错，表现为「重启后数据全没了」 | `paths.py` 强制 `DATA = APP_HOME/"data"`，构建后实测重启两次 |
| emtl OCR 模板缺失 | 登录报错含糊 | spec 里 `collect_data_files("emtl")`，并实测登录 |
| lightgbm DLL 缺失 | 打包后 `import lightgbm` 崩 | `collect_dynamic_libs("lightgbm")` |
| 端口占用 | 已有实例在跑 | `main.py` 预检端口并提示，不静默失败 |
| 首次启动慢 | 杀软扫描 + 冷读 | 可接受（1~2s）；one-file 才会到 5s+ |
| 中文/空格路径 | 用户名含中文时路径带空格 | 全程 `pathlib.Path`，已满足；build 目录也避免空格 |
| 打包后找不到 `web/` | 静态 404 整页白 | `paths.WEB` 双路径回退 + 启动时打印实际使用路径 |

---

## 六、验证清单（打包后逐项过一遍）

1. 双击 `QuantDesk.exe` → 控制台打印端口 → 浏览器自动打开首页
2. 首页、`/static/app.js`、`/static/style.css` 全部 200，图表能渲染
3. 「数据中枢 → 数据链」跑一次增量，确认能写库（不是写进临时目录）
4. 关闭再启动**第二次**，确认上次的账本/选股记录还在（这条最关键）
5. 「选股工作台 → 重新选股」能出候选（验证 lightgbm 模型加载 + 快照）
6. 「实盘复盘 → 同步账户」能登录（验证 emtl OCR 模板）
7. 定时任务与盘中自动刷新在后台线程正常（看 `logs/` 与「更新记录」）
8. 把整个目录复制到另一台机器（或另一路径）再跑一次，确认路径无关

---

## 七、工作量与顺序

| 阶段 | 内容 | 产出 |
|---|---|---|
| 1 | 新增 `paths.py`，改造 18 个模块的路径定义 | 开发环境行为不变，`python app.py` 仍正常 |
| 2 | 新增 `main.py` 启动器（建目录、补配置、开浏览器、端口预检） | 双击启动体验 |
| 3 | 写 `QuantDesk.spec` + 转 ico | 可重复构建 |
| 4 | 首次构建、按第六节清单逐项修 | 能跑的 exe |
| 5 | 组装交付目录、写使用说明 | 可迁移的绿色包 |

前 3 步是纯代码改动、不影响现有功能；第 4 步通常需要 1~2 轮补 hiddenimports（pandas 3.x 与 akshare 的 hook 偶尔要手工加）。

---

## 八、一句话建议

先做**方案 A**。它把「代码」和「数据」彻底分开：exe 负责程序，`data/` 负责你的资产。
以后升级只要替换 `QuantDesk.exe` 和 `_internal/`，`data/` 原封不动——这正是你频繁迭代策略和 UI 时最省事的方式。

---

## 九、首轮实测结果（2026-11-19）

**产物**：`dist/QuantDesk/` = 188MB 程序（exe 20MB + `_internal` 168MB）+ 961MB 数据。
比估算的 350MB 小很多——venv 里的 `.pyi`、测试、`__pycache__` 被打包器剔除了，真实的运行时依赖本来就只有这些。

**构建耗时**：首次 1 分钟（分析 51s + 收集 6s），增量重建 48s。

**逐项验证**

| 项目 | 结果 |
|---|---|
| `--selftest` 五项（Python/pandas/LightGBM 训练/emtl 模板/web 资源） | 全过 |
| 静态资源 `/`、`app.js`、`style.css`、`echarts.min.js` | 全 200 |
| `/api/status` `cache_count` | 5590 只，库读到的是副本不是空库 |
| 选股（复用快照，全市场） | 出 10 只候选，真实价换算正常（利和兴 24.14 / hfq 36.49） |
| 选股记录写入 → 关闭 → 重启 | 批次仍在，持久化 OK |
| 重复启动（端口占用） | 友好提示并退出，不抛异常 |
| UI 渲染（无头浏览器截图） | 与源码版一致，无错位 |
| 数据落盘位置 | `exe 同级/data/`，未写入 `_internal`（这条最容易出问题） |

**过程中修掉的两个坑**

1. `lightgbm.__version__` 在冻结环境里取不到（`AttributeError`），虽然训练/预测正常。自检改用 `getattr` 兜底。
2. 控制台中文乱码：`main.py` 缺少 `sys.stdout.reconfigure(encoding="utf-8")`，与 `app.py` 对齐后正常。

**新增的两个开关**

- `--selftest`：不开服务，只跑依赖自检，出问题时第一时间定位是环境还是数据。
- `--no-browser`：不自动开浏览器，便于开机自启或本机已有实例时使用。

**尚待验证**

- 真实券商账号登录（测试目录刻意没放 `emt.json`，避免密钥落到测试副本）：模板资源已确认存在且 `make_ocr()` 可初始化，但完整登录需在有账号的机器上点一次「同步账户」。
- 杀软态度：本机 Defender 未拦截，换机时若被拦，加信任目录即可。
