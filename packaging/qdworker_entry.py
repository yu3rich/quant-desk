# -*- coding: utf-8 -*-
"""行情下载 worker 的冻结入口（PyInstaller 第二个 exe：qdworker.exe）。

用法与源码模式完全一致：qdworker.exe --worker <chunk.json> <mode> <start> <end>
为什么单独打一个 exe：冻结模式下没有独立 python.exe，
bulkdata._run_parallel 不能再用 `sys.executable bulkdata.py` 起子进程。
"""
import multiprocessing
import sys
from pathlib import Path

multiprocessing.freeze_support()

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bulkdata import _worker_main  # noqa: E402

if len(sys.argv) >= 6 and sys.argv[1] == "--worker":
    _worker_main(sys.argv[2:6])
else:
    print("用法：qdworker.exe --worker <chunk.json> <mode> <start> <end>")
