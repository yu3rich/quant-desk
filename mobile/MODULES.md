# 手机版模块白名单（MODULES）

`mobile/sync_core.py` 只把下面列出的文件同步进 Android 工程。这是**手机版允许依赖的模块清单**，
来自打包设计文档 `quant-desk-packaging-design.md` 第 2.3 节功能取舍表。

核心原则：**不 fork 业务代码**。白名单内的文件在桌面版和手机版之间共享同一份源码，
任何修改都只发生在仓库根目录，再由 sync_core.py 单向同步。

## 同步到 `mobile/app/src/main/python/`（打进 APK 的 Python 源码）

| 文件 | 角色 | 手机版口径 |
|---|---|---|
| quant.py | 回测引擎（T+1 开盘口径、费用模型） | 原样复用，单只回测 |
| store.py | SQLite 数据层 | 原样复用，落 APK 私有目录 |
| chartdata.py | 分时/K线数据 | 原样复用，按需拉取 |
| industry.py | 行业强度（申万分类缓存） | 保留；行业中性化砍掉 |
| watchlist.py | 自选股票池 | 原样复用，选股范围限股票池 |
| screener.py | 选股 | 仅"方向加权基准分"口径；LightGBM 分砍掉（mlmodel.py 不入包） |
| spot.py | 腾讯批量行情 | 原样复用 |
| universe.py | 全 A 名单 | 保留查询能力；**不做全市场预热** |
| histcache.py | 日线缓存 | 原样复用，只存股票池+已浏览个股 |
| emtlink.py | emtl 连接层（jywg.18.cn HTTP 网关） | 实盘只读全套保留；后台自动刷新暂停属 Android 限制 |
| emtbook.py | 实盘分析层（FIFO/归因/体检） | 原样复用 |
| journal.py | 复盘日记（本地 Markdown） | 原样复用，可与桌面版手动互导 |
| llm.py | LLM 辅助 | 原样复用（手机端 API 调用走用户自配 key） |
| factors.py | 因子计算 | 计算逻辑原样复用；因子体检页砍掉 |
| portfolio.py | 组合分析 | 原样复用 |
| neutral.py | 行业中性化 | 仅文件保留供引用；手机版 UI 不暴露 |
| review.py | 交易复盘 | 原样复用 |
| app.py | Flask 服务入口（127.0.0.1） | 由 server_main.py 进程内拉起；scheduler 定时管道不启动 |
| config.json | 默认配置 | 首次启动复制到私有数据目录后可写 |

## 同步到 `mobile/app/src/main/assets/web/`（WebView 前端）

| 文件 | 说明 |
|---|---|
| web/ 整目录 | index.html / app.js / style.css / echarts.min.js / select-enhance.js / icon.svg |

首期不做移动端 UI 改版，桌面单页在 WebView 横屏可用；功能入口按砍掉的能力在后续版本隐藏。

## 明确不同步（黑名单要点）

- mlmodel.py / emtai.py / listing.py / capital.py / stockview.py / valsrc.py /
  factor_selfcheck.py / bulkdata.py / pipeline.py / scheduler.py / datasource.py /
  riskview.py / realtrades.py —— 涉及 LightGBM、全市场预热、定时管道或 Tushare 估值链
- akshare / tdxpy 依赖链 —— 手机版行情只走腾讯 + baostock
- 各 tests_*.py、tests_ui/、data/（手机数据库全新生成）

## 注意

`app.py` 顶层的白名单外模块 import（mlmodel / datasource / bulkdata / pipeline /
scheduler / emtai / listing / capital / stockview / valsrc）已改为条件导入：
桌面版真实导入行为不变；Chaquopy 环境缺失时降级为 `_MobileStub`（调用即抛
RuntimeError 提示"手机版未打包"），`import app` 可成功。factors.py / review.py
对 listing 的 import 同样加了 try/except 降级（无上市信息时全部票视为在市）。
qdpaths.py 已加入 sync 白名单（多个白名单模块的公共依赖，纯 stdlib）。
2026-11-25 已通过 `./gradlew assembleDebug` 实机验证打包成功。
