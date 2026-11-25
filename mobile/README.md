# QuantDesk 手机版（Android APK · Chaquopy + WebView）

定位：研究/查看端。引擎代码不 fork，全部由 `sync_core.py` 从仓库根目录按白名单同步
（白名单与取舍见 `MODULES.md`，依据打包设计文档第 2.3 节）。唯一例外：LightGBM 需要
Chaquopy build-wheel 自编译后才能进包（见下文）。

```
mobile/
├── settings.gradle.kts / build.gradle.kts / gradle.properties / gradle wrapper   # Gradle 8.2 + AGP 8.2.2
├── app/
│   ├── build.gradle.kts          # Chaquopy 15.0.1, python 3.11, abiFilters arm64-v8a+x86_64, pip 依赖
│   └── src/main/
│       ├── AndroidManifest.xml   # INTERNET 权限, PyApplication
│       ├── java/com/quantdesk/mobile/MainActivity.kt   # 进程内拉起 Flask(127.0.0.1:8765) + WebView
│       ├── python/               # sync_core.py 同步的引擎模块 + server_main.py 启动器
│       ├── assets/web/           # sync_core.py 同步的 web/ 前端
│       └── res/                  # 布局/主题/图标
├── sync_core.py                  # 白名单同步脚本（--check 只校验）
├── MODULES.md                    # 白名单说明
└── README.md
```

## 1. 在装了 Android Studio 的机器上出 APK

1. `git clone` 本仓库，Android Studio 打开 `mobile/` 目录（作为独立 Gradle 工程）。
2. 需要构建机上有 Python 3.11（Chaquopy 用它跑 pip 依赖解析）：
   - Windows: 装 3.11 后如在 PATH 找不到，可在 `app/build.gradle.kts` 的
     `chaquopy { defaultConfig { buildPython("C:/path/to/python.exe") } }` 指定。
   - Linux/macOS: `python3.11` 在 PATH 即可。
3. 同步引擎代码（在仓库根目录执行）：
   ```bash
   python3 mobile/sync_core.py          # 首次 / 引擎代码更新后
   python3 mobile/sync_core.py --check  # 校验手机包内文件与仓库一致
   ```
4. Android Studio 里 **Build → Build Bundle(s)/APK(s) → Build APK(s)**，或命令行
   `./gradlew assembleDebug`（首次会下载 Gradle 8.2 + Chaquopy wheel，网络到
   services.gradle.org / maven.google.com / chaquo.com 需通畅）。
5. 产物：`app/build/outputs/apk/debug/app-debug.apk`。装到 arm64 设备或 x86_64 模拟器。
6. 首次启动：`server_main.py` 在 `filesDir/data/` 生成最小 `config.json`，Flask 监听
   127.0.0.1:8765，WebView 打开。实盘页首次需重新填券商密码（Fernet 密钥按本机派生，
   与换电脑同规则）。

## 2. LightGBM wheel 自编译（ChaQuopy build-wheel）

Chaquopy 官方仓库（chaquo.com/pypi-13.1/）没有 lightgbm 的 Android wheel，需要在
**x86-64 Linux 主机**用 Chaquopy 的 build-wheel 脚本交叉编译：

1. 准备一台 x86-64 Linux（内存 ≥8G），装 Docker（官方推荐方式）或 Android NDK。
2. 取 Chaquopy 源码：
   ```bash
   git clone --recursive https://github.com/chaquo.com/chaquopy.git
   cd chaquopy/server/pypi
   ```
3. 单包编译（官方入口为 `build-wheel.py`，Docker 模式无需手动配 NDK）：
   ```bash
   # Chaquopy 15.x：通过 docker 镜像构建指定的 wheel
   ./build-wheel.py --build lightgbm          # 产物在 build/ 下
   # 若在旧版本中，用法为：
   #   python build-wheel.py lightgbm         # 见 chaquopy/server/pypi/README
   ```
   注意 lightgbm 是 C++ 扩展，build-wheel 会用 Chaquopy 自带的 NDK 交叉编译
   其 native 组件，并生成 `-cp311-cp311-android` 命名的 wheel。
4. 把产物 wheel 放进 `mobile/app/libs/`，在 `app/build.gradle.kts` 的 pip 块启用：
   ```kotlin
   install("./libs/lightgbm-<版本>-cp311-cp311-android.whl")
   ```
   （路径必须含 `/`，Chaquopy 才会识别为本地 wheel。）
5. 启用后把 `MODULES.md` 中"砍掉 LightGBM"的口径同步更新（mlmodel.py 等模块入包）。

## 3. 冒烟验证（本机已完成的部分）

无 Android SDK，验证的是"纯 Python 依赖在无图形 Linux 环境可用"这一层（Chaquopy
内置真实 CPython，纯 Python 包行为一致；含 C 扩展的 pandas/Pillow 由 Chaquopy 官方
Android wheel 提供，行为差异属未验证项）。方法与结果见仓库中 `mobile/SMOKE-REPORT.md`（原始输出原样贴出）。已验证：`import emtl`（/tmp wheel）与 baostock 拉
sh.600000 日线 10 根。

## 4. 未验证清单（上真机前必须逐项过）

1. **Chaquopy 构建链**：未装 Android SDK，本工程从未跑过 Gradle。AGP 8.2.2 +
   Chaquopy 15.0.1 + Gradle 8.2 组合在 Chaquopy 15.0.1 官方支持范围内，但首次构建
   可能在 Kotlin 插件版本、依赖下载上出问题。
2. **app.py 白名单外导入**：`app.py` 顶部 import 了 mlmodel/datasource/bulkdata/
   pipeline/scheduler/emtai/listing/capital/stockview/valsrc —— 手机包里没有这些
   文件，`import app` 会 ImportError。需按 MODULES.md「注意」二选一处理（推荐给
   app.py 加条件导入）。**这是 v1 上真机前最大的阻塞项。**
3. **baostock 在 Android/移动网络下的连通性**：桌面 Linux 下已验证（见冒烟报告），
   Android WebView/进程内 socket 行为与移动运营商网络未测。设计文档 2.6 风险 2。
4. **pandas / Pillow 的 Chaquopy Android wheel** 与仓库桌面版本（pandas 3.x）是否
   行为一致；baostock 对 pandas 内部 API 的用法（DataFrame.append 等）在 Chaquopy
   wheel 版本下是否仍兼容。
5. **emtl 在 Android 上的验证码 OCR**（内置纯 Python 轻量模型）与 Pillow wheel 的
   兼容性；Fernet（cryptography，含 C 扩展）在 Chaquopy 是否有 Android wheel ——
   build.gradle 未列 cryptography，实盘登录可能需要补装/自编译。
6. **Flask 在 Chaquopy 的线程模型**：`server_main.py` 用 daemon 线程跑 Flask，
   WebView 加载时序上没有做端口就绪等待（现靠 WebChromeClient 进度条兜底）。
7. **store.py 的 `data/` 相对路径**：`server_main.start()` 用 os.chdir(data_dir)
   兜底，但引擎中若有绝对路径假设（如 `BASE / "data"` 指向源码目录）需逐个核对。
8. **UI 适配**：桌面单页在手机 WebView 的横屏体验，未做任何移动端改版。
9. **Android 后台限制**：emtlink 盘中自动刷新在锁屏/后台会暂停（设计已接受），
   前台恢复逻辑未实现。
