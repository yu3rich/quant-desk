"""QuantDesk 手机版 Flask 启动器（Chaquopy 进程内调用）。

由 MainActivity 调用：
    server_main.start(port, data_dir, web_dir)

职责：
1. 把 Python 模块所在目录（Chaquopy 源码目录）加入 sys.path
2. 在 data_dir 准备 config.json（首次从打包资源复制默认值）
3. os.chdir(data_dir) —— 引擎模块里 data/ 相对路径都落私有数据目录
4. 后台线程启动 Flask（bind 127.0.0.1，仅 WebView 进程内可访问）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import traceback

_started = False
_lock = threading.Lock()

# config.json 的可写副本落在数据目录；首次启动从打包资源复制
_DEFAULT_CONFIG_RESOURCE = "config_default.json"


def _prepare_config(data_dir: str) -> None:
    target = os.path.join(data_dir, "config.json")
    if os.path.exists(target):
        return
    src = os.path.join(os.path.dirname(__file__), _DEFAULT_CONFIG_RESOURCE)
    if os.path.exists(src):
        shutil.copy(src, target)
    else:
        # 打包资源缺失时给出最小可运行配置（与 config.example.json 关键字段一致）
        with open(target, "w", encoding="utf-8") as f:
            json.dump({
                "port": 8765,
                "commission": 0.0003,
                "min_commission": 5.0,
                "stamp_tax": 0.0005,
                "transfer_fee": 0.00001,
                "slippage": 0.0,
                "limit_pct": 0.1,
                "cache_start": "2024-01-01",
                "stocks": [],
                "bulk_workers": 2,
            }, f, ensure_ascii=False, indent=2)


def start(port: int, data_dir: str, web_dir: str) -> None:
    global _started
    with _lock:
        if _started:
            return
        _prepare_config(data_dir)
        os.chdir(data_dir)  # data/ 相对路径统一落私有目录

        # 引擎模块与 app.py 同目录（Chaquopy 把 src/main/python 打进 APK）
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

        import app as quantdesk_app  # noqa: 模块导入即可，Flask 对象在 app.app

        flask_app = quantdesk_app.app
        # 关键：把 app.py 的前端目录指向 MainActivity 解包出的真实位置，
        # 否则 WEB = 源码目录/web 不存在 → 首页与 /static 全部 404
        from pathlib import Path
        quantdesk_app.WEB = Path(web_dir).resolve()
        flask_app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

        def _run() -> None:
            try:
                # 手机版不做 scheduler 定时管道 / emtlink 自动刷新（打开时按需增量）。
                # 需要时可在 HTTP 层显式触发；直接调用 app.scheduler.start() 属于未验证项。
                flask_app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
            except Exception:
                traceback.print_exc()

        t = threading.Thread(target=_run, name="quantdesk-flask", daemon=True)
        t.start()
        _started = True
