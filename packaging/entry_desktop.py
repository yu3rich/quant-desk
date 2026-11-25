# -*- coding: utf-8 -*-
"""QuantDesk 桌面版入口（PyInstaller 主 exe 用）。

职责顺序（顺序即正确性）：
1. multiprocessing.freeze_support() —— 必须是第一行可执行代码，
   冻结后的任何子进程都会重新执行本文件，不加会递归拉起整个程序
2. 单实例锁 —— 第二次双击图标不应再起一个后端，只把已有窗口带回来
3. 端口处理 —— 8765 已被本程序占用时直接开窗口；被别的程序占用时弹错误页
4. 启动后端线程（与 app.py __main__ 完全同构）+ pywebview 窗口
"""
from __future__ import annotations

import multiprocessing
import socket
import sys
import threading
import time
from pathlib import Path

multiprocessing.freeze_support()  # noqa: E402  必须最先执行

PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"


def _is_quantdesk_on_port(port: int) -> bool:
    """探测端口上是不是已经在跑的 QuantDesk（读取首页标题特征）。"""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            return b"QuantDesk" in r.read(4096)
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _acquire_single_instance() -> bool:
    """锁文件放在数据目录（qdpaths），退出时删除；异常残留不影响下次启动（覆盖写）。"""
    try:
        import qdpaths
        lock = qdpaths.data_path(".desktop.lock")
        lock.write_text(str(time.time()), encoding="utf-8")
        return True
    except Exception:
        return True  # 锁失败不拦启动，端口探测兜底


def _copy_examples() -> None:
    """首次启动把示例配置复制到数据目录（冻结后示例随 _internal 分发）。"""
    try:
        import qdpaths
        src_dir = Path(__file__).resolve().parent / "data"
        if not src_dir.exists():
            return
        for src in src_dir.glob("*.example.json"):
            dst = qdpaths.data_path(src.name)
            if not dst.exists():
                dst.write_bytes(src.read_bytes())
    except Exception:
        pass


def _run_backend() -> None:
    import app  # 仓库根的 Flask 入口模块
    import emtlink
    import scheduler

    c = app.cfg()
    port = int(c.get("port", PORT))
    scheduler.start()      # 收盘后自动更新数据链
    emtlink.start_auto()   # 盘中低频刷新 + 收盘日结（只读）
    app.app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


def main() -> None:
    _acquire_single_instance()
    _copy_examples()

    already = not _port_free(PORT)
    if already and not _is_quantdesk_on_port(PORT):
        _error_page("端口 8765 被其他程序占用。请关闭占用该端口的程序后重试。")
        return

    t = threading.Thread(target=_run_backend, daemon=True)
    t.start()
    if not already:
        _wait_backend()

    _open_window(URL)


def _wait_backend(timeout: float = 30.0) -> None:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    # 超时也不阻塞：窗口打开后刷新即可


def _open_window(url: str) -> None:
    import webview
    webview.create_window(
        "QuantDesk · 本地 A 股量化工作台",
        url,
        width=1500, height=920, min_size=(1100, 700),
    )
    webview.start()


def _error_page(msg: str) -> None:
    import html as _html
    import tempfile
    page = Path(tempfile.gettempdir()) / "quantdesk_error.html"
    page.write_text(
        f"<html><body style='font-family:sans-serif;padding:40px'>"
        f"<h2>QuantDesk 无法启动</h2><p>{_html.escape(msg)}</p></body></html>",
        encoding="utf-8")
    _open_window(page.as_uri())


if __name__ == "__main__":
    main()
