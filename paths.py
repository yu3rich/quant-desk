"""
运行路径解析（开发 / PyInstaller 打包两套环境统一入口）。

设计原则：
- **只读资源**（前端 web/）与**可写数据**（data/、config.json、logs/）严格分离；
- 冻结成 exe 后，`__file__` 指向 sys._MEIPASS（只读临时解压目录，重启可能变化），
  绝不能拿它当数据目录，否则数据库、账本、模型会写进临时目录并在重启后丢失；
- 开发态与打包态行为一致：APP_HOME 分别是「项目根」和「exe 所在目录」。

用法：
    from paths import BASE, DATA, WEB
    BASE   # 可写根目录（exe 同级 / 项目根）
    DATA   # BASE / "data"
    WEB    # 前端静态目录（优先 exe 同级，回落包内资源）
"""

import sys
from pathlib import Path

# PyInstaller 运行时会注入 sys.frozen
FROZEN = bool(getattr(sys, "frozen", False))

# 可写根：打包后是 exe 所在目录；开发时是源码目录
APP_HOME = (
    Path(sys.executable).resolve().parent
    if FROZEN
    else Path(__file__).resolve().parent
)

# 只读资源根：打包后指向 _MEIPASS，开发时同 APP_HOME
RES = Path(getattr(sys, "_MEIPASS", APP_HOME))

BASE = APP_HOME
DATA = APP_HOME / "data"


def _pick(*cands):
    """返回第一个存在的候选路径；都不存在时返回第一个（便于后续报错定位）。"""
    for c in cands:
        if c.exists():
            return c
    return cands[0]


# 前端优先取 exe 同级（方便直接改样式后刷新即生效），其次取打包进 exe 的资源
WEB = _pick(APP_HOME / "web", RES / "web")


def resource(rel: str) -> Path:
    """只读资源（示例配置等）：同样「exe 同级优先，包内兜底」。

    打包后 datas 落在 _internal/ 而不是 exe 同级，直接拼 APP_HOME 会找不到，
    表现为首次启动没生成 config.json、界面上一堆默认值为空。
    """
    return _pick(APP_HOME / rel, RES / rel)

# 需要运行时创建的可写目录
_DIRS = (
    "data",
    "data/ledger",
    "data/screen",
    "data/journal",
    "data/extra",
    "data/cache",
    "logs",
)


def ensure_dirs():
    """幂等创建可写目录。首次启动、换机迁移后都需要。"""
    for rel in _DIRS:
        (APP_HOME / rel).mkdir(parents=True, exist_ok=True)
    return APP_HOME


def describe():
    """启动时打印一行路径摘要，出问题时能第一时间判断资源/数据是否找对。"""
    return "APP_HOME=%s | WEB=%s | FROZEN=%s" % (APP_HOME, WEB, FROZEN)
